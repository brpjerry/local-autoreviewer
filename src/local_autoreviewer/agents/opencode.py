# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenCode adapter: read-only file tools confined to the job, a dedicated home, operator and PR config ignored.

`opencode run` has no output-schema flag, so the prompt carries the schema, and the review is the JSON object in
the final message. The adapter checks that object against the schema itself, as Claude's and Codex's schema
flags do: review.validate() is lenient on purpose, as it also reads reviews saved in older shapes.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from .. import review
from ..limits import Limit, Unavailable, number
from ..runner import Invocation
from .base import AgentResult, agent_env, classify_error, missing_flags, tail

AGENT = "review"  # the agent the locked config defines; --agent selects it
USAGE_PATH = "/api/experimental/usage/limits"  # the server's OpenCode Go usage, as the operator's usage bar reads it
TOOLS = ("read", "grep", "glob")
REQUIRED_FLAGS = ("--standalone", "--agent", "--format", "--model", "--session")
# opencode's own switches: no project config (opencode.json, .opencode/, AGENTS.md and the instruction files
# a project config names), no Claude Code compatibility (CLAUDE.md, .claude skills), no update checks and no
# file watcher. The first two each have two names; opencode reads the first if it is set.
LOCKED_ENV = {
    "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_FILEWATCHER": "1",
}
_FENCE_RE = re.compile(r"^```[a-z]*\s*\n(.*)\n\s*```$", re.DOTALL)


def permissions(context):
    """Every tool use denied, then the read-only file tools allowed, with reads outside the checkout only in
    the context directory. opencode applies the last rule that matches, after its own defaults."""
    return [{"action": "*", "resource": "*", "effect": "deny"},
            *({"action": tool, "resource": "*", "effect": "allow"} for tool in TOOLS),
            {"action": "external_directory", "resource": f"{context}/*", "effect": "allow"}]


def locked_config(context):
    """The whole config a review runs with (OPENCODE_CONFIG_CONTENT): no instructions, plugins, skills, MCP
    servers, language servers, formatters, snapshots, sharing, updates or web search."""
    rules = permissions(context)
    return {
        "$schema": "https://opencode.ai/config.json",
        "default_agent": AGENT,
        "agents": {AGENT: {"description": "Read-only code review", "mode": "primary", "permissions": rules}},
        "permissions": rules,
        "policies": [{"action": action, "resource": "*", "effect": "deny"}
                     for action in ("mcp.use", "skill.use", "plugin.use", "websearch.use")],
        "instructions": [], "plugins": [], "skills": [],
        "lsp": False, "formatter": False, "snapshots": False,
        "share": "disabled", "update": "disable", "websearch": False, "warming": False,
    }


def output_format(schema):
    """What the prompt adds, in place of a schema flag (schema: the parsed JSON Schema)."""
    return ("\n\n## Output format\n\nEnd with your answer as one JSON object that matches this JSON Schema: every "
            "property it requires, and no others. Your final message must be that object alone, with no text and "
            "no code fence around it.\n\n```json\n" + json.dumps(schema, separators=(",", ":")) + "\n```\n")


# The JSON Schema keywords output_errors understands; the shipped schemas use no others (tests check).
SCHEMA_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "description"}
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def output_errors(value, schema, where="$"):
    """Where value breaks schema, as messages: types, enums, required and unknown properties, items."""
    kind = schema.get("type")
    if kind == "integer":
        valid = type(value) is int or (type(value) is float and value.is_integer())
    else:
        valid = kind in _TYPES and type(value) is _TYPES[kind]
    if not valid:
        return [f"{where}: expected {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{where}: not one of {', '.join(map(str, schema['enum']))}"]
    errors = []
    if kind == "object":
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", ()) if key not in value]
        if missing:
            errors.append(f"{where}: missing {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{where}: unexpected {', '.join(extra)}")
        for key, item in value.items():
            if key in properties:
                errors += output_errors(item, properties[key], f"{where}.{key}")
    elif kind == "array" and "items" in schema:
        for index, item in enumerate(value):
            errors += output_errors(item, schema["items"], f"{where}[{index}]")
    return errors


