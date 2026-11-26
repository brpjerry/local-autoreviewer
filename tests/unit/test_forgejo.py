# SPDX-License-Identifier: GPL-3.0-or-later
"""Forgejo repos: the API client (against a fake HTTP opener), routing by repo name, links, git auth."""

import base64
import hashlib
import http.client
import io
import sqlite3
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from tests.helpers import OWNER, REPO, EnvTestCase, FakeGitHub, Remote, make_config
from local_autoreviewer import config as config_mod
from local_autoreviewer import db, events, feedback, forges, leakscan, pipeline, review, suggestions
from local_autoreviewer import __main__ as cli
from local_autoreviewer.__main__ import CliError, parse_ref
from local_autoreviewer import forgejo
from local_autoreviewer.forgejo import Forgejo, ForgejoError, _SameHostRedirects
from local_autoreviewer.github import GitHubError, read_token
from local_autoreviewer.triggers import poll as poll_mod
from local_autoreviewer.gitops import Git

TOKEN = "forgejo-secret-token"
FORGE_REPO = "forge.example/team/tool"


class Headers(dict):
    """Response headers, a field possibly repeated (as http.client's message)."""

    def get_all(self, name, failobj=None):
        value = self.get(name)
        return failobj if value is None else value if isinstance(value, list) else [value]


class Response(io.BytesIO):
    headers = Headers()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeServer:
    """Answers requests by (method, path), and records them."""

    def __init__(self, routes):
        self.routes = routes  # (method, path): data, or a list of pages, or an exception
        self.requests = []

    def __call__(self, request, timeout=None):
        url = urllib.parse.urlsplit(request.full_url)
        query = dict(urllib.parse.parse_qsl(url.query))
        self.requests.append({"method": request.get_method(), "url": request.full_url, "query": query,
                              "headers": dict(request.header_items()),
                              "payload": json.loads(request.data) if request.data else None})
        answer = self.routes[(request.get_method(), url.path)]
        if isinstance(answer, Exception):
            raise answer
        headers = Headers()
        if isinstance(answer, tuple):  # pages, as the server cuts them; it counts them all
            headers["X-Total-Count"] = str(sum(len(page) for page in answer))
            answer = answer[int(query.get("page", 1)) - 1] if int(query.get("page", 1)) <= len(answer) else []
        response = Response(answer if isinstance(answer, bytes) else json.dumps(answer).encode())
        response.headers = headers
        return response


def api(path):
    return f"/api/v1/{path}"


def forgejo_pr(number, **extra):
    return {"number": number, "state": "open", "merged": False, "draft": False, "title": f"PR {number}",
            "body": "text", "html_url": f"https://forge.example/team/tool/pulls/{number}",
            "updated_at": "2026-11-23T10:00:00-08:00", "user": {"login": "imouto", "id": 1},
            "head": {"ref": "feature", "sha": "a" * 40}, "base": {"ref": "main", "sha": "b" * 40}, **extra}


def comment(id, review_id, user, path="src/a.rs", position=10, original=0, at="2026-11-23T10:00:00Z", body="x"):
    return {"id": id, "pull_request_review_id": review_id, "user": {"login": user, "id": hash(user) % 1000},
            "path": path, "position": position, "original_position": original, "created_at": at, "body": body,
            "html_url": f"https://forge.example/c/{id}"}


