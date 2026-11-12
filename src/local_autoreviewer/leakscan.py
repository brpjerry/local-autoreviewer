# SPDX-License-Identifier: GPL-3.0-or-later
"""Block posting a review that contains credentials.

Agents can read files (Codex's shell can read outside the worktree), so a review
could quote a secret. The scan reports pattern names only, never the match.
"""

import base64
import json
import re
import sqlite3
from pathlib import Path

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


def collect_secrets_from(value, fields=()):
    """Credential-looking strings in parsed JSON, as collect_secrets finds them in files: under a key that names
    a secret, or one of fields."""
    found = []

    def walk(item, key=""):
        if isinstance(item, dict):
            for k, v in item.items():
                walk(v, k)
        elif isinstance(item, list):
            for v in item:
                walk(v, key)
        elif isinstance(item, str) and len(item) >= 16 and (_SECRET_KEY_RE.search(key) or key in fields):
            found.append(item)

    walk(value)
    return found


def collect_secrets(json_files):
    """Best-effort: credential-looking string values from local auth files (kept in memory only)."""
    found = []
    for path in json_files:
        try:
            with open(path) as handle:
                found.extend(collect_secrets_from(json.load(handle)))
        except (OSError, ValueError):
            continue
    return found


# opencode keeps logins in its SQLite database. credential.value is JSON: {"type": "key", "key"} for an API key,
# {"type": "oauth", "access", "refresh", "expires"} for OAuth (opencode 2.2.7's Credential.Value). Accounts keep
# their tokens in columns of their own.
_DB_QUERIES = ("SELECT value FROM credential", "SELECT access_token, refresh_token FROM account",
               "SELECT access_token, refresh_token FROM control_account")
_CREDENTIAL_FIELDS = ("key", "access", "refresh")


def collect_db_secrets(databases):
    """Best-effort: the credentials in opencode databases, read without writing to them (kept in memory only).
    A database that is missing or can't be read is skipped, as a missing auth file is."""
    found = []
    for path in databases:
        try:
            conn = sqlite3.connect(f"{Path(path).absolute().as_uri()}?mode=ro", uri=True, timeout=5)
        except (OSError, ValueError, sqlite3.Error):
            continue
        try:
            for query in _DB_QUERIES:
                try:
                    rows = conn.execute(query).fetchall()
                except sqlite3.Error:  # missing, unreadable, or a schema without this table or column
                    continue
                for row in rows:
                    for value in row:
                        found.extend(_stored_secrets(value))
        finally:
            conn.close()
    return found


def _stored_secrets(value):
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        return collect_secrets_from(parsed, _CREDENTIAL_FIELDS)
    return [value] if len(value) >= 16 else []
