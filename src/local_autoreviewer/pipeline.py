# SPDX-License-Identifier: GPL-3.0-or-later
"""Process one review job end to end: check PR, prepare workspace, run agent, post."""

import fcntl
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from string import Template

from . import combine, db, events, leakscan, review, suggestions
from .agents import make_agent
from .agents.base import JobPaths
from .config import AGENTS
from .github import GitHubError
from .gitops import GitError, ShaMismatch

log = logging.getLogger(__name__)

RATE_LIMIT_BACKOFF = 30 * 60
SHA_LAG_RETRY = 60
SHA_LAG_MAX_ATTEMPTS = 10
FOREGROUND_MAX_WAIT = 5 * 60  # `review` waits out retries up to this long; usage limits are longer
FOREGROUND_CANCEL_POLL = 2.0  # seconds between a foreground job's checks for cancel requests
POST_RECOVERY_RETRY = FOREGROUND_MAX_WAIT  # seconds before retrying a post; `review --post` waits it out
assert POST_RECOVERY_RETRY <= FOREGROUND_MAX_WAIT
DUPLICATE_CHECK_TIMEOUT = 10 * 60  # the check over the reviews of a commit before they are posted as one


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
        _settle_round(conn, svc, job_id, status, reason, fields)
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
    while True:
        handle = open(path, "a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Lock files are unlinked while locked; if that happened between open and flock, this
            # lock is on a deleted file that nobody else can see, so try again on the current one.
            if _same_file(handle, path):
                return handle
        except BaseException:
            handle.close()
            raise
        handle.close()


def _same_file(handle, path):
    try:
        current = os.stat(path)
    except FileNotFoundError:
        return False
    opened = os.fstat(handle.fileno())
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


def release_foreground_lock(state, job_id, handle):
    foreground_lock_path(state, job_id).unlink(missing_ok=True)
    handle.close()


@contextmanager
def orphaned_foreground(state, job_id):
    """Yield True, holding the job's lock, if no live process owns the foreground job."""
    path = foreground_lock_path(state, job_id)
    handle = unreadable = None
    try:
        handle = open(path, "a")
    except FileNotFoundError:  # no locks directory at all: nobody holds a lock
        pass
    except OSError as exc:  # can't tell, so assume the owner is alive
        unreadable = exc
    if handle is None:
        if unreadable:
            log.warning("job %s: cannot check foreground lock %s: %s", job_id, path, unreadable)
        yield unreadable is None
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


def remove_lock_if_free(state, job_id):
    """Delete a job's lock file unless a live process holds it."""
    with orphaned_foreground(state, job_id):
        pass  # orphaned_foreground unlinks the file when it gets the lock


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


def _wait_while(svc, conn, job_id, status, wait, sleep, *, stop_on_cancel=False):
    """Sleep up to `wait` seconds in short steps while the job keeps `status`, and return its status.
    A push or close ends the wait early: a queued job's status changes then, while a `posting` job
    only gets a cancel request (with stop_on_cancel, the wait ends but 'posting' is returned, and
    recover_post deals with the request)."""
    deadline = svc.now() + wait
    while (remaining := deadline - svc.now()) > 0:
        sleep(min(FOREGROUND_CANCEL_POLL, remaining))
        job = db.get_job(conn, job_id)
        if job["status"] != status or (stop_on_cancel and job["cancel_requested"]):
            return job["status"]
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
            if _wait_while(svc, conn, job_id, "posting", wait, sleep, stop_on_cancel=True) != "posting":
                return db.get_job(conn, job_id)["status"]
            try:
                recover_post(conn, svc, db.get_job(conn, job_id))  # also honours a cancel request
            except Exception as exc:  # e.g. a garbled review listing: a failed attempt, as in the worker
                log.exception("job %s: post retry failed", job_id)
                fail_post_attempt(conn, svc, job_id, exc)
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
                retry = ("use `enqueue` to let the worker retry and post it" if job["post"]
                         else "run `review` again later")
                reason = f"not waiting {math.ceil(wait / 60)} min in the foreground ({retry}); {job['reason']}"
                _cancel_foreground(conn, svc, job, reason, only_if=("queued",))  # unless a poll ended it meanwhile
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


def finish_waiting(svc, job_id, *, sleep=time.sleep, notify=lambda message: None):
    """For `review --post`, once all its jobs have run: post its job if it still waits, as the worker would,
    and retry the post in place (see _finish_post). A job that waits for another, still to finish, is
    left to the worker. Returns the job's status."""
    conn = svc.connect()
    try:
        try:
            status = post_waiting(conn, svc, job_id)
        except Exception as exc:  # as in the worker: a failed attempt, if it got as far as posting
            log.exception("job %s: posting a waiting review failed", job_id)
            fail_post_attempt(conn, svc, job_id, exc)
            status = db.get_job(conn, job_id)["status"]
    finally:
        conn.close()
    return _finish_post(svc, job_id, sleep, notify) if status == "posting" else status


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
            _cancel_foreground(conn, svc, job, reason)
    finally:
        conn.close()


def _cancel_foreground(conn, svc, job, reason, *, only_if=("queued", "running")):
    """Cancel a foreground job if its status is still one of only_if. In the same transaction, queue
    the service's own review of its head if a `review --post` job had stood in for it
    (events.requeue_for_service), so a crash can't end the job without the replacement. Returns
    whether the job was cancelled."""
    marks = ",".join("?" * len(only_if))
    with db.transaction(conn):
        cancelled = conn.execute(
            f"UPDATE jobs SET status='cancelled', reason=?, finished_at=? WHERE id=? AND status IN ({marks})",
            (reason[:1000], svc.now(), job["id"], *only_if)).rowcount
        if cancelled and job["post"]:
            events.requeue_for_service(conn, svc.config, job, now=svc.now(), token_owner=svc.token_owner)
        if cancelled:
            _settle_round(conn, svc, job["id"], "cancelled", reason, {})
    if cancelled:
        log.info("job %s -> cancelled: %s", job["id"], reason)
    return bool(cancelled)


def _cancelled(conn, svc, job, cancel):
    reason = cancel.reason or "cancelled"
    if reason == "shutdown":
        return _finish(conn, svc, job["id"], "queued", "requeued after worker shutdown", cancel_requested=0)
    current = db.get_job(conn, job["id"])
    status = "superseded" if (current["reason"] or "").startswith("superseded") else "cancelled"
    return _finish(conn, svc, job["id"], status, current["reason"] or reason)


@dataclass
class PreviousRound:
    """The round of reviews a follow-up checks: its head, and the issues still open after it."""
    head_sha: str
    findings: list  # finding rows with the reviewing agent

    def for_agent(self):
        """The findings as the follow-up agent sees them (previous-findings.json)."""
        return [{"id": f["id"], "reviewer": f["agent"], "severity": f["severity"], "impact": f["impact"],
                 "title": f["title"], "file": f["file"], "line": f["line"], "raised_at": f["raised_at"],
                 "trigger": f["occurs_when"],
                 "detail": f["detail"], "suggestion": f["suggestion"],
                 "proposed_edits": [{k: e[k] for k in ("file", "start_line", "end_line", "replacement")}
                                    for e in suggestions.applied(json.loads(f["edits"] or "[]"))]}
                for f in self.findings]


def previous_round(conn, job, base_contains=lambda base_sha: False):
    """The latest earlier round of reviews of this PR, or None for a full review. A round on another base
    counts only if base_contains(the base commit it was reviewed against): the PR was retargeted from a
    branch whose changes are now in the new base, as when the PR below it in a stack merges. A full
    round's open issues are its findings; a follow-up round's are its regressions plus the findings it
    found not, or only partly, fixed. No-post trials don't count as rounds."""
    scope = (f"repo=? AND number=? AND review_recorded=1 AND status IN ('posted', 'skipped')"
             f" AND NOT {db.TRIAL_SQL}")
    args = (job["repo"], job["number"])
    # A round at this head counts only on another base: a retarget without a push carries its findings on.
    row = conn.execute(f"SELECT id, head_sha, base_ref, base_sha FROM jobs WHERE {scope}"
                       " AND (head_sha!=? OR base_ref!=?) ORDER BY finished_at DESC, id DESC LIMIT 1",
                       (*args, db.FOREGROUND, job["head_sha"], job["base_ref"])).fetchone()
    if row is None:
        return None
    if row["base_ref"] != job["base_ref"] and not (row["base_sha"] and base_contains(row["base_sha"])):
        return None
    ids = set()
    for previous in conn.execute(f"SELECT id, review_kind FROM jobs WHERE {scope} AND head_sha=? AND base_ref=?",
                                 (*args, db.FOREGROUND, row["head_sha"], row["base_ref"])).fetchall():
        if previous["review_kind"] == "followup":
            ids.update(r[0] for r in conn.execute(
                "SELECT id FROM findings WHERE job_id=? AND kind='regression'", (previous["id"],)))
            ids.update(r[0] for r in conn.execute(
                f"SELECT finding_id FROM resolutions WHERE job_id=? AND status IN ({','.join('?' * len(review.OPEN_STATUSES))})",
                (previous["id"], *review.OPEN_STATUSES)))
        else:
            ids.update(r[0] for r in conn.execute(
                "SELECT id FROM findings WHERE job_id=? AND kind='finding'", (previous["id"],)))
    # Triaged as declined, a false positive, a duplicate or deferred: not asked about again. Nor is a
    # duplicate merged into another finding when the reviews were posted: that one stands for it.
    findings = _finding_rows(conn, f"f.id IN ({','.join('?' * len(ids))}) AND f.duplicate_of IS NULL"
                                   " AND (f.disposition IS NULL OR f.disposition='fixed')", sorted(ids)) if ids else []
    return PreviousRound(row["head_sha"], findings)


def _finding_rows(conn, where, params):
    """Finding rows with the agent that raised each, the head it was raised at, and in also_found_by the
    agents whose duplicates of it were merged into it."""
    rows = [dict(r) for r in conn.execute(
        "SELECT f.*, j.agent, j.head_sha AS raised_at, (SELECT json_group_array(DISTINCT dj.agent) FROM findings d"
        " JOIN jobs dj ON dj.id=d.job_id WHERE d.duplicate_of=f.id) AS also_found_by"
        f" FROM findings f JOIN jobs j ON j.id=f.job_id WHERE {where} ORDER BY f.id", params)]
    for row in rows:
        row["also_found_by"] = [agent for agent in json.loads(row["also_found_by"] or "[]") if agent != row["agent"]]
    return rows


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
        # A round on the PR's old base still counts if that base's changes are all in the new one.
        previous = previous_round(conn, job, lambda old: svc.git.contains(mirror, base_sha, old))
        if previous and not svc.git.has_commit(mirror, previous.head_sha):
            log.info("job %s: previous head %s is gone from the mirror; doing a full review", job["id"],
                     previous.head_sha[:12])
            previous = None
        start = svc.git.since_review_start(mirror, previous.head_sha, job["head_sha"], base_sha) if previous else None
        if previous and start is None:
            log.info("job %s: a conflicting merge or rebase mixes the PR's changes since %s with its base's; doing a "
                     "full review", job["id"], previous.head_sha[:12])
            previous = None
        if previous:
            since_bytes, since_files = svc.git.write_followup_context(
                mirror, start, job["head_sha"], paths.context, exclude=repo_cfg.diff_exclude,
                previous=previous.for_agent())
            if since_bytes > repo_cfg.max_diff_bytes or since_files > repo_cfg.max_changed_files:
                log.info("job %s: the changes since %s are too large for a follow-up; doing a full review", job["id"],
                         previous.head_sha[:12])
                for name in ("since-review.diff", "previous-findings.json"):
                    Path(paths.context, name).unlink(missing_ok=True)
                previous = None
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
    prompts = review.snapshot(followup=bool(previous))  # read once: a deploy may replace the prompts meanwhile
    prompt_version = review.prompt_version(prompts)
    fields = dict(
        repo=job["repo"], number=job["number"], title=pr.get("title", ""),
        author=(pr.get("author") or {}).get("login", ""), base_ref=pr["baseRefName"], head_sha=job["head_sha"],
        worktree=paths.worktree, context=paths.context, agent_note=agent.agent_note(),
        extra_instructions=repo_cfg.extra_instructions,
        project_docs=repo_cfg.project_docs and Path(paths.context, "project-docs").is_dir())
    if previous:
        prompt = review.build_followup_prompt(prompts=prompts, previous_sha=previous.head_sha,
                                              previous_count=len(previous.findings), **fields)
        schema = Path(paths.context, "followup.schema.json")  # the agent reads its own copy, not the live files
        schema.write_text(json.dumps(review.followup_schema(prompts)))
    else:
        prompt = review.build_prompt(template=Template(prompts["review.md"]), **fields)
        schema = Path(paths.context, "review.schema.json")
        schema.write_text(prompts["review.schema.json"])
    invocation = agent.invocation(unit=unit, paths=paths, prompt=prompt, session_id=session_id, schema=schema)
    log.info("job %s: running %s on %s#%s @%s%s", job["id"], job["agent"], job["repo"], job["number"],
             job["head_sha"][:12], f" (follow-up of {previous.head_sha[:12]})" if previous else "")
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

    if previous:
        structured = review.validate_followup(result.review, {f["id"] for f in previous.findings})
    else:
        structured = review.validate(result.review)
    # Inline suggestions must fall within the PR's diff against its base, also for a follow-up.
    hunks = suggestions.diff_hunks(Path(paths.context, "pr.diff").read_text(errors="replace"))
    structured = suggestions.apply(structured, worktree=paths.worktree, hunks=hunks, test_paths=repo_cfg.test_paths)
    if previous:
        structured["followup_of"] = previous.head_sha  # so a saved review.json records as a follow-up
        body = review.render_followup(structured, previous.findings, repo=job["repo"], number=job["number"],
                                      sha=job["head_sha"], previous_sha=previous.head_sha, agent=job["agent"],
                                      model=job["model"], job_id=job["id"])
    else:
        body = review.render(structured, repo=job["repo"], number=job["number"], sha=job["head_sha"],
                             base_ref=pr["baseRefName"], agent=job["agent"], model=job["model"], job_id=job["id"])
    inline = suggestions.comments(structured, review.comment_label(structured))
    Path(paths.out, "review.json").write_text(json.dumps(structured, indent=2))
    Path(paths.out, "review.md").write_text(body)
    Path(paths.out, "comments.json").write_text(json.dumps(inline, indent=2))
    hits = leakscan.scan("\n".join([body, *(comment["body"] for comment in inline)]), svc.secrets())
    if hits:
        raise Outcome("blocked", f"possible credential in review ({', '.join(hits)}); not posted, see "
                                 f"{paths.out}/review.md")
    with db.transaction(conn):  # only reviews that passed the scan reach the database
        db.record_review(conn, job["id"], structured, effort=cfg.agents[job["agent"]].effort,
                         prompt_version=prompt_version, followup_of=previous.head_sha if previous else None)
    if not job["post"]:
        raise Outcome("skipped", f"posting disabled; review saved to {paths.out}/review.md")
    return _post_round(conn, svc, db.get_job(conn, job["id"]), agent, cancel, title=pr.get("title", ""))


# One post per commit ----------------------------------------------------------------
# The reviews of a commit (the jobs for the same PR, head and base that post) are posted as one. A finished
# review waits, status 'waiting', for the others still to run, for up to combine_wait. The review that
# finishes last leads: it runs the duplicate check over them all, then claims the waiting ones (status
# 'posting', posted_with = the leader) and posts them together. Its post's outcome is theirs too
# (_settle_round). The worker posts a waiting review on its own once nothing is left to wait for, or the
# wait is over (post_waiting); a review that finishes after that is posted on its own as well.

def _siblings(conn, job, statuses):
    """The other jobs that post a review of the job's commit, in one of these statuses."""
    marks = ",".join("?" * len(statuses))
    return conn.execute(
        f"SELECT * FROM jobs WHERE repo=? AND number=? AND head_sha=? AND base_ref=? AND post=1 AND id!=?"
        f" AND status IN ({marks}) ORDER BY id",
        (job["repo"], job["number"], job["head_sha"], job["base_ref"], job["id"], *statuses)).fetchall()


def _pending(conn, job):
    """Reviews of the job's commit that are still to finish."""
    return _siblings(conn, job, ("queued", "running"))


def _waiting_with(conn, job):
    """Finished reviews of the job's commit waiting to be posted that go in one post with it: full reviews,
    or follow-ups of the same round."""
    return [row for row in _siblings(conn, job, ("waiting",)) if row["followup_of"] == job["followup_of"]]


def due(conn, svc, job):
    """Whether a waiting review should be posted now: nothing it waits for is left to finish, or its wait is over."""
    return job["not_before"] <= svc.now() or not _pending(conn, job)


def _display_order(rows):
    return sorted(rows, key=lambda row: (AGENTS.index(row["agent"]) if row["agent"] in AGENTS else len(AGENTS),
                                         row["id"]))


def _wait(conn, svc, job, pending):
    names = review.reviewer_names([row["agent"] for row in pending])
    db.update_job(conn, job["id"], status="waiting", posted_with=None,
                  reason=f"review ready; waiting to post it with {names}'s review of this commit",
                  not_before=svc.now() + svc.config.service.combine_wait)
    log.info("job %s -> waiting for job(s) %s", job["id"], ", ".join(str(row["id"]) for row in pending))
    return "waiting"


def _post_round(conn, svc, job, agent, cancel, *, title):
    """Post the job's review, recorded and saved, with the other reviews of its commit: wait while any is
    still to finish, then check the finished ones for duplicates and post them as one."""
    wait = svc.config.service.combine_wait
    with db.transaction(conn):
        pending = _pending(conn, job)
        if pending and wait > 0:
            return _wait(conn, svc, job, pending)
        others = _waiting_with(conn, job)
        for row in others:  # keep them from giving up on this post while the check runs
            db.update_job(conn, row["id"], not_before=max(row["not_before"],
                                                          svc.now() + DUPLICATE_CHECK_TIMEOUT + 5 * 60))
    check = None
    if others:
        check = _check_duplicates(conn, svc, job, others, agent, cancel, title=title)
        if cancel.reason == "shutdown":  # stopped mid-check: after the restart it posts like a waiting review
            with db.transaction(conn):
                db.update_job(conn, job["id"], status="waiting", not_before=svc.now(),
                              reason="review ready; posting it after a worker restart")
            return "waiting"
        if cancel():
            return _cancelled(conn, svc, job, cancel)
    with db.transaction(conn):
        pending = _pending(conn, job)  # one started meanwhile, say by `enqueue --force`
        if pending and wait > 0:
            return _wait(conn, svc, job, pending)
        still = {row["id"] for row in _waiting_with(conn, job)}
        _claim(conn, svc, job, [row for row in others if row["id"] in still], check)
    return post(conn, svc, db.get_job(conn, job["id"]))


def post_waiting(conn, svc, job_id):
    """Post a waiting review if it is due (see due()), with any other reviews of its commit still waiting.
    These are the reviews of a round whose last review failed, was cancelled or took too long, or whose
    worker stopped during the duplicate check. The check isn't run here: it needs an agent, and this runs
    like a post retry, which runs none. Returns the job's status afterwards."""
    with db.transaction(conn):
        job = db.get_job(conn, job_id)
        if job["status"] != "waiting" or not due(conn, svc, job):
            return job["status"]
        others = _waiting_with(conn, job)
        _claim(conn, svc, job, others, {"duplicates": [], "unchecked": "the check didn't run"} if others else None)
    try:
        return post(conn, svc, db.get_job(conn, job_id))
    except Outcome as outcome:
        return _finish(conn, svc, job_id, outcome.status, outcome.reason, **outcome.fields)


def _saved_review(svc, job):
    """A finished job's review as review.merge() takes it, from its saved review.json."""
    data = json.loads(Path(job_paths(svc.state, job["id"]).out, "review.json").read_text())
    structured = review.validate(data)
    if isinstance(data.get("resolutions"), list):  # a follow-up's statuses of the previous findings
        structured["resolutions"] = data["resolutions"]
    return {"job_id": job["id"], "agent": job["agent"], "model": job["model"], "review": structured}


def _check_duplicates(conn, svc, job, others, agent, cancel, *, title):
    """Have the job's agent name the items its review and the others' (finished jobs' rows) share. Returns
    the round's record for round.json: its members, the duplicates found as [repeat, kept] item pairs
    (see review.merge), and why the reviews weren't checked if they couldn't be."""
    rows = _display_order([job, *others])
    record = {"members": [row["id"] for row in rows], "duplicates": [], "unchecked": "", "checked_by": job["agent"]}
    paths = job_paths(svc.state, job["id"])
    check = JobPaths(root=paths.root, worktree=paths.worktree, context=paths.context,
                     out=str(Path(paths.out, "duplicates")))
    try:
        members = [_saved_review(svc, row) for row in rows]
        if not combine.worth_checking(members):
            return record
        Path(check.out).mkdir(parents=True, exist_ok=True)
        prompts = combine.snapshot()
        schema = Path(paths.context, "duplicates.schema.json")  # its own copy, as for the review
        schema.write_text(prompts["duplicates.schema.json"])
        prompt = combine.build_prompt(members, prompts=prompts, repo=job["repo"], number=job["number"], title=title,
                                      head_sha=job["head_sha"], worktree=paths.worktree, agent_note=agent.agent_note())
        unit = f"{scope_unit(job['id'])}-duplicates"
        with db.transaction(conn):  # so a restart or an abandoned `review` stops it, as it would the review
            db.update_job(conn, job["id"], scope_unit=unit)
        session_id = agent.new_session_id()
        log.info("job %s: checking jobs %s for duplicates", job["id"], ", ".join(map(str, record["members"])))
        run = svc.runner.run(agent.invocation(unit=unit, paths=check, prompt=prompt, session_id=session_id,
                                              schema=schema),
                             timeout=min(DUPLICATE_CHECK_TIMEOUT, svc.config.service.review_timeout),
                             should_cancel=cancel)
        result = agent.parse(run, check, session_id)
        if run.cancelled or run.timed_out or result.error_kind:
            why = ("it was cancelled" if run.cancelled else "it timed out" if run.timed_out
                   else "it hit a usage limit" if result.error_kind == "rate_limit" else "it failed")
            log.warning("job %s: duplicate check: %s %s", job["id"], why, result.error or "")
            return {**record, "unchecked": f"the check {why.removeprefix('it ')}"}
        sets = combine.groups(result.review, members)
        record.update(duplicates=[[list(repeat), list(kept)] for repeat, kept in combine.duplicates(members, sets).items()],
                      answer=result.review, usage={**result.usage, "seconds": round(run.duration, 1)},
                      prompt_version=review.prompt_version(prompts))
        log.info("job %s: %d duplicate(s) in %d set(s)", job["id"], len(record["duplicates"]), len(sets))
        return record
    except Exception as exc:  # the reviews still go out, unchecked
        log.exception("job %s: duplicate check failed", job["id"])
        return {**record, "unchecked": f"the check failed ({type(exc).__name__})"}


_ITEM_KINDS = {"nits": "nit", "test_gaps": "test_gap", "questions": "question"}


def _record_duplicates(conn, rows, pairs):
    """Record which items repeat which (findings.duplicate_of), replacing what was recorded for these jobs."""
    ids = [row["id"] for row in rows]
    followup = {row["id"]: bool(row["followup_of"]) for row in rows}
    conn.execute(f"UPDATE findings SET duplicate_of=NULL WHERE job_id IN ({','.join('?' * len(ids))})", ids)

    def finding_id(ref):
        job_id, key, position = ref
        kind = ("regression" if followup[job_id] else "finding") if key == "findings" else _ITEM_KINDS[key]
        row = conn.execute("SELECT id FROM findings WHERE job_id=? AND kind=? AND position=?",
                           (job_id, kind, position)).fetchone()
        return row[0] if row else None
    for repeat, kept in pairs:
        repeat_id, kept_id = finding_id(repeat), finding_id(kept)
        if repeat_id and kept_id:
            conn.execute("UPDATE findings SET duplicate_of=? WHERE id=?", (kept_id, repeat_id))


def _claim(conn, svc, job, others, check):
    """In the caller's transaction: make the job's post the one for its own review and the others' (waiting
    jobs' rows), which are then posting with posted_with = the job, and record the duplicates among them.
    The round's record goes to the job's out/round.json, written before the transaction commits."""
    rows = _display_order([job, *others])
    ids = [row["id"] for row in rows]
    path = Path(job_paths(svc.state, job["id"]).out, "round.json")
    if others:
        record = {**(check or {}), "members": ids, "unchecked": (check or {}).get("unchecked", "")}
        record["duplicates"] = [pair for pair in record.get("duplicates") or []
                                if pair[0][0] in ids and pair[1][0] in ids]  # one may have been superseded since
        _record_duplicates(conn, rows, record["duplicates"])
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(record, indent=2))
        os.replace(temp, path)
        for row in others:
            db.update_job(conn, row["id"], status="posting", posted_with=job["id"],
                          reason=f"posting with job {job['id']}")
        log.info("job %s: posting with job(s) %s", job["id"], ", ".join(str(row["id"]) for row in others))
    else:
        path.unlink(missing_ok=True)
    db.update_job(conn, job["id"], status="posting", posted_with=job["id"] if others else None, not_before=svc.now())


