# SPDX-License-Identifier: GPL-3.0-or-later
"""One post per commit: the reviews of a commit are posted together, duplicates merged and credited to
every reviewer that found them. And reviewers may end the review of a trivial PR early."""

import argparse
import contextlib
import io
import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import REPO, EnvTestCase, make_config
from local_autoreviewer import combine, db, events, pipeline, review, suggestions
from local_autoreviewer.github import GitHubError
from local_autoreviewer.worker import Worker

CALC = "def divide(a, b):\n    return a / b\n\n\ndef half(x):\n    return x / 2\n"
FIXED = "def divide(a, b):\n    if b == 0:\n        raise ValueError('b must not be zero')\n    return a / b\n"


def run_worker_posts(worker, svc, step):
    step(svc)
    for thread, _token, _agent in list(worker.threads.values()):
        thread.join(30)
    worker.reap()


class RoundTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.head = self.remote.open_pr(1, {"src/calc.py": CALC})
        self.github.set_pr(1, self.head)
        self.cfg = make_config(self.tmp)

    def queue(self, cfg=None, agents=("claude", "codex"), head=None, status="running"):
        svc = self.services(cfg or self.cfg)
        conn = svc.connect()
        ids = []
        with db.transaction(conn):
            for agent in agents:
                job_id = db.insert_job(conn, repo=REPO, number=1, head_sha=head or self.head, base_ref="main",
                                       agent=agent, model=svc.config.agents[agent].model, source="poll",
                                       not_before=0, now=self.clock(),
                                       rerun=db.next_rerun(conn, REPO, 1, head or self.head, "main", agent))
                db.update_job(conn, job_id, status=status, started_at=self.clock())
                ids.append(job_id)
        conn.close()
        return svc, ids

    def run_job(self, svc, job_id):
        return pipeline.run_job(svc, job_id, pipeline.CancelToken())

    def job(self, svc, job_id):
        conn = svc.connect()
        try:
            return dict(db.get_job(conn, job_id))
        finally:
            conn.close()

    def rows(self, svc, sql, *params):
        conn = svc.connect()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def checks(self, agent):
        return [call for call in self.agent_log(agent) if "Your only job is to find the duplicates" in call["prompt"]]

    def test_two_reviewers_of_one_type_are_posted_as_one(self):
        cfg = make_config(self.tmp, agents=("codex", "codex-sol"), further={"codex-sol": {"model": "gpt-6.1-sol"}})
        svc, (sol, codex) = self.queue(cfg, agents=("codex-sol", "codex"))  # sol's job first: shown second anyway
        self.assertEqual(self.run_job(svc, sol), "waiting")
        self.assertIn("waiting to post it with Codex's review", self.job(svc, sol)["reason"])
        self.assertEqual(self.run_job(svc, codex), "posted")

        (posted,) = self.github.posted
        body = posted["body"]
        self.assertIn("### Automated review · Codex and Codex sol (gpt-6.1-sol)", body)
        self.assertLess(body.index("**Codex:** Adds a divide helper"), body.index("**Codex sol:** Adds a divide helper"))
        self.assertIn("_(Codex and Codex sol; correctness, impact 6/10, common)_", body)
        for job_id, agent in ((sol, "codex-sol"), (codex, "codex")):
            self.assertIn(review.marker(job_id, self.head, agent), body)
        self.assertIn("· Codex and Codex sol</sub>", body)
        self.assertEqual(self.job(svc, sol)["model"], "gpt-6.1-sol")

    def test_both_reviews_are_posted_as_one(self):
        svc, (claude, codex) = self.queue()
        self.assertEqual(self.run_job(svc, claude), "waiting")
        self.assertEqual(self.github.posted, [])
        self.assertIn("waiting to post it with Codex's review", self.job(svc, claude)["reason"])
        self.assertEqual(self.run_job(svc, codex), "posted")

        (posted,) = self.github.posted
        body = posted["body"]
        self.assertIn("### Automated review · Claude Code (opus) and Codex", body)
        self.assertIn("**Claude Code:** Adds a divide helper", body)
        self.assertIn("**Codex:** Adds a divide helper", body)
        self.assertEqual(body.count("Division by zero is unhandled"), 1)
        self.assertIn("_(Claude Code and Codex; correctness, impact 6/10, common)_", body)
        self.assertIn("- No test for b == 0. _(Claude Code and Codex)_", body)
        self.assertIn("<details><summary>Nits (1)</summary>", body)
        for job_id, agent in ((claude, "claude"), (codex, "codex")):
            self.assertIn(review.marker(job_id, self.head, agent), body)
        self.assertIn(f"local-autoreviewer · jobs {claude}, {codex} · Claude Code and Codex", body)
        # One suggestion for the finding and one for the nit, each credited to both reviewers.
        self.assertEqual([(c["path"], c["line"]) for c in posted["comments"]], [("src/calc.py", 2), ("src/calc.py", 1)])
        self.assertIn("**1. Division by zero is unhandled** (local-autoreviewer · Claude Code and Codex)",
                      posted["comments"][0]["body"])

        jobs = [self.job(svc, job_id) for job_id in (claude, codex)]
        self.assertEqual([(j["status"], j["posted_with"], j["review_id"]) for j in jobs],
                         [("posted", codex, posted["id"])] * 2)
        out = Path(pipeline.job_paths(self.state, codex).out)
        self.assertEqual((out / "round.md").read_text(), body)
        self.assertEqual(json.loads((out / "round.json").read_text())["members"], [claude, codex])

        # Each reviewer's own review is kept; the repeats point at what was posted for them.
        rows = self.rows(svc, "SELECT id, job_id, kind, duplicate_of FROM findings ORDER BY id")
        kept = {row["kind"]: row["id"] for row in rows if row["job_id"] == claude}
        self.assertEqual({row["kind"]: row["duplicate_of"] for row in rows if row["job_id"] == codex}, kept)
        self.assertTrue(all(row["duplicate_of"] is None for row in rows if row["job_id"] == claude))

        # The review that finished last ran the check, once, with the same locked invocation as its review.
        self.assertEqual(self.checks("claude"), [])
        (check,) = self.checks("codex")
        self.assertIn("--ignore-user-config", check["argv"])
        self.assertEqual(check["argv"][check["argv"].index("--output-schema") + 1],
                         str(Path(pipeline.job_paths(self.state, codex).context, "duplicates.schema.json")))
        self.assertIn('"reviewer": "Claude Code"', check["prompt"])
        self.assertNotIn("$", check["prompt"].split("## Items")[0])

    def test_a_failed_check_posts_the_reviews_unmerged(self):
        svc, (claude, codex) = self.queue(make_config(self.tmp, codex="dupfail"))
        self.run_job(svc, claude)
        self.assertEqual(self.run_job(svc, codex), "posted")
        (posted,) = self.github.posted
        self.assertEqual(posted["body"].count("**Division by zero is unhandled**"), 2)
        self.assertIn("weren't checked for duplicates (the check failed)", posted["body"])
        self.assertEqual(self.rows(svc, "SELECT id FROM findings WHERE duplicate_of IS NOT NULL"), [])

    def test_a_worker_stopped_during_the_check_posts_after_its_restart(self):
        svc, (claude, codex) = self.queue(make_config(self.tmp, codex="dupsleep"))
        self.run_job(svc, claude)
        token, result = pipeline.CancelToken(), {}
        thread = threading.Thread(target=lambda: result.setdefault("s", pipeline.run_job(svc, codex, token)))
        thread.start()
        deadline = time.monotonic() + 20
        while not self.checks("codex") and time.monotonic() < deadline:
            time.sleep(0.05)
        token.cancel("shutdown")
        thread.join(20)
        self.assertEqual(result["s"], "waiting")  # its review isn't run again
        worker = Worker(lambda cfg: svc, None)
        run_worker_posts(worker, svc, worker.post_waiting)
        (posted,) = self.github.posted
        self.assertIn("weren't checked for duplicates (the check didn't run)", posted["body"])
        self.assertEqual({self.job(svc, job_id)["status"] for job_id in (claude, codex)}, {"posted"})
        self.assertEqual(len([c for c in self.agent_log("codex") if "find the duplicates" not in c["prompt"]]), 1)

    def test_a_review_whose_partner_fails_is_posted_on_its_own(self):
        svc, (claude, codex) = self.queue(make_config(self.tmp, codex="fail", service={"max_attempts": 1}))
        self.assertEqual(self.run_job(svc, claude), "waiting")
        self.assertEqual(self.run_job(svc, codex), "failed")
        worker = Worker(lambda cfg: svc, None)
        run_worker_posts(worker, svc, worker.post_waiting)
        (posted,) = self.github.posted
        self.assertIn("### Automated review · Claude Code (opus)\n", posted["body"])
        self.assertNotIn("Claude Code and Codex", posted["body"])
        self.assertEqual(self.job(svc, claude)["status"], "posted")

    def test_a_review_waits_for_the_other_no_longer_than_combine_wait(self):
        svc, (claude, codex) = self.queue()
        conn = svc.connect()
        with db.transaction(conn):  # Codex hasn't started, say because it hit a usage limit
            db.update_job(conn, codex, status="queued", not_before=self.clock() + 3600)
        conn.close()
        self.assertEqual(self.run_job(svc, claude), "waiting")
        worker = Worker(lambda cfg: svc, None)
        run_worker_posts(worker, svc, worker.post_waiting)
        self.assertEqual(self.github.posted, [])
        self.clock.advance(svc.config.service.combine_wait)
        run_worker_posts(worker, svc, worker.post_waiting)
        self.assertEqual(self.job(svc, claude)["status"], "posted")
        conn = svc.connect()
        with db.transaction(conn):
            db.update_job(conn, codex, status="running")
        conn.close()
        self.assertEqual(self.run_job(svc, codex), "posted")  # it comes too late: on its own
        self.assertEqual([p["body"].count("Automated review · ") for p in self.github.posted], [1, 1])
        self.assertIn("Automated review · Codex", self.github.posted[1]["body"])

    def test_without_a_wait_each_review_is_posted_on_its_own(self):
        svc, (claude, codex) = self.queue(make_config(self.tmp, service={"combine_wait": "0s"}))
        self.assertEqual([self.run_job(svc, claude), self.run_job(svc, codex)], ["posted", "posted"])
        self.assertEqual(len(self.github.posted), 2)
        self.assertEqual(self.checks("codex"), [])

    def test_a_push_ends_a_waiting_review(self):
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        conn = svc.connect()
        with db.transaction(conn):
            events.stop_jobs(conn, REPO, 1, "superseded", "superseded by abc")
        conn.close()
        self.assertEqual(self.job(svc, claude)["status"], "superseded")
        self.assertEqual(self.job(svc, codex)["cancel_requested"], 1)

    def test_a_failed_round_post_is_retried_once_for_all(self):
        svc, (claude, codex) = self.queue()
        real_post, calls = self.github.post_review, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise GitHubError("gh api failed: HTTP 502")
            return real_post(*args, **kwargs)
        self.github.post_review = flaky
        self.run_job(svc, claude)
        self.assertEqual(self.run_job(svc, codex), "posting")
        self.assertEqual((self.job(svc, claude)["status"], self.job(svc, claude)["posted_with"]), ("posting", codex))
        self.clock.advance(pipeline.POST_RECOVERY_RETRY)
        worker = Worker(lambda cfg: svc, None)
        worker.recover_posts(svc)
        self.assertEqual(set(worker.threads), {codex})  # the post carrying Claude's review is retried, not Claude's
        for thread, _token, _agent in list(worker.threads.values()):
            thread.join(30)
        (posted,) = self.github.posted
        self.assertEqual([self.job(svc, job_id)["review_id"] for job_id in (claude, codex)], [posted["id"]] * 2)

    def test_an_interrupted_round_post_that_reached_github_is_not_posted_twice(self):
        svc, (claude, codex) = self.queue()
        real_post = self.github.post_review

        def accepted_then_timed_out(*args, **kwargs):
            real_post(*args, **kwargs)
            raise GitHubError("gh api timed out")
        self.github.post_review = accepted_then_timed_out
        self.run_job(svc, claude)
        self.assertEqual(self.run_job(svc, codex), "posting")
        self.github.post_review = real_post
        self.clock.advance(pipeline.POST_RECOVERY_RETRY)
        worker = Worker(lambda cfg: svc, None)
        run_worker_posts(worker, svc, worker.recover_posts)
        self.assertEqual(len(self.github.posted), 1)
        self.assertEqual({self.job(svc, job_id)["status"] for job_id in (claude, codex)}, {"posted"})

    def test_a_leader_without_its_review_hands_the_others_back(self):
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        with mock.patch.object(pipeline, "post", return_value="posting"):  # interrupted just after the claim
            self.run_job(svc, codex)
        Path(pipeline.job_paths(self.state, codex).out, "review.md").unlink()
        conn = svc.connect()
        pipeline.recover_post(conn, svc, db.get_job(conn, codex))
        conn.close()
        self.assertEqual(self.job(svc, codex)["status"], "queued")  # its review runs again
        claude_job = self.job(svc, claude)
        self.assertEqual((claude_job["status"], claude_job["posted_with"]), ("waiting", None))
        # Claude's review may now be posted on its own, so none of it is a duplicate of Codex's any more.
        self.assertEqual(self.rows(svc, "SELECT id FROM findings WHERE duplicate_of IS NOT NULL"), [])

    def start_in_thread(self, svc, job_id):
        token, result = pipeline.CancelToken(), {}
        thread = threading.Thread(target=lambda: result.setdefault("s", pipeline.run_job(svc, job_id, token)))
        thread.start()
        deadline = time.monotonic() + 20
        while not self.checks("codex") and time.monotonic() < deadline:
            time.sleep(0.05)
        return token, thread, result

    def test_a_push_during_the_check_posts_nothing(self):
        svc, (claude, codex) = self.queue(make_config(self.tmp, codex="dupsleep"))
        self.run_job(svc, claude)
        token, thread, result = self.start_in_thread(svc, codex)
        conn = svc.connect()
        with db.transaction(conn):
            events.stop_jobs(conn, REPO, 1, "superseded", "superseded by abc")
        conn.close()
        token.cancel("superseded by abc")
        thread.join(20)
        self.assertEqual(result["s"], "superseded")
        self.assertEqual(self.job(svc, claude)["status"], "superseded")
        self.assertEqual(self.github.posted, [])

    def test_check_fallbacks(self):
        for mode, note in (("dupratelimit", "the check hit a usage limit"), ("dupsleep", "the check timed out")):
            with self.subTest(mode=mode), mock.patch.object(pipeline, "DUPLICATE_CHECK_TIMEOUT", 1):
                self.github.posted.clear()
                svc, (claude, codex) = self.queue(make_config(self.tmp, codex=mode))
                self.run_job(svc, claude)
                self.assertEqual(self.run_job(svc, codex), "posted")
                (posted,) = self.github.posted
                self.assertIn(f"weren't checked for duplicates ({note})", posted["body"])
                conn = svc.connect()
                with db.transaction(conn):  # out of the next subtest's way
                    conn.execute("UPDATE jobs SET status='superseded'")
                conn.close()

    def test_a_credential_in_the_merged_post_blocks_the_round(self):
        svc, (claude, codex) = self.queue()
        svc.secrets = lambda: ["Claude Code and Codex"]  # only the merged post says this
        self.run_job(svc, claude)
        self.assertEqual(self.run_job(svc, codex), "blocked")
        self.assertEqual({self.job(svc, job_id)["status"] for job_id in (claude, codex)}, {"blocked"})
        self.assertEqual(self.github.posted, [])

    def test_a_review_started_during_the_check_is_waited_for(self):
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        real_check = pipeline._check_duplicates

        def check_then_enqueue(conn, svc_, job, *args, **kwargs):
            record = real_check(conn, svc_, job, *args, **kwargs)
            with db.transaction(conn):  # `enqueue --force` for Claude, say
                db.insert_job(conn, repo=REPO, number=1, head_sha=self.head, base_ref="main", agent="claude",
                              model="opus", source="manual", not_before=0, now=self.clock(), rerun=1)
            return record
        with mock.patch.object(pipeline, "_check_duplicates", side_effect=check_then_enqueue):
            self.assertEqual(self.run_job(svc, codex), "waiting")
        self.assertEqual(self.job(svc, claude)["status"], "waiting")
        self.assertEqual(self.github.posted, [])

    def test_a_running_review_is_waited_for_past_combine_wait(self):
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        self.clock.advance(svc.config.service.combine_wait + 60)
        worker = Worker(lambda cfg: svc, None)
        run_worker_posts(worker, svc, worker.post_waiting)
        self.assertEqual(self.github.posted, [])  # Codex is still running: it finishes within review_timeout
        conn = svc.connect()
        with db.transaction(conn):  # its review failed and waits for a retry
            db.update_job(conn, codex, status="queued", not_before=self.clock() + 600)
        conn.close()
        run_worker_posts(worker, svc, worker.post_waiting)
        self.assertEqual(self.job(svc, claude)["status"], "posted")

    def test_turning_combining_off_posts_waiting_reviews_on_their_own(self):
        svc, (claude, other_claude, codex) = self.queue(agents=("claude", "claude", "codex"))
        self.run_job(svc, claude)
        self.run_job(svc, other_claude)  # both wait for Codex
        svc = self.services(make_config(self.tmp, service={"combine_wait": "0s"}))
        worker = Worker(lambda cfg: svc, None)
        run_worker_posts(worker, svc, worker.post_waiting)
        self.assertEqual([self.job(svc, job_id)["status"] for job_id in (claude, other_claude)], ["posted"] * 2)
        self.assertEqual(len(self.github.posted), 2)
        self.assertEqual(self.run_job(svc, codex), "posted")
        self.assertEqual(len(self.github.posted), 3)
        self.assertEqual(self.checks("codex"), [])

    def test_a_garbled_review_json_posts_the_reviews_separately(self):
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        Path(pipeline.job_paths(self.state, claude).out, "review.json").write_text("{not json")
        self.assertEqual(self.run_job(svc, codex), "posted")
        self.assertEqual(self.job(svc, claude)["status"], "waiting")  # handed back with its own review.md
        worker = Worker(lambda cfg: svc, None)
        run_worker_posts(worker, svc, worker.post_waiting)
        self.assertEqual(self.job(svc, claude)["status"], "posted")
        self.assertEqual([p["body"].count("Automated review · ") for p in self.github.posted], [1, 1])
        self.assertEqual(self.rows(svc, "SELECT id FROM findings WHERE duplicate_of IS NOT NULL"), [])

    def test_a_refused_fallback_post_points_at_the_review_it_tried(self):
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        Path(pipeline.job_paths(self.state, claude).out, "review.json").write_text("{not json")
        self.github.post_review = mock.Mock(side_effect=GitHubError("gh api failed: Not Found (HTTP 404)"))
        self.assertEqual(self.run_job(svc, codex), "failed")
        self.assertIn(f"{pipeline.job_paths(self.state, codex).out}/review.md", self.job(svc, codex)["reason"])
        self.assertEqual(self.job(svc, claude)["status"], "waiting")  # released before the post: not failed with it

    def test_a_carried_review_is_never_retried_on_its_own(self):
        svc, (claude, codex) = self.queue()
        real_post = self.github.post_review
        self.github.post_review = mock.Mock(side_effect=GitHubError("gh api failed: HTTP 502"))
        self.run_job(svc, claude)
        self.assertEqual(self.run_job(svc, codex), "posting")
        self.github.post_review = real_post
        self.clock.advance(pipeline.POST_RECOVERY_RETRY)
        # `review --post` owning Claude's job must leave it to the post that carries it.
        self.assertEqual(pipeline._finish_post(svc, claude, self.fail, lambda message: None), "posting")
        self.assertEqual(self.github.posted, [])

    def test_a_refused_round_post_points_at_the_merged_review(self):
        svc, (claude, codex) = self.queue()
        self.github.post_review = mock.Mock(side_effect=GitHubError("gh api failed: Not Found (HTTP 404)"))
        self.run_job(svc, claude)
        self.assertEqual(self.run_job(svc, codex), "failed")
        saved = f"{pipeline.job_paths(self.state, codex).out}/round.md"
        self.assertIn(saved, self.job(svc, codex)["reason"])
        self.assertIn(f"with job {codex}: post failed", self.job(svc, claude)["reason"])

    def test_findings_show_rounds_and_duplicates(self):
        import os
        from local_autoreviewer import __main__ as cli
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        self.run_job(svc, codex)
        with mock.patch.dict(os.environ, {"STATE_DIRECTORY": str(self.state)}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["findings", "--json", "--job", str(codex), "--kind", "finding"])
            (row,) = json.loads(out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["findings", "--job", str(codex), "--kind", "finding"])
        (kept,) = self.rows(svc, "SELECT id FROM findings WHERE job_id=? AND kind='finding'", claude)
        self.assertEqual((row["trivial"], row["posted_with"], row["duplicate_of"]), (0, codex, kept["id"]))
        self.assertIn(f"(duplicate of {kept['id']})", out.getvalue())

    def test_review_post_command_posts_one_review_when_the_other_fails(self):
        from local_autoreviewer import __main__ as cli
        cfg = make_config(self.tmp, codex="fail", service={"max_attempts": 1})
        svc = self.services(cfg)
        with mock.patch.object(cli, "load_config", return_value=cfg), \
                mock.patch.object(cli, "build_services", return_value=svc), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.cmd_review(argparse.Namespace(ref=f"{REPO}#1", agent=None, post=True))
        self.assertEqual(code, 1)  # Codex's review failed
        (posted,) = self.github.posted
        self.assertIn("### Automated review · Claude Code (opus)\n", posted["body"])

    def test_review_post_command_leaves_a_waiting_review_to_the_worker(self):
        from local_autoreviewer import __main__ as cli
        svc, (codex,) = self.queue(agents=("codex",), status="queued")  # the service's review, not started
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=self.cfg), \
                mock.patch.object(cli, "build_services", return_value=svc), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.cmd_review(argparse.Namespace(ref=f"{REPO}#1", agent=["claude"], post=True))
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertIn("]: waiting — review ready; waiting to post it with Codex's review", stdout.getvalue())
        self.assertIn("the worker, when it runs, will post it", stderr.getvalue())
        self.assertEqual(self.github.posted, [])

    def trials(self, cfg=None):
        svc = self.services(cfg)
        conn = svc.connect()
        ids = []
        with db.transaction(conn):
            for agent in ("claude", "codex"):
                job_id = db.insert_job(conn, repo=REPO, number=1, head_sha=self.head, base_ref="main", agent=agent,
                                       model="", source=db.FOREGROUND, not_before=0, now=self.clock(), post=False,
                                       rerun=db.next_rerun(conn, REPO, 1, self.head, "main", agent))
                db.update_job(conn, job_id, status="running", started_at=self.clock())
                ids.append(job_id)
        conn.close()
        for job_id in ids:
            self.assertEqual(self.run_job(svc, job_id), "skipped")
        return svc, ids

    def test_the_preview_check_counts_as_a_running_review(self):
        svc, ids = self.trials()
        real_check, seen = pipeline._check_duplicates, []

        def check(conn, svc_, job, *args, **kwargs):
            seen.append(self.job(svc, job["id"])["status"])
            return real_check(conn, svc_, job, *args, **kwargs)
        with mock.patch.object(pipeline, "_check_duplicates", side_effect=check):
            self.assertTrue(pipeline.preview_round(svc, ids))
        self.assertEqual(seen, ["running"])
        self.assertEqual([self.job(svc, job_id)["status"] for job_id in ids], ["skipped"] * 2)

    def test_no_preview_where_the_service_would_not_merge(self):
        svc, ids = self.trials(make_config(self.tmp, service={"combine_wait": "0s"}))
        self.assertIsNone(pipeline.preview_round(svc, ids))
        svc, ids = self.trials()
        conn = svc.connect()
        with db.transaction(conn):  # a follow-up and a full review
            db.update_job(conn, ids[0], followup_of="f" * 40)
        conn.close()
        self.assertIsNone(pipeline.preview_round(svc, ids))
        self.assertEqual(self.checks("codex"), [])

    def test_a_followup_round_credits_both_reviewers(self):
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        self.run_job(svc, codex)
        (kept,) = self.rows(svc, "SELECT id FROM findings WHERE job_id=? AND kind='finding'", claude)
        self.clock.advance(60)
        head_b = self.remote.push_pr(1, {"src/calc.py": FIXED})
        self.github.set_pr(1, head_b)
        svc, (claude_b, codex_b) = self.queue(head=head_b)
        self.assertEqual(self.run_job(svc, claude_b), "waiting")
        previous = json.loads(Path(pipeline.job_paths(self.state, claude_b).context, "previous-findings.json")
                              .read_text())
        self.assertEqual([p["id"] for p in previous], [kept["id"]])  # Codex's copy of it isn't asked about
        self.assertEqual(self.run_job(svc, codex_b), "posted")
        body = self.github.posted[-1]["body"]
        self.assertIn("### Follow-up review · Claude Code (opus) and Codex", body)
        self.assertIn("- Division by zero is unhandled _(Claude Code and Codex, major, ", body)
        self.assertIn("  - Claude Code: ✅ Fixed — checked", body)
        self.assertIn("  - Codex: ✅ Fixed — checked", body)
        self.assertEqual(body.count("**The fix dropped the docstring**"), 1)
        self.assertIn("_(Claude Code and Codex; correctness, impact 2/10, common)_", body)
        self.assertIn("· follow-up</sub>", body)

    def test_a_whole_pr_followup_round_merges_its_nits_and_test_gaps(self):
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        self.run_job(svc, codex)
        self.clock.advance(60)
        head_b = self.remote.push_pr(1, {"src/calc.py": FIXED})
        self.github.set_pr(1, head_b)
        svc, (claude_b, codex_b) = self.queue(cfg=make_config(self.tmp, claude="newcode", codex="newcode"), head=head_b)
        self.run_job(svc, claude_b)
        self.assertEqual(self.run_job(svc, codex_b), "posted")
        body = self.github.posted[-1]["body"]
        self.assertIn("so Claude Code and Codex also reviewed the whole PR.", body)
        self.assertIn("#### New findings", body)
        self.assertEqual(body.count("No test for the new code."), 1)  # both reported it; posted once
        self.assertIn("- No test for the new code. _(Claude Code and Codex)_", body)
        self.assertEqual(body.count("Name the new helper."), 1)
        # The second reviewer's copies are recorded as duplicates of the first's, as for a full review.
        copies = self.rows(svc, "SELECT kind FROM findings WHERE job_id=? AND duplicate_of IS NOT NULL ORDER BY kind",
                           codex_b)
        self.assertEqual([row["kind"] for row in copies], ["nit", "regression", "test_gap"])
        # A third round is told of the nit and the test gap once each, and of the first review's own.
        head_c = self.remote.push_pr(1, {"src/calc.py": FIXED + "\n"})
        self.github.set_pr(1, head_c)
        svc, (claude_c, _) = self.queue(head=head_c)
        self.run_job(svc, claude_c)
        raised = json.loads(Path(pipeline.job_paths(self.state, claude_c).context, "already-raised.json").read_text())
        self.assertEqual(sorted((r["kind"], r["text"]) for r in raised),
                         [("nit", "Add a docstring."), ("nit", "Name the new helper."),
                          ("test_gap", "No test for b == 0."), ("test_gap", "No test for the new code.")])

    def test_review_post_command_posts_once(self):
        from local_autoreviewer import __main__ as cli
        svc = self.services()
        out = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=self.cfg), \
                mock.patch.object(cli, "build_services", return_value=svc), contextlib.redirect_stdout(out):
            code = cli.cmd_review(argparse.Namespace(ref=f"{REPO}#1", agent=None, post=True))
        self.assertEqual(code, 0, out.getvalue())
        (posted,) = self.github.posted
        self.assertIn("Claude Code (opus) and Codex", posted["body"])
        self.assertEqual(out.getvalue().count("]: posted"), 2)

    def test_review_command_saves_the_merged_review(self):
        from local_autoreviewer import __main__ as cli
        svc = self.services()
        out = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=self.cfg), \
                mock.patch.object(cli, "build_services", return_value=svc), contextlib.redirect_stdout(out):
            code = cli.cmd_review(argparse.Namespace(ref=f"{REPO}#1", agent=None, post=False))
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.github.posted, [])
        merged = Path(pipeline.job_paths(self.state, 2).out, "round.md")
        self.assertIn(f"reviews merged as they would be posted: {merged}", out.getvalue())
        self.assertEqual(merged.read_text().count("Division by zero is unhandled"), 1)

    def test_re_recording_a_review_releases_its_duplicates(self):
        svc, (claude, codex) = self.queue()
        self.run_job(svc, claude)
        self.run_job(svc, codex)
        conn = svc.connect()
        try:
            with db.transaction(conn):
                db.record_review(conn, claude, review.validate({"summary": "again"}))
        finally:
            conn.close()
        self.assertEqual(self.rows(svc, "SELECT id FROM findings WHERE duplicate_of IS NOT NULL"), [])


class TrivialTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.head = self.remote.open_pr(1, {"src/calc.py": CALC})
        self.github.set_pr(1, self.head)

    def run_both(self, cfg, agents):
        svc = self.services(cfg)
        conn = svc.connect()
        ids = []
        with db.transaction(conn):
            for agent in agents:
                job_id = db.insert_job(conn, repo=REPO, number=1, head_sha=self.head, base_ref="main", agent=agent,
                                       model=cfg.agents[agent].model, source="poll", not_before=0, now=self.clock())
                db.update_job(conn, job_id, status="running", started_at=self.clock())
                ids.append(job_id)
        conn.close()
        return svc, [pipeline.run_job(svc, job_id, pipeline.CancelToken()) for job_id in ids]

    def test_a_trivial_pr_gets_a_short_review(self):
        svc, statuses = self.run_both(make_config(self.tmp, claude="trivial", agents=("claude",)), ("claude",))
        self.assertEqual(statuses, ["posted"])
        body = self.github.posted[0]["body"]
        self.assertIn("Bumps the version.\n\n_Trivial change, so the review ended early._", body)
        self.assertIn("No significant issues found.", body)
        conn = svc.connect()
        self.assertEqual(conn.execute("SELECT trivial FROM jobs").fetchone()[0], 1)
        conn.close()

    def test_a_trivial_review_with_a_full_one_needs_no_check(self):
        svc, statuses = self.run_both(make_config(self.tmp, claude="trivial"), ("claude", "codex"))
        self.assertEqual(statuses, ["waiting", "posted"])
        body = self.github.posted[0]["body"]
        self.assertIn("**Claude Code:** _(trivial change, so the review ended early)_ Bumps the version.", body)
        self.assertIn("_(Codex; correctness, impact 6/10, common)_", body)
        self.assertFalse(any("find the duplicates" in call["prompt"] for call in self.agent_log("codex")))

    def test_the_prompt_and_schema_offer_it(self):
        prompt = review.build_prompt(repo="o/r", number=5, title="T", author="me", base_ref="main", head_sha="abc",
                                     worktree="/w", context="/c")
        self.assertLess(prompt.index("## Trivial PRs"), prompt.index("## Method: this is the only pass"))
        schema = json.loads(review.schema_path().read_text())
        self.assertIn("trivial", schema["required"])
        self.assertEqual(schema["properties"]["trivial"]["type"], "boolean")

    def test_older_reviews_are_not_trivial(self):
        self.assertFalse(review.validate({"summary": "s"})["trivial"])
        self.assertFalse(review.validate({"summary": "s", "trivial": "yes"})["trivial"])
        self.assertTrue(review.validate({"summary": "s", "trivial": True})["trivial"])


