# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared pieces for agent adapters."""

import os
import re
import subprocess
from dataclasses import dataclass, field

# Only these variables reach an agent. Notably absent: GH_TOKEN/GITHUB_TOKEN,
# SSH_AUTH_SOCK, and anything else the operator's shell or other agent tooling exported.
ENV_ALLOWLIST = ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "XDG_RUNTIME_DIR",
                 "DBUS_SESSION_BUS_ADDRESS", "TZ")
_RATE_LIMIT_RE = re.compile(r"usage limit|rate.?limit|too many requests|\b429\b|quota|limit reached|"
                            r"out of (?:credits|usage)", re.IGNORECASE)


@dataclass
class JobPaths:
    root: str
    worktree: str
    context: str
    out: str


@dataclass
class AgentResult:
    review: dict = None
    session_id: str = None
    usage: dict = field(default_factory=dict)
    error_kind: str = None  # None | "rate_limit" | "error"
    error: str = ""


def agent_env(extra=None):
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env["TERM"] = "dumb"
    env.update(extra or {})
    return env


def classify_error(text):
    return "rate_limit" if _RATE_LIMIT_RE.search(text or "") else "error"


def tail(path, limit=2000):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode(errors="replace").strip()
    except OSError:
        return ""


def missing_flags(argv, flags, env):
    """Flags the installed CLI's help doesn't mention (an older CLI would reject or ignore them)."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return list(flags)
    text = proc.stdout + proc.stderr
    return [flag for flag in flags if flag not in text]