def read_events(text):
    events = []
    for line in text.split("\n"):  # JSON lines: not splitlines(), which also splits at U+2028 inside strings
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def final_text(events):
    """The text of the last step that wrote any: the final message."""
    steps, current = [], []
    for event in events:
        if event.get("type") == "step_start":
            current = []
            steps.append(current)
        elif event.get("type") == "text":
            current.append((event.get("part") or {}).get("text") or "")
    for texts in reversed(steps or [current]):
        text = "".join(texts).strip()
        if text:
            return text
    return ""


def extract_json(text):
    """The JSON object a final message holds: alone, in a code fence, or among prose. Among prose, it is the last
    whole object in it, skipping brace expressions that aren't JSON; the schema check then catches a wrong one."""
    text = text.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except ValueError:
        decoder, found, failures, start = json.JSONDecoder(), [], [], text.find("{")
        while start != -1:
            try:
                value, end = decoder.raw_decode(text, start)
            except ValueError as error:
                failures.append(error)
                start = text.find("{", start + 1)
                continue
            if isinstance(value, dict):
                found.append(value)
            start = text.find("{", end)  # past this object, so none nested in it counts
        if not found:
            if failures:
                raise ValueError(f"its JSON doesn't parse ({failures[0]})") from None
            raise ValueError("it holds no JSON object") from None
        value = found[-1]
    if not isinstance(value, dict):
        raise ValueError("its JSON isn't an object")
    return value


def opencode_limits(reply):
    """The limits in a usage reply: OpenCode Go's rolling, weekly and monthly meters and any budgets. Without a
    Go subscription the reply's data is null, so there are none: free models report no limits."""
    data = reply.get("data") if isinstance(reply.get("data"), dict) else None
    go = (data or {}).get("go")
    found = []
    for key in ("rolling", "weekly", "monthly") if isinstance(go, dict) else ():
        meter = go.get(key)
        if isinstance(meter, dict) and number(meter.get("percent")):
            limited = meter.get("limited") is True or meter.get("status") == "rate-limited"
            resets = int(meter["resetsAt"] / 1000) if number(meter.get("resetsAt")) and meter["resetsAt"] > 0 else 0
            found.append(Limit(key, 100.0 if limited else float(meter["percent"]), resets))
    budgets = (data or {}).get("budgets")
    for budget in budgets if isinstance(budgets, list) else ():
        if isinstance(budget, dict) and number(budget.get("limitDollars")) and budget["limitDollars"] > 0 \
                and number(budget.get("spentDollars")):
            resets = int(budget["resetsAt"] / 1000) if number(budget.get("resetsAt")) and budget["resetsAt"] > 0 \
                else 0
            name = f"{budget.get('per') or ''} budget".strip()
            found.append(Limit(name, 100.0 * budget["spentDollars"] / budget["limitDollars"], resets))
    return found


def _usage(events):
    totals, cost, steps = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}, 0.0, 0
    for event in events:
        if event.get("type") != "step_finish":
            continue
        part = event.get("part") or {}
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        for key, value in (("input", tokens.get("input")), ("output", tokens.get("output")),
                           ("reasoning", tokens.get("reasoning")), ("cache_read", cache.get("read")),
                           ("cache_write", cache.get("write"))):
            totals[key] += value if isinstance(value, int) else 0
        cost += part.get("cost") if isinstance(part.get("cost"), (int, float)) else 0
        steps += 1
    return {"usage": totals, "total_cost_usd": cost, "num_turns": steps}


def _error(event):
    error = event.get("error") or {}
    if isinstance(error, dict):
        return " ".join(filter(None, (str(error.get("type") or ""), str(error.get("message") or "")))) or "error"
    return str(error)


