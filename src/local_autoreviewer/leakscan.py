# SPDX-License-Identifier: GPL-3.0-or-later
"""Block posting a review that contains credentials.

Agents can read files (Codex's shell can read outside the worktree), so a review
could quote a secret. The scan reports pattern names only, never the match.
"""

import base64
import json
import re

PATTERNS = {
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    "github-fine-grained-pat": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "openai-key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "private-key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
}


def _variants(secret):
    raw = secret.encode()
    yield "exact", secret
    yield "reversed", secret[::-1]
    yield "base64", base64.b64encode(raw).decode().rstrip("=")
    yield "base64url", base64.urlsafe_b64encode(raw).decode().rstrip("=")
    yield "hex", raw.hex()


def scan(text, secrets=()):
    """Return the names of everything that looks like a credential in text."""
    hits = [name for name, pattern in PATTERNS.items() if pattern.search(text)]
    lowered = text.lower()
    for index, secret in enumerate(s for s in secrets if s and len(s) >= 8):
        for kind, variant in _variants(secret):
            haystack = lowered if kind == "hex" else text
            if variant in haystack:
                hits.append(f"known-secret-{index}-{kind}")
    return hits


_SECRET_KEY_RE = re.compile(r"token|secret|key|password|authorization", re.IGNORECASE)


def collect_secrets(json_files):
    """Best-effort: credential-looking string values from local auth files (kept in memory only)."""
    found = []

    def walk(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and len(value) >= 16 and _SECRET_KEY_RE.search(key):
            found.append(value)

    for path in json_files:
        try:
            with open(path) as handle:
                walk(json.load(handle))
        except (OSError, ValueError):
            continue
    return found