class ClientTests(unittest.TestCase):
    def client(self, routes):
        server = FakeServer(routes)
        return Forgejo("forge.example", TOKEN, opener=server), server

    def test_requests_carry_the_token_to_the_hosts_api(self):
        client, server = self.client({("GET", api("user")): {"login": "imouto"}})
        self.assertEqual(client.viewer_login(), "imouto")
        (request,) = server.requests
        self.assertEqual(request["url"], "https://forge.example/api/v1/user")
        self.assertEqual(request["headers"]["Authorization"], f"token {TOKEN}")
        self.assertIsNone(client.token_expiry())

    def test_open_prs_page_through_and_take_githubs_shape(self):
        first = [forgejo_pr(n) for n in range(1, 51)]
        client, server = self.client({("GET", api("repos/team/tool/pulls")): (first, [forgejo_pr(51, draft=True),
                                                                                      None])})
        prs = client.list_open_prs("team/tool")
        self.assertEqual([pr["number"] for pr in prs], list(range(1, 52)))
        self.assertEqual(prs[-1], {"number": 51, "state": "OPEN", "headRefOid": "a" * 40, "baseRefName": "main",
                                   "baseRefOid": "b" * 40, "isDraft": True, "author": {"login": "imouto"},
                                   "title": "PR 51", "body": "text",
                                   "url": "https://forge.example/team/tool/pulls/51",
                                   "updatedAt": "2026-11-23T10:00:00-08:00"})
        self.assertEqual([(r["query"]["state"], r["query"]["page"]) for r in server.requests],
                         [("open", "1"), ("open", "2")])
        self.assertEqual(len(client.list_open_prs("team/tool", limit=3)), 3)

    def test_a_server_that_sends_shorter_pages_is_read_to_the_end(self):
        pages = tuple([forgejo_pr(n) for n in range(start, min(start + 20, 46))] for start in (1, 21, 41))
        client, server = self.client({("GET", api("repos/team/tool/pulls")): pages})
        self.assertEqual([pr["number"] for pr in client.list_open_prs("team/tool")], list(range(1, 46)))
        self.assertEqual(len(server.requests), 3)
        endless = tuple([forgejo_pr(1)] for _ in range(forgejo.MAX_PAGES + 1))
        client, _ = self.client({("GET", api("repos/team/tool/pulls")): endless})
        with self.assertRaises(ForgejoError):  # not a partial list taken for the whole
            client.list_open_prs("team/tool")

    def test_without_a_count_the_next_link_says_there_is_more(self):
        class Uncounted(FakeServer):  # a proxy that drops X-Total-Count but keeps Link
            def __call__(self, request, timeout=None):
                response = super().__call__(request, timeout)
                page = int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.full_url).query))["page"])
                response.headers = Headers(Link=['<prev>; rel="prev"', '<next>; rel=next'] if page < 3  # two fields,
                                           else '<prev>; rel="prev"')  # the next one unquoted
                return response
        pages = tuple([forgejo_pr(n) for n in range(start, min(start + 20, 46))] for start in (1, 21, 41))
        server = Uncounted({("GET", api("repos/team/tool/pulls")): pages})
        client = Forgejo("forge.example", TOKEN, opener=server)
        self.assertEqual(len(client.list_open_prs("team/tool")), 45)
        self.assertEqual(len(server.requests), 3)

    def test_without_any_paging_headers_an_empty_page_ends_the_list(self):
        class Bare(FakeServer):  # a proxy that drops both headers
            def __call__(self, request, timeout=None):
                response = super().__call__(request, timeout)
                response.headers = Headers()
                return response
        pages = tuple([forgejo_pr(n) for n in range(start, min(start + 20, 46))] for start in (1, 21, 41))
        server = Bare({("GET", api("repos/team/tool/pulls")): pages})
        self.assertEqual(len(Forgejo("forge.example", TOKEN, opener=server).list_open_prs("team/tool")), 45)
        self.assertEqual(len(server.requests), 4)

    def test_a_cut_off_error_page_is_still_a_forgejo_error(self):
        class CutOff(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b"partial")
        for status in (403, 422, 502):
            with self.subTest(status=status):
                refused = urllib.error.HTTPError("u", status, "error", {}, CutOff())
                self.addCleanup(refused.close)
                client, _ = self.client({("GET", api("user")): refused})
                with self.assertRaises(ForgejoError) as caught:
                    client.viewer_login()
                self.assertIn(f"HTTP {status}:", str(caught.exception))  # a 422 still drops the inline comments
                self.assertFalse(pipeline._rejected(caught.exception))  # retried, as a dropped connection is

    def test_an_answer_that_isnt_json_is_a_forgejo_error(self):
        client, _ = self.client({("GET", api("user")): b"<html>Sign in</html>"})
        with self.assertRaises(ForgejoError):
            client.viewer_login()
        client, _ = self.client({("GET", api("user")): {"message": "no login here"}})
        with self.assertRaises(ForgejoError):
            client.viewer_login()

    def test_a_closed_pr_is_merged_or_closed(self):
        for extra, state in (({}, "OPEN"), ({"state": "closed", "merged": True}, "MERGED"),
                             ({"state": "closed"}, "CLOSED")):
            client, _ = self.client({("GET", api("repos/team/tool/pulls/4")): forgejo_pr(4, **extra)})
            self.assertEqual(client.pr_view("team/tool", 4)["state"], state)

    def test_pending_reviews_are_left_out(self):
        reviews = [{"id": 1, "state": "COMMENT", "user": {"login": "imouto"}, "body": "a",
                    "submitted_at": "2026-11-23T10:00:00Z"},
                   {"id": 2, "state": "PENDING", "user": {"login": "imouto"}, "body": "draft"}]
        client, _ = self.client({("GET", api("repos/team/tool/pulls/4/reviews")): (reviews,)})
        self.assertEqual([r["id"] for r in client.list_reviews("team/tool", 4)], [1])

    def test_marked_review_is_found_by_marker_and_login(self):
        body = review.marker(7, "a" * 40, "codex")
        reviews = [{"id": 1, "state": "COMMENT", "user": {"login": "someone"}, "body": body},
                   {"id": 2, "state": "COMMENT", "user": {"login": "Imouto"}, "body": body}]
        client, _ = self.client({("GET", api("repos/team/tool/pulls/4/reviews")): (reviews,)})
        self.assertEqual(client.find_marked_review("team/tool", 4, 7, "imouto")["id"], 2)
        self.assertIsNone(client.find_marked_review("team/tool", 4, 8, "imouto"))

    def test_inline_comments_on_one_line_are_a_thread(self):
        reviews = [{"id": 1, "state": "COMMENT", "comments_count": 4, "user": {"login": "imouto"},
                    "body": review.marker(1, "a" * 40, "codex"), "submitted_at": "2026-11-23T02:00:00-08:00"},
                   {"id": 2, "state": "COMMENT", "comments_count": 0, "user": {"login": "imouto"},
                    "submitted_at": "2026-11-23T10:30:00Z"},
                   {"id": 3, "state": "COMMENT", "comments_count": 2, "user": {"login": "author"},
                    "submitted_at": "2026-11-23T11:00:01Z"}]
        # Our post: two suggestions ending on one line, created as the review was submitted. Then the author,
        # posting as the same account, replies in the web UI, which files the reply under our review.
        ours = [comment(11, 1, "imouto", at="2026-11-23T10:00:00Z"),
                comment(12, 1, "imouto", at="2026-11-23T02:00:00-08:00"),
                comment(13, 1, "imouto", at="2026-11-23T12:00:00Z", body="won't fix, same account"),
                comment(14, 1, "imouto", path="src/b.rs", at="2026-11-23T10:00:00Z")]
        theirs = [comment(31, 3, "author", at="2026-11-23T11:00:00Z", body="won't fix"),
                  comment(32, 3, "author", position=0, original=4, at="2026-11-23T11:00:01Z")]
        client, server = self.client({("GET", api("repos/team/tool/pulls/4/reviews")): (reviews,),
                                      ("GET", api("repos/team/tool/pulls/4/reviews/1/comments")): ours,
                                      ("GET", api("repos/team/tool/pulls/4/reviews/3/comments")): theirs})
        found = {c["id"]: c for c in client.list_review_comments("team/tool", 4)}
        self.assertEqual({i: (c["pull_request_review_id"], c["in_reply_to_id"]) for i, c in found.items()},
                         {11: (1, None), 12: (1, None), 13: (None, 11), 14: (1, None), 31: (None, 11),
                          32: (3, None)})
        self.assertEqual((found[31]["line"], found[32]["line"], found[32]["original_line"]), (10, None, 4))
        self.assertNotIn(api("repos/team/tool/pulls/4/reviews/2/comments"),
                         [urllib.parse.urlsplit(r["url"]).path for r in server.requests])  # no comments to list
        self.assertEqual(client.review_threads("team/tool", 4), [])  # nothing Forgejo's API could resolve

    def test_replies_and_feedback_are_read_when_the_author_is_the_account_reviews_post_as(self):
        body = "review\n\n" + review.marker(7, "a" * 40, "codex")
        reviews = [{"id": 1, "state": "COMMENT", "comments_count": 2, "user": {"login": "imouto"}, "body": body,
                    "commit_id": "a" * 40, "submitted_at": "2026-11-23T02:00:00-08:00", "html_url": "r1"}]
        comments = [comment(11, 1, "imouto", at="2026-11-23T10:00:00Z", body="**1. Bug**\n\n```diff\n-a\n+b\n```"),
                    comment(13, 1, "imouto", at="2026-11-23T12:00:00Z", body="Won't fix: not supported.")]
        issue = [{"id": 21, "user": {"login": "imouto"}, "body": "Review feedback: 1 was right.",
                  "created_at": "2026-11-23T13:00:00Z", "html_url": "c21"}]
        client, _ = self.client({("GET", api("repos/team/tool/pulls/4/reviews")): (reviews,),
                                 ("GET", api("repos/team/tool/pulls/4/reviews/1/comments")): comments,
                                 ("GET", api("repos/team/tool/issues/4/comments")): issue})
        said = feedback.author_comments(client, "team/tool", 4, "imouto", login="imouto")
        self.assertEqual([(c["kind"], c["body"]) for c in said],
                         [("reply", "Won't fix: not supported."), ("comment", "Review feedback: 1 was right.")])
        self.assertEqual(said[0]["thread"], "src/a.rs:10 **1. Bug**")
        rows = feedback.gather(client, "team/tool", 4, "imouto")
        self.assertEqual(sorted((r["source"], r["github_id"], r["review_id"]) for r in rows),
                         [("comment", 21, 1), ("reply", 13, 1)])

    def test_a_later_review_of_ours_on_the_same_line_is_not_a_reply(self):
        def ours(review_id, at):
            return {"id": review_id, "state": "COMMENT", "comments_count": 2, "user": {"login": "imouto"},
                    "body": review.marker(review_id, "a" * 40, "codex"), "submitted_at": at}
        reviews = [ours(1, "2026-11-23T10:00:00Z"), ours(2, "2026-11-23T11:00:00Z")]
        first = [comment(11, 1, "imouto", at="2026-11-23T10:00:00Z", body="**1. Old**")]
        second = [comment(21, 2, "imouto", at="2026-11-23T11:00:00Z", body="**1. New**"),
                  comment(22, 2, "imouto", at="2026-11-23T12:00:00Z", body="Won't fix.")]
        client, _ = self.client({("GET", api("repos/team/tool/pulls/4/reviews")): (reviews,),
                                 ("GET", api("repos/team/tool/pulls/4/reviews/1/comments")): first,
                                 ("GET", api("repos/team/tool/pulls/4/reviews/2/comments")): second,
                                 ("GET", api("repos/team/tool/issues/4/comments")): []})
        found = {c["id"]: (c["pull_request_review_id"], c["in_reply_to_id"])
                 for c in client.list_review_comments("team/tool", 4)}
        self.assertEqual(found, {11: (1, None), 21: (2, None), 22: (None, 21)})
        self.assertEqual([(r["source"], r["github_id"], r["review_id"], r["context"])
                          for r in feedback.gather(client, "team/tool", 4, "imouto")],
                         [("reply", 22, 2, "src/a.rs:10 **1. New**")])

    def test_a_review_posts_with_its_comments_on_their_lines(self):
        client, server = self.client({("POST", api("repos/team/tool/pulls/4/reviews")): {"id": 9, "html_url": "u"}})
        comments = [{"path": "a.rs", "line": 5, "side": "RIGHT", "body": "one"},
                    {"path": "b.rs", "line": 9, "side": "RIGHT", "start_line": 7, "start_side": "RIGHT",
                     "body": "three"}]
        self.assertEqual(client.post_review("team/tool", 4, "c" * 40, "body", comments=comments),
                         {"id": 9, "html_url": "u"})
        self.assertEqual(server.requests[0]["payload"], {
            "commit_id": "c" * 40, "event": "COMMENT", "body": "body",
            "comments": [{"path": "a.rs", "body": "one", "new_position": 5, "extra_lines_count": 0},
                         {"path": "b.rs", "body": "three", "new_position": 7, "extra_lines_count": 2}]})
        self.assertEqual(server.requests[0]["headers"]["Content-type"], "application/json")
        client.post_review("team/tool", 4, "c" * 40, "body")
        self.assertNotIn("comments", server.requests[1]["payload"])

    def test_errors_name_the_status_without_the_token(self):
        refused = urllib.error.HTTPError("u", 422, "Unprocessable Entity", {}, io.BytesIO(
            f"line not in diff, token {TOKEN}".encode()))
        client, _ = self.client({("POST", api("repos/team/tool/pulls/4/reviews")): refused,
                                 ("GET", api("user")): urllib.error.URLError("timed out")})
        with self.assertRaises(ForgejoError) as caught:
            client.post_review("team/tool", 4, "c" * 40, "body")
        self.assertIn("HTTP 422", str(caught.exception))  # what pipeline._post_review retries without comments on
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertTrue(pipeline._rejected(caught.exception))
        with self.assertRaises(GitHubError) as caught:
            client.viewer_login()
        self.assertFalse(pipeline._rejected(caught.exception))

    def test_redirects_stay_on_the_tokens_host(self):
        handler = _SameHostRedirects("forge.example")
        request = urllib.request.Request("https://forge.example/api/v1/repos/team/old",
                                         headers={"Authorization": "token t"})
        moved = handler.redirect_request(request, None, 301, "Moved", {}, "https://forge.example/api/v1/repos/team/new")
        self.assertEqual(moved.full_url, "https://forge.example/api/v1/repos/team/new")
        post = urllib.request.Request("https://forge.example/api/v1/repos/team/old/pulls/1/reviews", data=b"{}",
                                      method="POST", headers={"Authorization": "token t"})
        for req, target in ((request, "https://elsewhere.example/steal"),
                            (request, "http://forge.example/api/v1/repos/team/new"),  # in the clear
                            (post, "https://forge.example/api/v1/repos/team/new/pulls/1/reviews")):  # would be a GET
            with self.subTest(target=target), self.assertRaises(urllib.error.HTTPError) as caught:
                handler.redirect_request(req, io.BytesIO(), 307, "Moved", {}, target)
            caught.exception.close()

    def test_the_default_opener_ignores_proxies_from_the_environment(self):
        with mock.patch.dict(os.environ, {"https_proxy": "http://proxy.example:3128"}):
            client = Forgejo("forge.example", TOKEN)
        handlers = client._open.__self__.handlers
        self.assertFalse([h for h in handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies])


