# SPDX-License-Identifier: GPL-3.0-or-later
"""Feedback on posted reviews, collected from a PR's comments once it closes, for tuning the prompts.

Every posted review asks for feedback (review.FEEDBACK_REQUEST). What counts as feedback:
- comment: a PR conversation comment with a line that starts with TAG;
- review: a PR review other than ours whose body has such a line;
- reply: any reply in the thread of one of our inline suggestions;
- inline: another inline comment with such a line.
Each is tied to the review it answers: a reply to the review its thread belongs to, the others to our
latest review posted before them (not counting "review skipped" notices, which ask for nothing).

A closed PR is collected by the first poll that sees it closed, and once more LATE after that, for
feedback given after the merge. Each collection replaces the PR's earlier one, so feedback deleted or
untagged since then goes."""

import logging
import re
from datetime import datetime
from pathlib import Path

from . import db, review
from .github import MARKER_PREFIX, GitHubError
from .pipeline import job_paths

log = logging.getLogger(__name__)

TAG = review.FEEDBACK_TAG
# A line starting with the tag, perhaps as a heading or in bold; not a quoted line.
_TAG_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)?[*_]*" + re.escape(TAG) + r"(?![A-Za-z0-9])",
                     re.IGNORECASE | re.MULTILINE)
MAX_ATTEMPTS = 5   # GitHub failures in a row before a closed PR's collection is given up
PER_POLL = 5       # closed PRs collected per repo per poll
LATE = 86400       # when a closed PR is collected again
MAX_TEXT = review.MAX_BODY


def is_feedback(body):
    return bool(body) and MARKER_PREFIX not in body and _TAG_RE.search(body) is not None


def _time(value):
    return int(datetime.fromisoformat(value).timestamp())


def _login(item):
    return ((item.get("user") or {}).get("login") or "")


def _where(comment):
    line = comment.get("line") or comment.get("original_line")
    return f"{comment.get('path') or ''}:{line}" if line else comment.get("path") or ""


def _context(comment):
    """Where an inline suggestion was, and its heading, for the replies in its thread."""
    heading = (comment.get("body") or "").strip().split("\n", 1)[0][:300]
    return f"{_where(comment)} {heading}".strip()


def gather(github, repo, number, login):
    """The PR's feedback on our reviews (those `login` posted), as rows for store()."""
    reviews = github.list_reviews(repo, number)
    ours = sorted((r for r in reviews if _login(r).lower() == login.lower() and MARKER_PREFIX in (r.get("body") or "")
                   and r.get("submitted_at")), key=lambda r: _time(r["submitted_at"]))
    our_ids = {r["id"] for r in ours}

    def answered(created_at):
        latest = None
        for r in ours:
            if _time(r["submitted_at"]) > created_at:
                break
            if not r["body"].startswith(review.SKIPPED_HEADING):
                latest = r["id"]
        return latest

    rows = []

    def add(source, item, created, review_id, context=None):
        rows.append({"source": source, "github_id": item["id"], "url": item.get("html_url") or "",
                     "author": _login(item), "created_at": created, "body": (item.get("body") or "")[:MAX_TEXT],
                     "review_id": review_id, "context": context})

    for comment in github.list_issue_comments(repo, number):
        if is_feedback(comment.get("body")):
            created = _time(comment["created_at"])
            add("comment", comment, created, answered(created))
    for r in reviews:
        if r["id"] not in our_ids and r.get("submitted_at") and is_feedback(r.get("body")):
            created = _time(r["submitted_at"])
            add("review", r, created, answered(created))
    comments = github.list_review_comments(repo, number)
    by_id = {c["id"]: c for c in comments}
    for comment in comments:
        created = _time(comment["created_at"])
        root = by_id.get(comment.get("in_reply_to_id"))
        if root is not None and root.get("pull_request_review_id") in our_ids:
            add("reply", comment, created, root["pull_request_review_id"], _context(root))
        elif comment.get("pull_request_review_id") not in our_ids and is_feedback(comment.get("body")):
            add("inline", comment, created, answered(created), _where(comment) or None)
    return rows


def store(conn, repo, number, rows, *, state, now):
    """Save a PR's collected feedback in place of what an earlier collection found, in the caller's transaction.
    state: the PR's state on GitHub when it was collected. A closed PR is marked collected only if the poller
    has it as closed too: a PR that closed during a collection started while it was open is still collected
    on its close."""
    found = {(row["source"], row["github_id"]) for row in rows}
    gone = [row["id"] for row in conn.execute("SELECT id, source, github_id FROM feedback WHERE repo=? AND number=?",
                                              (repo, number)) if (row["source"], row["github_id"]) not in found]
    conn.executemany("DELETE FROM feedback WHERE id=?", [(feedback_id,) for feedback_id in gone])
    conn.executemany(
        "INSERT INTO feedback (repo, number, source, github_id, url, author, created_at, body, review_id, context,"
        " collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (repo, source, github_id) DO UPDATE SET url=excluded.url, body=excluded.body,"
        " review_id=excluded.review_id, context=excluded.context, collected_at=excluded.collected_at",
        [(repo, number, row["source"], row["github_id"], row["url"], row["author"], row["created_at"], row["body"],
          row["review_id"], row["context"], now) for row in rows])
    if state == "OPEN":
        conn.execute("UPDATE prs SET merged=NULL WHERE repo=? AND number=?", (repo, number))
    else:
        conn.execute("UPDATE prs SET feedback_collected_at=?, feedback_attempts=0, merged=? WHERE repo=? AND number=?"
                     " AND state='closed'", (now, int(state == "MERGED"), repo, number))


