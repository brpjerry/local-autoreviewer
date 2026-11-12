# SPDX-License-Identifier: GPL-3.0-or-later
"""Codex adapter: read-only sandbox, a dedicated CODEX_HOME, operator config ignored."""

import json
import os
import selectors
import subprocess
import time
from pathlib import Path

from .. import review
from ..limits import Limit, Unavailable, number
from ..runner import Invocation
from .base import AgentResult, agent_env, classify_error, missing_flags, operator_env, tail

# Features that add tools or side channels a review doesn't need.
DISABLED_FEATURES = ("hooks", "plugins", "apps", "browser_use", "computer_use", "daemon_auto_start",
                     "goals", "image_generation", "multi_agent")
NO_SHELL_FEATURES = ("shell_tool", "unified_exec")
INLINE_LIMIT = 400_000
REQUIRED_FLAGS = ("--ignore-user-config", "--ignore-rules", "--sandbox", "--output-schema",
                  "--output-last-message", "--skip-git-repo-check", "--json")


def _latest_comments(text, limit):
    """author-comments.json within limit characters, by leaving out its oldest comments whole. A left-out
    comment's review numbering (see feedback.author_comments) passes to the next one answering that review."""
    try:
        comments = json.loads(text)
    except ValueError:
        return text, ""
    if not isinstance(comments, list):
        return text, ""
    shown, dropped = json.dumps(comments, ensure_ascii=False, indent=2), 0
    while len(shown) > limit and len(comments) > 1:
        gone = comments.pop(0)
        if isinstance(gone, dict) and isinstance(gone.get("review"), dict):  # keep its review's numbering
            heir = next((c for c in comments if isinstance(c, dict)
                         and c.get("review_id") == gone["review"].get("id")), None)
            if heir is not None and "review" not in heir:
                heir["review"] = gone["review"]
        dropped += 1
        shown = json.dumps(comments, ensure_ascii=False, indent=2)
    return shown, f"\n(the {dropped} oldest comments left out: size limit)" if dropped else ""


def _window_name(minutes):
    return {300: "5-hour", 10080: "weekly"}.get(minutes) or (f"{minutes // 60}-hour" if minutes else "plan")


def codex_limits(reply):
    """The limits in an account/rateLimits/read reply: the codex bucket's windows, and those of another bucket
    tied to one model (normalModelSlug), which count only for a reviewer running that model."""
    main = reply.get("rateLimits") if isinstance(reply.get("rateLimits"), dict) else None
    main_ids = {"codex", str((main or {}).get("limitId") or "codex")}
    buckets = {key: raw for key, raw in (reply.get("rateLimitsByLimitId") or {}).items() if isinstance(raw, dict)} \
        if isinstance(reply.get("rateLimitsByLimitId"), dict) else {}
    if main is not None and not main_ids & set(buckets):
        buckets[str(main.get("limitId") or "codex")] = main
    found = []
    for key, bucket in buckets.items():
        model = "" if key in main_ids else str(bucket.get("normalModelSlug") or "")
        if key not in main_ids and not model:
            continue  # another product's quota, not the reviews'
        for slot in ("primary", "secondary"):
            window = bucket.get(slot)
            if isinstance(window, dict) and number(window.get("usedPercent")):
                minutes = int(window["windowDurationMins"]) if number(window.get("windowDurationMins")) else 0
                name = _window_name(minutes) + (f" ({model})" if model else "")
                resets = int(window["resetsAt"]) if number(window.get("resetsAt")) else 0
                found.append(Limit(name, float(window["usedPercent"]), resets, model))
    if reply.get("ordinaryUsageAllowed") is False:  # blocked, whatever the windows say
        found.append(Limit("plan", 100.0))
    return found


