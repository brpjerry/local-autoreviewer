# SPDX-License-Identifier: GPL-3.0-or-later
"""Process one review job end to end: check PR, prepare workspace, run agent, post."""

import fcntl
import json
import logging
import shutil
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
    if pr["isDraft"] and job["source"] != "manual":
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


def run_foreground(svc, job_id, *, sleep=time.sleep, notify=lambda message: None):
    """Run a claimed job to a final state without the worker: wait out short retries in place,
    and cancel the job rather than leave a long deferral (a usage limit) queued for the worker."""
    while True:
        status = run_job(svc, job_id, CancelToken())
        if status != "queued":
            return status
        conn = svc.connect()
        try:
            job = db.get_job(conn, job_id)
            wait = max(0, job["not_before"] - svc.now())
            if wait > FOREGROUND_MAX_WAIT:
                return _finish(conn, svc, job_id, "cancelled",
                               f"not waiting {wait // 60} min in the foreground; {job['reason']}")
            notify(f"job {job_id} [{job['agent']}]: {job['reason']}; trying again in {wait}s")
            sleep(wait)
            with db.transaction(conn):
                db.update_job(conn, job_id, status="running", started_at=svc.now())
        finally:
            conn.close()


def abandon(svc, job_ids, reason):
    """Cancel jobs a foreground run is leaving unfinished (for example after Ctrl-C), so a worker
    never picks them up. A job caught mid-post is left to the worker's marker-checked recovery."""
    conn = svc.connect()
    try:
        for job_id in job_ids:
            job = db.get_job(conn, job_id)
            if job["status"] not in ("queued", "running"):
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


def post(conn, svc, job):
    """Post out/review.md for a job, after confirming the PR still points at the reviewed head."""
    paths = job_paths(svc.state, job["id"])
    body = Path(paths.out, "review.md").read_text()
    with db.transaction(conn):
        db.update_job(conn, job["id"], status="posting")
    _check_pr(svc, job)
    response = svc.github.post_review(job["repo"], job["number"], job["head_sha"], body)
    return _finish(conn, svc, job["id"], "posted", "review posted", posted_at=svc.now(),
                   review_id=response.get("id"), review_url=response.get("html_url"))