def _members(conn, job):
    return conn.execute("SELECT * FROM jobs WHERE posted_with=? AND id!=?", (job["id"], job["id"])).fetchall()


def _release_round(conn, svc, job_id):
    """The job's post won't carry the others after all: they wait again, for it among others."""
    for row in _members(conn, {"id": job_id}):
        db.update_job(conn, row["id"], status="waiting", posted_with=None, reason="review ready; waiting to post it",
                      not_before=svc.now() + svc.config.service.combine_wait)
    db.update_job(conn, job_id, posted_with=None)


def _settle_round(conn, svc, job_id, status, reason, fields):
    """In the caller's transaction: a post that carries other jobs' reviews ended, so they end the same way;
    or it went back to the queue, so they wait again."""
    if status not in db.FINAL:
        if status != "posting":
            _release_round(conn, svc, job_id)
        return
    shared = {key: fields[key] for key in ("posted_at", "review_id", "review_url", "error") if key in fields}
    for row in _members(conn, {"id": job_id}):
        db.update_job(conn, row["id"], status=status, reason=f"with job {job_id}: {reason}"[:1000],
                      finished_at=svc.now(), **shared)


def _round_post(conn, svc, job):
    """What the job posts: its own review, or with the reviews it carries, all of them merged (saved as
    round.md and round-comments.json and scanned for credentials like a single review)."""
    out = Path(job_paths(svc.state, job["id"]).out)
    others = _members(conn, job)
    if not others:
        saved = out / "comments.json"
        return (out / "review.md").read_text(), json.loads(saved.read_text()) if saved.is_file() else []
    rows = _display_order([job, *others])
    try:
        record = json.loads((out / "round.json").read_text())
    except (OSError, ValueError):
        record = {}
    body, inline = _render_round(conn, svc, rows, record)
    (out / "round.md").write_text(body)
    (out / "round-comments.json").write_text(json.dumps(inline, indent=2))
    hits = leakscan.scan("\n".join([body, *(comment["body"] for comment in inline)]), svc.secrets())
    if hits:
        raise Outcome("blocked", f"possible credential in review ({', '.join(hits)}); not posted, see {out}/round.md")
    return body, inline


