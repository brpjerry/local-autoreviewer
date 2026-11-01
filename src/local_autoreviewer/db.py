# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite state: PR snapshots, trigger events and the review job queue."""

import contextlib
import json
import sqlite3
import time
from pathlib import Path

# waiting: the review is ready and waits for the other reviews of its commit, to be posted with them.
ACTIVE = ("queued", "running", "waiting", "posting")
FINAL = ("posted", "failed", "superseded", "cancelled", "skipped", "blocked")
STATUSES = ACTIVE + FINAL
# Job sources: "poll" and "webhook" (a trigger), "manual" (`enqueue`, run by the worker), and FOREGROUND.
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
    # 2: failed posts are retried without running the agent again; count them apart from `attempts`.
    "ALTER TABLE jobs ADD COLUMN post_attempts INTEGER NOT NULL DEFAULT 0;",
    # 3: review data kept for analysis: what each review reported, and what was done about it.
    # The reviewer's own ratings come from the review; rated_impact onwards is filled in by `triage`.
    """
    ALTER TABLE jobs ADD COLUMN effort TEXT NOT NULL DEFAULT '';
    ALTER TABLE jobs ADD COLUMN prompt_version TEXT NOT NULL DEFAULT '';
    ALTER TABLE jobs ADD COLUMN summary TEXT;
    ALTER TABLE jobs ADD COLUMN review_recorded INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE findings (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs (id),
        kind TEXT NOT NULL, position INTEGER NOT NULL,
        severity TEXT, impact INTEGER, likelihood TEXT, occurs_when TEXT, category TEXT,
        file TEXT NOT NULL DEFAULT '', line INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '', suggestion TEXT NOT NULL DEFAULT '',
        reviewer_severity TEXT, readability TEXT, loc_delta INTEGER, edits TEXT NOT NULL DEFAULT '[]',
        rated_impact INTEGER, disposition TEXT, fixed_in TEXT, note TEXT,
        triaged_by TEXT, triaged_at INTEGER,
        UNIQUE (job_id, kind, position));
    CREATE INDEX findings_job ON findings (job_id);
    """,
    # 4: follow-up reviews, which check the previous round's findings and look for regressions.
    """
    ALTER TABLE jobs ADD COLUMN review_kind TEXT NOT NULL DEFAULT 'full';
    ALTER TABLE jobs ADD COLUMN followup_of TEXT;
    CREATE TABLE resolutions (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs (id),
        finding_id INTEGER NOT NULL REFERENCES findings (id), status TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '', UNIQUE (job_id, finding_id));
    CREATE INDEX resolutions_finding ON resolutions (finding_id);
    """,
    # 5: the reviews of a commit are posted as one, with the items several reviewers reported merged;
    # posted_with is the job whose post carries this job's review. And a reviewer may call a PR trivial.
    """
    ALTER TABLE jobs ADD COLUMN posted_with INTEGER;
    ALTER TABLE jobs ADD COLUMN trivial INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE findings ADD COLUMN duplicate_of INTEGER REFERENCES findings (id);
    """,
    # 6: feedback on the posted reviews, collected from a PR's comments once it closes (see feedback.py).
    # review_id is the GitHub id of the review answered, as in jobs.review_id. Closed PRs from before this
    # migration have feedback_collected_at NULL too, so their feedback is collected as well.
    """
    ALTER TABLE prs ADD COLUMN merged INTEGER;
    ALTER TABLE prs ADD COLUMN feedback_collected_at INTEGER;
    ALTER TABLE prs ADD COLUMN feedback_attempts INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE feedback (
        id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL, source TEXT NOT NULL,
        github_id INTEGER NOT NULL, url TEXT NOT NULL DEFAULT '', author TEXT NOT NULL,
        created_at INTEGER NOT NULL, body TEXT NOT NULL, review_id INTEGER, context TEXT,
        collected_at INTEGER NOT NULL,
        UNIQUE (repo, source, github_id));
    CREATE INDEX feedback_pr ON feedback (repo, number);
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
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn):
    # Fast path without the write lock: every connection checks, and most find nothing to do.
    if conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
        return
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


FINDING_KINDS = ("finding", "regression", "test_gap", "question", "nit")  # regressions: from follow-ups
DISPOSITIONS = ("fixed", "declined", "false-positive", "duplicate", "deferred")


def record_review(conn, job_id, review, *, effort=None, prompt_version=None, followup_of=None):
    """Keep a validated review's contents for analysis, replacing any earlier record for the job. A
    follow-up (followup_of: the previous head) records its regressions and each previous finding's status."""
    conn.execute("UPDATE findings SET duplicate_of=NULL WHERE duplicate_of IN (SELECT id FROM findings WHERE job_id=?)",
                 (job_id,))
    conn.execute("DELETE FROM resolutions WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM findings WHERE job_id=?", (job_id,))
    kind = "regression" if followup_of else "finding"
    rows = [(job_id, kind, i, f["severity"], f.get("impact"), f.get("likelihood") or None,
             f.get("trigger") or None, f["category"], f["file"], f["line"], f["title"], f["detail"], f["suggestion"],
             f.get("reviewer_severity"), f.get("readability"), f.get("loc_delta"), _edits_json(f))
            for i, f in enumerate(review["findings"])]
    for kind, texts in (("test_gap", review["test_gaps"]), ("question", review["questions"])):
        rows += [(job_id, kind, i, None, None, None, None, None, "", 0, text, "", "", None, None, None, "[]")
                 for i, text in enumerate(texts)]
    rows += [(job_id, "nit", i, None, None, None, None, None, n["file"], n["line"], n["text"], "", "",
              None, None, None, _edits_json(n))
             for i, n in enumerate(review["nits"])]
    conn.executemany(
        "INSERT INTO findings (job_id, kind, position, severity, impact, likelihood, occurs_when, category, file,"
        " line, title, detail, suggestion, reviewer_severity, readability, loc_delta, edits)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO resolutions (job_id, finding_id, status, detail) VALUES (?, ?, ?, ?)",
                     [(job_id, r["finding"], r["status"], r["detail"]) for r in review.get("resolutions") or []])
    known = {"effort": effort, "prompt_version": prompt_version, "trivial": int(bool(review.get("trivial")))}
    if followup_of:
        known.update(review_kind="followup", followup_of=followup_of)
    update_job(conn, job_id, summary=review["summary"], review_recorded=1,
               **{key: value for key, value in known.items() if value is not None})