def app_server_call(command, env, cwd, method, timeout=30):
    """One request to a short-lived `codex app-server` over stdio, as the operator's usage bar makes it: no thread,
    no inference. Raises Unavailable with a message that never holds the reply."""
    try:
        proc = subprocess.Popen([command, "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=cwd)
    except OSError as exc:
        raise Unavailable(f"can't start codex app-server: {type(exc).__name__}") from None
    deadline, pending = time.monotonic() + timeout, bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)

            def send(message):
                proc.stdin.write((json.dumps(message) + "\n").encode())
                proc.stdin.flush()

            def receive(request_id):
                while True:
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending[:] = rest
                        try:
                            message = json.loads(line)
                        except ValueError:
                            raise Unavailable("codex app-server sent something that isn't JSON") from None
                        if isinstance(message, dict) and message.get("id") == request_id:
                            if "result" not in message:
                                raise Unavailable(f"codex app-server refused {method}")
                            return message["result"]
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise Unavailable("codex app-server timed out")
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        raise Unavailable("codex app-server exited early")
                    pending.extend(chunk)
                    if len(pending) > 4 * 1024 * 1024:
                        raise Unavailable("codex app-server's reply is too large")

            send({"id": 1, "method": "initialize", "params": {"clientInfo": {
                "name": "local_autoreviewer", "title": "local-autoreviewer", "version": "1"}}})
            receive(1)
            send({"method": "initialized"})
            send({"id": 2, "method": method})
            return receive(2)
    except OSError as exc:
        raise Unavailable(f"codex app-server: {type(exc).__name__}") from None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for stream in (proc.stdin, proc.stdout):
            try:  # closing stdin flushes what a failed send left in its buffer, into a pipe that is gone
                stream.close()
            except OSError:
                pass


class CodexAgent:
    name = "codex"

    def __init__(self, cfg, state_dir):
        self.cfg = cfg
        # Separate from ~/.codex, because the global AGENTS.md there loads even with --ignore-user-config.
        # Every Codex reviewer shares this home's own login, and its reviews run side by side: processes
        # sharing one auth.json take turns refreshing it (Codex keeps an auth.lock beside it). A copy of
        # an auth.json in a second home would race the original for the rotating refresh token.
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
        for name in ("previous-findings.json", "already-raised.json", "author-comments.json", "since-review.diff"):
            extra = Path(paths.context, name)
            if extra.is_file():
                text = extra.read_text(errors="replace")
                cut = ""
                if name == "author-comments.json":  # oldest first: leave out whole old ones, keep the latest
                    text, cut = _latest_comments(text, INLINE_LIMIT // 4)
                if len(text) > INLINE_LIMIT // 4:
                    cut = "\n(truncated: size limit)"
                parts.append(f"\n## {name}\n\n```\n{text[:INLINE_LIMIT // 4]}\n```{cut}\n")
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

    def invocation(self, *, unit, paths, prompt, session_id, schema=None):
        argv = [self.cfg.command, "exec", *self._locked_flags(), "--sandbox", "read-only",
                "-C", paths.worktree, "--skip-git-repo-check", "--json",
                "-o", f"{paths.out}/review.json", "--output-schema", str(schema or review.schema_path()),
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
            operator_env({"CODEX_HOME": str(self.home)})

    def ask(self, *, paths, session_id, question, timeout=900):
        out = Path(paths.out, f"ask-{os.getpid()}.txt")
        argv = [self.cfg.command, "exec", "resume", session_id, *self._locked_flags(),
                "--skip-git-repo-check", "-o", str(out), *self._model_flags(), "-"]
        proc = subprocess.run(argv, cwd=paths.worktree, env=self.ensure_home(), input=question,
                              capture_output=True, text=True, timeout=timeout)
        answer = out.read_text() if out.is_file() else (proc.stderr or proc.stdout)
        return proc.returncode, answer

    def limits_key(self):
        return f"codex:{self.home}"

    def read_limits(self):
        """The plan's limits, from the reviews' own login."""
        reply = app_server_call(self.cfg.command, self.ensure_home(), self.home, "account/rateLimits/read")
        limits = codex_limits(reply if isinstance(reply, dict) else {})
        if not limits:
            raise Unavailable("codex reported no limits")
        return limits

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
