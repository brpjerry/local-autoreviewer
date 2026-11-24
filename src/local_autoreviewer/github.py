# SPDX-License-Identifier: GPL-3.0-or-later
"""GitHub access through `gh`, with the token confined to these subprocesses."""

import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

PR_LIST_FIELDS = "number,headRefOid,baseRefName,baseRefOid,isDraft,author,title,updatedAt"
PR_VIEW_FIELDS = "number,state,headRefOid,baseRefName,baseRefOid,isDraft,author,title,body,url"
MARKER_PREFIX = "<!-- local-autoreviewer "

log = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    pass


def read_token(path, what="GitHub token", hint="a fine-grained PAT"):
    try:
        token = path.read_text().strip()
    except FileNotFoundError:
        raise GitHubError(f"{what} missing: create {path} (mode 0600) with {hint}") from None
    except (OSError, UnicodeError) as exc:
        raise GitHubError(f"cannot read {what} file {path}: {type(exc).__name__}") from None
    if not token:
        raise GitHubError(f"{what} file {path} is empty")
    if not token.isascii() or not token.isprintable() or " " in token:  # it goes in a header: never quote it
        raise GitHubError(f"{what} file {path} must hold the token alone, on one line")
    try:
        mode = path.stat().st_mode
    except OSError as exc:  # gone since it was read
        raise GitHubError(f"cannot read {what} file {path}: {type(exc).__name__}") from None
    if mode & 0o077:
        raise GitHubError(f"{what} file {path} must not be group/world readable (chmod 600)")
    return token


class TokenFile:
    """A token file read again whenever it changes, so a long-running worker uses a replaced token without a
    restart. The first read fails as read_token does. A later change that leaves the file unusable (empty or
    half written, say) keeps the last good token, and the error is logged once for each version of the file."""

    def __init__(self, path, what="GitHub token", hint="a fine-grained PAT"):
        self.path = Path(path)
        self.what = what
        self.hint = hint
        self.seen = []  # every token read, oldest first: a review must quote none of them
        self._token = None
        self._stamp = None  # the version of the file the token was read from (_stat)
        self._failed = None  # the stamp of the version last reported unusable
        self._lock = threading.Lock()

    def _stat(self):
        """What changes when the file is replaced or rewritten, and when its mode is (a chmod 600 makes a
        refused file usable)."""
        try:
            st = self.path.stat()
        except OSError:
            return None
        return st.st_ino, st.st_size, st.st_mode, st.st_mtime_ns, st.st_ctime_ns

    def get(self):
        with self._lock:
            stamp = self._stat()  # before reading: a change after it makes the next call read again
            if self._token is not None and stamp == self._stamp:
                return self._token
            try:
                token = read_token(self.path, self.what, self.hint)
            except GitHubError as exc:
                if self._token is None:
                    raise
                if stamp != self._failed:
                    log.error("%s; still using the token read before", exc)
                    self._failed = stamp
                return self._token
            if self._token is not None and token != self._token:
                log.info("%s reloaded from %s", self.what, self.path)
            self._token, self._stamp, self._failed = token, stamp, None
            if token not in self.seen:
                self.seen.append(token)
            return token


def find_marked(reviews, job_id, login):
    """The review `login` posted for this job, by its marker, if any."""
    needle = f"{MARKER_PREFIX}job={job_id} "
    for review in reviews:
        user = (review.get("user") or {}).get("login", "")
        if user.lower() == login.lower() and needle in (review.get("body") or ""):
            return review
    return None


def gh_env(token, config_dir):
    """Minimal environment for gh and git: the token, but none of the user's gh config."""
    return {
        "GH_TOKEN": token,
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
        "GH_CONFIG_DIR": str(config_dir),
        "NO_COLOR": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "LANG": "C.UTF-8",
    }


THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved comments(first: 1) { nodes { body pullRequestReview { databaseId fullDatabaseId } } } }
      }
    }
  }
}"""
RESOLVE_MUTATION = """
mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"""


class GitHub:
    def __init__(self, token, config_dir, gh="gh"):
        self._token = token  # the token, or a TokenFile to read it from as it is at each call
        self.gh = gh
        self.config_dir = config_dir
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @property
    def token(self):
        return self._token.get() if isinstance(self._token, TokenFile) else self._token

    def env(self):
        return gh_env(self.token, self.config_dir)

    def _run(self, args, *, stdin=None, timeout=120):
        token = self.token  # read once: the call redacts the token it ran with
        try:
            proc = subprocess.run([self.gh, *args], input=stdin, capture_output=True, text=True,
                                  env=gh_env(token, self.config_dir), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise GitHubError(f"gh {' '.join(args[:3])} timed out") from None
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip().replace(token, "***")
            raise GitHubError(f"gh {' '.join(args[:3])} failed: {message[:500]}")
        return proc.stdout

    def _json(self, args, **kwargs):
        output = self._run(args, **kwargs)
        return json.loads(output) if output.strip() else None

    def viewer_login(self):
        return self._json(["api", "user"])["login"]

    def token_expiry(self):
        """Return the PAT's expiry as an aware datetime, or None if it never expires."""
        output = self._run(["api", "--include", "user"])
        for line in output.splitlines():
            if not line.strip():
                break
            name, _, value = line.partition(":")
            if name.strip().lower() == "github-authentication-token-expiration":
                value = value.strip()
                for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
                    try:
                        parsed = datetime.strptime(value, fmt)
                        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                raise GitHubError(f"unparseable token expiry {value!r}")
        return None

    def list_open_prs(self, repo, limit=500):
        return self._json(["pr", "list", "-R", repo, "--state", "open", "--limit", str(limit),
                           "--json", PR_LIST_FIELDS])

    def pr_view(self, repo, number):
        return self._json(["pr", "view", str(number), "-R", repo, "--json", PR_VIEW_FIELDS])

    def _list(self, path):
        # One compact JSON object per line across all pages (no reliance on newer gh flags).
        output = self._run(["api", "--paginate", path, "--jq", ".[] | tojson"])
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def list_reviews(self, repo, number):
        return self._list(f"repos/{repo}/pulls/{number}/reviews")

    def list_issue_comments(self, repo, number):
        """The PR's conversation comments."""
        return self._list(f"repos/{repo}/issues/{number}/comments")

    def list_review_comments(self, repo, number):
        """The PR's inline comments, replies included (in_reply_to_id: the thread's first comment)."""
        return self._list(f"repos/{repo}/pulls/{number}/comments")

    def review_threads(self, repo, number):
        """The PR's review threads: id, resolved, and the body of the thread's first comment and the id of the
        review it belongs to (review_id, as REST numbers reviews: fullDatabaseId, which GitHub added because
        review ids outgrow databaseId's 32-bit Int, else databaseId; None if neither)."""
        owner, name = repo.split("/", 1)
        threads, after = [], None
        while True:
            args = ["api", "graphql", "-f", f"query={THREADS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}",
                    "-F", f"number={number}"] + (["-f", f"after={after}"] if after else [])
            page = self._json(args)["data"]["repository"]["pullRequest"]["reviewThreads"]
            for node in page["nodes"]:
                first = (node["comments"]["nodes"] or [{}])[0]
                posted_in = first.get("pullRequestReview") or {}
                review_id = posted_in.get("fullDatabaseId") or posted_in.get("databaseId")
                threads.append({"id": node["id"], "resolved": bool(node["isResolved"]), "body": first.get("body") or "",
                                "review_id": int(review_id) if review_id is not None else None})
            if not page["pageInfo"]["hasNextPage"]:
                return threads
            after = page["pageInfo"]["endCursor"]

    def resolve_thread(self, thread_id):
        self._json(["api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-f", f"id={thread_id}"])

    def find_marked_review(self, repo, number, job_id, login):
        """Find a review we already posted for this job (crash between post and DB update)."""
        return find_marked(self.list_reviews(repo, number), job_id, login)

    def post_review(self, repo, number, commit_sha, body, comments=None):
        """Post a COMMENT review pinned to commit_sha, with optional inline comments (suggested changes)."""
        review = {"commit_id": commit_sha, "event": "COMMENT", "body": body}
        if comments:
            review["comments"] = comments
        payload = json.dumps(review)
        return self._json(["api", "--method", "POST", f"repos/{repo}/pulls/{number}/reviews",
                           "--input", "-"], stdin=payload)


def pr_author(pr):
    return ((pr.get("author") or {}).get("login") or "").lower()
