# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude Code adapter: headless, no command execution, file tools confined to the job."""

import http.client
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .. import review
from ..limits import Limit, Unavailable, epoch, number
from ..runner import Invocation
from .base import AgentResult, agent_env, classify_error, missing_flags, operator_env, tail

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


# The endpoint behind Claude Code's /usage: the plan's 5-hour and weekly windows, and weekly limits on one model.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_WINDOW_NAMES = {"session": "5-hour", "weekly_all": "weekly", "five_hour": "5-hour", "seven_day": "weekly"}


def claude_limits(usage):
    """The limits in a usage reply: its `limits` list, else its five_hour and seven_day windows."""
    found, items = [], usage.get("limits")
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or not number(item.get("percent")):
            continue
        scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
        on_model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        model = str(on_model.get("display_name") or "")
        name = _WINDOW_NAMES.get(item.get("kind"), str(item.get("group") or item.get("kind") or "plan"))
        found.append(Limit(f"{name} ({model})" if model else name, float(item["percent"]),
                           epoch(item.get("resets_at")), model))
    if found:
        return found
    for key in ("five_hour", "seven_day"):
        raw = usage.get(key)
        if isinstance(raw, dict) and number(raw.get("utilization")):
            found.append(Limit(_WINDOW_NAMES[key], float(raw["utilization"]), epoch(raw.get("resets_at"))))
    return found


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

    def invocation(self, *, unit, paths, prompt, session_id, schema=None):
        argv = [self.cfg.command, "-p", *self._locked_flags(paths), "--session-id", session_id,
                "--output-format", "json", "--json-schema", review.schema_text(schema), *self._model_flags()]
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
        return [self.cfg.command, "--resume", session_id], operator_env()

    def ask(self, *, paths, session_id, question, timeout=900):
        argv = [self.cfg.command, "-p", *self._locked_flags(paths), "--resume", session_id,
                "--output-format", "text", *self._model_flags()]
        proc = subprocess.run(argv, cwd=paths.worktree, env=agent_env(QUIET_ENV), input=question,
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or proc.stderr

    def limits_key(self):
        return "claude"

    def read_limits(self):
        """The plan's limits, read with the login reviews use. Its access token is never refreshed here: that
        would rotate the refresh token out from under Claude Code. An expired one waits for Claude's next run."""
        try:
            oauth = json.loads((Path.home() / ".claude/.credentials.json").read_text())["claudeAiOauth"]
            token, expires = oauth["accessToken"], oauth["expiresAt"] / 1000
        except (OSError, ValueError, KeyError, TypeError):
            raise Unavailable("no Claude login to read them with") from None
        if not token or expires <= time.time():
            raise Unavailable("Claude's access token has expired; Claude renews it on its next run") from None
        request = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json",
            "User-Agent": "local-autoreviewer"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                usage = json.load(response)
        except urllib.error.HTTPError as exc:
            raise Unavailable(f"the usage endpoint answered HTTP {exc.code}") from None
        except (OSError, ValueError, http.client.HTTPException) as exc:  # IncompleteRead is an HTTPException
            raise Unavailable(f"the usage endpoint failed: {type(exc).__name__}") from None
        limits = claude_limits(usage if isinstance(usage, dict) else {})
        if not limits:
            raise Unavailable("the usage endpoint reported no limits")
        return limits

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
