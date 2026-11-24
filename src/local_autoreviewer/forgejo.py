# SPDX-License-Identifier: GPL-3.0-or-later
"""Forgejo (and Gitea) access over its REST API, answering in the shapes github.GitHub returns, so the rest of
the service treats both alike. The token goes only into the Authorization header of requests to its own host:
no proxy from the environment, and no redirect to another host."""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .github import GitHubError, find_marked

PAGE = 50  # Forgejo's default MAX_RESPONSE_ITEMS; a shorter page is the last
MAX_PAGES = 100


class ForgejoError(GitHubError):
    pass


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect (a renamed repo, say) only within the host, which the token belongs to."""

    def __init__(self, host):
        self.host = host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc.lower() != self.host:
            raise urllib.error.HTTPError(newurl, code, f"redirect to another host refused: {msg}", headers, fp)
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

    def _request(self, method, path, *, query=None, payload=None):
        url = self.base + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": f"token {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self._open(urllib.request.Request(url, data=data, method=method, headers=headers),
                            timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:  # "HTTP 4xx" is what pipeline._rejected and the 422 retry look for
            detail = exc.read().decode(errors="replace").strip() if exc.fp else ""
            raise ForgejoError(f"forgejo {method} {path} failed: HTTP {exc.code}: {exc.reason} {detail[:500]}"
                               .replace(self.token, "***")) from None
        except (urllib.error.URLError, OSError) as exc:  # timeouts too
            raise ForgejoError(f"forgejo {method} {path} failed: {exc}".replace(self.token, "***")) from None
        return json.loads(body) if body.strip() else None

    def _pages(self, path, query=None, limit=None):
        items = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._request("GET", path, query={**(query or {}), "page": page, "limit": PAGE}) or []
            items += batch
            if len(batch) < PAGE or (limit and len(items) >= limit):
                break
        return items[:limit] if limit else items

    def viewer_login(self):
        return self._request("GET", "user")["login"]

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
        return self._request("GET", f"repos/{repo}/issues/{number}/comments") or []

    def list_review_comments(self, repo, number):
        """Every inline comment, as GitHub lists them. Forgejo lists them per review and has no reply links: it
        shows the comments on one line of a file as one conversation. So a comment counts as a reply to the
        first one on its line, unless it was posted with it (two of our suggestions that ended on one line,
        say). Forgejo files a reply made in its web UI under the review it answers, so what tells a reply
        from the review's own comments is that it came after the review was submitted: the PR's author may
        be the account our reviews post as."""
        comments, submitted = [], {}
        for r in self.list_reviews(repo, number):
            submitted[r["id"]] = r.get("submitted_at")
            if r.get("comments_count"):
                comments += self._request("GET", f"repos/{repo}/pulls/{number}/reviews/{r['id']}/comments") or []
        comments.sort(key=lambda c: (_time(c.get("created_at")), c["id"]))
        roots, out = {}, []
        for c in comments:
            key = (c.get("path"), c.get("position") or 0, c.get("original_position") or 0)
            root = roots.setdefault(key, c)
            review_id = c.get("pull_request_review_id")
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
