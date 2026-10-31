# SPDX-License-Identifier: GPL-3.0-or-later
"""Polling trigger: diff `gh pr list` snapshots against the prs table."""

import fcntl
import logging
from contextlib import contextmanager
from pathlib import Path

from .. import db, events
from ..events import PrEvent
from ..github import pr_author

log = logging.getLogger(__name__)


def _action(row, pr):
    """Which event a change between the stored row and the current PR represents, if any."""
    if row is None:
        return "opened"
    if row["state"] != "open":
        return "reopened"
    if row["head_sha"] != pr["headRefOid"]:
        return "synchronize"
    if row["base_ref"] != pr["baseRefName"]:
        return "retargeted"
    if row["is_draft"] and not pr["isDraft"]:
        return "ready_for_review"
    if not row["is_draft"] and pr["isDraft"]:
        return "converted_to_draft"
    return None


@contextmanager
def poll_lock(state):
    """Run one poll at a time (the timer and a manual `poll`). GitHub is fetched outside the
    database write lock, so without this an older snapshot could be applied over a newer one.
    Unlike the write lock, it doesn't block the worker."""
    path = Path(state) / "poll.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another poll is running; waiting for it to finish")
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def fetch_repo(conn, svc, repo):
    """Everything poll_repo needs from GitHub, fetched before taking the write lock: holding it
    across network calls would stall the worker's writes for as long as GitHub takes.
    Returns the open PRs and, for each known-open PR missing from them, its current state."""
    prs = svc.github.list_open_prs(repo)
    listed = {pr["number"] for pr in prs}
    known_open = [row["number"] for row in conn.execute("SELECT number FROM prs WHERE repo=? AND state='open'",
                                                        (repo,))]
    # Gone from the open list: confirm each one, in case the list was truncated.
    gone = {number: svc.github.pr_view(repo, number) for number in known_open if number not in listed}
    return prs, gone


def poll_repo(conn, svc, repo_cfg, now, prs, gone):
    """Apply one repo's fetched state inside the caller's transaction; return the events produced."""
    repo = repo_cfg.name
    known = {row["number"]: row for row in conn.execute("SELECT * FROM prs WHERE repo=?", (repo,))}
    bootstrap_key = f"bootstrapped:{repo.lower()}"
    review_existing = db.kv_get(conn, bootstrap_key) is not None or svc.config.service.bootstrap == "review"
    produced = []
    open_numbers = set()
    for pr in prs:
        number = pr["number"]
        open_numbers.add(number)
        row = known.get(number)
        action = _action(row, pr)
        head_changed_at = now if row is None or row["head_sha"] != pr["headRefOid"] else row["head_changed_at"]
        conn.execute(
            """INSERT INTO prs (repo, number, state, is_draft, head_sha, base_ref, author, title,
                   head_changed_at, last_seen_at)
               VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo, number) DO UPDATE SET state='open', is_draft=excluded.is_draft,
                   head_sha=excluded.head_sha, base_ref=excluded.base_ref, author=excluded.author,
                   title=excluded.title, head_changed_at=excluded.head_changed_at,
                   last_seen_at=excluded.last_seen_at""",
            (repo, number, int(pr["isDraft"]), pr["headRefOid"], pr["baseRefName"], pr_author(pr),
             pr.get("title") or "", head_changed_at, now))
        if action and review_existing:
            produced.append(PrEvent(repo=repo, number=number, action=action, head_sha=pr["headRefOid"],
                                    base_ref=pr["baseRefName"], is_draft=pr["isDraft"], author=pr_author(pr),
                                    source="poll", head_changed_at=head_changed_at))
    for number, row in known.items():
        if row["state"] != "open" or number in open_numbers:
            continue
        pr = gone.get(number)
        if pr is None or pr["state"] == "OPEN":
            continue  # still open; or, defensively, became known as open after fetch_repo ran
        conn.execute("UPDATE prs SET state='closed', last_seen_at=? WHERE repo=? AND number=?", (now, repo, number))
        produced.append(PrEvent(repo=repo, number=number, action="closed", head_sha=pr["headRefOid"],
                                base_ref=pr["baseRefName"], is_draft=pr["isDraft"], author=pr_author(pr),
                                source="poll"))
    if not review_existing:
        db.kv_set(conn, bootstrap_key, now)
        log.info("%s: baseline recorded for %d open PR(s); future updates will be reviewed", repo, len(prs))
    for event in produced:
        events.record(conn, event, now)
        events.ingest(conn, svc.config, event, now=now, token_owner=svc.token_owner)
    return produced


def poll(svc, *, dry_run=False):
    """Poll every configured repo. Each repo commits separately so one failure doesn't block others."""
    with poll_lock(svc.state):
        return _poll(svc, dry_run)


def _poll(svc, dry_run):
    conn = svc.connect()
    results, failures = {}, 0
    try:
        for repo_cfg in svc.config.repos.values():
            now = svc.now()
            try:
                prs, gone = fetch_repo(conn, svc, repo_cfg.name)
                conn.execute("BEGIN IMMEDIATE")
                produced = poll_repo(conn, svc, repo_cfg, now, prs, gone)
                conn.execute("ROLLBACK" if dry_run else "COMMIT")
                results[repo_cfg.name] = produced
            except Exception as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                failures += 1
                log.error("poll %s failed: %s", repo_cfg.name, exc)
        if not dry_run:
            with db.transaction(conn):
                db.kv_set(conn, "last_poll", svc.now())
    finally:
        conn.close()
    return results, failures
