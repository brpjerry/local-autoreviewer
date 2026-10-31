# SPDX-License-Identifier: GPL-3.0-or-later
import unittest

from tests.helpers import OWNER, REPO, EnvTestCase, make_config
from local_autoreviewer import db, events
from local_autoreviewer.triggers import poll as poll_mod


class DbTests(EnvTestCase):
    def test_migrate_and_too_new(self):
        conn = db.connect(self.state / "s.db")
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
        conn.close()
        with self.assertRaises(db.SchemaTooNew):
            db.connect(self.state / "s.db")


class PollTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.svc = self.services(make_config(self.tmp, service={"settle": "3m", "bootstrap": "review"}))

    def poll(self):
        results, failures = poll_mod.poll(self.svc)
        self.assertEqual(failures, 0)
        return [(e.number, e.action) for e in results.get(REPO, [])]

    def jobs(self, *where):
        conn = self.svc.connect()
        try:
            sql = "SELECT * FROM jobs" + (" WHERE " + where[0] if where else "") + " ORDER BY id"
            return [dict(r) for r in conn.execute(sql, where[1:])]
        finally:
            conn.close()

    def test_lifecycle(self):
        self.github.set_pr(1, "a" * 40)
        self.assertEqual(self.poll(), [(1, "opened")])
        queued = self.jobs("status='queued'")
        self.assertEqual({j["agent"] for j in queued}, {"claude", "codex"})
        self.assertTrue(all(j["not_before"] == self.clock() + 180 for j in queued))

        self.assertEqual(self.poll(), [])  # no change, no event

        self.clock.advance(60)
        self.github.set_pr(1, "b" * 40)  # push
        self.assertEqual(self.poll(), [(1, "synchronize")])
        self.assertEqual({j["status"] for j in self.jobs(f"head_sha='{'a' * 40}'")}, {"superseded"})
        self.assertEqual(len(self.jobs(f"status='queued' AND head_sha='{'b' * 40}'")), 2)

        self.github.set_pr(1, "b" * 40, draft=True)
        self.assertEqual(self.poll(), [(1, "converted_to_draft")])
        self.assertEqual({j["status"] for j in self.jobs(f"head_sha='{'b' * 40}'")}, {"cancelled"})

        self.github.set_pr(1, "b" * 40, draft=False)
        self.assertEqual(self.poll(), [(1, "ready_for_review")])
        requeued = self.jobs(f"status='queued' AND head_sha='{'b' * 40}'")
        self.assertEqual([j["rerun"] for j in requeued], [1, 1])  # cancelled head is reviewed again

        self.github.set_pr(1, "b" * 40, state="CLOSED")
        self.assertEqual(self.poll(), [(1, "closed")])
        self.assertEqual(self.jobs("status IN ('queued','running')"), [])

    def test_running_job_gets_cancel_request(self):
        self.github.set_pr(2, "c" * 40)
        self.poll()
        conn = self.svc.connect()
        with db.transaction(conn):
            conn.execute("UPDATE jobs SET status='running'")
        conn.close()
        self.github.set_pr(2, "d" * 40)
        self.poll()
        running = self.jobs("status='running'")
        self.assertTrue(all(j["cancel_requested"] == 1 and j["reason"].startswith("superseded") for j in running))

    def test_author_and_draft_filtering(self):
        self.github.set_pr(3, "e" * 40, author="mallory")
        self.github.set_pr(4, "f" * 40, draft=True)
        self.poll()
        self.assertEqual(self.jobs(), [])

    def test_baseline_bootstrap(self):
        svc = self.services(make_config(self.tmp, service={"bootstrap": "baseline"}))
        self.github.set_pr(5, "1" * 40)
        results, _ = poll_mod.poll(svc)
        self.assertEqual(results[REPO], [])
        self.github.set_pr(5, "2" * 40)
        results, _ = poll_mod.poll(svc)
        self.assertEqual([(e.number, e.action) for e in results[REPO]], [(5, "synchronize")])

    def test_retarget_triggers_review(self):
        self.github.set_pr(6, "9" * 40)
        self.poll()
        self.github.set_pr(6, "9" * 40, base="develop")
        self.assertEqual(self.poll(), [(6, "retargeted")])

    def test_dry_run_records_nothing(self):
        self.github.set_pr(7, "8" * 40)
        results, _ = poll_mod.poll(self.svc, dry_run=True)
        self.assertEqual([(e.number, e.action) for e in results[REPO]], [(7, "opened")])
        self.assertEqual(self.jobs(), [])


class ManualIngestTests(EnvTestCase):
    def ingest(self, **kw):
        svc = self.services()
        event = events.PrEvent(repo=REPO, number=9, action="opened", head_sha="a" * 40, base_ref="main",
                               is_draft=False, author="someone-else", source="manual", **kw)
        conn = svc.connect()
        with db.transaction(conn):
            created = events.ingest(conn, self.cfg, event, now=self.clock(), token_owner=OWNER)
        conn.close()
        return created

    def test_manual_bypasses_author_gate_and_dedupes(self):
        self.assertEqual(len(self.ingest()), 2)
        self.assertEqual(self.ingest(), [])
        self.assertEqual(self.ingest(force=True), [])  # still in flight: no duplicate
        self.assertEqual(len(self.ingest(agents=("claude",))), 0)

    def test_a_foreground_review_does_not_count_as_reviewed(self):
        svc = self.services()
        conn = svc.connect()
        with db.transaction(conn):  # a no-post `review` trial of the same head
            trial = db.insert_job(conn, repo=REPO, number=9, head_sha="a" * 40, base_ref="main", agent="claude",
                                  model="", source=db.FOREGROUND, not_before=0, now=self.clock(), post=False)
            db.update_job(conn, trial, status="skipped")
        conn.close()
        self.assertEqual(len(self.ingest(agents=("claude",))), 1)
        self.assertEqual(self.ingest(agents=("claude",)), [])

    def test_unconfigured_repo_ignored(self):
        svc = self.services()
        event = events.PrEvent(repo="x/y", number=1, action="opened", head_sha="a" * 40, base_ref="main",
                               is_draft=False, author=OWNER, source="poll")
        conn = svc.connect()
        with db.transaction(conn):
            self.assertEqual(events.ingest(conn, self.cfg, event, now=1, token_owner=OWNER), [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
