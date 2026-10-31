# SPDX-License-Identifier: GPL-3.0-or-later
"""Codex adapter: read-only sandbox, a dedicated CODEX_HOME, operator config ignored."""

import json
import os
import subprocess
from pathlib import Path

from .. import review
from ..runner import Invocation
from .base import AgentResult, agent_env, classify_error, missing_flags, tail

# Features that add tools or side channels a review doesn't need.
DISABLED_FEATURES = ("hooks", "plugins", "apps", "browser_use", "computer_use", "daemon_auto_start",
                     "goals", "image_generation", "multi_agent")
NO_SHELL_FEATURES = ("shell_tool", "unified_exec")
INLINE_LIMIT = 400_000
REQUIRED_FLAGS = ("--ignore-user-config", "--ignore-rules", "--sandbox", "--output-schema",
                  "--output-last-message", "--skip-git-repo-check", "--json")


class CodexAgent:
    name = "codex"

    def __init__(self, cfg, state_dir):
        self.cfg = cfg
        # Separate from ~/.codex: the global AGENTS.md there loads even with
        # --ignore-user-config, and sharing auth.json would race token refreshes.
        self.home = Path(state_dir) / "codex-home"

    def env(self):
        return agent_env({"CODEX_HOME": str(self.home)})

    def ensure_home(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.env()

    def _locked_flags(self):
        flags = ["--ignore-user-config", "--ignore-rules",
                 "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"',
                 "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"']
        features = DISABLED_FEATURES + (NO_SHELL_FEATURES if self.cfg.mode == "inline" else ())
        for feature in features:
            flags += ["--disable", feature]
        return flags

    def _model_flags(self):
        flags = []
        if self.cfg.model:
            flags += ["-m", self.cfg.model]
        if self.cfg.effort:
            flags += ["-c", f'model_reasoning_effort="{self.cfg.effort}"']
        return flags

    def new_session_id(self):
        return None  # assigned by codex; read from the thread.started event

    def agent_note(self):
        if self.cfg.mode == "inline":
            return "You have no tools. Everything you may use (the diff and the changed files) is included below."
        return ("You can run read-only shell commands (cat, rg, git log/show) in a sandbox with no "
                "network and no write access. Use them only to read code; never build, install or run it.")

    def inline_context(self, paths):
        """For mode=inline: the diff and changed files' contents, size-capped."""
        parts, size = [], 0
        diff = Path(paths.context, "pr.diff").read_text(errors="replace")
        parts.append(f"\n## pr.diff\n\n```diff\n{diff[:INLINE_LIMIT // 2]}\n```\n")
        size += len(parts[-1])
        for line in Path(paths.context, "files.txt").read_text().splitlines():
            fields = line.split("\t")
            if not fields or fields[0].startswith("D"):
                continue
            path = Path(paths.worktree, fields[-1])
            if not path.is_file() or path.stat().st_size > 200_000:
                continue
            chunk = f"\n## {fields[-1]}\n\n```\n{path.read_text(errors='replace')}\n```\n"
            if size + len(chunk) > INLINE_LIMIT:
                parts.append("\n(further files omitted: size limit)\n")
                break
            parts.append(chunk)
            size += len(chunk)
        return "".join(parts)

    def invocation(self, *, unit, paths, prompt, session_id):
        argv = [self.cfg.command, "exec", *self._locked_flags(), "--sandbox", "read-only",
                "-C", paths.worktree, "--skip-git-repo-check", "--json",
                "-o", f"{paths.out}/review.json", "--output-schema", str(review.schema_path()),
                *self._model_flags(), "-"]
        if self.cfg.mode == "inline":
            prompt += self.inline_context(paths)
        return Invocation(argv=argv, cwd=paths.worktree, env=self.ensure_home(), stdin=prompt.encode(),
                          stdout_path=f"{paths.out}/agent.stdout", stderr_path=f"{paths.out}/agent.stderr",
                          unit=unit, memory_max=self.cfg.memory_max, tasks_max=self.cfg.tasks_max)

    def parse(self, run, paths, session_id):
        thread_id, usage, errors = None, {}, []
        try:
            with open(f"{paths.out}/agent.stdout") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    kind = event.get("type")
                    if kind == "thread.started":
                        thread_id = event.get("thread_id")
                    elif kind == "turn.completed":
                        usage = event.get("usage") or {}
                    elif kind in ("error", "turn.failed"):
                        detail = event.get("message") or (event.get("error") or {}).get("message") or ""
                        errors.append(str(detail))
        except OSError:
            pass
        result = AgentResult(session_id=thread_id, usage={"usage": usage})
        output = Path(paths.out, "review.json")
        if run.returncode != 0 or errors or not output.is_file():
            message = "; ".join(errors) or tail(f"{paths.out}/agent.stderr") or f"exit {run.returncode}"
            result.error_kind, result.error = classify_error(message), message[:2000]
            return result
        try:
            result.review = json.loads(output.read_text())
        except ValueError:
            result.error_kind, result.error = "error", "codex final message is not JSON"
        return result

    def resume_interactive(self, *, paths, session_id):
        return [self.cfg.command, "resume", session_id, "-C", paths.worktree, "--sandbox", "read-only"], \
            {"CODEX_HOME": str(self.home)}

    def ask(self, *, paths, session_id, question, timeout=900):
        out = Path(paths.out, f"ask-{os.getpid()}.txt")
        argv = [self.cfg.command, "exec", "resume", session_id, *self._locked_flags(),
                "--skip-git-repo-check", "-o", str(out), *self._model_flags(), "-"]
        proc = subprocess.run(argv, cwd=paths.worktree, env=self.ensure_home(), input=question,
                              capture_output=True, text=True, timeout=timeout)
        answer = out.read_text() if out.is_file() else (proc.stderr or proc.stdout)
        return proc.returncode, answer

    def doctor(self):
        try:
            proc = subprocess.run([self.cfg.command, "login", "status"], capture_output=True, text=True,
                                  timeout=60, env=self.ensure_home())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"codex not runnable: {exc}"
        text = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0 or "logged in" not in text.lower():
            return False, (f"codex has no login in {self.home}; run `local-autoreviewer codex-login` "
                           f"({text[:120]})")
        missing = missing_flags([self.cfg.command, "exec", "--help"], REQUIRED_FLAGS, self.env())
        if missing:
            return False, f"codex is too old: `codex exec --help` lacks {', '.join(missing)} (tested with 0.163.0)"
        return True, f"codex: {text.splitlines()[0]} (CODEX_HOME={self.home})"
