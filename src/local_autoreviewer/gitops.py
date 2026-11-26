# SPDX-License-Identifier: GPL-3.0-or-later
"""Bare mirrors, PR worktrees and review context, using git without running PR code."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import forges

# A PR's own agent configuration must never load: hooks, settings, MCP servers or
# instructions. Matched case-insensitively against every path component.
AGENT_FILES = {"claude.md", "claude.local.md", "agents.md", "agents.override.md", ".mcp.json", "opencode.json",
               "opencode.jsonc"}
AGENT_DIRS = {".claude", ".codex", ".agents", ".opencode"}
PROJECT_DOCS = ("CLAUDE.md", "AGENTS.md")

HARDENING = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    # Symlinks become plain files holding the target path, so a PR cannot point the
    # agent's file tools (confined to the worktree) at files outside it.
    "-c", "core.symlinks=false",
    "-c", "submodule.recurse=false",
    "-c", "protocol.ext.allow=never",
    "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.process=",
    "-c", "filter.lfs.required=false",
    "-c", "diff.external=",
    "-c", "advice.detachedHead=false",
]


class GitError(RuntimeError):
    pass


class ShaMismatch(GitError):
    """GitHub's refs/pull/N/head has not caught up with the head the API reported."""


# How the diffs the agent reads are made. A deleted file shows only its header (--irreversible-delete): its old
# content is in the repo, and a PR that drops a vendored library would otherwise be all of it.
DIFF = ("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", "--irreversible-delete")


@dataclass
class Context:
    merge_base: str
    base_sha: str
    changed_files: int  # files whose changes pr.diff shows: not excluded, not deleted (see shown_files)
    diff_bytes: int
    excluded: list