def _render_round(conn, svc, rows, record):
    """The body and inline comments of one post for several reviews of a commit (finished jobs' rows, in
    display order), with the duplicates in record (see _check_duplicates) merged."""
    ids = {row["id"] for row in rows}
    pairs = {tuple(repeat): tuple(kept) for repeat, kept in record.get("duplicates") or []
             if repeat[0] in ids and kept[0] in ids}
    merged = review.merge([_saved_review(svc, row) for row in rows], pairs, unchecked=record.get("unchecked") or "")
    lead = rows[0]
    if lead["followup_of"]:
        marks = ",".join("?" * len(ids))
        previous = _finding_rows(conn, f"f.id IN (SELECT finding_id FROM resolutions WHERE job_id IN ({marks}))",
                                 sorted(ids))
        body = review.render_followup_round(merged, previous, repo=lead["repo"], number=lead["number"],
                                            sha=lead["head_sha"], previous_sha=lead["followup_of"])
    else:
        body = review.render_round(merged, repo=lead["repo"], number=lead["number"], sha=lead["head_sha"],
                                   base_ref=lead["base_ref"])
    return body, suggestions.comments(merged, review.comment_label(merged))


def preview_round(svc, job_ids, *, title=""):
    """For `review` without --post: its jobs' reviews merged as the service would post them, checked for
    duplicates by the last job's agent, saved as round.md in that job's out/. Returns the path, or None
    with fewer than two reviews to merge."""
    conn = svc.connect()
    try:
        rows = [db.get_job(conn, job_id) for job_id in job_ids]
        rows = [row for row in rows if row is not None and row["status"] == "skipped" and row["review_recorded"]
                and Path(job_paths(svc.state, row["id"]).out, "review.json").is_file()]
        if not rows:
            return None
        lead = rows[-1]
        others = [row for row in rows[:-1] if row["followup_of"] == lead["followup_of"]]
        if not others:
            return None
        agent = make_agent(svc.config.agents[lead["agent"]], svc.state)
        record = _check_duplicates(conn, svc, lead, others, agent, CancelToken(), title=title)
        rows = _display_order([lead, *others])
        with db.transaction(conn):
            _record_duplicates(conn, rows, record["duplicates"])
        body, _inline = _render_round(conn, svc, rows, record)
        out = Path(job_paths(svc.state, lead["id"]).out)
        (out / "round.json").write_text(json.dumps(record, indent=2))
        (out / "round.md").write_text(body)
        return out / "round.md"
    finally:
        conn.close()


