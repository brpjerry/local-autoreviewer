# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared test fixtures: a local git 'GitHub', a fake GitHub API and fake agents."""

import itertools
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.environ["LOCAL_AUTOREVIEWER_SHARE"] = str(ROOT)

from local_autoreviewer import config as config_mod, pipeline  # noqa: E402
from local_autoreviewer.github import MARKER_PREFIX  # noqa: E402
from local_autoreviewer.gitops import Git  # noqa: E402
from local_autoreviewer.runner import DirectRunner  # noqa: E402

FAKE_AGENT = ROOT / "tests" / "fakes" / "fake_agent.py"
OWNER = "alice"
REPO = "alice/demo"


def git(*args, cwd=None, check=True):
    env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"],
           "HOME": os.environ.get("HOME", "/"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=check, capture_output=True,
                          text=True).stdout.strip()


class Remote:
    """A bare repo laid out like GitHub: <base>/alice/demo.git with refs/pull/N/head."""

    def __init__(self, base):
        self.base = Path(base)
        self.bare = self.base / f"{REPO}.git"
        self.work = self.base / "work"
        self.bare.parent.mkdir(parents=True)
        git("init", "--bare", "-q", "-b", "main", str(self.bare))
        git("init", "-q", "-b", "main", str(self.work))
        self.write("README.md", "demo\n")
        self.write("src/calc.py", "def divide(a, b):\n    return a / b\n")
        self.base_sha = self.commit("initial")
        git("push", "-q", str(self.bare), "main", cwd=self.work)

    def write(self, path, text):
        target = self.work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", message, cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def open_pr(self, number, files, message="feature"):
        """Branch from main, commit files, publish as refs/pull/N/head. Returns head SHA."""
        git("checkout", "-q", "-B", f"pr{number}", "main", cwd=self.work)
        for path, text in files.items():
            self.write(path, text)
        sha = self.commit(message)
        git("push", "-q", "-f", str(self.bare), f"HEAD:refs/pull/{number}/head", cwd=self.work)
        git("checkout", "-q", "main", cwd=self.work)
        return sha

    def push_pr(self, number, files, message="update"):
        git("checkout", "-q", f"pr{number}", cwd=self.work)
        for path, text in files.items():
            self.write(path, text)
        sha = self.commit(message)
        git("push", "-q", "-f", str(self.bare), f"HEAD:refs/pull/{number}/head", cwd=self.work)
        git("checkout", "-q", "main", cwd=self.work)
        return sha


class FakeGitHub:
    def __init__(self, login=OWNER):
        self.login = login
        self.prs = {}  # (repo, number) -> dict
        self.posted = []
        self.ids = itertools.count(1000)

    def env(self):
        return {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}

    def set_pr(self, number, head, *, repo=REPO, state="OPEN", draft=False, author=OWNER, base="main",
               title="Add feature", body="Please review."):
        self.prs[(repo, number)] = {
            "number": number, "state": state, "headRefOid": head, "baseRefName": base, "baseRefOid": "",
            "isDraft": draft, "author": {"login": author}, "title": title, "body": body,
            "url": f"https://github.com/{repo}/pull/{number}", "updatedAt": "",
        }

    def viewer_login(self):
        return self.login

    def list_open_prs(self, repo, limit=500):
        return [dict(pr) for (r, _), pr in self.prs.items() if r == repo and pr["state"] == "OPEN"]

    def pr_view(self, repo, number):
        return dict(self.prs[(repo, number)])

    def post_review(self, repo, number, commit_sha, body, comments=None):
        review_id = next(self.ids)
        self.posted.append({"repo": repo, "number": number, "commit_id": commit_sha, "body": body,
                            "comments": comments or [], "id": review_id, "user": {"login": self.login}})
        return {"id": review_id, "html_url": f"https://github.com/{repo}/pull/{number}#review-{review_id}"}

    def find_marked_review(self, repo, number, job_id, login):
        needle = f"{MARKER_PREFIX}job={job_id} "
        for review in self.posted:
            if review["repo"] == repo and review["number"] == number and needle in review["body"]:
                return review
        return None


def agent_command(tmp, name, mode):
    """A wrapper script that runs the fake agent in a given mode, logging to <tmp>/<name>.log."""
    script = Path(tmp) / f"fake-{name}-{mode}"
    log = Path(tmp) / f"{name}.log"
    script.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_AGENT} --mode {mode} --log {log} -- \"$@\"\n")
    script.chmod(0o755)
    return str(script), log


def make_config(tmp, *, claude="ok", codex="ok", repo_extra=None, service=None, agents=("claude", "codex")):
    claude_cmd, _ = agent_command(tmp, "claude", claude)
    codex_cmd, _ = agent_command(tmp, "codex", codex)
    data = {
        "service": {"settle": "0s", "review_timeout": "30s", **(service or {})},
        "defaults": {"agents": list(agents)},
        "agents": {"claude": {"command": claude_cmd, "model": "opus"}, "codex": {"command": codex_cmd}},
        "repo": [{"name": REPO, **(repo_extra or {})}],
    }
    return config_mod.parse(data)


class Clock:
    def __init__(self, start=1_800_000_000):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class EnvTestCase(unittest.TestCase):
    """Temp state dir + local remote + fake GitHub + services."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lar-test-")
        self.tmp = Path(self._tmp.name)
        self.remote = Remote(self.tmp / "remote")
        self.github = FakeGitHub()
        self.clock = Clock()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.cfg = make_config(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def services(self, cfg=None, secrets=()):
        return pipeline.Services(
            config=cfg or self.cfg, state=self.state, github=self.github,
            git=Git(str(self.remote.base), self.github.env()), runner=DirectRunner(), token_owner=OWNER,
            secrets=lambda: list(secrets), clock=self.clock)

    def agent_log(self, name):
        import json
        path = self.tmp / f"{name}.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