class OpenCodeAgent:
    name = "opencode"

    def __init__(self, cfg, state_dir):
        self.cfg = cfg
        # Its own HOME and XDG directories: opencode keeps its config, data (logins and sessions) and caches
        # there, and looks for ~/.claude, ~/.agents and ~/.opencode there. So the operator's own opencode setup
        # never loads, and reviews log in separately (`local-autoreviewer opencode-login`).
        self.home = Path(state_dir) / "opencode-home"
        self.schemas = {}  # out directory -> the schema its run was asked for, which parse() checks against

    def env(self, *, config_dir, context):
        """The environment for a run. config_dir is the global config directory opencode reads, which it creates
        and leaves empty: a job's own, so nothing can carry over into it, as the whole config is
        OPENCODE_CONFIG_CONTENT."""
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home = self.home
        return agent_env({
            "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_STATE_HOME": str(home / ".local/state"), "XDG_CACHE_HOME": str(home / ".cache"),
            **LOCKED_ENV, "OPENCODE_CONFIG_DIR": str(config_dir),
            "OPENCODE_CONFIG_CONTENT": json.dumps(locked_config(context)),
        })

    def job_env(self, paths):
        return self.env(config_dir=Path(paths.root, "opencode-config"), context=paths.context)

    def tool_env(self):
        """For commands outside a job (doctor, login): no context directory to read."""
        return self.env(config_dir=self.home / "config", context=self.home / "no-context")

    def _locked_flags(self):
        # --standalone: a private server for this run, not the background service (the operator's, or one
        # this would start and leave running).
        return ["--standalone", "--agent", AGENT]

    def _model_flags(self):
        if not self.cfg.model:
            return []
        model = self.cfg.model
        if self.cfg.effort and "#" not in model:
            model += f"#{self.cfg.effort}"  # opencode's model variant, such as a reasoning effort
        return ["--model", model]

    def new_session_id(self):
        return None  # assigned by opencode; read from the events

    def agent_note(self):
        return ("You have read-only file tools (read, grep, glob) for the checkout and the context directory. "
                "You can't run commands, change files or use the network.")

    def invocation(self, *, unit, paths, prompt, session_id, schema=None):
        argv = [self.cfg.command, "run", *self._locked_flags(), "--format", "json", *self._model_flags()]
        self.schemas[str(paths.out)] = parsed = json.loads(review.schema_text(schema))
        return Invocation(argv=argv, cwd=paths.worktree, env=self.job_env(paths),
                          stdin=(prompt + output_format(parsed)).encode(),
                          stdout_path=f"{paths.out}/agent.stdout", stderr_path=f"{paths.out}/agent.stderr",
                          unit=unit, memory_max=self.cfg.memory_max, tasks_max=self.cfg.tasks_max)

    def parse(self, run, paths, session_id):
        stdout = f"{paths.out}/agent.stdout"
        try:
            events = read_events(Path(stdout).read_text(errors="replace"))
        except OSError:
            events = []
        sessions = [event["sessionID"] for event in events if isinstance(event.get("sessionID"), str)]
        result = AgentResult(session_id=sessions[0] if sessions else session_id, usage=_usage(events))
        errors = [_error(event) for event in events if event.get("type") == "error"]
        if run.returncode != 0 or errors:
            message = "; ".join(errors) or tail(f"{paths.out}/agent.stderr") or tail(stdout) or f"exit {run.returncode}"
            result.error_kind, result.error = classify_error(message), message[:2000]
            return result
        finishes = [(event.get("part") or {}).get("reason") for event in events if event.get("type") == "step_finish"]
        if finishes and finishes[-1] == "length":
            result.error_kind, result.error = "error", "opencode's final message was cut off at the output limit"
            return result
        try:
            answer = extract_json(final_text(events))
        except ValueError as exc:
            result.error_kind, result.error = "error", f"opencode's final message is not the review: {exc}"
            return result
        schema = self.schemas.get(str(paths.out)) or json.loads(review.schema_text())
        errors = output_errors(answer, schema)
        if errors:
            shown = "; ".join(errors[:5]) + (f"; and {len(errors) - 5} more" if len(errors) > 5 else "")
            result.error_kind, result.error = "error", f"opencode's answer doesn't match the schema: {shown}"[:2000]
            return result
        result.review = answer
        return result

    def resume_interactive(self, *, paths, session_id):
        """The review's session lives in the reviews' home, so resume there, in the review's environment and
        under its read-only rules (the TUI takes no --agent; the locked config's default agent is the review's)."""
        return [self.cfg.command, "--standalone", "--session", session_id], self.interactive_env(paths)

    def interactive_env(self, paths):
        return {**self.job_env(paths), "TERM": os.environ.get("TERM", "dumb")}

    def ask(self, *, paths, session_id, question, timeout=900):
        argv = [self.cfg.command, "run", *self._locked_flags(), "--format", "json", "--session", session_id,
                *self._model_flags()]
        proc = subprocess.run(argv, cwd=paths.worktree, env=self.job_env(paths), input=question,
                              capture_output=True, text=True, timeout=timeout)
        events = read_events(proc.stdout)
        errors = [_error(event) for event in events if event.get("type") == "error"]
        if proc.returncode != 0 or errors:  # the reason, not progress text from before it
            return proc.returncode or 1, "; ".join(errors) or proc.stderr.strip() or proc.stdout.strip() \
                or f"exit {proc.returncode}"
        return 0, final_text(events)

    def limits_key(self):
        return f"opencode:{self.home}"

    def read_limits(self):
        """The plan's limits, from the server a private `opencode api` call starts in the reviews' home."""
        try:
            proc = subprocess.run([self.cfg.command, "api", "--standalone", "get", USAGE_PATH], capture_output=True,
                                  text=True, timeout=60, env=self.tool_env(), cwd=self.home)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Unavailable(f"opencode api: {type(exc).__name__}") from None
        if proc.returncode != 0:
            raise Unavailable(f"opencode api exited {proc.returncode}")
        try:
            reply = json.loads(proc.stdout)
        except ValueError:
            raise Unavailable("opencode api's reply isn't JSON") from None
        return opencode_limits(reply if isinstance(reply, dict) else {})

    def login_argv(self, integration=None):
        return [self.cfg.command, "auth", "login", "--standalone", integration or "opencode-go"]

    def doctor(self):
        env = self.tool_env()
        try:
            proc = subprocess.run([self.cfg.command, "--version"], capture_output=True, text=True, timeout=60,
                                  env=env, cwd=self.home)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"opencode not runnable: {exc}"
        version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr).strip() else "?"
        missing = missing_flags([self.cfg.command, "run", "--help"], REQUIRED_FLAGS, env)
        if missing:
            return False, f"opencode is too old: `opencode run --help` lacks {', '.join(missing)} (tested with 2.2.7)"
        try:
            proc = subprocess.run([self.cfg.command, "models", "--standalone"], capture_output=True, text=True,
                                  timeout=120, env=env, cwd=self.home)
        except subprocess.TimeoutExpired:
            return False, "opencode models timed out"
        models = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        model = self.cfg.model.split("#", 1)[0]
        if proc.returncode != 0 or not models:
            return False, f"opencode models failed: {(proc.stderr or proc.stdout).strip()[:200]}"
        if not model:
            return True, f"{version}: no model set, so opencode picks one (HOME={self.home})"
        if model not in models:
            return False, (f"{version}: model {model} isn't available to the reviews' opencode (HOME={self.home}). "
                           "Free models need no login; for OpenCode Go run `local-autoreviewer opencode-login`")
        return True, f"{version}: {model} available (HOME={self.home})"
