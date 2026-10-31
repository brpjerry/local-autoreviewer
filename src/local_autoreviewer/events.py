# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalized PR events and the single policy path that turns them into jobs."""

import logging
from dataclasses import dataclass

from . import db
from .config import TOKEN_OWNER

log = logging.getLogger(__name__)

REVIEW_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review", "retargeted")
STOP_ACTIONS = ("closed", "converted_to_draft")


@dataclass(frozen=True)
class PrEvent:
    repo: str
    number: int
    action: str
    head_sha: str
    base_ref: str
    is_draft: bool
    author: str
    source: str  # poll | webhook | manual
    delivery_id: str = None
    head_changed_at: int = None
    force: bool = False           # manual only: review again even if this head was reviewed
    agents: tuple = None          # manual only: subset of the repo's agents


def author_allowed(repo_cfg, author, token_owner):
    allowed = {token_owner.lower() if a == TOKEN_OWNER and token_owner else a for a in repo_cfg.authors}
    return author.lower() in allowed


def record(conn, event, now):
    """Store the raw event; return False for a duplicate webhook delivery."""
    cursor = conn.execute(
        "INSERT OR IGNORE INTO events (source, delivery_id, repo, number, action, head_sha, received_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event.source, event.delivery_id, event.repo, event.number, event.action, event.head_sha, now))
    return bool(cursor.rowcount)


def stop_jobs(conn, repo, number, status, reason, *, agent=None, keep=None):
    """Supersede/cancel active jobs. Queued ones end now; running ones get a cancel request."""
    stopped = []
    for job in db.active_jobs(conn, repo, number, agent):
        if keep and keep(job):
            continue
        if job["status"] == "queued":
            db.update_job(conn, job["id"], status=status, reason=reason)
        else:
            db.update_job(conn, job["id"], cancel_requested=1, reason=reason)
        stopped.append(job["id"])
    return stopped


def requeue_for_service(conn, cfg, job, *, now, token_owner):
    """Queue the review the poller would have queued for a cancelled `review --post` job's head:
    while that job was active it counted as the head's review, so a poll that saw the head skipped
    it. Only that one job is added, under the poller's rules, and only if no other job already covers
    the head; nothing else is superseded. Does nothing if the poller hasn't seen the head, since its
    next poll will. Returns the new job id or None."""
    repo_cfg = cfg.repo(job["repo"])
    row = conn.execute("SELECT * FROM prs WHERE repo=? AND number=? AND state='open'",
                       (job["repo"], job["number"])).fetchone()
    if (repo_cfg is None or job["agent"] not in repo_cfg.agents or row is None or row["is_draft"]
            or (row["head_sha"], row["base_ref"]) != (job["head_sha"], job["base_ref"])
            or not author_allowed(repo_cfg, row["author"], token_owner)):
        return None
    covered = conn.execute(
        "SELECT 1 FROM jobs WHERE repo=? AND number=? AND head_sha=? AND base_ref=? AND agent=? AND id!=?"
        f" AND NOT {db.TRIAL_SQL} AND status NOT IN ('cancelled', 'superseded')",
        (job["repo"], job["number"], job["head_sha"], job["base_ref"], job["agent"], job["id"],
         db.FOREGROUND)).fetchone()
    if covered:
        return None
    job_id = db.insert_job(
        conn, repo=job["repo"], number=job["number"], head_sha=job["head_sha"], base_ref=job["base_ref"],
        agent=job["agent"], model=cfg.agents[job["agent"]].model, source="poll",
        not_before=max(now, row["head_changed_at"] + cfg.service.settle), now=now, post=repo_cfg.post,
        rerun=db.next_rerun(conn, job["repo"], job["number"], job["head_sha"], job["base_ref"], job["agent"]))
    if job_id:
        log.info("queued job %s: %s#%s @%s %s (in place of cancelled job %s)", job_id, job["repo"], job["number"],
                 job["head_sha"][:12], job["agent"], job["id"])
    return job_id


def ingest(conn, cfg, event, *, now, token_owner):
    """Apply policy to one event inside the caller's transaction. Returns new job ids."""
    repo_cfg = cfg.repo(event.repo)
    if repo_cfg is None:
        log.debug("ignoring %s#%s: repo not configured", event.repo, event.number)
        return []
    repo = repo_cfg.name

    if event.action in STOP_ACTIONS or (event.is_draft and event.source != "manual"):
        reason = "PR closed" if event.action == "closed" else "PR is a draft"

        def requested_for_this_target(job):
            # `enqueue` and `review` may review a draft on purpose; a close, a new head or a new base stops them.
            return (event.action != "closed" and job["source"] in ("manual", db.FOREGROUND)
                    and job["head_sha"] == event.head_sha and job["base_ref"] == event.base_ref)

        stopped = stop_jobs(conn, repo, event.number, "cancelled", reason, keep=requested_for_this_target)
        if stopped:
            log.info("%s#%s %s: cancelled jobs %s", repo, event.number, event.action, stopped)
        return []
    if event.action not in REVIEW_ACTIONS:
        return []
    if event.source != "manual" and not author_allowed(repo_cfg, event.author, token_owner):
        log.info("ignoring %s#%s by %s: author not in allowlist", repo, event.number, event.author)
        return []

    agents = event.agents or repo_cfg.agents
    changed_at = event.head_changed_at if event.head_changed_at is not None else now
    not_before = now if event.source == "manual" else changed_at + cfg.service.settle
    def same_target(job):
        return job["head_sha"] == event.head_sha and job["base_ref"] == event.base_ref

    created = []
    for agent in agents:
        superseded = stop_jobs(conn, repo, event.number, "superseded",
                               f"superseded by {event.head_sha[:12]}", agent=agent, keep=same_target)
        if superseded:
            log.info("%s#%s %s: superseded jobs %s", repo, event.number, agent, superseded)
        rerun = 0
        if event.force:
            if any(same_target(job) and not db.is_trial(job)
                   for job in db.active_jobs(conn, repo, event.number, agent)):
                continue  # already in flight for this exact head
            rerun = db.next_rerun(conn, repo, event.number, event.head_sha, event.base_ref, agent)
        else:
            # Dedupe on the head, but a head whose review was cancelled or superseded
            # (closed then reopened, or force-pushed back) gets reviewed again.
            # A no-post `review` trial doesn't count; a foreground `review --post` does.
            latest = conn.execute(
                "SELECT status, rerun FROM jobs WHERE repo=? AND number=? AND head_sha=? AND base_ref=? AND agent=?"
                f" AND NOT {db.TRIAL_SQL} ORDER BY rerun DESC LIMIT 1",
                (repo, event.number, event.head_sha, event.base_ref, agent, db.FOREGROUND)).fetchone()
            if latest is not None and latest["status"] not in ("cancelled", "superseded"):
                continue  # already reviewed, or in flight
            rerun = db.next_rerun(conn, repo, event.number, event.head_sha, event.base_ref, agent)
        job_id = db.insert_job(
            conn, repo=repo, number=event.number, head_sha=event.head_sha, base_ref=event.base_ref,
            agent=agent, model=cfg.agents[agent].model, source=event.source, not_before=not_before,
            now=now, post=repo_cfg.post, rerun=rerun)
        if job_id:
            created.append(job_id)
            log.info("queued job %s: %s#%s @%s %s (%s)", job_id, repo, event.number,
                     event.head_sha[:12], agent, event.action)
    return created