def _rejected(exc):
    """True for a definite refusal (HTTP 4xx other than a timeout or rate limit): retrying won't help.
    GitHub reports primary and secondary rate limits as 403 too, so those are recognized by wording."""
    text = str(exc)
    match = re.search(r"\bHTTP (4\d\d)\b", text)
    if not match or match.group(1) in ("408", "429"):
        return False
    return not re.search(r"rate limit", text, re.IGNORECASE)


def post_failed(conn, svc, job, exc):
    """A post that failed or whose result is unknown (a timeout after GitHub accepted it, say). It stays
    `posting` for marker-checked recovery, without running the agent again, for max_attempts post
    attempts (counted apart from the agent's); a definite refusal fails it at once. The review stays
    in out/review.md either way."""
    post_attempts = db.get_job(conn, job["id"])["post_attempts"] + 1
    rejected = _rejected(exc)
    if not rejected and post_attempts >= svc.config.service.max_attempts:
        try:  # out of retries: one last look, in case a failure that wasn't a refusal did reach GitHub
            existing = svc.github.find_marked_review(job["repo"], job["number"], job["id"], svc.token_owner)
        except Exception:
            existing = None
        if existing:
            return _finish(conn, svc, job["id"], "posted", "review found after a failed post", posted_at=svc.now(),
                           post_attempts=post_attempts, review_id=existing.get("id"),
                           review_url=existing.get("html_url"))
    if rejected or post_attempts >= svc.config.service.max_attempts:
        saved = f"review saved to {job_paths(svc.state, job['id']).out}/review.md"
        return _finish(conn, svc, job["id"], "failed", f"post failed: {exc}; {saved}"[:1000],
                       post_attempts=post_attempts, error=str(exc)[:2000])
    log.warning("job %s: post failed (%s); retrying in %d min", job["id"], exc, POST_RECOVERY_RETRY // 60)
    with db.transaction(conn):
        db.update_job(conn, job["id"], post_attempts=post_attempts, reason=f"post failed: {exc}"[:1000],
                      not_before=svc.now() + POST_RECOVERY_RETRY)
    return "posting"


def fail_post_attempt(conn, svc, job_id, exc):
    """Count an unexpected error during a post retry as a failed attempt, if the job is still posting
    (the error may have come after it was finished, from the hand-back say)."""
    job = db.get_job(conn, job_id)
    if job["status"] == "posting":
        post_failed(conn, svc, job, exc)


def recover_post(conn, svc, job):
    """Finish a job left `posting` by a failed or interrupted post: confirm it if the review reached
    GitHub, otherwise post the saved review, unless a push, retarget or close has ended the job since.
    Returns the job's status afterwards."""
    try:
        existing = svc.github.find_marked_review(job["repo"], job["number"], job["id"], svc.token_owner)
    except GitHubError as exc:
        if not db.get_job(conn, job["id"])["cancel_requested"]:
            return post_failed(conn, svc, job, exc)
        existing = None  # a push or close ended the job: don't wait on GitHub any longer
    if existing:
        return _finish(conn, svc, job["id"], "posted", "review found after an interrupted post",
                       posted_at=svc.now(), review_id=existing.get("id"), review_url=existing.get("html_url"))
    job = db.get_job(conn, job["id"])
    if job["cancel_requested"]:
        reason = job["reason"] or "cancelled"
        return _finish(conn, svc, job["id"], "superseded" if reason.startswith("superseded") else "cancelled", reason)
    if not Path(job_paths(svc.state, job["id"]).out, "review.md").is_file():
        with db.transaction(conn):  # the reviews it was to carry can still be posted
            _release_round(conn, svc, job["id"])
        if job["source"] == db.FOREGROUND:  # no owner left to run it again
            _cancel_foreground(conn, svc, job, "interrupted post had no saved review", only_if=("posting",))
            return db.get_job(conn, job["id"])["status"]
        return _finish(conn, svc, job["id"], "queued", "requeued: review output missing")
    try:
        return post(conn, svc, job)
    except Outcome as outcome:
        return _finish(conn, svc, job["id"], outcome.status, outcome.reason, **outcome.fields)


def _post_review(svc, job, body, inline):
    """Post the review with its inline suggestions. If GitHub refuses those (HTTP 422, e.g. a line it
    doesn't count as part of the diff), post the body alone: it shows every proposed change too."""
    try:
        return svc.github.post_review(job["repo"], job["number"], job["head_sha"], body, comments=inline)
    except GitHubError as exc:
        if not inline or not re.search(r"\bHTTP 422\b", str(exc)):
            raise
        log.warning("job %s: GitHub refused the inline suggestions (%s); posting the review without them",
                    job["id"], exc)
        return svc.github.post_review(job["repo"], job["number"], job["head_sha"], body)


def post(conn, svc, job):
    """Post a job's review (out/review.md, or the round it posts, see _round_post), after confirming the PR
    still points at the reviewed head."""
    with db.transaction(conn):
        db.update_job(conn, job["id"], status="posting")
    try:
        body, inline = _round_post(conn, svc, job)
        _check_pr(svc, job)
        response = _post_review(svc, job, body, inline)
        review_id, review_url = response.get("id"), response.get("html_url")
    except Outcome:
        raise  # the PR closed, moved or was retargeted: not a failed post
    except Exception as exc:  # including a garbled response to a post that may have succeeded
        return post_failed(conn, svc, job, exc)
    return _finish(conn, svc, job["id"], "posted", "review posted", posted_at=svc.now(),
                   review_id=review_id, review_url=review_url)
