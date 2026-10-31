# SPDX-License-Identifier: GPL-3.0-or-later
"""Daily housekeeping: expire job directories and prune mirrors. Touches only the state dir."""

import logging
import shutil
from pathlib import Path

from . import db
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
            paths = job_paths(svc.state, row["id"])
            if not Path(paths.root).exists():
                continue
            mirror = svc.git.mirror_path(svc.state, row["repo"])
            if Path(paths.worktree).exists() and mirror.exists():
                svc.git.remove_worktree(mirror, paths.worktree)
            shutil.rmtree(paths.root, ignore_errors=True)
            removed += 1
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
        log.info("housekeeping: removed %d expired job dir(s), pruned %d mirror(s)", removed, mirrors)
    finally:
        conn.close()
