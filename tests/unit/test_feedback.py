# SPDX-License-Identifier: GPL-3.0-or-later
"""Feedback on posted reviews: the request every review ends with, and its collection when the PR closes."""

import contextlib
import io
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import OWNER, REPO, EnvTestCase, iso, make_config
from local_autoreviewer import __main__ as cli
from local_autoreviewer import db, feedback, pipeline, review
from local_autoreviewer.github import GitHubError
from local_autoreviewer.triggers import poll as poll_mod

SAMPLE = {"summary": "One problem.", "findings": [], "test_gaps": [], "questions": [], "nits": []}


class RequestTests(unittest.TestCase):
    def test_every_posted_review_asks_for_feedback(self):
        structured = review.validate(SAMPLE)
        full = review.render(structured, repo="o/r", number=5, sha="a" * 40, base_ref="main", agent="claude",
                             model="opus", job_id=42)
        followup = review.render_followup(review.validate_followup({**SAMPLE, "resolutions": [], "regressions": []},
                                                                   set()),
                                          [], repo="o/r", number=5, sha="b" * 40, previous_sha="a" * 40,
                                          agent="codex", model="", job_id=43)
        for body in (full, followup):
            self.assertIn(review.FEEDBACK_REQUEST, body)
            self.assertLess(body.index(review.FEEDBACK_REQUEST), body.index("<!-- local-autoreviewer"))
        skipped = review.render_skipped(reason="too big", sha="a" * 40, job_id=44, agent="claude")
        self.assertNotIn(review.FEEDBACK_REQUEST, skipped)

    def test_what_counts_as_feedback(self):
        for body in ("Review feedback: 1 was right", "From: Opus 5.5\n\n**Review feedback**\n- 2 was noise",
                     "## Review feedback\nToo long.", "review feedback — fine", "_Review feedback_: ok"):
            self.assertTrue(feedback.is_feedback(body), body)
        for body in ("", None, "LGTM", "> Review feedback: quoted", "Some review feedback, mid-line",
                     "Review feedbacks", f"Review feedback\n{review.marker(1, 'a', 'claude')}",
                     review.FEEDBACK_REQUEST):
            self.assertFalse(feedback.is_feedback(body), body)


class CollectionTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.head = self.remote.open_pr(1, {"src/calc.py": "def divide(a, b):\n    return a / b if b else 0\n"})
        self.github.set_pr(1, self.head)
        self.svc = self.services(make_config(self.tmp, agents=("claude",)))
        self.assertEqual(self.poll(), [])  # the baseline: PR 1 is known and open, nothing queued

    def poll(self, dry_run=False):
        results, failures = poll_mod.poll(self.svc, dry_run=dry_run)
        self.assertEqual(failures, 0)
        return [(e.number, e.action) for e in results.get(REPO, [])]

    def post_review(self):
        conn = self.svc.connect()
        with db.transaction(conn):
            job_id = db.insert_job(conn, repo=REPO, number=1, head_sha=self.head, base_ref="main", agent="claude",
                                   model="opus", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, job_id, status="running", started_at=self.clock())
        conn.close()
        self.assertEqual(pipeline.run_job(self.svc, job_id, pipeline.CancelToken()), "posted")
        return job_id, self.github.posted[-1]

    def rows(self, sql, *params):
        conn = self.svc.connect()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def close(self, state="MERGED"):
        self.github.set_pr(1, self.head, state=state)
        return self.poll()

    def collected(self):
        return self.rows("SELECT source, author, body, review_id, context FROM feedback ORDER BY created_at, source")

    def cli(self, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"STATE_DIRECTORY": str(self.state)}), contextlib.redirect_stdout(out), \
                mock.patch.object(cli, "load_config", return_value=self.svc.config), \
                mock.patch.object(cli, "build_services", return_value=self.svc):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_feedback_is_collected_when_the_pr_merges(self):
        _job_id, posted = self.post_review()
        suggestion = self.github.review_comments[(REPO, 1)][0]
        self.clock.advance(60)
        self.github.comment(1, "From: Opus 5.5\n\n**Review feedback:** 1 was right, the nit was noise.")
        self.github.comment(1, "LGTM")
        self.github.comment(1, "> Review feedback: quoted, not feedback")
        self.github.comment(1, "Fixed in abc1234.", reply_to=suggestion["id"])
        self.github.reviews[(REPO, 1)] = [{"id": 7, "user": {"login": "bob"}, "body": "## Review feedback\nToo long.",
                                           "submitted_at": iso(self.clock()), "html_url": "u"}]
        self.clock.advance(60)
        for body in ("Review feedback: it missed this.", "Rename this?"):  # inline comments outside our threads
            self.github.review_comments[(REPO, 1)].append(
                {"id": len(body), "pull_request_review_id": 8, "path": "src/calc.py", "line": 1, "body": body,
                 "user": {"login": "bob"}, "created_at": iso(self.clock()), "html_url": "u"})
        self.assertEqual(self.close(), [(1, "closed")])
        self.assertEqual(self.collected(), [
            {"source": "comment", "author": OWNER, "body": "From: Opus 5.5\n\n**Review feedback:** 1 was right, the "
             "nit was noise.", "review_id": posted["id"], "context": None},
            {"source": "reply", "author": OWNER, "body": "Fixed in abc1234.", "review_id": posted["id"],
             "context": "src/calc.py:2 **1. Division by zero is unhandled** (local-autoreviewer)"},
            {"source": "review", "author": "bob", "body": "## Review feedback\nToo long.", "review_id": posted["id"],
             "context": None},
            {"source": "inline", "author": "bob", "body": "Review feedback: it missed this.", "review_id": posted["id"],
             "context": "src/calc.py:1"},
        ])
        (pr,) = self.rows("SELECT merged, feedback_collected_at, feedback_attempts FROM prs WHERE number=1")
        self.assertEqual(pr, {"merged": 1, "feedback_collected_at": self.clock(), "feedback_attempts": 0})
        reads = list(self.github.reads)
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.github.reads, reads)  # collected once

    def test_feedback_answers_the_latest_review_posted_before_it(self):
        self.github.comment(1, "Review feedback: before any review")
        self.clock.advance(10)
        _first, first = self.post_review()
        self.clock.advance(10)
        self.github.comment(1, "Review feedback: on the first")
        self.clock.advance(10)
        self.github.posted.append({**first, "id": 5000, "submitted_at": iso(self.clock())})  # a second round
        self.clock.advance(10)
        self.github.comment(1, "Review feedback: on the second")
        rows = feedback.gather(self.github, REPO, 1, OWNER)
        self.assertEqual([(row["body"], row["review_id"]) for row in rows],
                         [("Review feedback: before any review", None), ("Review feedback: on the first", first["id"]),
                          ("Review feedback: on the second", 5000)])

    def test_a_failed_collection_is_retried_then_given_up(self):
        self.post_review()
        self.github.fail = GitHubError("gh api failed: HTTP 502")
        self.assertEqual(self.close(), [(1, "closed")])
        for attempt in range(2, feedback.MAX_ATTEMPTS + 1):
            self.poll()
            (pr,) = self.rows("SELECT feedback_collected_at, feedback_attempts FROM prs WHERE number=1")
            self.assertEqual(pr, {"feedback_collected_at": None, "feedback_attempts": attempt})
        reads = len(self.github.reads)
        self.github.fail = None
        self.poll()
        self.assertEqual(len(self.github.reads), reads)  # given up

    def test_a_reopened_pr_is_collected_again_when_it_closes(self):
        self.post_review()
        self.github.comment(1, "Review feedback: first")
        self.close()
        self.github.set_pr(1, self.head)
        self.assertEqual(self.poll(), [(1, "reopened")])
        self.github.comment(1, "Review feedback: second")
        self.github.issue_comments[(REPO, 1)][0]["body"] = "Review feedback: first, edited"
        self.close(state="CLOSED")
        self.assertEqual([row["body"] for row in self.collected()],
                         ["Review feedback: first, edited", "Review feedback: second"])
        (pr,) = self.rows("SELECT merged FROM prs WHERE number=1")
        self.assertEqual(pr["merged"], 0)

    def test_only_reviewed_prs_are_collected_and_not_on_a_dry_run(self):
        self.github.set_pr(1, self.head, state="MERGED")
        self.assertEqual(self.poll(dry_run=True), [(1, "closed")])
        self.assertEqual(self.close(), [(1, "closed")])  # nothing of ours was posted on it
        self.assertEqual(self.github.reads, [])
        self.assertEqual(feedback.pending(self.svc.connect(), REPO), [])

    def test_github_is_called_without_holding_the_write_lock(self):
        self.post_review()
        probes = []

        def probe(call):
            def wrapped(*args, **kwargs):
                other = sqlite3.connect(self.state / "state.sqlite3", timeout=0, isolation_level=None)
                try:
                    other.execute("BEGIN IMMEDIATE")  # "database is locked" if the collector holds the write lock
                    other.execute("ROLLBACK")
                finally:
                    other.close()
                probes.append(call.__name__)
                return call(*args, **kwargs)
            return wrapped

        for name in ("pr_view", "list_reviews", "list_issue_comments", "list_review_comments"):
            setattr(self.github, name, probe(getattr(self.github, name)))
        self.close()
        self.assertEqual(probes, ["pr_view", "pr_view", "list_reviews", "list_issue_comments",
                                  "list_review_comments"])

    def test_closed_prs_from_before_the_migration_are_collected(self):
        path = self.state / "old.db"
        old = sqlite3.connect(path, isolation_level=None)
        for script in db.MIGRATIONS[:5]:
            for statement in db._statements(script):
                old.execute(statement)
        old.execute("PRAGMA user_version=5")
        old.execute("INSERT INTO prs (repo, number, state, is_draft, head_sha, base_ref, author, head_changed_at,"
                    " last_seen_at) VALUES (?, 1, 'closed', 0, 'abc', 'main', ?, 0, 0)", (REPO, OWNER))
        old.execute("INSERT INTO jobs (repo, number, head_sha, base_ref, agent, source, status, created_at, review_id)"
                    " VALUES (?, 1, 'abc', 'main', 'claude', 'poll', 'posted', 0, 99)", (REPO,))
        old.close()
        conn = db.connect(path)
        try:
            self.assertEqual(feedback.pending(conn, REPO), [1])
        finally:
            conn.close()

    def test_export(self):
        job_id, posted = self.post_review()
        self.github.comment(1, "Review feedback: useful.")
        self.close()
        code, out = self.cli("feedback", "--json")
        self.assertEqual(code, 0)
        (item,) = json.loads(out)
        self.assertEqual({key: item[key] for key in ("repo", "number", "title", "merged", "source", "body")},
                         {"repo": REPO, "number": 1, "title": "Add feature", "merged": True, "source": "comment",
                          "body": "Review feedback: useful."})
        body_file = str(Path(pipeline.job_paths(self.state, job_id).out, "review.md"))
        self.assertEqual(item["review"], {
            "id": posted["id"], "url": posted["html_url"], "body_file": body_file,
            "jobs": [{"id": job_id, "agent": "claude", "model": "opus",
                      "effort": self.svc.config.agents["claude"].effort, "prompt_version": review.prompt_version(),
                      "review_kind": "full", "head_sha": self.head}]})
        self.assertEqual(Path(body_file).read_text(), posted["body"])
        code, out = self.cli("feedback", f"{REPO}#1")
        self.assertEqual(code, 0)
        self.assertIn(f"{REPO}#1 (merged)  comment by {OWNER}", out)
        self.assertIn(f"on {posted['html_url']} ({job_id} claude)", out)
        self.assertIn("| Review feedback: useful.", out)
        self.assertEqual(self.cli("feedback", f"{REPO}#2", "--json"), (0, "[]\n"))

    def test_collect_one_pr_now(self):
        self.post_review()
        self.github.comment(1, "Review feedback: early.")
        code, out = self.cli("feedback", "--collect", f"{REPO}#1", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([item["body"] for item in json.loads(out)], ["Review feedback: early."])
        (pr,) = self.rows("SELECT state, merged FROM prs WHERE number=1")
        self.assertEqual(pr, {"state": "open", "merged": None})  # still open: not merged, not closed either
        self.github.fail = GitHubError("gh api failed: HTTP 404")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.cli("feedback", "--collect", f"{REPO}#1")[0], 2)
        self.assertIn("cannot collect the feedback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
