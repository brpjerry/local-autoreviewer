# SPDX-License-Identifier: GPL-3.0-or-later
"""Forgejo access over its REST API, answering in the shapes github.GitHub returns, so the rest of the service
treats both alike. The token goes only into the Authorization header of requests to its own host over HTTPS:
no proxy from the environment, and no redirect anywhere else. Every failure is a ForgejoError."""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.client import HTTPException

from .github import MARKER_PREFIX, GitHubError, find_marked

PAGE = 50  # asked for; a server may send fewer (MAX_RESPONSE_ITEMS), so X-Total-Count (or Link) says when to stop
MAX_PAGES = 100


class ForgejoError(GitHubError):
    pass


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a GET's redirect (a renamed repo, say) only to HTTPS on the token's own host. urllib would turn a
    redirected POST into a GET and drop the review, so a POST's redirect fails instead."""

    def __init__(self, host):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if req.get_method() != "GET" or target.scheme != "https" or target.netloc.lower() != self.host:
            raise urllib.error.HTTPError(newurl, code, f"redirect refused: {msg}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _time(value):
    return datetime.fromisoformat(value).timestamp() if value else 0.0


def _pr(pr):
    """A PR as `gh pr view --json` gives it (github.PR_VIEW_FIELDS)."""
    state = "OPEN" if pr["state"] == "open" else "MERGED" if pr.get("merged") else "CLOSED"
    return {"number": pr["number"], "state": state, "headRefOid": pr["head"]["sha"],
            "baseRefName": pr["base"]["ref"], "baseRefOid": pr["base"].get("sha") or "",
            "isDraft": bool(pr.get("draft")), "author": {"login": (pr.get("user") or {}).get("login") or ""},
            "title": pr.get("title") or "", "body": pr.get("body") or "", "url": pr.get("html_url") or "",
            "updatedAt": pr.get("updated_at") or ""}


def _comment(comment):
    """A GitHub review comment (see suggestions.comments) as Forgejo takes it: the first line, and how many
    more it covers."""
    first = comment.get("start_line") or comment["line"]
    side = "old_position" if comment.get("side") == "LEFT" else "new_position"
    return {"path": comment["path"], "body": comment["body"], side: first,
            "extra_lines_count": comment["line"] - first}


class Forgejo:
    def __init__(self, host, token, *, opener=None, timeout=60):
        self.host = host
        self.token = token
        self.base = f"https://{host}/api/v1/"
        self.timeout = timeout
        self._open = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                           _SameHostRedirects(host)).open

    def _fail(self, method, path, detail):
        return ForgejoError(f"forgejo {method} {path} failed: {detail}".replace(self.token, "***"))

    def _request(self, method, path, *, query=None, payload=None, with_headers=False):
        url = self.base + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": f"token {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self._open(urllib.request.Request(url, data=data, method=method, headers=headers),
                            timeout=self.timeout) as response:
                body, answered = response.read(), response.headers
            result = json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as exc:  # "HTTP 4xx" is what pipeline._rejected and the 422 retry look for
            try:
                detail = exc.read().decode(errors="replace").strip() if exc.fp else ""
            except (OSError, HTTPException):  # a cut-off error page
                detail = ""
            raise self._fail(method, path, f"HTTP {exc.code}: {exc.reason} {detail[:500]}") from None
        except (urllib.error.URLError, OSError, HTTPException) as exc:  # timeouts and cut-off answers too
            raise self._fail(method, path, exc) from None
        except ValueError as exc:  # not JSON: a sign-in page, say
            raise self._fail(method, path, f"unreadable answer ({type(exc).__name__})") from None
        return (result, answered) if with_headers else result

    def _pages(self, path, query=None, limit=None):
        items = []
        for page in range(1, MAX_PAGES + 1):
            batch, headers = self._request("GET", path, query={**(query or {}), "page": page, "limit": PAGE},
                                           with_headers=True)
            if not isinstance(batch, list):
                raise self._fail("GET", path, "the answer is not a list")
            items += batch
            total = (headers.get("X-Total-Count") or "").strip() if headers else ""
            more = 'rel="next"' in ((headers.get("Link") or "") if headers else "")  # without a count (a proxy's)
            if not batch or (limit and len(items) >= limit) or (
                    len(items) >= int(total) if total.isdigit() else not more and len(batch) < PAGE):
                return items[:limit] if limit else items
        raise self._fail("GET", path, f"more than {MAX_PAGES} pages")

    def viewer_login(self):
        user = self._request("GET", "user")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or not login:
            raise self._fail("GET", "user", "the answer has no login")
        return login

    def token_expiry(self):
        return None  # Forgejo's access tokens don't expire

    def list_open_prs(self, repo, limit=500):
        # The list may hold null for a PR Forgejo failed to load.
        return [_pr(pr) for pr in self._pages(f"repos/{repo}/pulls", {"state": "open"}, limit) if pr]

    def pr_view(self, repo, number):
        return _pr(self._request("GET", f"repos/{repo}/pulls/{number}"))

    def list_reviews(self, repo, number):
        """Submitted reviews (a pending one is its author's draft), with GitHub's fields."""
        return [r for r in self._pages(f"repos/{repo}/pulls/{number}/reviews") if r.get("state") != "PENDING"]

    def list_issue_comments(self, repo, number):
        return self._request("GET", f"repos/{repo}/issues/{number}/comments") or []  # not paginated: all of them

    def list_review_comments(self, repo, number):
        """Every inline comment, as GitHub lists them. Forgejo lists them per review (each list whole) and has no
        reply links: it shows the comments on one line of a file as one conversation. So a comment counts as a
        reply to the first one on its line, unless it was posted with it (two of our suggestions that ended on
        one line, say). Forgejo files a reply made in its web UI under the review it answers, so what tells a
        reply from the review's own comments is that it came after the review was submitted: the PR's author
        may be the account our reviews post as. Each of our reviews (by its marker) has threads of its own, so
        a later review's suggestion on a line an earlier one commented on is never a reply."""
        comments, submitted, ours = [], {}, {}
        for r in self.list_reviews(repo, number):
            submitted[r["id"]] = r.get("submitted_at")
            if MARKER_PREFIX in (r.get("body") or ""):
                ours[r["id"]] = {}
            if r.get("comments_count"):
                comments += self._request("GET", f"repos/{repo}/pulls/{number}/reviews/{r['id']}/comments") or []
        comments.sort(key=lambda c: (_time(c.get("created_at")), c["id"]))
        roots, out = {}, []
        for c in comments:
            key = (c.get("path"), c.get("position") or 0, c.get("original_position") or 0)
            review_id = c.get("pull_request_review_id")
            root = roots.setdefault(key, c)  # the conversation Forgejo shows: a reply in another review joins it
            if review_id in ours:  # but each of our reviews starts threads of its own
                root = ours[review_id].setdefault(key, c)
            posted_with_root = review_id == root.get("pull_request_review_id") and (
                submitted.get(review_id) is None or _time(c.get("created_at")) <= _time(submitted[review_id]))
            reply = root is not c and not posted_with_root
            out.append({**c, "line": c.get("position") or None, "original_line": c.get("original_position") or None,
                        "pull_request_review_id": None if reply else review_id,
                        "in_reply_to_id": root["id"] if reply else None})
        return out

    def review_threads(self, repo, number):
        return []  # Forgejo's API can't resolve a conversation, so there are none to sweep

    def find_marked_review(self, repo, number, job_id, login):
        return find_marked(self.list_reviews(repo, number), job_id, login)

    def post_review(self, repo, number, commit_sha, body, comments=None):
        review = {"commit_id": commit_sha, "event": "COMMENT", "body": body}
        if comments:
            review["comments"] = [_comment(c) for c in comments]
        return self._request("POST", f"repos/{repo}/pulls/{number}/reviews", payload=review)