def _edits_json(item):
    """A finding's or nit's proposed edits, as recorded: where, what, whether it was a one-click suggestion,
    and how it was placed (see suggestions.check_edit)."""
    return json.dumps([{key: edit.get(key) for key in ("file", "start_line", "end_line", "replacement", "inline",
                                                        "current", "anchor", "stated")}
                       for edit in item.get("edits") or []])


def triage(conn, finding_id, *, now, rated_impact=None, disposition=None, fixed_in=None, note=None, by=None):
    """Record what was done about a finding. Fields left as None keep their current value."""
    if disposition is not None and disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition {disposition!r}")
    if rated_impact is not None and not 0 <= rated_impact <= 10:
        raise ValueError("impact must be between 0 and 10")
    fields = {key: value for key, value in (("rated_impact", rated_impact), ("disposition", disposition),
                                            ("fixed_in", fixed_in), ("note", note), ("triaged_by", by))
              if value is not None}
    columns = ", ".join(f"{name}=?" for name in fields)
    updated = conn.execute(f"UPDATE findings SET {columns}, triaged_at=? WHERE id=?",
                           (*fields.values(), now, finding_id)).rowcount
    if not updated:
        raise KeyError(finding_id)


def is_trial(job):
    """A no-post `review` run in the foreground: it neither counts as a review nor blocks one."""
    return job["source"] == FOREGROUND and not job["post"]


TRIAL_SQL = "(source=? AND post=0)"  # is_trial() as a WHERE condition; bind FOREGROUND


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
