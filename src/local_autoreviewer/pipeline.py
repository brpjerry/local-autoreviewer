# SPDX-License-Identifier: GPL-3.0-or-later
"""Process one review job end to end: check PR, prepare workspace, run agent, post."""

import fcntl
import json
import logging
import math
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import db, leakscan, review
from .agents import make_agent
from .agents.base import JobPaths
from .github import GitHubError
from .gitops import GitError, ShaMismatch

log = logging.getLogger(__name__)

RATE_LIMIT_BACKOFF = 30 * 60
SHA_LAG_RETRY = 60
SHA_LAG_MAX_ATTEMPTS = 10
FOREGROUND_MAX_WAIT = 5 * 60  # `review` waits out retries up to this long; usage limits are longer
FOREGROUND_CANCEL_POLL = 2.0  # seconds between a foreground job's checks for cancel requests
POST_RECOVERY_RETRY = 5 * 60  # seconds before retrying a post that failed or was interrupted


@dataclass
class Services:
    config: object
    state: Path
    github: object
    git: object
    runner: object
    token_owner: str
    secrets: object = list  # callable returning credentials to block in posted text
    clock: object = time.time

    def connect(self):
        return db.connect(self.state / "state.sqlite3")

    def now(self):
        return int(self.clock())


class CancelToken:
    """Truthy once a cancel is requested; `reason` says why (superseded, closed, shutdown)."""

    def __init__(self):
        self.reason = None

    def cancel(self, reason):
        if self.reason is None:
            self.reason = reason

    def __call__(self):
        return self.reason is not None


def job_paths(state, job_id):
    root = Path(state) / "jobs" / str(job_id)
    return JobPaths(root=str(root), worktree=str(root / "repo"), context=str(root / "context"),
                    out=str(root / "out"))


def scope_unit(job_id):
    return f"local-autoreviewer-job-{job_id}"


