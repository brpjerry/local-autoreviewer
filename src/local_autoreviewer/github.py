# SPDX-License-Identifier: GPL-3.0-or-later
"""GitHub access through `gh`, with the token confined to these subprocesses."""

import json
import os
import subprocess
from datetime import datetime, timezone

PR_LIST_FIELDS = "number,headRefOid,baseRefName,baseRefOid,isDraft,author,title,updatedAt"
PR_VIEW_FIELDS = "number,state,headRefOid,baseRefName,baseRefOid,isDraft,author,title,body,url"
MARKER_PREFIX = "<!-- local-autoreviewer "


class GitHubError(RuntimeError):
    pass


def read_token(path):
    try:
        token = path.read_text().strip()
    except FileNotFoundError:
        raise GitHubError(f"GitHub token missing: create {path} (mode 0600) with a fine-grained PAT") from None
    if not token:
        raise GitHubError(f"GitHub token file {path} is empty")
    if path.stat().st_mode & 0o077:
        raise GitHubError(f"GitHub token file {path} must not be group/world readable (chmod 600)")
    return token


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


class GitHub:
    def __init__(self, token, config_dir, gh="gh"):
        self.token = token
        self.gh = gh
        self.config_dir = config_dir
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def env(self):
        return gh_env(self.token, self.config_dir)

    def _run(self, args, *, stdin=None, timeout=120):
        try:
            proc = subprocess.run([self.gh, *args], input=stdin, capture_output=True, text=True,
                                  env=self.env(), timeout=timeout)
        except subprocess.TimeoutExpired:
            raise GitHubError(f"gh {' '.join(args[:3])} timed out") from None
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip().replace(self.token, "***")
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

    def find_marked_review(self, repo, number, job_id, login):
        """Find a review we already posted for this job (crash between post and DB update)."""
        needle = f"{MARKER_PREFIX}job={job_id} "
        for review in self.list_reviews(repo, number):
            user = (review.get("user") or {}).get("login", "")
            if user.lower() == login.lower() and needle in (review.get("body") or ""):
                return review
        return None

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
