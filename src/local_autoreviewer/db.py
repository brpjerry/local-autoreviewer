# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite state: PR snapshots, trigger events and the review job queue."""

import contextlib
import sqlite3
import time
from pathlib import Path

ACTIVE = ("queued", "running", "posting")
FINAL = ("posted", "failed", "superseded", "cancelled", "skipped", "blocked")
STATUSES = ACTIVE + FINAL
FOREGROUND = "foreground"  # job source for `review`, which runs its jobs itself, never the worker

MIGRATIONS = [
    # 1: initial schema. Times are integer Unix seconds (UTC).
    """
    CREATE TABLE prs (
        repo TEXT NOT NULL, number INTEGER NOT NULL, state TEXT NOT NULL,
        is_draft INTEGER NOT NULL, head_sha TEXT NOT NULL, base_ref TEXT NOT NULL,
        author TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
        head_changed_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
        PRIMARY KEY (repo, number));
    CREATE TABLE events (
        id INTEGER PRIMARY KEY, source TEXT NOT NULL, delivery_id TEXT,
        repo TEXT NOT NULL, number INTEGER NOT NULL, action TEXT NOT NULL,
        head_sha TEXT, received_at INTEGER NOT NULL,
        UNIQUE (source, delivery_id));
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
        head_sha TEXT NOT NULL, base_ref TEXT NOT NULL, base_sha TEXT,
        agent TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
        rerun INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, reason TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, not_before INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0, post INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER,
        posted_at INTEGER, session_id TEXT, scope_unit TEXT, workdir TEXT,
        review_id INTEGER, review_url TEXT, usage_json TEXT, error TEXT,
        UNIQUE (repo, number, head_sha, base_ref, agent, rerun));
    CREATE INDEX jobs_status ON jobs (status, not_before);
    CREATE INDEX jobs_pr ON jobs (repo, number, agent);
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaTooNew(RuntimeError):
    pass


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL is persistent; switching needs an exclusive lock that ignores busy_timeout,
    # so only the first opener does it (retrying if another connection is mid-setup).
    for attempt in range(50):
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError:
            if attempt == 49:
                raise
            time.sleep(0.1)
    migrate(conn)
    return conn


def migrate(conn):
    while True:
        # Read the version under the write lock, so concurrent openers migrate once.
        with transaction(conn):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise SchemaTooNew(
                    f"state database schema {version} is newer than this build supports ({SCHEMA_VERSION}); "
                    "upgrade local-autoreviewer instead of downgrading")
            if version == SCHEMA_VERSION:
                return
            for statement in _statements(MIGRATIONS[version]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version={version + 1}")


def _statements(script):
    return [part.strip() for part in script.split(";") if part.strip()]


@contextlib.contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def kv_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(conn, key, value):
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


def get_job(conn, job_id):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def update_job(conn, job_id, **fields):
    if not fields:
        return
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"unknown job status {fields['status']!r}")
    columns = ", ".join(f"{name}=?" for name in fields)
    conn.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*fields.values(), job_id))


def active_jobs(conn, repo, number, agent=None):
    sql = f"SELECT * FROM jobs WHERE repo=? AND number=? AND status IN ({','.join('?' * len(ACTIVE))})"
    params = [repo, number, *ACTIVE]
    if agent is not None:
        sql += " AND agent=?"
        params.append(agent)
    return conn.execute(sql, params).fetchall()


def insert_job(conn, *, repo, number, head_sha, base_ref, agent, model, source, not_before, now,
               post=True, rerun=0):
    """Insert a queued job; return its id, or None if this exact job already exists."""
    cursor = conn.execute(
        """INSERT OR IGNORE INTO jobs (repo, number, head_sha, base_ref, agent, model, source, rerun,
               status, not_before, post, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
        (repo, number, head_sha, base_ref, agent, model, source, rerun, not_before, int(post), now))
    return cursor.lastrowid if cursor.rowcount else None


def next_rerun(conn, repo, number, head_sha, base_ref, agent):
    row = conn.execute(
        "SELECT MAX(rerun) FROM jobs WHERE repo=? AND number=? AND head_sha=? AND base_ref=? AND agent=?",
        (repo, number, head_sha, base_ref, agent)).fetchone()
    return 0 if row[0] is None else row[0] + 1


def started_since(conn, agent, since):
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE agent=? AND started_at IS NOT NULL AND started_at>=?",
        (agent, since)).fetchone()[0]