class NamingTests(unittest.TestCase):
    def test_a_repo_names_its_forge(self):
        self.assertIsNone(forges.host("alice/demo"))
        self.assertEqual(forges.host("Forge.Example/team/tool"), "forge.example")
        self.assertEqual(forges.api_name("forge.example/team/tool"), "team/tool")
        self.assertEqual(forges.api_name("alice/demo"), "alice/demo")

    def test_links_point_at_the_repos_forge(self):
        sha = "a" * 40
        self.assertEqual(forges.file_url("alice/demo", sha, "src/a b.py", 3),
                         f"https://github.com/alice/demo/blob/{sha}/src/a%20b.py#L3")
        self.assertEqual(forges.file_url(FORGE_REPO, sha, "src/a.rs"),
                         f"https://forge.example/team/tool/src/commit/{sha}/src/a.rs")
        self.assertEqual(forges.commit_url("alice/demo", 4, sha), f"https://github.com/alice/demo/pull/4/commits/{sha}")
        self.assertEqual(forges.commit_url(FORGE_REPO, 4, sha), f"https://forge.example/team/tool/commit/{sha}")
        self.assertTrue(forges.suggests("alice/demo"))
        self.assertFalse(forges.suggests(FORGE_REPO))

    def test_config_takes_forgejo_repos_by_host(self):
        def parse(name):
            return config_mod.parse({"agents": {"codex": {}}, "defaults": {"agents": ["codex"]},
                                     "repo": [{"name": name}]})
        self.assertEqual(parse("forge.example/team/tool").repo("FORGE.example/team/tool").owner, "team")
        self.assertEqual(parse("forge.example:3000/team/tool").repo("forge.example:3000/team/tool").owner, "team")
        for bad in ("github.com/alice/demo", "a/b/c/d", "forge example/team/tool"):
            with self.subTest(bad=bad), self.assertRaises(config_mod.ConfigError):
                parse(bad)

    def test_pr_refs_name_forgejo_repos_too(self):
        self.assertEqual(parse_ref("alice/demo#3"), ("alice/demo", 3))
        self.assertEqual(parse_ref("https://github.com/alice/demo/pull/3"), ("alice/demo", 3))
        self.assertEqual(parse_ref("forge.example/team/tool#3"), ("forge.example/team/tool", 3))
        self.assertEqual(parse_ref("https://forge.example/team/tool/pulls/3/"), ("forge.example/team/tool", 3))
        with self.assertRaises(CliError):
            parse_ref("https://forge.example/team/tool/pull/3")


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tokens = Path(self._tmp.name) / "forgejo-tokens"
        self.tokens.mkdir()
        self.made = []

        def make(host, token):
            client = mock.Mock(token=token)
            client.viewer_login.return_value = "imouto"
            self.made.append((host, client))
            return client
        self.github = FakeGitHub()
        self.forges = forges.Forges(self.github, self.tokens, make_forgejo=make)

    def tearDown(self):
        self._tmp.cleanup()

    def token(self, host, value=TOKEN, mode=0o600):
        path = self.tokens / host
        path.write_text(value + "\n")
        path.chmod(mode)

    def test_github_repos_go_to_github_and_forgejo_ones_to_their_host(self):
        self.github.set_pr(1, "a" * 40)
        self.assertEqual(self.forges.pr_view("alice/demo", 1)["number"], 1)
        self.token("forge.example")
        self.forges.post_review(FORGE_REPO, 4, "c" * 40, "body", comments=[])
        ((host, client),) = self.made
        self.assertEqual((host, client.token), ("forge.example", TOKEN))
        client.post_review.assert_called_once_with("team/tool", 4, "c" * 40, "body", comments=[])
        self.forges.list_review_comments(FORGE_REPO, 4)
        self.assertEqual(len(self.made), 1)  # one client per host

    def test_a_missing_or_open_token_names_its_file(self):
        with self.assertRaises(GitHubError) as caught:
            self.forges.pr_view(FORGE_REPO, 1)
        self.assertIn(str(self.tokens / "forge.example"), str(caught.exception))
        self.token("forge.example", mode=0o644)
        with self.assertRaises(GitHubError) as caught:
            self.forges.pr_view(FORGE_REPO, 1)
        self.assertIn("chmod 600", str(caught.exception))

    def test_a_token_file_must_hold_one_readable_token(self):
        for name, content in (("two-lines", b"abc123\nsecond-line"), ("spaced", b"token abc123"),
                              ("binary", b"\xff\xfeabc")):
            with self.subTest(name=name):
                path = self.tokens / name
                path.write_bytes(content)
                path.chmod(0o600)
                with self.assertRaises(GitHubError) as caught:
                    read_token(path)
                self.assertNotIn("abc", str(caught.exception))

    def test_a_token_file_whose_metadata_cant_be_read_is_a_github_error(self):
        self.token("forge.example")
        with mock.patch.object(os, "fstat", side_effect=OSError("metadata unavailable")), \
                self.assertRaises(GitHubError):
            read_token(self.tokens / "forge.example")

    def test_a_fifo_in_place_of_a_token_file_fails_at_once(self):
        os.mkfifo(self.tokens / "forge.example", 0o600)
        with self.assertRaisesRegex(GitHubError, "must be a regular file"):
            read_token(self.tokens / "forge.example")

    def test_the_token_owner_is_looked_up_once_per_host(self):
        self.token("forge.example")
        self.assertEqual(self.forges.owner(FORGE_REPO), "imouto")
        self.assertEqual(self.forges.owner("forge.example/other/repo"), "imouto")
        self.made[0][1].viewer_login.assert_called_once_with()

    def test_git_sends_each_token_to_its_own_host_through_the_environment(self):
        self.assertEqual(self.forges.git_env(), {})
        self.token("forge.example")
        self.token("other.example", "other-token")
        self.forges.forgejo("forge.example")
        env = self.forges.git_env()
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")  # only tokens read so far
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://forge.example/.extraHeader")
        scheme, credentials = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: ").split()
        self.assertEqual((scheme, base64.b64decode(credentials).decode()), ("Basic", f"local-autoreviewer:{TOKEN}"))
        self.assertEqual((env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]),
                         ("http.https://forge.example/.followRedirects", "false"))

    def test_the_leak_check_knows_every_token_and_what_git_sends_for_it(self):
        self.token("forge.example")
        self.token("other.example", "other-token")
        (self.tokens / "forge.example.swp").write_bytes(b"\xff\xfe\x00binary")
        (self.tokens / "subdir").mkdir()
        self.forges.forgejo("forge.example")
        secrets = self.forges.secrets()
        self.assertIn(TOKEN, secrets)
        self.assertIn("other-token", secrets)  # not read for a request yet
        header = self.forges.git_env()["GIT_CONFIG_VALUE_0"]
        self.assertTrue([hit for hit in leakscan.scan(f"The service sends {header} to git.", secrets)
                         if hit.startswith("known-secret-") and hit.endswith("-exact")])

    def test_git_runs_with_the_auth_env_and_never_puts_a_token_in_argv(self):
        git = Git("https://github.com", {"PATH": os.environ["PATH"]}, auth_env=lambda: {"GIT_CONFIG_COUNT": "1",
                                                                                          "GIT_CONFIG_VALUE_0": TOKEN})
        self.assertEqual(git.url(FORGE_REPO), "https://forge.example/team/tool.git")
        self.assertEqual(git.url("alice/demo"), "https://github.com/alice/demo.git")
        self.assertEqual(Git("/tmp/remote", {}).url(FORGE_REPO), "/tmp/remote/forge.example/team/tool.git")
        self.assertEqual(git.mirror_path("/s", "Forge.Example/Team/Tool"),
                         Path("/s/mirrors/forge.example/team/tool.git"))
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            git.run(["status"])
        argv, env = run.call_args.args[0], run.call_args.kwargs["env"]
        self.assertEqual((env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_VALUE_0"]), ("1", TOKEN))
        self.assertNotIn(TOKEN, " ".join(argv))


class RenderTests(unittest.TestCase):
    def test_a_forgejo_review_links_there_and_shows_its_inline_change_as_a_diff(self):
        edit = {"file": "src/a.rs", "start_line": 2, "end_line": 2, "replacement": "new", "current": "old",
                "original": ["old"], "inline": True, "anchor": "matched"}
        finding = {"title": "Bug", "file": "src/a.rs", "line": 2, "severity": "minor", "category": "correctness",
                   "detail": "d", "suggestion": "", "trigger": "", "impact": 3, "likelihood": "rare",
                   "edits": [edit], "readability": "same"}
        data = review.validate({"summary": "s", "findings": [finding], "nits": [], "test_gaps": [], "questions": []})
        data["findings"][0]["edits"] = [edit]
        sha = "a" * 40
        body = review.render(data, repo=FORGE_REPO, number=4, sha=sha, base_ref="main", agent="codex", model="m",
                             job_id=7)
        self.assertIn(f"(https://forge.example/team/tool/commit/{sha})", body)
        self.assertIn(f"(https://forge.example/team/tool/src/commit/{sha}/src/a.rs#L2)", body)
        self.assertIn("(also inline on the PR)", body)
        self.assertNotIn("github.com", body)
        self.assertNotIn("one-click", body)
        (inline,) = suggestions.comments(data, review.comment_label(data), suggest=False)
        self.assertIn("```diff\n-old\n+new\n```", inline["body"])
        self.assertNotIn("suggestion", inline["body"])
        (github_inline,) = suggestions.comments(data, review.comment_label(data))
        self.assertIn("```suggestion\nnew\n```", github_inline["body"])

    def test_an_inline_diff_is_whole_while_the_comment_has_room(self):
        def comment_for(width):
            edit = {"file": "a.rs", "start_line": 1, "end_line": 1, "current": "x", "original": ["x"],
                    "replacement": "\n".join("y" * width for _ in range(100)), "inline": True, "anchor": "matched"}
            item = {"title": "T", "file": "a.rs", "line": 1, "severity": "nit", "edits": [edit]}
            (inline,) = suggestions.comments({"findings": [], "nits": [item]}, lambda index, item: "**T**",
                                             suggest=False)
            return inline["body"]
        fits = comment_for(590)  # about 59,100 characters of diff, under MAX_COMMENT with a short heading
        self.assertLessEqual(len(fits), suggestions.MAX_COMMENT)
        self.assertNotIn("more lines", fits)
        too_long = comment_for(700)
        self.assertIn("more lines", too_long)  # shortened, as the body's copy is
        self.assertLessEqual(len(too_long), suggestions.MAX_COMMENT)

    def test_a_pointer_to_the_rest_of_the_fix_never_overfills_the_comment(self):
        def edit(line, text):
            return {"file": "a.rs", "start_line": line, "end_line": line, "current": "x", "original": ["x"],
                    "replacement": text, "inline": line == 1, "anchor": "matched"}
        def comment(edits):
            item = {"title": "T", "file": "a.rs", "line": 1, "severity": "nit", "edits": edits}
            (inline,) = suggestions.comments({"findings": [], "nits": [item]}, lambda index, item: "**T**",
                                             suggest=False)
            return inline
        lines = ["y" * 500] * 100
        alone = len(comment([edit(1, "\n".join(lines))])["body"])
        lines[-1] += "y" * (suggestions.MAX_COMMENT - 20 - alone)  # 20 characters to spare: too few for a pointer
        inline = comment([edit(1, "\n".join(lines)), edit(5, "z")])
        self.assertLessEqual(len(inline["body"]), suggestions.MAX_COMMENT)
        self.assertNotIn("more lines", inline["body"])  # the inline change stays whole; the body lists the rest

    def test_a_long_inline_change_is_whole_in_its_forgejo_comment(self):
        old = [f"old {n}" for n in range(220)]
        edit = {"file": "a.rs", "start_line": 1, "end_line": 220, "current": "\n".join(old), "original": old,
                "replacement": "\n".join(f"new {n}" for n in range(220)), "inline": True, "anchor": "matched"}
        finding = {"title": "Long", "file": "a.rs", "line": 1, "severity": "minor", "category": "correctness",
                   "detail": "d", "suggestion": "", "trigger": "", "impact": 3, "likelihood": "rare",
                   "edits": [edit], "readability": "same"}
        data = review.validate({"summary": "s", "findings": [finding], "nits": [], "test_gaps": [], "questions": []})
        data["findings"][0]["edits"] = [edit]
        render = dict(number=4, sha="a" * 40, base_ref="main", agent="codex", model="m", job_id=7)
        self.assertIn("more lines", review.render(data, repo=FORGE_REPO, **render))  # as on GitHub: the inline
        (inline,) = suggestions.comments(data, review.comment_label(data), suggest=False)  # copy has it whole
        self.assertIn("-old 0\n", inline["body"])
        self.assertIn("+new 219\n```", inline["body"])
        self.assertNotIn("more lines", inline["body"])


class PipelineTests(EnvTestCase):
    """A review of a Forgejo repo end to end, with the fake forge standing in for its API."""

    def setUp(self):
        super().setUp()
        self.remote = Remote(self.tmp / "forge-remote", repo=FORGE_REPO)
        self.head = self.remote.open_pr(4, {"src/calc.py": "def divide(a, b):\n    return a / b  # todo\n"})
        self.github.set_pr(4, self.head, repo=FORGE_REPO)
        self.cfg = make_config(self.tmp, repo=FORGE_REPO)

    def test_a_forgejo_pr_is_reviewed_and_posted_there(self):
        svc = self.services()
        conn = svc.connect()
        with db.transaction(conn):
            job_id = db.insert_job(conn, repo=FORGE_REPO, number=4, head_sha=self.head, base_ref="main",
                                   agent="claude", model="opus", source="poll", not_before=0, now=self.clock(),
                                   post=True)
            db.update_job(conn, job_id, status="running", started_at=self.clock())
        conn.close()
        self.assertEqual(pipeline.run_job(svc, job_id, pipeline.CancelToken()), "posted")
        (posted,) = self.github.posted
        self.assertEqual((posted["repo"], posted["number"]), (FORGE_REPO, 4))
        self.assertIn(f"https://forge.example/team/tool/commit/{self.head}", posted["body"])
        self.assertNotIn("github.com", posted["body"])
        self.assertTrue(posted["comments"])
        self.assertFalse([c for c in posted["comments"] if "```suggestion" in c["body"]])
        self.assertEqual(svc.owner(FORGE_REPO), OWNER)

    def write_lock_probe(self, result):
        """An owner lookup that records whether the database's write lock was held when it ran."""
        calls = []

        def owner(repo):
            other = sqlite3.connect(self.state / "state.sqlite3", timeout=0, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
                calls.append("unlocked")
            except sqlite3.OperationalError:
                calls.append("locked")
            finally:
                other.close()
            if isinstance(result, Exception):
                raise result
            return result
        return owner, calls

    def test_an_ended_foreground_post_looks_up_the_owner_without_the_write_lock(self):
        for result, handed_back in ((OWNER, 1), (GitHubError("forgejo GET user failed: HTTP 502"), 0)):
            with self.subTest(result=result):
                svc = self.services()
                conn = svc.connect()
                with db.transaction(conn):
                    conn.execute("DELETE FROM jobs")
                    conn.execute("DELETE FROM prs")
                    conn.execute("INSERT INTO prs (repo, number, state, is_draft, head_sha, base_ref, author, title,"
                                 " head_changed_at, last_seen_at) VALUES (?, 4, 'open', 0, ?, 'main', ?, '', 0, 0)",
                                 (FORGE_REPO, self.head, OWNER))
                    events.record(conn, events.PrEvent(repo=FORGE_REPO, number=4, action="synchronize",
                                                       head_sha=self.head, base_ref="main", is_draft=False,
                                                       author=OWNER, source="poll"), self.clock())
                    job_id = db.insert_job(conn, repo=FORGE_REPO, number=4, head_sha=self.head, base_ref="main",
                                           agent="claude", model="", source=db.FOREGROUND, not_before=0,
                                           now=self.clock(), post=True)
                    db.update_job(conn, job_id, status="running")
                conn.close()
                self.github.owner, calls = self.write_lock_probe(result)
                pipeline.abandon(svc, [job_id], "foreground review ended before the job finished")
                conn = svc.connect()
                try:
                    self.assertEqual(db.get_job(conn, job_id)["status"], "cancelled")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs WHERE source='poll'").fetchone()[0],
                                     handed_back)
                finally:
                    conn.close()
                self.assertEqual(calls, ["unlocked"])

    def test_each_forge_admits_its_own_token_owners_prs(self):
        self.github.owners = {"forge.example": "imouto"}
        cfg = make_config(self.tmp, repo=FORGE_REPO, more_repos=[REPO],
                          service={"settle": "0s", "bootstrap": "review"})
        for repo in (FORGE_REPO, REPO):
            self.github.set_pr(1, "a" * 40, repo=repo, author="imouto")
            self.github.set_pr(2, "b" * 40, repo=repo, author=OWNER)
        results, failures = poll_mod.poll(self.services(cfg))
        self.assertEqual(failures, 0)
        conn = self.services(cfg).connect()
        try:
            jobs = sorted({(row["repo"], row["number"]) for row in conn.execute("SELECT repo, number FROM jobs")})
        finally:
            conn.close()
        self.assertEqual(jobs, [(REPO, 2), (FORGE_REPO, 1)])

    def test_recreating_a_checkout_loads_the_hosts_token_before_fetching(self):
        for missing in (False, True):  # without one, a public repo still fetches
            with self.subTest(missing=missing):
                self.recreate_checkout(missing)

    def recreate_checkout(self, missing):
        loaded = []

        def forgejo(host):
            loaded.append(host)
            if missing:
                raise GitHubError(f"Forgejo token for {host} missing")
        self.github.forgejo = forgejo
        svc = self.services()
        conn = svc.connect()
        with db.transaction(conn):
            conn.execute("DELETE FROM jobs")
            job_id = db.insert_job(conn, repo=FORGE_REPO, number=4, head_sha=self.head, base_ref="main",
                                   agent="claude", model="", source="poll", not_before=0, now=self.clock(), post=True)
            job = dict(db.get_job(conn, job_id))
        conn.close()
        with mock.patch.object(cli, "build_services", return_value=svc):
            paths = cli._ensure_worktree(self.cfg, job)
        self.assertEqual(loaded, ["forge.example"])
        self.assertTrue(Path(paths.worktree, "src/calc.py").is_file())
        svc.git.remove_worktree(svc.git.mirror_path(self.state, FORGE_REPO), paths.worktree)
        shutil.rmtree(svc.git.mirror_path(self.state, FORGE_REPO))  # so the next one fetches again


class OwnerCacheTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.tokens = self.tmp / "forgejo-tokens"
        self.tokens.mkdir()
        self.cfg = make_config(self.tmp, repo=FORGE_REPO)
        self.answer = "imouto"

    def forges(self):
        def make(host, token):
            client = mock.Mock(token=token)
            if isinstance(self.answer, Exception):
                client.viewer_login.side_effect = self.answer
            else:
                client.viewer_login.return_value = self.answer
            return client
        made = forges.Forges(FakeGitHub(), self.tokens, make_forgejo=make)
        cli.resolve_forgejo_owners(made, self.cfg, self.state)
        return made.owners

    def token(self, value):
        path = self.tokens / "forge.example"
        path.write_text(value)
        path.chmod(0o600)

    def test_an_unreachable_host_falls_back_to_the_owner_seen_for_the_same_token(self):
        self.assertEqual(self.forges(), {})  # no token: nothing resolved, and no error
        self.token("token-a")
        self.assertEqual(self.forges(), {"forge.example": "imouto"})
        self.answer = GitHubError("forgejo GET user failed: timed out")
        self.assertEqual(self.forges(), {"forge.example": "imouto"})
        self.token("token-b")  # another account's, perhaps: the old owner isn't assumed
        self.assertEqual(self.forges(), {})

    def test_the_owner_is_stored_under_the_token_its_request_used(self):
        self.token("token-a")
        asked = []

        def make(host, token):
            if not asked and token == "token-a":
                self.token("token-b")  # replaced just after the first read
            return mock.Mock(token=token, viewer_login=lambda: asked.append(token) or "imouto")
        made = forges.Forges(FakeGitHub(), self.tokens, make_forgejo=make)
        cli.resolve_forgejo_owners(made, self.cfg, self.state)
        conn = db.connect(self.state / "state.sqlite3")
        try:
            stored = {row["key"] for row in conn.execute("SELECT key FROM kv WHERE key LIKE 'token_owner:%'")}
        finally:
            conn.close()
        digest = hashlib.sha256(b"token-a").hexdigest()[:16]
        self.assertEqual((asked, stored), (["token-a"], {f"token_owner:forge.example:{digest}"}))


if __name__ == "__main__":
    unittest.main()
