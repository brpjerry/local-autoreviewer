# SPDX-License-Identifier: GPL-3.0-or-later
import fcntl
import sqlite3
import threading
import unittest
from unittest import mock

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

    def test_connect_closes_its_connection_when_the_schema_is_too_new(self):
        conn = db.connect(self.state / "s.db")
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
        conn.close()
        opened, real_connect = [], sqlite3.connect

        def connect(*args, **kwargs):
            opened.append(real_connect(*args, **kwargs))
            return opened[-1]

        with mock.patch.object(db.sqlite3, "connect", side_effect=connect), self.assertRaises(db.SchemaTooNew):
            db.connect(self.state / "s.db")
        with self.assertRaises(sqlite3.ProgrammingError):  # "Cannot operate on a closed database"
            opened[0].execute("SELECT 1")

    def test_migrate_does_not_need_the_write_lock_when_current(self):
        db.connect(self.state / "s.db").close()
        writer = sqlite3.connect(self.state / "s.db", isolation_level=None)
        reader = sqlite3.connect(self.state / "s.db", isolation_level=None, timeout=0)
        writer.execute("BEGIN IMMEDIATE")
        try:
            db.migrate(reader)  # raises "database is locked" if it takes the write lock
        finally:
            writer.execute("ROLLBACK")
            writer.close()
            reader.close()


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

    def test_github_is_called_without_holding_the_write_lock(self):
        probes = []

        def probe(call):
            def wrapped(*args, **kwargs):
                # Fails at once with "database is locked" if the poller holds the write lock.
                other = sqlite3.connect(self.state / "state.sqlite3", timeout=0, isolation_level=None)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("ROLLBACK")
                finally:
                    other.close()
                probes.append(call.__name__)
                return call(*args, **kwargs)
            return wrapped

        self.github.set_pr(1, "a" * 40)
        self.poll()
        self.github.set_pr(1, "a" * 40, state="CLOSED")  # gone from the open list: poll confirms with pr_view
        self.github.list_open_prs = probe(self.github.list_open_prs)
        self.github.pr_view = probe(self.github.pr_view)
        self.assertEqual(self.poll(), [(1, "closed")])
        self.assertEqual(probes, ["list_open_prs", "pr_view"])

    def test_polls_run_one_at_a_time(self):
        self.github.set_pr(1, "a" * 40)
        started, result = threading.Event(), {}
        listing = self.github.list_open_prs

        def list_open_prs(repo, limit=500):
            started.set()
            return listing(repo, limit)

        self.github.list_open_prs = list_open_prs
        with poll_mod.poll_lock(self.state):  # another poll is running
            thread = threading.Thread(target=lambda: result.setdefault("r", poll_mod.poll(self.svc)))
            thread.start()
            self.assertFalse(started.wait(0.3))
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(started.is_set())
        self.assertEqual(result["r"][1], 0)

    def test_enqueue_waits_for_a_running_poll(self):
        import argparse
        from unittest import mock
        from local_autoreviewer import __main__ as cli
        self.github.set_pr(1, "a" * 40)
        started, result = threading.Event(), {}
        view = self.github.pr_view

        def pr_view(repo, number):
            started.set()
            return view(repo, number)

        self.github.pr_view = pr_view
        args = argparse.Namespace(ref=f"{REPO}#1", agent=None, force=False)
        with mock.patch.object(cli, "load_config", return_value=self.svc.config), \
                mock.patch.object(cli, "build_services", return_value=self.svc):
            with poll_mod.poll_lock(self.state):  # a poll is between its fetch and its apply
                thread = threading.Thread(target=lambda: result.setdefault("r", cli.cmd_enqueue(args)))
                thread.start()
                self.assertFalse(started.wait(0.3))
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["r"], 0)
        self.assertEqual(len(self.jobs("status='queued'")), 2)

    def test_a_failed_fetch_changes_nothing_and_releases_the_lock(self):
        from local_autoreviewer.github import GitHubError
        self.github.set_pr(1, "a" * 40)
        self.poll()
        self.github.set_pr(1, "a" * 40, state="CLOSED")
        real_view = self.github.pr_view

        def failing_view(repo, number):
            raise GitHubError("HTTP 502")

        self.github.pr_view = failing_view
        results, failures = poll_mod.poll(self.svc)
        self.assertEqual((results, failures), ({}, 1))
        self.assertEqual(len(self.jobs("status='queued'")), 2)  # the open PR's jobs are untouched
        with open(self.state / "poll.lock") as handle:  # raises BlockingIOError if still held
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.github.pr_view = real_view
        self.assertEqual(self.poll(), [(1, "closed")])

    def test_an_unconfirmed_open_pr_is_left_for_the_next_poll(self):
        conn = self.svc.connect()
        with db.transaction(conn):
            conn.execute("INSERT INTO prs (repo, number, state, is_draft, head_sha, base_ref, author, title,"
                         " head_changed_at, last_seen_at) VALUES (?, 5, 'open', 0, ?, 'main', ?, '', 0, 0)",
                         (REPO, "c" * 40, OWNER))
            produced = poll_mod.poll_repo(conn, self.svc, self.svc.config.repo(REPO), self.clock(),
                                          prs=[], gone={})
            state = conn.execute("SELECT state FROM prs WHERE number=5").fetchone()[0]
        conn.close()
        self.assertEqual((produced, state), ([], "open"))

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

    def foreground_job(self, status, post):
        svc = self.services()
        conn = svc.connect()
        with db.transaction(conn):
            job_id = db.insert_job(conn, repo=REPO, number=9, head_sha="a" * 40, base_ref="main", agent="claude",
                                   model="", source=db.FOREGROUND, not_before=0, now=self.clock(), post=post)
            db.update_job(conn, job_id, status=status)
        conn.close()

    def test_a_no_post_foreground_trial_does_not_count_as_reviewed(self):
        self.foreground_job("skipped", post=False)
        self.assertEqual(len(self.ingest(agents=("claude",))), 1)
        self.assertEqual(self.ingest(agents=("claude",)), [])

    def test_a_running_trial_does_not_block_a_forced_review(self):
        self.foreground_job("running", post=False)
        self.assertEqual(len(self.ingest(agents=("claude",), force=True)), 1)

    def test_a_posted_foreground_review_counts(self):
        self.foreground_job("posted", post=True)
        self.assertEqual(self.ingest(agents=("claude",)), [])

    def test_a_running_foreground_review_with_post_blocks_a_duplicate(self):
        self.foreground_job("running", post=True)
        self.assertEqual(self.ingest(agents=("claude",)), [])
        self.assertEqual(self.ingest(agents=("claude",), force=True), [])

    def test_a_draft_poll_keeps_reviews_requested_for_that_draft(self):
        self.foreground_job("running", post=False)
        svc = self.services()
        conn = svc.connect()
        with db.transaction(conn):
            enqueued = db.insert_job(conn, repo=REPO, number=9, head_sha="a" * 40, base_ref="main", agent="codex",
                                     model="", source="manual", not_before=0, now=self.clock())
        self.assertIsNotNone(enqueued)

        def poll_sees(head, action="synchronize", base="main"):
            event = events.PrEvent(repo=REPO, number=9, action=action, head_sha=head, base_ref=base,
                                   is_draft=True, author=OWNER, source="poll")
            with db.transaction(conn):
                events.ingest(conn, self.cfg, event, now=self.clock(), token_owner=OWNER)
            return {row["agent"]: (row["status"], row["cancel_requested"])
                    for row in conn.execute("SELECT * FROM jobs WHERE number=9")}

        try:
            self.assertEqual(poll_sees("a" * 40), {"claude": ("running", 0), "codex": ("queued", 0)})
            self.assertEqual(poll_sees("a" * 40, "retargeted", base="release"),
                             {"claude": ("running", 1), "codex": ("cancelled", 0)})
        finally:
            conn.close()

    def test_a_new_head_on_a_draft_stops_requested_reviews(self):
        self.foreground_job("running", post=False)
        svc = self.services()
        conn = svc.connect()
        event = events.PrEvent(repo=REPO, number=9, action="synchronize", head_sha="b" * 40, base_ref="main",
                               is_draft=True, author=OWNER, source="poll")
        with db.transaction(conn):
            events.ingest(conn, self.cfg, event, now=self.clock(), token_owner=OWNER)
        row = conn.execute("SELECT status, cancel_requested FROM jobs WHERE number=9").fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("running", 1))

    def test_converting_to_draft_keeps_requested_reviews(self):
        self.foreground_job("running", post=False)
        svc = self.services()
        conn = svc.connect()
        event = events.PrEvent(repo=REPO, number=9, action="converted_to_draft", head_sha="a" * 40,
                               base_ref="main", is_draft=True, author=OWNER, source="poll")
        with db.transaction(conn):
            events.ingest(conn, self.cfg, event, now=self.clock(), token_owner=OWNER)
        row = conn.execute("SELECT status, cancel_requested FROM jobs WHERE number=9").fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("running", 0))

    def test_closing_a_draft_stops_requested_reviews(self):
        self.foreground_job("running", post=False)
        svc = self.services()
        conn = svc.connect()
        event = events.PrEvent(repo=REPO, number=9, action="closed", head_sha="a" * 40, base_ref="main",
                               is_draft=True, author=OWNER, source="poll")
        with db.transaction(conn):
            events.ingest(conn, self.cfg, event, now=self.clock(), token_owner=OWNER)
        row = conn.execute("SELECT status, cancel_requested FROM jobs WHERE number=9").fetchone()
        conn.close()
        self.assertEqual(tuple(row), ("running", 1))

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
