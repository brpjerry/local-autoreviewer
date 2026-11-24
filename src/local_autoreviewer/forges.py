# SPDX-License-Identifier: GPL-3.0-or-later
"""Where a repo lives: GitHub for `owner/name`, a Forgejo server for `host/owner/name`.

The name says which forge hosts the repo, so everything keyed by it (jobs, mirrors, PR refs on the command
line) stays apart across forges, and a call finds its forge without a lookup. Forges sends each call to the
repo's forge behind github.GitHub's interface."""

import base64
from pathlib import Path
from urllib.parse import quote

from .forgejo import Forgejo
from .github import read_token

GITHUB = "https://github.com"


def host(repo):
    """The Forgejo host of a `host/owner/name` repo, or None for GitHub's `owner/name`."""
    parts = repo.split("/")
    return parts[0].lower() if len(parts) == 3 else None


def api_name(repo):
    """The repo as its forge's API names it: owner/name."""
    return repo.split("/", 1)[1] if host(repo) else repo


def web_url(repo):
    forgejo = host(repo)
    return f"https://{forgejo}/{api_name(repo)}" if forgejo else f"{GITHUB}/{repo}"


def file_url(repo, sha, file, line=None):
    kind = "src/commit" if host(repo) else "blob"
    return f"{web_url(repo)}/{kind}/{sha}/{quote(file)}" + (f"#L{line}" if line else "")


def commit_url(repo, number, sha):
    """A commit of the PR: on GitHub in the PR's own view of it."""
    return f"{web_url(repo)}/commit/{sha}" if host(repo) else f"{web_url(repo)}/pull/{number}/commits/{sha}"


def suggests(repo):
    """Whether a ```suggestion block in an inline comment becomes a change the author can apply: on GitHub only.
    Forgejo shows it as plain code, so its inline comments carry diffs instead."""
    return host(repo) is None


def _git_credentials(token):
    return base64.b64encode(f"local-autoreviewer:{token}".encode()).decode()


class Forges:
    """The GitHub client for `owner/name` repos and a Forgejo client per host for `host/owner/name` ones, so
    callers pass the repo as configured and never care where it lives. A Forgejo host's token is read from
    token_dir/<host> on first use."""

    def __init__(self, github, token_dir, *, make_forgejo=Forgejo):
        self.github = github
        self.token_dir = Path(token_dir)
        self.make_forgejo = make_forgejo
        self._forgejo = {}
        self.owners = {}  # Forgejo host: its token's owner

    def forgejo(self, forgejo_host):
        client = self._forgejo.get(forgejo_host)
        if client is None:
            token = read_token(self.token_dir / forgejo_host, what=f"Forgejo token for {forgejo_host}",
                               hint="an access token")
            client = self._forgejo[forgejo_host] = self.make_forgejo(forgejo_host, token)
        return client

    def _route(self, repo):
        forgejo = host(repo)
        return (self.forgejo(forgejo), api_name(repo)) if forgejo else (self.github, repo)

    def host_owner(self, forgejo_host):
        """The login reviews post as on a Forgejo host: its token's owner, looked up once."""
        if forgejo_host not in self.owners:
            self.owners[forgejo_host] = self.forgejo(forgejo_host).viewer_login()
        return self.owners[forgejo_host]

    def owner(self, repo):
        return self.host_owner(host(repo))

    def secrets(self):
        """Every Forgejo token, read or not yet, and the credentials git sends for it (_git_credentials), for the
        check that no posted text quotes one. Files in the token directory that aren't text are skipped."""
        found = {client.token for client in list(self._forgejo.values())}
        try:
            for path in self.token_dir.iterdir():
                try:
                    if path.is_file():
                        found.add(path.read_text().strip())
                except (OSError, UnicodeError):
                    continue
        except OSError:
            pass
        return sorted({secret for token in found if token for secret in (token, _git_credentials(token))})

    def git_env(self):
        """Git configuration, as environment variables, that sends each Forgejo token read so far with git's
        requests to its own host only, and doesn't follow that host's redirects: git would send the header on
        to wherever one led. A token never goes on a command line."""
        settings = []
        for forgejo_host, client in sorted(list(self._forgejo.items())):
            prefix = f"http.https://{forgejo_host}/"
            settings += [(f"{prefix}.extraHeader", f"Authorization: Basic {_git_credentials(client.token)}"),
                         (f"{prefix}.followRedirects", "false")]
        env = {}
        for n, (key, value) in enumerate(settings):
            env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"] = key, value
        if settings:
            env["GIT_CONFIG_COUNT"] = str(len(settings))
        return env

    # GitHub's interface; see github.GitHub.
    def env(self):
        return self.github.env()

    def viewer_login(self):
        return self.github.viewer_login()

    def token_expiry(self):
        return self.github.token_expiry()

    def resolve_thread(self, thread_id):  # only GitHub lists threads to resolve (see Forgejo.review_threads)
        return self.github.resolve_thread(thread_id)

    def list_open_prs(self, repo, limit=500):
        client, name = self._route(repo)
        return client.list_open_prs(name, limit=limit)

    def pr_view(self, repo, number):
        client, name = self._route(repo)
        return client.pr_view(name, number)

    def list_reviews(self, repo, number):
        client, name = self._route(repo)
        return client.list_reviews(name, number)

    def list_issue_comments(self, repo, number):
        client, name = self._route(repo)
        return client.list_issue_comments(name, number)

    def list_review_comments(self, repo, number):
        client, name = self._route(repo)
        return client.list_review_comments(name, number)

    def review_threads(self, repo, number):
        client, name = self._route(repo)
        return client.review_threads(name, number)

    def find_marked_review(self, repo, number, job_id, login):
        client, name = self._route(repo)
        return client.find_marked_review(name, number, job_id, login)

    def post_review(self, repo, number, commit_sha, body, comments=None):
        client, name = self._route(repo)
        return client.post_review(name, number, commit_sha, body, comments=comments)
