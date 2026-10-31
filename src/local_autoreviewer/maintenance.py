# SPDX-License-Identifier: GPL-3.0-or-later
"""Daily housekeeping: expire job checkouts, record saved reviews, prune mirrors. Touches only the state dir."""

import json
import logging
import shutil
from pathlib import Path

from . import db, pipeline, review
from .pipeline import job_paths

log = logging.getLogger(__name__)
INTERVAL = 86400


def due(conn, now):
    last = db.kv_get(conn, "last_housekeeping")
    return last is None or now - int(last) >= INTERVAL


def housekeeping(svc):
    conn = svc.connect()
    now = svc.now()
    try:
        cutoff = now - svc.config.service.retain_jobs
        rows = conn.execute(
            f"SELECT id, repo FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ? "
            f"AND status NOT IN ({','.join('?' * len(db.ACTIVE))})", (cutoff, *db.ACTIVE)).fetchall()
        removed = 0
        for row in rows:
            # The checkout and context go; out/ (the agent's output and the review) is kept for analysis.
            paths = job_paths(svc.state, row["id"])
            if not (Path(paths.worktree).exists() or Path(paths.context).exists()):
                continue
            mirror = svc.git.mirror_path(svc.state, row["repo"])
            if Path(paths.worktree).exists() and mirror.exists():
                svc.git.remove_worktree(mirror, paths.worktree)
            shutil.rmtree(paths.worktree, ignore_errors=True)
            shutil.rmtree(paths.context, ignore_errors=True)
            removed += 1
        _remove_stale_locks(conn, svc)
        recorded = record_saved_reviews(conn, svc)
        mirrors = 0
        for repo in {r["repo"] for r in conn.execute("SELECT DISTINCT repo FROM prs")}:
            mirror = svc.git.mirror_path(svc.state, repo)
            if not mirror.exists():
                continue
            closed = {r["number"] for r in conn.execute(
                "SELECT number FROM prs WHERE repo=? AND state='closed'", (repo,))}
            active = {r["number"] for r in conn.execute(
                f"SELECT DISTINCT number FROM jobs WHERE repo=? AND status IN ({','.join('?' * len(db.ACTIVE))})",
                (repo, *db.ACTIVE))}
            for number in (svc.git.pr_numbers_with_refs(mirror) & closed) - active:
                svc.git.delete_pr_refs(mirror, number)
            svc.git.run(["-C", str(mirror), "worktree", "prune"], check=False)
            svc.git.run(["-C", str(mirror), "gc", "--auto", "--quiet"], check=False)
            mirrors += 1
        with db.transaction(conn):
            db.kv_set(conn, "last_housekeeping", now)
        log.info("housekeeping: removed %d expired job checkout(s), recorded %d saved review(s), "
                 "pruned %d mirror(s)", removed, recorded, mirrors)
    finally:
        conn.close()


def _remove_stale_locks(conn, svc):
    """Delete lock files left by a `review` killed after its jobs finished. Only finished jobs': an
    active job's lock is the worker's business, and a lock with no job row may belong to a `review`
    that is creating that job right now (it takes the lock before the row commits)."""
    for path in (Path(svc.state) / "locks").glob("job-*.lock"):
        try:
            job_id = int(path.stem.removeprefix("job-"))
        except ValueError:
            continue
        job = db.get_job(conn, job_id)
        if job is not None and job["status"] not in db.ACTIVE:
            pipeline.remove_lock_if_free(svc.state, job_id)


def record_saved_reviews(conn, svc):
    """Record reviews that are on disk but not in the database: ones from before review data was
    kept, or whose recording failed. Blocked reviews (a possible credential) are never recorded."""
    rows = conn.execute(
        f"SELECT id FROM jobs WHERE review_recorded=0 AND status NOT IN ({','.join('?' * len(db.ACTIVE))}, 'blocked')",
        db.ACTIVE).fetchall()
    recorded = 0
    for row in rows:
        path = Path(job_paths(svc.state, row["id"]).out, "review.json")
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
            structured = review.validate(data)
        except (OSError, ValueError) as exc:  # ReviewFormatError and JSONDecodeError are ValueErrors
            log.warning("job %s: cannot record saved review %s: %s", row["id"], path, exc)
            continue
        followup_of = data.get("followup_of") if isinstance(data.get("followup_of"), str) else None
        if followup_of:  # a saved follow-up: keep its statuses for findings that still exist
            known = {r[0] for r in conn.execute("SELECT id FROM findings")}
            structured["resolutions"] = [
                r for r in data.get("resolutions") or [] if isinstance(r, dict)
                and r.get("status") in (*review.FOLLOWUP_STATUSES, "unchecked") and r.get("finding") in known]
        with db.transaction(conn):
            db.record_review(conn, row["id"], structured, followup_of=followup_of)
        recorded += 1
    return recorded
