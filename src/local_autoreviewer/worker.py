# SPDX-License-Identifier: GPL-3.0-or-later
"""Long-running worker: claims queued jobs, runs at most one per agent, honours cancels."""

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, paths, pipeline
from .github import GitHubError

log = logging.getLogger(__name__)
TICK = 2.0
STATUS_EVERY = 10.0
POST_RECOVERY_RETRY = 300  # seconds before retrying an interrupted post whose GitHub lookup failed


def start_of_local_day(now):
    local = datetime.fromtimestamp(now).astimezone()
    return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


class Worker:
    def __init__(self, make_services, watcher, tick=TICK):
        self.make_services = make_services  # cfg -> pipeline.Services
        self.watcher = watcher
        self.tick = tick
        self.stop = threading.Event()
        self.threads = {}  # job_id -> (thread, CancelToken, agent)
        self.version = paths.read_version()
        self.cap_logged = set()
        self.last_status = 0.0

    def install_signals(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())

    # Startup -------------------------------------------------------------------

    def recover(self, svc):
        conn = svc.connect()
        try:
            # Foreground jobs belong to their `review` process; see release_orphaned_foreground().
            for job in conn.execute("SELECT * FROM jobs WHERE status='running' AND source!=?",
                                    (db.FOREGROUND,)).fetchall():
                if job["scope_unit"]:
                    svc.runner.cleanup(job["scope_unit"])
                with db.transaction(conn):
                    db.update_job(conn, job["id"], status="queued", reason="requeued after worker restart",
                                  cancel_requested=0, not_before=svc.now())
                log.info("job %s requeued after restart", job["id"])
        finally:
            conn.close()
        self.run_guarded(svc, self.release_orphaned_foreground)
        # Interrupted posts are finished from the main loop by recover_posts().

    @staticmethod
    def run_guarded(svc, *steps):
        for step in steps:
            try:
                step(svc)
            except Exception:  # housekeeping must never take the worker down
                log.exception("%s failed", getattr(step, "__name__", step))

    def release_orphaned_foreground(self, svc):
        """Clean up after `review` commands that died without finishing their jobs: a queued or
        running job whose lock nobody holds is cancelled and its scope stopped. One caught
        mid-post is finished by recover_posts()."""
        conn = svc.connect()
        try:
            ids = [row["id"] for row in conn.execute(
                "SELECT id FROM jobs WHERE source=? AND status IN ('queued','running')", (db.FOREGROUND,))]
            for job_id in ids:
                with pipeline.orphaned_foreground(svc.state, job_id) as orphaned:
                    if not orphaned:
                        continue
                    # abandon() re-reads the job under the lock: the owner may have just finished it.
                    pipeline.abandon(svc, [job_id], "foreground review exited before finishing")
        finally:
            conn.close()

    def recover_posts(self, svc):
        """Finish posts interrupted by a crash (the worker's own, or a dead `review`'s). Each runs
        in a thread like a job, so a slow GitHub can't stall the loop, and occupies its agent's slot."""
        conn = svc.connect()
        try:
            rows = conn.execute("SELECT id, source, agent FROM jobs WHERE status='posting' AND not_before<=?",
                                (svc.now(),)).fetchall()
        finally:
            conn.close()
        for row in rows:
            if row["id"] in self.threads:
                continue  # one of our job threads is posting it right now
            if row["source"] == db.FOREGROUND:
                with pipeline.orphaned_foreground(svc.state, row["id"]) as orphaned:
                    if not orphaned:
                        continue  # its `review` is still posting it
            thread = threading.Thread(target=self._recover_post, args=(svc, row["id"]), name=f"recover-{row['id']}")
            self.threads[row["id"]] = (thread, pipeline.CancelToken(), row["agent"])
            thread.start()

    def _recover_post(self, svc, job_id):
        conn = svc.connect()
        try:
            job = db.get_job(conn, job_id)
            if job["status"] != "posting":
                return
            self.recover_posting(svc, conn, job)
            if job["source"] == db.FOREGROUND:  # requeued if there was nothing to post: no owner to run it
                pipeline.abandon(svc, [job_id], "foreground review exited before finishing")
        except Exception:
            log.exception("job %s: post recovery failed; retrying in %d min", job_id, POST_RECOVERY_RETRY // 60)
            try:
                with db.transaction(conn):
                    db.update_job(conn, job_id, not_before=svc.now() + POST_RECOVERY_RETRY)
            except Exception:
                log.exception("job %s: cannot schedule the retry", job_id)
        finally:
            conn.close()

    def recover_posting(self, svc, conn, job):
        """A crash between posting and recording: look for our marker before posting again."""
        try:
            existing = svc.github.find_marked_review(job["repo"], job["number"], job["id"], svc.token_owner)
            if existing:
                with db.transaction(conn):
                    db.update_job(conn, job["id"], status="posted", reason="review found after restart",
                                  review_id=existing.get("id"), review_url=existing.get("html_url"),
                                  posted_at=svc.now(), finished_at=svc.now())
                return
            if Path(pipeline.job_paths(svc.state, job["id"]).out, "review.md").is_file():
                try:
                    pipeline.post(conn, svc, job)
                except pipeline.Outcome as outcome:
                    pipeline._finish(conn, svc, job["id"], outcome.status, outcome.reason)
                return
        except GitHubError as exc:
            log.warning("job %s: cannot verify posting yet (%s); retrying in %d min",
                        job["id"], exc, POST_RECOVERY_RETRY // 60)
            with db.transaction(conn):
                db.update_job(conn, job["id"], not_before=svc.now() + POST_RECOVERY_RETRY)
            return
        with db.transaction(conn):
            db.update_job(conn, job["id"], status="queued", reason="requeued: review output missing")

    # Main loop ------------------------------------------------------------------

    def run(self):
        svc = self.make_services(self.watcher.get())
        self.recover(svc)
        log.info("worker started (version %s)", self.version)
        while not self.stop.is_set():
            svc = self.make_services(self.watcher.get())
            self.reap()
            self.sync_cancels(svc)
            self.run_guarded(svc, self.release_orphaned_foreground, self.recover_posts)
            if self.upgrade_pending():
                if not self.threads:
                    log.info("package version changed to %s; exiting to restart on new code", paths.read_version())
                    break
            elif not svc.config.service.paused:
                self.claim(svc)
            if time.monotonic() - self.last_status > STATUS_EVERY:
                self.write_status(svc)
            self.stop.wait(self.tick)
        self.shutdown()
        self.write_status(self.make_services(self.watcher.get()))
        return 0

    def upgrade_pending(self):
        return self.version != "dev" and paths.read_version() != self.version

    def reap(self):
        for job_id, (thread, _token, _agent) in list(self.threads.items()):
            if not thread.is_alive():
                thread.join()
                del self.threads[job_id]

    def sync_cancels(self, svc):
        if not self.threads:
            return
        conn = svc.connect()
        try:
            ids = list(self.threads)
            rows = conn.execute(
                f"SELECT id, reason FROM jobs WHERE cancel_requested=1 AND id IN ({','.join('?' * len(ids))})",
                ids).fetchall()
        finally:
            conn.close()
        for row in rows:
            self.threads[row["id"]][1].cancel(row["reason"] or "cancelled")

    def claim(self, svc):
        busy = {agent for (_t, _c, agent) in self.threads.values()}
        free = [agent for agent in svc.config.agents if agent not in busy]
        if not free:
            return
        now = svc.now()
        day = start_of_local_day(now)
        conn = svc.connect()
        try:
            for agent in free:
                with db.transaction(conn):
                    started = db.started_since(conn, agent, day)
                    if started >= svc.config.service.max_reviews_per_day:
                        if (agent, day) not in self.cap_logged:
                            log.warning("%s: daily cap of %d reviews reached; waiting until tomorrow",
                                        agent, svc.config.service.max_reviews_per_day)
                            self.cap_logged.add((agent, day))
                        continue
                    job = conn.execute(
                        "SELECT * FROM jobs WHERE status='queued' AND agent=? AND not_before<=? AND source!=? "
                        "ORDER BY not_before, id LIMIT 1", (agent, now, db.FOREGROUND)).fetchone()
                    if job is None:
                        continue
                    db.update_job(conn, job["id"], status="running", started_at=now, cancel_requested=0)
                token = pipeline.CancelToken()
                thread = threading.Thread(target=self._run_job, args=(svc, job["id"], token),
                                          name=f"job-{job['id']}")
                self.threads[job["id"]] = (thread, token, agent)
                thread.start()
        finally:
            conn.close()

    def _run_job(self, svc, job_id, token):
        try:
            pipeline.run_job(svc, job_id, token)
        except Exception:
            log.exception("job %s: unhandled error", job_id)

    def shutdown(self):
        if not self.threads:
            return
        log.info("stopping %d running job(s); they will be requeued", len(self.threads))
        for _thread, token, _agent in self.threads.values():
            token.cancel("shutdown")
        for job_id, (thread, _token, _agent) in list(self.threads.items()):
            thread.join(timeout=90)
            if thread.is_alive():
                log.warning("job %s did not stop in time", job_id)

    # Status ---------------------------------------------------------------------

    def write_status(self, svc):
        self.last_status = time.monotonic()
        try:
            write_status(svc, version=self.version)
        except Exception as exc:  # status is informational; never take the worker down
            log.warning("could not write status.json: %s", exc)


def write_status(svc, *, version):
    conn = svc.connect()
    try:
        counts = {row["status"]: row["n"] for row in
                  conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        running = [dict(row) for row in conn.execute(
            "SELECT id, repo, number, agent, head_sha, started_at FROM jobs WHERE status IN ('running','posting')")]
        recent = [dict(row) for row in conn.execute(
            "SELECT id, repo, number, agent, head_sha, status, reason, review_url, finished_at FROM jobs "
            "ORDER BY id DESC LIMIT 20")]
        last_poll = db.kv_get(conn, "last_poll")
    finally:
        conn.close()
    for row in running + recent:
        for key in ("started_at", "finished_at"):
            if key in row:
                row[key] = iso(row[key])
    status = {
        "updated": iso(svc.now()), "version": version, "paused": svc.config.service.paused,
        "last_poll": iso(int(last_poll)) if last_poll else None,
        "counts": counts, "running": running, "recent": recent,
    }
    target = svc.state / "status.json"
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(status, indent=2) + "\n")
    os.replace(temp, target)
