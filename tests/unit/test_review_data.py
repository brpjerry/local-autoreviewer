# SPDX-License-Identifier: GPL-3.0-or-later
"""Review data kept for analysis: recording, backfill, retention, and the findings/triage commands."""

import contextlib
import io
import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import REPO, EnvTestCase, make_config
from local_autoreviewer import __main__ as cli
from local_autoreviewer import db, maintenance, pipeline, review

OLD_REVIEW = {  # the shape written before the calibrated schema: no impact, likelihood or trigger
    "summary": "One problem.",
    "findings": [{"severity": "minor", "category": "tests", "file": "t.py", "line": 4, "title": "Order-dependent test",
                  "detail": "d", "suggestion": "s"}],
    "test_gaps": [], "questions": ["Intended?"], "nits": [],
}


class ReviewDataTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.head = self.remote.open_pr(1, {"src/calc.py": "def divide(a, b):\n    return a / b if b else 0\n"})
        self.github.set_pr(1, self.head)
        self.svc = self.services()

    def run_job(self, cfg=None, post=True):
        svc = self.services(cfg) if cfg else self.svc
        conn = svc.connect()
        with db.transaction(conn):
            job_id = db.insert_job(conn, repo=REPO, number=1, head_sha=self.head, base_ref="main", agent="claude",
                                   model="opus", source="poll", not_before=0, now=self.clock(), post=post)
            db.update_job(conn, job_id, status="running", started_at=self.clock())
        conn.close()
        return job_id, pipeline.run_job(svc, job_id, pipeline.CancelToken())

    def rows(self, sql, *params):
        conn = self.svc.connect()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def saved_job(self, status, data):
        """A finished job whose review is on disk but not in the database, as before review data was kept."""
        conn = self.svc.connect()
        with db.transaction(conn):
            job_id = db.insert_job(conn, repo=REPO, number=1, head_sha=self.head, base_ref="main", agent="codex",
                                   model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, job_id, status=status, finished_at=self.clock())
        conn.close()
        out = Path(pipeline.job_paths(self.state, job_id).out)
        out.mkdir(parents=True)
        (out / "review.json").write_text(json.dumps(data))
        return job_id

    def record_saved_reviews(self):
        conn = self.svc.connect()
        try:
            return maintenance.record_saved_reviews(conn, self.svc)
        finally:
            conn.close()

    def cli(self, *argv):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"STATE_DIRECTORY": str(self.state)}), contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_a_review_is_recorded(self):
        job_id, status = self.run_job()
        self.assertEqual(status, "posted")
        (job,) = self.rows("SELECT * FROM jobs WHERE id=?", job_id)
        self.assertEqual((job["review_recorded"], job["effort"], job["prompt_version"], job["summary"]),
                         (1, self.cfg.agents["claude"].effort, review.prompt_version(),
                          "Adds a divide helper; one real bug."))
        (recorded,) = self.rows("SELECT reviewer_severity, readability, loc_delta, edits FROM findings"
                                " WHERE job_id=? AND kind='finding'", job_id)
        self.assertEqual({key: recorded[key] for key in ("reviewer_severity", "readability", "loc_delta")},
                         {"reviewer_severity": "major", "readability": "same", "loc_delta": 2})
        self.assertEqual([(e["file"], e["start_line"], e["inline"]) for e in json.loads(recorded["edits"])],
                         [("src/calc.py", 2, True)])
        findings = self.rows("SELECT kind, position, severity, impact, likelihood, occurs_when, file, line, title"
                             " FROM findings WHERE job_id=? ORDER BY kind, position", job_id)
        self.assertEqual(findings, [
            {"kind": "finding", "position": 0, "severity": "major", "impact": 6, "likelihood": "common",
             "occurs_when": "Any caller passes b=0.", "file": "src/calc.py", "line": 2,
             "title": "Division by zero is unhandled"},
            {"kind": "nit", "position": 0, "severity": None, "impact": None, "likelihood": None, "occurs_when": None,
             "file": "src/calc.py", "line": 1, "title": "Add a docstring."},
            {"kind": "test_gap", "position": 0, "severity": None, "impact": None, "likelihood": None,
             "occurs_when": None, "file": "", "line": 0, "title": "No test for b == 0."},
        ])

    def test_a_no_post_review_is_recorded_too(self):
        job_id, status = self.run_job(post=False)
        self.assertEqual(status, "skipped")
        self.assertEqual(len(self.rows("SELECT * FROM findings WHERE job_id=?", job_id)), 3)

    def test_a_blocked_review_is_never_recorded(self):
        job_id, status = self.run_job(make_config(self.tmp, claude="leak"))
        self.assertEqual(status, "blocked")
        self.assertEqual(self.rows("SELECT * FROM findings"), [])
        self.record_saved_reviews()  # nor by the backfill
        self.assertEqual(self.rows("SELECT * FROM findings"), [])
        self.assertEqual(self.rows("SELECT review_recorded FROM jobs WHERE id=?", job_id), [{"review_recorded": 0}])

    def test_housekeeping_records_reviews_saved_before_review_data_was_kept(self):
        job_id = self.saved_job("posted", OLD_REVIEW)
        maintenance.housekeeping(self.svc)
        self.assertEqual(
            self.rows("SELECT kind, severity, impact, likelihood, title FROM findings WHERE job_id=? ORDER BY kind",
                      job_id),
            [{"kind": "finding", "severity": "minor", "impact": None, "likelihood": None,
              "title": "Order-dependent test"},
             {"kind": "question", "severity": None, "impact": None, "likelihood": None, "title": "Intended?"}])
        (job,) = self.rows("SELECT review_recorded, effort, prompt_version, summary FROM jobs WHERE id=?", job_id)
        self.assertEqual(job, {"review_recorded": 1, "effort": "", "prompt_version": "", "summary": "One problem."})
        self.record_saved_reviews()  # recorded once
        self.assertEqual(len(self.rows("SELECT * FROM findings WHERE job_id=?", job_id)), 2)

    def test_an_unreadable_saved_review_is_skipped(self):
        job_id = self.saved_job("posted", {"summary": ""})
        self.assertEqual(self.record_saved_reviews(), 0)
        self.assertEqual(self.rows("SELECT review_recorded FROM jobs WHERE id=?", job_id), [{"review_recorded": 0}])

    def test_expiry_removes_the_checkout_but_keeps_the_review(self):
        job_id, _ = self.run_job()
        paths = pipeline.job_paths(self.state, job_id)
        self.assertTrue(Path(paths.worktree).is_dir())
        conn = self.svc.connect()
        with db.transaction(conn):
            db.update_job(conn, job_id, finished_at=self.clock() - self.cfg.service.retain_jobs - 1)
        conn.close()
        maintenance.housekeeping(self.svc)
        self.assertFalse(Path(paths.worktree).exists())
        self.assertFalse(Path(paths.context).exists())
        self.assertTrue(Path(paths.out, "review.json").is_file())
        self.assertTrue(Path(paths.out, "review.md").is_file())

    def test_triage_and_export(self):
        job_id, _ = self.run_job()
        (finding,) = self.rows("SELECT id FROM findings WHERE job_id=? AND kind='finding'", job_id)
        code, out = self.cli("triage", str(finding["id"]), "--impact", "3", "--disposition", "fixed",
                             "--commit", "abc1234", "--note", "guarded", "--by", "tester")
        self.assertEqual((code, out.strip()), (0, "triaged 1 finding(s)"))
        code, out = self.cli("findings", "--json", "--kind", "finding")
        (row,) = json.loads(out)
        self.assertEqual({key: row[key] for key in ("id", "impact", "rated_impact", "disposition", "fixed_in", "note",
                                                     "triaged_by", "repo", "number", "agent", "head_sha")},
                         {"id": finding["id"], "impact": 6, "rated_impact": 3, "disposition": "fixed",
                          "fixed_in": "abc1234", "note": "guarded", "triaged_by": "tester", "repo": REPO,
                          "number": 1, "agent": "claude", "head_sha": self.head})
        self.assertEqual(row["triaged_at"], row["triaged_at"] and int(row["triaged_at"]))
        code, out = self.cli("findings", "--untriaged", "--json")
        self.assertEqual(sorted(r["kind"] for r in json.loads(out)), ["nit", "test_gap"])
        code, out = self.cli("findings", f"{REPO.upper()}#1")
        self.assertIn("major 6/10  [fixed 3/10]", out)

    def test_triage_keeps_fields_it_is_not_given(self):
        job_id, _ = self.run_job()
        (finding,) = self.rows("SELECT id FROM findings WHERE job_id=? AND kind='finding'", job_id)
        self.cli("triage", str(finding["id"]), "--impact", "2")
        self.cli("triage", str(finding["id"]), "--disposition", "declined")
        (row,) = self.rows("SELECT rated_impact, disposition FROM findings WHERE id=?", finding["id"])
        self.assertEqual(row, {"rated_impact": 2, "disposition": "declined"})

    def test_triage_rejects_bad_input_and_changes_nothing(self):
        job_id, _ = self.run_job()
        ids = [str(r["id"]) for r in self.rows("SELECT id FROM findings WHERE job_id=?", job_id)]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli("triage", ids[0], "999999", "--disposition", "fixed")[0], 2)  # all or nothing
            self.assertEqual(self.cli("triage", ids[0])[0], 2)  # nothing to record
            for bad in (["--impact", "11"], ["--disposition", "maybe"]):
                with self.subTest(bad=bad), self.assertRaises(SystemExit):
                    self.cli("triage", ids[0], *bad)
        self.assertEqual(self.rows("SELECT * FROM findings WHERE disposition IS NOT NULL OR rated_impact IS NOT NULL"),
                         [])


class MigrationTests(EnvTestCase):
    def test_an_existing_database_gains_review_data(self):
        path = self.state / "old.db"
        old = sqlite3.connect(path, isolation_level=None)
        for statement in db._statements(db.MIGRATIONS[0]):
            old.execute(statement)
        old.execute("PRAGMA user_version=1")
        old.execute("INSERT INTO jobs (repo, number, head_sha, base_ref, agent, source, status, created_at)"
                    " VALUES ('o/r', 1, 'abc', 'main', 'claude', 'poll', 'posted', 0)")
        old.close()
        conn = db.connect(path)
        try:
            (job,) = conn.execute("SELECT effort, prompt_version, summary, review_recorded FROM jobs").fetchall()
            self.assertEqual(tuple(job), ("", "", None, 0))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