def finding(title, severity="minor", impact=3, **extra):
    return {"severity": severity, "impact": impact, "likelihood": "rare", "trigger": "", "category": "correctness",
            "file": "a.py", "line": 1, "title": title, "detail": "", "suggestion": "", "edits": [], **extra}


def edit(line, replacement, inline=True):
    """A proposed edit as suggestions.apply() leaves it: checked against the file, one-click if in the diff."""
    return {"file": "a.py", "start_line": line, "end_line": line, "replacement": replacement, "original": ["old"],
            "inline": inline, "anchor": "exact"}


class DuplicateTests(unittest.TestCase):
    def members(self):
        first = review.validate({"summary": "one", "findings": [finding("Leak", "major", 6)],
                                 "nits": [{"file": "a.py", "line": 1, "text": "Rename x"}], "test_gaps": ["no test"]})
        second = review.validate({"summary": "two", "findings": [finding("Handle leaks")],
                                  "test_gaps": ["untested"], "questions": ["why?"]})
        return [{"job_id": 1, "agent": "claude", "model": "", "review": first},
                {"job_id": 2, "agent": "codex", "model": "", "review": second}]

    def test_catalogue(self):
        self.assertEqual(combine.catalogue(self.members()),
                         {"F1": (1, "findings", 0), "F2": (2, "findings", 0), "N1": (1, "nits", 0),
                          "T1": (1, "test_gaps", 0), "T2": (2, "test_gaps", 0), "Q1": (2, "questions", 0)})

    def test_only_sets_that_can_merge_are_kept(self):
        answer = {"duplicates": [{"ids": ["F1", "F2"], "reason": "same"},
                                 {"ids": ["T1", "Q1"], "reason": "a test gap and a question"},
                                 {"ids": ["F2", "N1"], "reason": "F2 is taken: N1 alone has one reviewer"},
                                 {"ids": ["T1", "T2", "X9", "T2"], "reason": "an unknown id and a repeat"},
                                 {"ids": "F1", "reason": "not a list"}, "garbage"]}
        self.assertEqual(combine.groups(answer, self.members()),
                         [[(1, "findings", 0), (2, "findings", 0)], [(1, "test_gaps", 0), (2, "test_gaps", 0)]])
        self.assertEqual(combine.groups({"duplicates": "x"}, self.members()), [])
        self.assertEqual(combine.groups(None, self.members()), [])

    def test_the_more_severe_finding_is_kept_and_credited_to_both(self):
        members = self.members()
        sets = combine.groups({"duplicates": [{"ids": ["F2", "F1"]}, {"ids": ["T2", "T1"]}]}, members)
        pairs = combine.duplicates(members, sets)
        self.assertEqual(pairs, {(2, "findings", 0): (1, "findings", 0), (2, "test_gaps", 0): (1, "test_gaps", 0)})
        merged = review.merge(members, pairs)
        self.assertEqual([(f["title"], f["reviewers"]) for f in merged["findings"]], [("Leak", ["claude", "codex"])])
        self.assertEqual([(t["text"], t["reviewers"]) for t in merged["test_gaps"]], [("no test", ["claude", "codex"])])
        body = review.render_round(merged, repo="o/r", number=1, sha="a" * 40, base_ref="main")
        self.assertIn("Codex rated it minor (impact 3/10).", body)
        self.assertIn("- why? _(Codex)_", body)

    def test_a_finding_wins_over_a_nit_it_repeats(self):
        members = self.members()
        pairs = combine.duplicates(members, combine.groups({"duplicates": [{"ids": ["N1", "F2"]}]}, members))
        self.assertEqual(pairs, {(1, "nits", 0): (2, "findings", 0)})
        merged = review.merge(members, pairs)
        self.assertEqual(merged["nits"], [])
        handle = next(f for f in merged["findings"] if f["title"] == "Handle leaks")
        self.assertEqual((handle["reviewers"], handle["also"][0]["severity"]), (["claude", "codex"], "nit"))
        self.assertIn("Claude Code reported it as a nit.",
                      review.render_round(merged, repo="o/r", number=1, sha="a" * 40, base_ref="main"))

    def test_another_rating_is_the_reviewers_own(self):
        members = self.members()
        demoted = dict(members[1]["review"]["findings"][0], severity="nit", reviewer_severity="minor")
        members[1]["review"]["findings"] = [demoted]  # the readability modifier made Codex's a nit
        merged = review.merge(members, {(2, "findings", 0): (1, "findings", 0)})
        body = review.render_round(merged, repo="o/r", number=1, sha="a" * 40, base_ref="main")
        self.assertIn("Codex rated it minor (impact 3/10).", body)
        self.assertNotIn("reported it as a nit", body)

    def with_fixes(self, claude_edits, codex_edits, key="findings"):
        """The members, with each reviewer's copy of one issue proposing the given edits."""
        members = self.members()
        if key == "findings":
            for member, edits, suggestion in ((members[0], claude_edits, "Close it."),
                                              (members[1], codex_edits, "Use a with block.")):
                member["review"]["findings"] = [dict(member["review"]["findings"][0], edits=edits,
                                                     suggestion=suggestion)]
            return members, {(2, "findings", 0): (1, "findings", 0)}
        for member, edits in ((members[0], claude_edits), (members[1], codex_edits)):
            member["review"]["nits"] = [{"file": "a.py", "line": 1, "text": "Rename x", "edits": edits}]
        return members, {(2, "nits", 0): (1, "nits", 0)}

    def test_a_repeat_with_another_fix_shows_it_under_the_kept_finding(self):
        members, pairs = self.with_fixes([edit(1, "f.close()")], [edit(1, "with open(p) as f:"), edit(4, "pass")])
        merged = review.merge(members, pairs)
        (kept,) = [f for f in merged["findings"] if f["title"] == "Leak"]
        self.assertEqual([(a["agent"], len(a["edits"])) for a in kept["alternatives"]], [("codex", 2)])
        body = review.render_round(merged, repo="o/r", number=1, sha="a" * 40, base_ref="main")
        self.assertIn("**Codex proposed a different fix.**", body)
        self.assertIn("> **Codex's suggestion:** Use a with block.", body)
        self.assertEqual(body.count("<summary>Codex's proposed change to <code>a.py:"), 2)
        self.assertIn("<summary>Proposed change to <code>a.py:1</code> (also a one-click suggestion on the PR)", body)
        self.assertNotIn("Codex's proposed change to <code>a.py:1</code> (also", body)  # never inline
        # Only the kept fix is an inline suggestion, so two never compete for the same lines.
        comments = suggestions.comments(merged, review.comment_label(merged))
        self.assertEqual([c["body"].split("```suggestion\n", 1)[1] for c in comments], ["f.close()\n```"])
        # Inside the finding's list item, like its own proposed change, and after a blank line: right after
        # the </details> above, GitHub would show it as raw text.
        self.assertIn("</details>\n\n   **Codex proposed a different fix.**", body)

    def test_a_repeat_with_the_same_fix_adds_nothing(self):
        members, pairs = self.with_fixes([edit(1, "f.close()")], [edit(1, "f.close()")])
        merged = review.merge(members, pairs)
        self.assertEqual(merged["findings"][0]["alternatives"], [])
        self.assertNotIn("proposed a different fix",
                         review.render_round(merged, repo="o/r", number=1, sha="a" * 40, base_ref="main"))
        # Nor does one with no fix, or one whose edits didn't match the file.
        for codex_edits in ([], [dict(edit(3, "x"), anchor="dropped")]):
            members, pairs = self.with_fixes([edit(1, "f.close()")], codex_edits)
            self.assertEqual(review.merge(members, pairs)["findings"][0]["alternatives"], [])

    def test_a_kept_finding_without_a_fix_shows_the_repeats(self):
        members, pairs = self.with_fixes([], [edit(1, "with open(p) as f:")])
        merged = review.merge(members, pairs)
        self.assertEqual(len(merged["findings"][0]["alternatives"]), 1)

    def test_a_repeated_nit_with_another_change_shows_it(self):
        members, pairs = self.with_fixes([edit(1, "count = 0")], [edit(1, "total = 0")], key="nits")
        merged = review.merge(members, pairs)
        body = review.render_round(merged, repo="o/r", number=1, sha="a" * 40, base_ref="main")
        self.assertIn("</details>\n\n  **Codex proposed a different fix.**", body)  # inside the nit's item
        self.assertIn("<summary>Codex's proposed change to <code>a.py:1</code></summary>", body)
        self.assertNotIn("Codex's suggestion", body)  # a nit has none

    def test_a_partly_dropped_alternative_says_so(self):
        members, pairs = self.with_fixes([edit(1, "f.close()")],
                                         [edit(1, "with open(p) as f:"), dict(edit(5, "x"), anchor="dropped")])
        merged = review.merge(members, pairs)
        body = review.render_round(merged, repo="o/r", number=1, sha="a" * 40, base_ref="main")
        self.assertIn("Codex proposed a different fix.**\n   > **Codex's suggestion:** Use a with block.\n"
                      "   _1 proposed change didn't match the file and was left out._", body)
        self.assertEqual(body.count("Codex's proposed change to"), 1)

    def test_an_edit_without_an_inline_copy_is_shown_whole(self):
        long = "\n".join(f"line {n}" for n in range(100))
        members, pairs = self.with_fixes([edit(1, long), edit(2, long, inline=False)], [edit(3, long)])
        body = review.render_round(review.merge(members, pairs), repo="o/r", number=1, sha="a" * 40, base_ref="main")
        # Only the kept, inline edit is shortened: the other two have no copy outside the body.
        self.assertEqual(body.count("… 21 more lines"), 1)
        self.assertEqual(body.count("+line 99"), 2)
        # A huge one is still cut, at a much higher limit, so a few can't fill the review.
        huge = "\n".join(f"line {n}" for n in range(600))
        members, pairs = self.with_fixes([edit(1, "x")], [edit(3, huge)])
        body = review.render_round(review.merge(members, pairs), repo="o/r", number=1, sha="a" * 40, base_ref="main")
        self.assertIn(f"… {601 - suggestions.MAX_BODY_ONLY_LINES} more lines", body)

    def test_alternatives_are_not_repeated(self):
        members, pairs = self.with_fixes([edit(1, "f.close()")], [edit(1, "with open(p) as f:")])
        # A second copy from Codex with the same fix, and one from Claude with another: only a new fix is listed,
        # under the reviewer that proposed it, even when that's the kept item's own reviewer.
        members[1]["review"]["findings"].append(dict(members[1]["review"]["findings"][0]))
        members[0]["review"]["findings"].append(dict(members[0]["review"]["findings"][0],
                                                     edits=[edit(2, "f = None")]))
        pairs.update({(2, "findings", 1): (1, "findings", 0), (1, "findings", 1): (1, "findings", 0)})
        merged = review.merge(members, pairs)
        self.assertEqual([a["agent"] for a in merged["findings"][0]["alternatives"]], ["claude", "codex"])
        # Trailing spaces can change what a fix does (here, the string's value); a final newline doesn't.
        members, pairs = self.with_fixes([edit(1, 'message = """hello\nworld"""')],
                                         [edit(1, 'message = """hello  \nworld"""')])
        self.assertEqual(len(review.merge(members, pairs)["findings"][0]["alternatives"]), 1)
        members, pairs = self.with_fixes([edit(1, "f.close()")], [edit(1, "f.close()\n")])
        self.assertEqual(review.merge(members, pairs)["findings"][0]["alternatives"], [])

    def test_malformed_ids_are_ignored(self):
        answer = {"duplicates": [{"ids": [["F1"], {"x": 1}, "F1", "F2"]}]}
        self.assertEqual(combine.groups(answer, self.members()), [[(1, "findings", 0), (2, "findings", 0)]])

    def test_nothing_to_check_without_items_from_two_reviewers(self):
        members = self.members()
        members[1]["review"] = review.validate({"summary": "fine"})
        self.assertFalse(combine.worth_checking(members))
        self.assertTrue(combine.worth_checking(self.members()))


if __name__ == "__main__":
    unittest.main()