@contextmanager
def repo_lock(mirror):
    lock_path = Path(str(mirror) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class Outcome(Exception):
    """Raised inside the pipeline to end the job with a given status."""

    def __init__(self, status, reason, **fields):
        super().__init__(reason)
        self.status, self.reason, self.fields = status, reason, fields


def _finish(conn, svc, job_id, status, reason, **fields):
    final = status not in db.ACTIVE
    with db.transaction(conn):
        db.update_job(conn, job_id, status=status, reason=reason[:1000] if reason else reason,
                      **({"finished_at": svc.now()} if final else {}), **fields)
    level = logging.WARNING if status in ("failed", "blocked") else logging.INFO
    log.log(level, "job %s -> %s: %s", job_id, status, reason)
    return status


def _retry(conn, svc, job, reason, *, delay=None, count=True):
    attempts = job["attempts"] + (1 if count else 0)
    if count and attempts >= svc.config.service.max_attempts:
        return _finish(conn, svc, job["id"], "failed", f"giving up after {attempts} attempts: {reason}",
                       attempts=attempts, error=reason[:2000])
    delay = delay if delay is not None else 120 * (2 ** max(0, attempts - 1))
    return _finish(conn, svc, job["id"], "queued", f"retry: {reason}", attempts=attempts,
                   not_before=svc.now() + delay, error=reason[:2000], cancel_requested=0)


def _check_pr(svc, job):
    pr = svc.github.pr_view(job["repo"], job["number"])
    if pr["state"] != "OPEN":
        raise Outcome("cancelled", f"PR is {pr['state'].lower()}")
    if pr["headRefOid"] != job["head_sha"]:
        raise Outcome("superseded", f"head moved to {pr['headRefOid'][:12]}")
    if pr["baseRefName"] != job["base_ref"]:
        raise Outcome("superseded", f"retargeted to {pr['baseRefName']}")
    if pr["isDraft"] and job["source"] not in ("manual", db.FOREGROUND):
        raise Outcome("cancelled", "PR is a draft")
    return pr


def run_job(svc, job_id, cancel):
    """Run one claimed job (status already 'running'). Returns the resulting status."""
    conn = svc.connect()
    try:
        job = db.get_job(conn, job_id)
        try:
            return _run(conn, svc, job, cancel)
        except Outcome as outcome:
            return _finish(conn, svc, job_id, outcome.status, outcome.reason, **outcome.fields)
        except ShaMismatch as exc:
            if job["attempts"] + 1 >= SHA_LAG_MAX_ATTEMPTS:
                return _finish(conn, svc, job_id, "failed", str(exc))
            return _finish(conn, svc, job_id, "queued", f"waiting for GitHub: {exc}",
                           attempts=job["attempts"] + 1, not_before=svc.now() + SHA_LAG_RETRY)
        except (GitHubError, GitError, OSError, review.ReviewFormatError) as exc:
            return _retry(conn, svc, job, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # unexpected: record and retry rather than wedge the queue
            log.exception("job %s crashed", job_id)
            return _retry(conn, svc, job, f"internal error: {type(exc).__name__}: {exc}")
    finally:
        conn.close()


# Foreground jobs ---------------------------------------------------------------
# `review` runs its jobs itself. They carry source=db.FOREGROUND, which the worker never claims or
# recovers, and the command holds an flock per job for as long as it owns it. The worker cancels
# (or, mid-post, recovers) a foreground job only once that lock is free, i.e. its owner is gone.

def foreground_lock_path(state, job_id):
    return Path(state) / "locks" / f"job-{job_id}.lock"


def hold_foreground_lock(state, job_id):
    """Lock job_id for this process. Take it before the job's row commits, so no reader can see
    an unlocked foreground job. The lock lasts until the returned file is closed."""
    path = foreground_lock_path(state, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def release_foreground_lock(state, job_id, handle):
    foreground_lock_path(state, job_id).unlink(missing_ok=True)
    handle.close()


@contextmanager
def orphaned_foreground(state, job_id):
    """Yield True, holding the job's lock, if no live process owns the foreground job."""
    path = foreground_lock_path(state, job_id)
    try:
        handle = open(path, "a")
    except FileNotFoundError:  # no locks directory at all: nobody holds a lock
        yield True
        return
    except OSError as exc:  # can't tell, so assume the owner is alive
        log.warning("job %s: cannot check foreground lock %s: %s", job_id, path, exc)
        yield False
        return
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            path.unlink(missing_ok=True)


def _check_cancel(svc, job_id, token):
    """Cancel token if the job has a cancel request (a newer push, the PR closing). True if so."""
    try:
        conn = svc.connect()
        try:
            row = conn.execute("SELECT cancel_requested, reason FROM jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, db.SchemaTooNew) as exc:  # e.g. busy: the next check tries again
        log.debug("job %s: cancel check failed: %s", job_id, exc)
        return False
    if row is not None and row["cancel_requested"]:
        token.cancel(row["reason"] or "cancelled")
        return True
    return False


def _watch_cancel(svc, job_id, token, done):
    """Give a foreground run the worker's cancel semantics."""
    while not done.wait(FOREGROUND_CANCEL_POLL):
        if _check_cancel(svc, job_id, token):
            return


def _wait_while(svc, conn, job_id, status, wait, sleep):
    """Sleep up to `wait` seconds in short steps while the job keeps `status`; return early with its
    new status if a push or close ends it meanwhile."""
    deadline = svc.now() + wait
    while (remaining := deadline - svc.now()) > 0:
        sleep(min(FOREGROUND_CANCEL_POLL, remaining))
        current = db.get_job(conn, job_id)["status"]
        if current != status:
            return current
    return status


def _finish_post(svc, job_id, sleep, notify):
    """A post that failed under `review --post`: retry it in place while the retry fits in the wait,
    as the worker would. Returns the job's status; 'posting' leaves it to the worker."""
    conn = svc.connect()
    try:
        while True:
            job = db.get_job(conn, job_id)
            wait = max(0, job["not_before"] - svc.now())
            if job["status"] != "posting" or wait > FOREGROUND_MAX_WAIT:
                return job["status"]
            if wait:
                notify(f"job {job_id} [{job['agent']}]: {job['reason']}; trying again in {wait}s")
            if _wait_while(svc, conn, job_id, "posting", wait, sleep) != "posting":
                return db.get_job(conn, job_id)["status"]
            recover_post(conn, svc, db.get_job(conn, job_id))
    finally:
        conn.close()


def run_foreground(svc, job_id, *, sleep=time.sleep, notify=lambda message: None):
    """Run a foreground job (status 'running', lock held) to a final state: wait out short retries
    in place, and cancel the job rather than wait out a long deferral such as a usage limit."""
    while True:
        token, done = CancelToken(), threading.Event()
        _check_cancel(svc, job_id, token)  # a cancel requested while it waited its turn
        watcher = threading.Thread(target=_watch_cancel, args=(svc, job_id, token, done), daemon=True,
                                   name=f"cancel-watch-{job_id}")
        watcher.start()
        try:
            status = run_job(svc, job_id, token)
        finally:
            done.set()
            watcher.join()
        if status == "posting":
            return _finish_post(svc, job_id, sleep, notify)
        if status != "queued":
            return status
        conn = svc.connect()
        try:
            job = db.get_job(conn, job_id)
            wait = max(0, job["not_before"] - svc.now())
            if wait > FOREGROUND_MAX_WAIT:
                reason = (f"not waiting {math.ceil(wait / 60)} min in the foreground (use `enqueue` to let the "
                          f"worker retry); {job['reason']}")
                with db.transaction(conn):  # only if a poll hasn't superseded it meanwhile
                    cancelled = conn.execute(
                        "UPDATE jobs SET status='cancelled', reason=?, finished_at=? WHERE id=? AND status='queued'",
                        (reason[:1000], svc.now(), job_id)).rowcount
                if cancelled:
                    log.info("job %s -> cancelled: %s", job_id, reason)
                return db.get_job(conn, job_id)["status"]
            if wait:
                notify(f"job {job_id} [{job['agent']}]: {job['reason']}; trying again in {wait}s")
            status = _wait_while(svc, conn, job_id, "queued", wait, sleep)
            if status != "queued":
                return status  # a push or close ended it during the wait
            with db.transaction(conn):
                # A push or close during the wait may have ended the job; never revive it.
                # Like a worker claim, drop a stale cancel request (one left by a requeue that kept it).
                resumed = conn.execute("UPDATE jobs SET status='running', started_at=?, cancel_requested=0"
                                       " WHERE id=? AND status='queued'", (svc.now(), job_id)).rowcount
            if not resumed:
                return db.get_job(conn, job_id)["status"]
        finally:
            conn.close()


def abandon(svc, job_ids, reason):
    """Cancel foreground jobs that are still queued or running: this process's own after Ctrl-C or
    an error, or a dead process's (from the worker). It re-reads each job, so one that has just
    finished is left alone. A job caught mid-post is left to the worker's marker-checked recovery."""
    if not job_ids:
        return
    conn = svc.connect()
    try:
        for job_id in job_ids:
            job = db.get_job(conn, job_id)
            if job is None or job["source"] != db.FOREGROUND or job["status"] not in ("queued", "running"):
                continue
            if job["scope_unit"]:
                svc.runner.cleanup(job["scope_unit"])
            _finish(conn, svc, job_id, "cancelled", reason)
    finally:
        conn.close()


def _cancelled(conn, svc, job, cancel):
    reason = cancel.reason or "cancelled"
    if reason == "shutdown":
        return _finish(conn, svc, job["id"], "queued", "requeued after worker shutdown", cancel_requested=0)
    current = db.get_job(conn, job["id"])
    status = "superseded" if (current["reason"] or "").startswith("superseded") else "cancelled"
    return _finish(conn, svc, job["id"], status, current["reason"] or reason)


def _run(conn, svc, job, cancel):
    cfg = svc.config
    repo_cfg = cfg.repo(job["repo"])
    if repo_cfg is None:
        raise Outcome("cancelled", "repo is no longer configured")
    agent = make_agent(cfg.agents[job["agent"]], svc.state)
    paths = job_paths(svc.state, job["id"])
    for directory in (paths.root, paths.out):
        Path(directory).mkdir(parents=True, exist_ok=True)

    pr = _check_pr(svc, job)
    # Both agents' jobs for a PR start together; the lock also covers creating the mirror.
    with repo_lock(svc.git.mirror_path(svc.state, job["repo"])):
        mirror = svc.git.ensure_mirror(svc.state, job["repo"])
        base_sha = svc.git.fetch_pr(mirror, job["number"], pr["baseRefName"], job["head_sha"])
        removed = svc.git.add_worktree(mirror, job["head_sha"], paths.worktree)
        if Path(paths.context).exists():
            shutil.rmtree(paths.context)
        ctx = svc.git.write_context(
            mirror, job["head_sha"], base_sha, paths.context, exclude=repo_cfg.diff_exclude,
            project_docs=repo_cfg.project_docs,
            pr={"repo": job["repo"], "number": job["number"], "title": pr.get("title", ""),
                "author": (pr.get("author") or {}).get("login", ""), "url": pr.get("url", ""),
                "body": pr.get("body", ""), "base_ref": pr["baseRefName"]})
    if removed:
        log.info("job %s: removed agent files from checkout: %s", job["id"], ", ".join(removed))
    with db.transaction(conn):
        db.update_job(conn, job["id"], base_sha=base_sha, workdir=paths.root)

    if ctx.changed_files > repo_cfg.max_changed_files or ctx.diff_bytes > repo_cfg.max_diff_bytes:
        reason = (f"PR too large for automated review: {ctx.changed_files} files, {ctx.diff_bytes} diff bytes "
                  f"(limits {repo_cfg.max_changed_files} files, {repo_cfg.max_diff_bytes} bytes)")
        if job["post"]:
            body = review.render_skipped(reason=reason, sha=job["head_sha"], job_id=job["id"], agent=job["agent"])
            svc.github.post_review(job["repo"], job["number"], job["head_sha"], body)
        raise Outcome("skipped", reason)

    if cancel():
        return _cancelled(conn, svc, job, cancel)

    session_id = agent.new_session_id()
    unit = scope_unit(job["id"])
    with db.transaction(conn):
        db.update_job(conn, job["id"], session_id=session_id, scope_unit=unit)
    prompt = review.build_prompt(
        repo=job["repo"], number=job["number"], title=pr.get("title", ""),
        author=(pr.get("author") or {}).get("login", ""), base_ref=pr["baseRefName"], head_sha=job["head_sha"],
        worktree=paths.worktree, context=paths.context, agent_note=agent.agent_note(),
        extra_instructions=repo_cfg.extra_instructions,
        project_docs=repo_cfg.project_docs and Path(paths.context, "project-docs").is_dir())
    invocation = agent.invocation(unit=unit, paths=paths, prompt=prompt, session_id=session_id)
    log.info("job %s: running %s on %s#%s @%s", job["id"], job["agent"], job["repo"], job["number"],
             job["head_sha"][:12])
    run = svc.runner.run(invocation, timeout=cfg.service.review_timeout, should_cancel=cancel)
    if run.cancelled:
        return _cancelled(conn, svc, job, cancel)
    result = agent.parse(run, paths, session_id)
    with db.transaction(conn):
        db.update_job(conn, job["id"], session_id=result.session_id or session_id,
                      usage_json=json.dumps({**result.usage, "seconds": round(run.duration, 1)}))
    if run.timed_out:
        return _retry(conn, svc, job, f"timed out after {cfg.service.review_timeout}s")
    if result.error_kind == "rate_limit":
        return _retry(conn, svc, job, f"usage limit: {result.error}", delay=RATE_LIMIT_BACKOFF, count=False)
    if result.error_kind:
        return _retry(conn, svc, job, result.error or "agent failed")

    structured = review.validate(result.review)
    body = review.render(structured, repo=job["repo"], number=job["number"], sha=job["head_sha"],
                         base_ref=pr["baseRefName"], agent=job["agent"], model=job["model"], job_id=job["id"])
    Path(paths.out, "review.json").write_text(json.dumps(structured, indent=2))
    Path(paths.out, "review.md").write_text(body)
    hits = leakscan.scan(body, svc.secrets())
    if hits:
        raise Outcome("blocked", f"possible credential in review ({', '.join(hits)}); not posted, see "
                                 f"{paths.out}/review.md")
    if not job["post"]:
        raise Outcome("skipped", f"posting disabled; review saved to {paths.out}/review.md")
    return post(conn, svc, job)


def _rejected(exc):
    """True for a definite refusal (HTTP 4xx other than a timeout or rate limit): retrying won't help."""
    match = re.search(r"\bHTTP (4\d\d)\b", str(exc))
    return bool(match) and match.group(1) not in ("408", "429")


def post_failed(conn, svc, job, exc):
    """A post that failed or whose result is unknown (a timeout after GitHub accepted it, say). It stays
    `posting` for marker-checked recovery, without running the agent again, until max_attempts; a
    definite refusal fails it at once. The review stays in out/review.md either way."""
    attempts = db.get_job(conn, job["id"])["attempts"] + 1
    saved = f"review saved to {job_paths(svc.state, job['id']).out}/review.md"
    if _rejected(exc) or attempts >= svc.config.service.max_attempts:
        return _finish(conn, svc, job["id"], "failed", f"post failed: {exc}; {saved}"[:1000], attempts=attempts,
                       error=str(exc)[:2000])
    log.warning("job %s: post failed (%s); retrying in %d min", job["id"], exc, POST_RECOVERY_RETRY // 60)
    with db.transaction(conn):
        db.update_job(conn, job["id"], attempts=attempts, reason=f"post failed: {exc}"[:1000],
                      not_before=svc.now() + POST_RECOVERY_RETRY)
    return "posting"


def recover_post(conn, svc, job):
    """Finish a job left `posting` by a failed or interrupted post: confirm it if the review reached
    GitHub, otherwise post the saved review, unless a push, retarget or close has ended the job since.
    Returns the job's status afterwards."""
    try:
        existing = svc.github.find_marked_review(job["repo"], job["number"], job["id"], svc.token_owner)
    except GitHubError as exc:
        return post_failed(conn, svc, job, exc)
    if existing:
        return _finish(conn, svc, job["id"], "posted", "review found after an interrupted post",
                       posted_at=svc.now(), review_id=existing.get("id"), review_url=existing.get("html_url"))
    job = db.get_job(conn, job["id"])
    if job["cancel_requested"]:
        reason = job["reason"] or "cancelled"
        return _finish(conn, svc, job["id"], "superseded" if reason.startswith("superseded") else "cancelled", reason)
    if not Path(job_paths(svc.state, job["id"]).out, "review.md").is_file():
        if job["source"] == db.FOREGROUND:  # no owner left to run it again
            return _finish(conn, svc, job["id"], "cancelled", "interrupted post had no saved review")
        return _finish(conn, svc, job["id"], "queued", "requeued: review output missing")
    try:
        return post(conn, svc, job)
    except Outcome as outcome:
        return _finish(conn, svc, job["id"], outcome.status, outcome.reason, **outcome.fields)


def post(conn, svc, job):
    """Post out/review.md for a job, after confirming the PR still points at the reviewed head."""
    paths = job_paths(svc.state, job["id"])
    body = Path(paths.out, "review.md").read_text()
    with db.transaction(conn):
        db.update_job(conn, job["id"], status="posting")
    try:
        _check_pr(svc, job)
        response = svc.github.post_review(job["repo"], job["number"], job["head_sha"], body)
    except GitHubError as exc:
        return post_failed(conn, svc, job, exc)
    return _finish(conn, svc, job["id"], "posted", "review posted", posted_at=svc.now(),
                   review_id=response.get("id"), review_url=response.get("html_url"))