class Git:
    def __init__(self, base_url, env, auth_env=dict):
        # base_url is https://github.com normally; tests point it at a local directory.
        # auth_env: more environment for each run, the Forgejo tokens (forges.Forges.git_env).
        self.base_url = base_url.rstrip("/")
        self.local = not self.base_url.startswith("https://")
        self.auth_env = auth_env
        self.env = dict(env)
        self.env.update({
            "GIT_CONFIG_GLOBAL": os.devnull,  # ignore the user's ~/.gitconfig (helpers, aliases, signing)
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        })

    def run(self, args, *, cwd=None, check=True, timeout=600, text=True):
        command = ["git", *HARDENING]
        if self.local:
            command += ["-c", "protocol.file.allow=always"]
        else:
            command += ["-c", "protocol.file.allow=never",
                        "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
        try:
            proc = subprocess.run([*command, *args], cwd=cwd, env={**self.env, **self.auth_env()},
                                  capture_output=True, text=text, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out") from None
        if check and proc.returncode != 0:
            stderr = proc.stderr if text else proc.stderr.decode(errors="replace")
            raise GitError(f"git {' '.join(args[:2])} failed: {stderr.strip()[:500]}")
        return proc

    def out(self, args, **kwargs):
        return self.run(args, **kwargs).stdout.strip()

    # Mirrors -----------------------------------------------------------------

    def url(self, repo):
        """Where the repo is fetched from: its Forgejo host for host/owner/name (see forges), else under
        base_url."""
        if forges.host(repo) and not self.local:
            return f"https://{repo}.git"
        return f"{self.base_url}/{repo}.git"

    def mirror_path(self, state, repo):
        owner, name = repo.split("/", 1)  # host/owner/name: under the host
        return Path(state) / "mirrors" / owner.lower() / f"{name.lower()}.git"

    def ensure_mirror(self, state, repo):
        path = self.mirror_path(state, repo)
        if not (path / "HEAD").exists():
            path.mkdir(parents=True, exist_ok=True)
            self.run(["init", "--bare", "--quiet", "--template=", str(path)])  # no sample hooks
            self.run(["-C", str(path), "config", "remote.origin.url", self.url(repo)])
            self.run(["-C", str(path), "config", "gc.auto", "256"])
        return path

    def fetch_pr(self, mirror, number, base_ref, expected_sha):
        head_ref = f"refs/autoreview/pr/{number}/head"
        base_ref_local = f"refs/autoreview/pr/{number}/base"
        self.run(["-C", str(mirror), "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "origin",
                  f"+refs/pull/{number}/head:{head_ref}", f"+refs/heads/{base_ref}:{base_ref_local}"])
        head = self.out(["-C", str(mirror), "rev-parse", "--verify", f"{head_ref}^{{commit}}"])
        if head != expected_sha:
            raise ShaMismatch(f"refs/pull/{number}/head is {head[:12]}, expected {expected_sha[:12]}")
        return self.out(["-C", str(mirror), "rev-parse", "--verify", f"{base_ref_local}^{{commit}}"])

    def delete_pr_refs(self, mirror, number):
        for kind in ("head", "base"):
            self.run(["-C", str(mirror), "update-ref", "-d", f"refs/autoreview/pr/{number}/{kind}"], check=False)

    def pr_numbers_with_refs(self, mirror):
        output = self.out(["-C", str(mirror), "for-each-ref", "--format=%(refname)", "refs/autoreview/pr/"])
        numbers = set()
        for ref in output.splitlines():
            parts = ref.split("/")
            if len(parts) >= 4 and parts[3].isdigit():
                numbers.add(int(parts[3]))
        return numbers

    # Worktrees ---------------------------------------------------------------

    def add_worktree(self, mirror, sha, dest):
        dest = Path(dest)
        if dest.exists():
            self.remove_worktree(mirror, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.run(["-C", str(mirror), "worktree", "add", "--detach", "--force", "--quiet", str(dest), sha])
        removed = strip_agent_files(dest)
        return removed

    def remove_worktree(self, mirror, dest):
        dest = Path(dest)
        self.run(["-C", str(mirror), "worktree", "remove", "--force", str(dest)], check=False)
        if dest.exists():
            shutil.rmtree(dest)
        self.run(["-C", str(mirror), "worktree", "prune"], check=False)

    # Context -----------------------------------------------------------------

    def write_context(self, mirror, head_sha, base_sha, ctx_dir, *, pr, exclude, project_docs):
        ctx_dir = Path(ctx_dir)
        ctx_dir.mkdir(parents=True, exist_ok=True)
        m = str(mirror)
        merge_base = self.out(["-C", m, "merge-base", base_sha, head_sha])
        excludes = [f":(exclude,glob){pattern}" for pattern in exclude]

        diff = self.run(["-C", m, *DIFF, merge_base, head_sha, "--", ".", *excludes], text=False).stdout
        # Not stripped (as out() would): a path may end in a space, and inline mode matches the two by name.
        excluded = self.run(["-C", m, "diff", "--no-color", "--name-only", merge_base, head_sha, "--",
                             *[f":(glob){pattern}" for pattern in exclude]]).stdout.splitlines() if exclude else []
        name_status = self.run(["-C", m, "diff", "--no-color", "--find-renames", "--name-status",
                                merge_base, head_sha]).stdout.removesuffix("\n")
        commits = self.out(["-C", m, "log", "--no-color", "--format=%h %an: %s%n%b",
                            f"{merge_base}..{head_sha}"])

        (ctx_dir / "pr.diff").write_bytes(diff)
        (ctx_dir / "files.txt").write_text(name_status + "\n")
        (ctx_dir / "commits.txt").write_text(commits + "\n")
        (ctx_dir / "pr.json").write_text(json.dumps({
            "repo": pr["repo"], "number": pr["number"], "title": pr.get("title", ""),
            "author": pr.get("author", ""), "url": pr.get("url", ""), "body": pr.get("body", ""),
            "base_ref": pr.get("base_ref", ""), "base_sha": base_sha, "merge_base": merge_base,
            "head_sha": head_sha, "excluded_from_diff": excluded,
        }, indent=2) + "\n")

        if project_docs:
            docs_dir = ctx_dir / "project-docs"
            for name in PROJECT_DOCS:
                proc = self.run(["-C", m, "show", f"{base_sha}:{name}"], check=False, text=False)
                if proc.returncode == 0 and proc.stdout:
                    docs_dir.mkdir(exist_ok=True)
                    (docs_dir / name).write_bytes(proc.stdout[:200_000])

        return Context(merge_base=merge_base, base_sha=base_sha,
                       changed_files=self.shown_files(m, merge_base, head_sha, excludes),
                       diff_bytes=len(diff), excluded=excluded)

    def shown_files(self, mirror, start, head_sha, excludes):
        """How many files a diff from start shows the changes of, for the size limits: excluded and deleted files
        are listed but leave nothing to read."""
        names = self.run(["-C", str(mirror), "diff", "--no-color", "--find-renames", "--name-only", "-z",
                          "--diff-filter=d", start, head_sha, "--", ".", *excludes], text=False).stdout
        return names.count(b"\0")

    def has_commit(self, mirror, sha):
        return self.run(["-C", str(mirror), "cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode == 0

    def contains(self, mirror, sha, other_sha):
        """Whether everything other_sha has is already in sha: merging it in would change nothing. That is
        so for an ancestor, and also for a branch merged by squash, which isn't one. False if other_sha
        isn't in the mirror."""
        m = str(mirror)
        merged = self.run(["-C", m, "merge-tree", "--write-tree", sha, other_sha], check=False)
        if merged.returncode != 0:  # a conflict, or other_sha is missing
            return False
        return merged.stdout.split()[0] == self.out(["-C", m, "rev-parse", f"{sha}^{{tree}}"])

    def since_review_start(self, mirror, previous_sha, head_sha, base_sha):
        """What to compare head_sha with to see only the PR's own changes since previous_sha. If the PR has
        merged (or been rebased onto) newer base commits since, that is previous_sha with those base commits
        merged in, so they don't show up as the PR's changes. None if that merge conflicts: the PR's changes
        can't then be told apart from the base's."""
        m = str(mirror)
        merge_base = self.out(["-C", m, "merge-base", base_sha, head_sha])
        if self.run(["-C", m, "merge-base", "--is-ancestor", merge_base, previous_sha], check=False).returncode == 0:
            return previous_sha
        merged = self.run(["-C", m, "merge-tree", "--write-tree", merge_base, previous_sha], check=False)
        return merged.stdout.split()[0] if merged.returncode == 0 else None

    def write_followup_context(self, mirror, start, head_sha, ctx_dir, *, exclude, previous, raised=()):
        """A follow-up's extra context: what the PR changed since the previous review (from start, see
        since_review_start), that review's open findings, and what earlier reviews raised besides."""
        excludes = [f":(exclude,glob){pattern}" for pattern in exclude]
        diff = self.run(["-C", str(mirror), *DIFF, start, head_sha, "--", ".", *excludes], text=False).stdout
        (Path(ctx_dir) / "since-review.diff").write_bytes(diff)
        (Path(ctx_dir) / "previous-findings.json").write_text(json.dumps(previous, indent=2))
        (Path(ctx_dir) / "already-raised.json").write_text(json.dumps(list(raised), indent=2))
        return len(diff), self.shown_files(mirror, start, head_sha, excludes)  # for the size limits, as the PR's


def strip_agent_files(root):
    """Delete agent config/instruction files from a checkout; return relative paths removed."""
    root = Path(root)
    removed = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in list(dirnames):
            if name.lower() in AGENT_DIRS:
                path = Path(dirpath) / name
                removed.append(str(path.relative_to(root)))
                if path.is_symlink():
                    path.unlink()
                else:
                    shutil.rmtree(path)
                dirnames.remove(name)
        for name in filenames:
            if name.lower() in AGENT_FILES:
                path = Path(dirpath) / name
                removed.append(str(path.relative_to(root)))
                path.unlink()
    return sorted(removed)
