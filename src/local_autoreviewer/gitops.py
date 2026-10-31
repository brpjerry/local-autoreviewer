# SPDX-License-Identifier: GPL-3.0-or-later
"""Bare mirrors, PR worktrees and review context, using git without running PR code."""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# A PR's own agent configuration must never load: hooks, settings, MCP servers or
# instructions. Matched case-insensitively against every path component.
AGENT_FILES = {"claude.md", "claude.local.md", "agents.md", "agents.override.md", ".mcp.json"}
AGENT_DIRS = {".claude", ".codex", ".agents"}
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


@dataclass
class Context:
    merge_base: str
    base_sha: str
    changed_files: int
    diff_bytes: int
    excluded: list


class Git:
    def __init__(self, base_url, env):
        # base_url is https://github.com normally; tests point it at a local directory.
        self.base_url = base_url.rstrip("/")
        self.local = not self.base_url.startswith("https://")
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
            proc = subprocess.run([*command, *args], cwd=cwd, env=self.env, capture_output=True,
                                  text=text, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out") from None
        if check and proc.returncode != 0:
            stderr = proc.stderr if text else proc.stderr.decode(errors="replace")
            raise GitError(f"git {' '.join(args[:2])} failed: {stderr.strip()[:500]}")
        return proc

    def out(self, args, **kwargs):
        return self.run(args, **kwargs).stdout.strip()

    # Mirrors -----------------------------------------------------------------

    def mirror_path(self, state, repo):
        owner, name = repo.split("/", 1)
        return Path(state) / "mirrors" / owner.lower() / f"{name.lower()}.git"

    def ensure_mirror(self, state, repo):
        path = self.mirror_path(state, repo)
        if not (path / "HEAD").exists():
            path.mkdir(parents=True, exist_ok=True)
            self.run(["init", "--bare", "--quiet", "--template=", str(path)])  # no sample hooks
            self.run(["-C", str(path), "config", "remote.origin.url", f"{self.base_url}/{repo}.git"])
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

        diff = self.run(["-C", m, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames",
                         merge_base, head_sha, "--", ".", *excludes], text=False).stdout
        excluded = self.out(["-C", m, "diff", "--no-color", "--name-only", merge_base, head_sha, "--",
                             *[f":(glob){pattern}" for pattern in exclude]]).splitlines() if exclude else []
        name_status = self.out(["-C", m, "diff", "--no-color", "--find-renames", "--name-status",
                                merge_base, head_sha])
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

        changed = len([line for line in name_status.splitlines() if line.strip()])
        return Context(merge_base=merge_base, base_sha=base_sha, changed_files=changed,
                       diff_bytes=len(diff), excluded=excluded)

    def has_commit(self, mirror, sha):
        return self.run(["-C", str(mirror), "cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode == 0

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

    def write_followup_context(self, mirror, start, head_sha, ctx_dir, *, exclude, previous):
        """A follow-up's extra context: what the PR changed since the previous review (from start, see
        since_review_start), and that review's findings."""
        excludes = [f":(exclude,glob){pattern}" for pattern in exclude]
        diff = self.run(["-C", str(mirror), "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames",
                         start, head_sha, "--", ".", *excludes], text=False).stdout
        (Path(ctx_dir) / "since-review.diff").write_bytes(diff)
        (Path(ctx_dir) / "previous-findings.json").write_text(json.dumps(previous, indent=2))
        names = self.run(["-C", str(mirror), "diff", "--no-color", "--name-only", "-z", start, head_sha, "--", ".",
                          *excludes], text=False).stdout
        return len(diff), names.count(b"\0")  # for the size limits, like the PR's own diff


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
