# SPDX-License-Identifier: GPL-3.0-or-later
"""Polling trigger: diff `gh pr list` snapshots against the prs table."""

import logging

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


def poll_repo(conn, svc, repo_cfg, now):
    """Poll one repo inside the caller's transaction; return the events produced."""
    repo = repo_cfg.name
    prs = svc.github.list_open_prs(repo)
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
        # Gone from the open list: confirm before cancelling, in case the list was truncated.
        pr = svc.github.pr_view(repo, number)
        if pr["state"] == "OPEN":
            continue
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
    conn = svc.connect()
    results, failures = {}, 0
    try:
        for repo_cfg in svc.config.repos.values():
            now = svc.now()
            try:
                conn.execute("BEGIN IMMEDIATE")
                produced = poll_repo(conn, svc, repo_cfg, now)
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
