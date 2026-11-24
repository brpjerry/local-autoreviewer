# SPDX-License-Identifier: GPL-3.0-or-later
"""Forgejo repos: the API client (against a fake HTTP opener), routing by repo name, links, git auth."""

import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from tests.helpers import OWNER, EnvTestCase, FakeGitHub, Remote, make_config
from local_autoreviewer import config as config_mod
from local_autoreviewer import db, forges, pipeline, review, suggestions
from local_autoreviewer.__main__ import CliError, parse_ref
from local_autoreviewer.forgejo import Forgejo, ForgejoError, _SameHostRedirects
from local_autoreviewer.github import GitHubError
from local_autoreviewer.gitops import Git

TOKEN = "forgejo-secret-token"
FORGE_REPO = "forge.example/team/tool"


class Response(io.BytesIO):
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
        if isinstance(answer, tuple):  # pages
            answer = answer[int(query.get("page", 1)) - 1] if int(query.get("page", 1)) <= len(answer) else []
        return Response(json.dumps(answer).encode())


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
        reviews = [{"id": 1, "state": "COMMENT", "comments_count": 2, "user": {"login": "imouto"}},
                   {"id": 2, "state": "COMMENT", "comments_count": 0, "user": {"login": "imouto"}},
                   {"id": 3, "state": "COMMENT", "comments_count": 2, "user": {"login": "author"}}]
        ours = [comment(11, 1, "imouto", at="2026-11-23T10:00:00Z"),
                comment(12, 1, "imouto", at="2026-11-23T10:00:00Z")]  # two of our suggestions ending on one line
        theirs = [comment(31, 3, "author", at="2026-11-23T11:00:00Z", body="won't fix"),
                  comment(32, 3, "author", position=0, original=4, at="2026-11-23T11:00:01Z")]
        client, server = self.client({("GET", api("repos/team/tool/pulls/4/reviews")): (reviews,),
                                      ("GET", api("repos/team/tool/pulls/4/reviews/1/comments")): ours,
                                      ("GET", api("repos/team/tool/pulls/4/reviews/3/comments")): theirs})
        found = {c["id"]: c for c in client.list_review_comments("team/tool", 4)}
        self.assertEqual({i: (c["pull_request_review_id"], c["in_reply_to_id"]) for i, c in found.items()},
                         {11: (1, None), 12: (1, None), 31: (None, 11), 32: (3, None)})
        self.assertEqual((found[31]["line"], found[32]["line"], found[32]["original_line"]), (10, None, 4))
        self.assertNotIn(api("repos/team/tool/pulls/4/reviews/2/comments"),
                         [urllib.parse.urlsplit(r["url"]).path for r in server.requests])  # no comments to list
        self.assertEqual(client.review_threads("team/tool", 4), [])  # nothing Forgejo's API could resolve

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
        with self.assertRaises(urllib.error.HTTPError) as caught:
            handler.redirect_request(request, io.BytesIO(), 301, "Moved", {}, "https://elsewhere.example/steal")
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
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")  # only tokens read so far
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://forge.example/.extraHeader")
        scheme, credentials = env["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: ").split()
        self.assertEqual((scheme, base64.b64decode(credentials).decode()), ("Basic", f"local-autoreviewer:{TOKEN}"))
        self.assertEqual(self.forges.tokens(), sorted([TOKEN, "other-token"]))  # every token, for the leak check

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


if __name__ == "__main__":
    unittest.main()