def collect(conn, svc, repo, number):
    """Collect one PR's feedback now. Returns how many items it found, or None if GitHub failed (then a closed
    PR is retried by a later poll, up to MAX_ATTEMPTS times)."""
    try:
        state = svc.github.pr_view(repo, number)["state"]
        rows = gather(svc.github, repo, number, svc.token_owner)
    except (GitHubError, ValueError, KeyError, TypeError, AttributeError) as exc:  # unexpected JSON too
        with db.transaction(conn):
            conn.execute("UPDATE prs SET feedback_attempts=feedback_attempts+1 WHERE repo=? AND number=?",
                         (repo, number))
        log.warning("%s#%s: cannot collect review feedback: %s", repo, number, exc)
        return None
    with db.transaction(conn):
        store(conn, repo, number, rows, state=state, now=svc.now())
    log.info("%s#%s: collected %d review feedback item(s)", repo, number, len(rows))
    return len(rows)


def pending(conn, repo, now, limit=PER_POLL):
    """Closed PRs of the repo that we reviewed and whose feedback is due (limit -1: all of them): not collected
    since they closed, or not since LATE after. A closed PR's last_seen_at is when the poller saw it close."""
    return [row["number"] for row in conn.execute(
        "SELECT number FROM prs p WHERE repo=? AND state='closed' AND feedback_attempts<?"
        " AND (feedback_collected_at IS NULL OR (feedback_collected_at<last_seen_at+? AND last_seen_at+?<=?))"
        " AND EXISTS (SELECT 1 FROM jobs j WHERE j.repo=p.repo AND j.number=p.number AND j.review_id IS NOT NULL)"
        " ORDER BY feedback_attempts, last_seen_at LIMIT ?", (repo, MAX_ATTEMPTS, LATE, LATE, now, limit))]


def collect_pending(conn, svc, repo, limit=PER_POLL):
    """Collect the PRs pending() returns; returns how many of them failed."""
    return sum(collect(conn, svc, repo, number) is None for number in pending(conn, repo, svc.now(), limit))


def canonical_repo(conn, config, repo, number):
    """owner/repo as the database spells it for this PR (the configured name, for PRs the service knows), else
    as configured, else as given. GitHub ignores case; the database doesn't."""
    row = conn.execute("SELECT repo FROM prs WHERE repo=? COLLATE NOCASE AND number=? UNION ALL"
                       " SELECT repo FROM jobs WHERE repo=? COLLATE NOCASE AND number=? LIMIT 1",
                       (repo, number, repo, number)).fetchone()
    if row is not None:
        return row["repo"]
    repo_cfg = config.repo(repo)
    return repo_cfg.name if repo_cfg is not None else repo


def export(conn, state, repo=None, number=None):
    """Collected feedback with the review each item answers: its jobs (agent, model, effort, prompt version)
    and the file holding the review as posted."""
    where, params = [], []
    if repo is not None:
        where.append("f.repo=? COLLATE NOCASE AND f.number=?")
        params += [repo, number]
    rows = [dict(r) for r in conn.execute(
        "SELECT f.*, p.title, p.merged FROM feedback f LEFT JOIN prs p ON p.repo=f.repo AND p.number=f.number"
        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY f.repo, f.number, f.created_at, f.id",
        params)]
    for row in rows:
        row["merged"] = None if row["merged"] is None else bool(row["merged"])
        row["review"] = _review(conn, state, row["repo"], row.pop("review_id"))
    return rows


def _review(conn, state, repo, review_id):
    if review_id is None:
        return None
    jobs = [dict(r) for r in conn.execute(
        "SELECT id, agent, model, effort, prompt_version, review_kind, head_sha, posted_with, review_url FROM jobs"
        " WHERE repo=? AND review_id=? ORDER BY id", (repo, review_id))]
    body_file = None
    for job in jobs:  # the post's job: its round.md if it carried other reviews, else its review.md
        if job["posted_with"] in (None, job["id"]):
            out = Path(job_paths(state, job["id"]).out)
            name = "round.md" if job["posted_with"] == job["id"] else "review.md"
            body_file = str(out / name) if (out / name).is_file() else None
            break
    return {"id": review_id, "url": jobs[0]["review_url"] if jobs else None, "body_file": body_file,
            "jobs": [{key: job[key] for key in ("id", "agent", "model", "effort", "prompt_version", "review_kind",
                                                "head_sha")} for job in jobs]}
