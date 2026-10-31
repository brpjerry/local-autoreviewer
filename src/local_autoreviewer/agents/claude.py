# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code adapter: headless, no command execution, file tools confined to the job."""

import json
import subprocess
import uuid

from .. import review
from ..runner import Invocation
from .base import AgentResult, agent_env, classify_error, missing_flags, tail

TOOLS = "Read,Grep,Glob"
REQUIRED_FLAGS = ("--restricted", "--safe-mode", "--strict-mcp-config", "--tools", "--permission-prompts",
                  "--json-schema", "--session-id", "--add-dir")
QUIET_ENV = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_UPDATES": "1",
    "DISABLE_INSTALLATION_CHECKS": "1",
}


class ClaudeAgent:
    name = "claude"

    def __init__(self, cfg, state_dir):
        self.cfg = cfg
        self.state_dir = state_dir

    def _locked_flags(self, paths):
        # --restricted: no code-running tools or WebFetch, user/project settings ignored,
        # file tools confined to the working dirs. --safe-mode: no CLAUDE.md, skills, hooks.
        return ["--restricted", "--safe-mode", "--strict-mcp-config", "--tools", TOOLS,
                "--permission-prompts", "none", "--add-dir", paths.context]

    def _model_flags(self):
        flags = []
        if self.cfg.model:
            flags += ["--model", self.cfg.model]
        if self.cfg.effort:
            flags += ["--effort", self.cfg.effort]
        return flags

    def new_session_id(self):
        return str(uuid.uuid4())

    def agent_note(self):
        return "You have read-only file tools (Read, Grep, Glob) confined to the checkout and the context directory."

    def invocation(self, *, unit, paths, prompt, session_id):
        argv = [self.cfg.command, "-p", *self._locked_flags(paths), "--session-id", session_id,
                "--output-format", "json", "--json-schema", review.schema_text(), *self._model_flags()]
        return Invocation(argv=argv, cwd=paths.worktree, env=agent_env(QUIET_ENV), stdin=prompt.encode(),
                          stdout_path=f"{paths.out}/agent.stdout", stderr_path=f"{paths.out}/agent.stderr",
                          unit=unit, memory_max=self.cfg.memory_max, tasks_max=self.cfg.tasks_max)

    def parse(self, run, paths, session_id):
        stdout = f"{paths.out}/agent.stdout"
        try:
            with open(stdout) as handle:
                data = json.loads(handle.read().strip().splitlines()[-1])
        except (OSError, ValueError, IndexError):
            message = tail(f"{paths.out}/agent.stderr") or tail(stdout) or f"exit {run.returncode}"
            return AgentResult(session_id=session_id, error_kind=classify_error(message), error=message)
        result = AgentResult(session_id=data.get("session_id") or session_id, usage={
            "usage": data.get("usage"), "total_cost_usd": data.get("total_cost_usd"),
            "num_turns": data.get("num_turns"), "duration_ms": data.get("duration_ms")})
        if data.get("is_error") or data.get("subtype") != "success":
            message = str(data.get("result") or data.get("subtype") or "error")
            status = data.get("api_error_status")
            result.error_kind = "rate_limit" if status == 429 else classify_error(message)
            result.error = f"{data.get('subtype')}: {message}"[:2000]
            return result
        structured = data.get("structured_output")
        if structured is None:
            try:
                structured = json.loads(data.get("result") or "")
            except ValueError:
                result.error_kind, result.error = "error", "no structured output in Claude result"
                return result
        result.review = structured
        return result

    def resume_interactive(self, *, paths, session_id):
        # A normal interactive session with the operator's own config and tools.
        return [self.cfg.command, "--resume", session_id], None

    def ask(self, *, paths, session_id, question, timeout=900):
        argv = [self.cfg.command, "-p", *self._locked_flags(paths), "--resume", session_id,
                "--output-format", "text", *self._model_flags()]
        proc = subprocess.run(argv, cwd=paths.worktree, env=agent_env(QUIET_ENV), input=question,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or proc.stderr

    def doctor(self):
        try:
            proc = subprocess.run([self.cfg.command, "auth", "status", "--json"], capture_output=True,
                                  text=True, timeout=60, env=agent_env(QUIET_ENV))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"claude not runnable: {exc}"
        try:
            status = json.loads(proc.stdout)
        except ValueError:
            return False, f"claude auth status failed: {(proc.stderr or proc.stdout).strip()[:200]}"
        if not status.get("loggedIn"):
            return False, "claude is not logged in (run `claude` once and /login)"
        missing = missing_flags([self.cfg.command, "--help"], REQUIRED_FLAGS, agent_env(QUIET_ENV))
        if missing:
            return False, f"claude is too old: its --help lacks {', '.join(missing)} (tested with 2.1.296)"
        return True, f"claude logged in ({status.get('authMethod')}, {status.get('subscriptionType')})"
