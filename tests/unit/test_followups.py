# SPDX-License-Identifier: GPL-3.0-or-later
"""Follow-up reviews: a later head is checked against the previous round's findings and for regressions."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import REPO, EnvTestCase, git, make_config
from local_autoreviewer import db, maintenance, pipeline, review
from local_autoreviewer.agents import claude as claude_mod
from local_autoreviewer.agents import codex as codex_mod

FIXED = "def divide(a, b):\n    if b == 0:\n        raise ValueError('b must not be zero')\n    return a / b\n"


class FollowupTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        self.head_a = self.remote.open_pr(1, {"src/calc.py": "def divide(a, b):\n    return a / b if b else 0\n"})
        self.svc = self.services(make_config(self.tmp, agents=("claude",)))

    def review_at(self, head, *, post=True, source="poll", base="main"):
        self.github.set_pr(1, head, base=base)
        conn = self.svc.connect()
        with db.transaction(conn):
            job_id = db.insert_job(conn, repo=REPO, number=1, head_sha=head, base_ref=base, agent="claude",
                                   model="opus", source=source, not_before=0, now=self.clock(), post=post,
                                   rerun=db.next_rerun(conn, REPO, 1, head, base, "claude"))
            db.update_job(conn, job_id, status="running", started_at=self.clock())
        conn.close()
        status = pipeline.run_job(self.svc, job_id, pipeline.CancelToken())
        self.clock.advance(60)
        return job_id, status

    def rows(self, sql, *params):
        conn = self.svc.connect()
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def context(self, job_id, name):
        return Path(pipeline.job_paths(self.state, job_id).context, name)

    def test_a_new_head_gets_a_followup(self):
        first, status = self.review_at(self.head_a)
        self.assertEqual(status, "posted")
        (finding,) = self.rows("SELECT id, title FROM findings WHERE job_id=? AND kind='finding'", first)
        head_b = self.remote.push_pr(1, {"src/calc.py": FIXED})
        second, status = self.review_at(head_b)
        self.assertEqual(status, "posted")

        (job,) = self.rows("SELECT review_kind, followup_of, prompt_version FROM jobs WHERE id=?", second)
        self.assertEqual((job["review_kind"], job["followup_of"]), ("followup", self.head_a))
        self.assertEqual(job["prompt_version"], review.prompt_version(followup=True))
        self.assertIn("This is a follow-up.", self.agent_log("claude")[-1]["prompt"])
        previous = json.loads(self.context(second, "previous-findings.json").read_text())
        self.assertEqual([(p["id"], p["title"], p["reviewer"]) for p in previous],
                         [(finding["id"], finding["title"], "claude")])
        self.assertIn("raise ValueError", self.context(second, "since-review.diff").read_text())

        self.assertEqual(self.rows("SELECT finding_id, status FROM resolutions WHERE job_id=?", second),
                         [{"finding_id": finding["id"], "status": "fixed"}])
        self.assertEqual([r["title"] for r in self.rows("SELECT title FROM findings WHERE job_id=? AND kind='regression'",
                                                        second)], ["The fix dropped the docstring"])
        posted = self.github.posted[-1]
        self.assertIn("### Follow-up review · Claude Code (opus)", posted["body"])
        self.assertIn(f"✅ Fixed: {finding['title']}", posted["body"])
        self.assertIn("1. **The fix dropped the docstring**", posted["body"])
        self.assertEqual([(c["path"], c["line"]) for c in posted["comments"]], [("src/calc.py", 1)])

    def test_base_commits_merged_into_the_pr_are_not_its_changes(self):
        self.review_at(self.head_a)
        work = self.remote.work
        git("checkout", "-q", "main", cwd=work)
        self.remote.write("BASE_ONLY.txt", "from main\n")
        self.remote.commit("main moves on")
        git("push", "-q", str(self.remote.bare), "main", cwd=work)
        git("checkout", "-q", "pr1", cwd=work)
        git("merge", "-q", "--no-edit", "main", cwd=work)  # the PR catches up with its base
        self.remote.write("src/calc.py", FIXED)
        head = self.remote.commit("fix the division")
        git("push", "-q", "-f", str(self.remote.bare), "HEAD:refs/pull/1/head", cwd=work)
        second, _ = self.review_at(head)
        diff = self.context(second, "since-review.diff").read_text()
        self.assertIn("raise ValueError", diff)
        self.assertNotIn("BASE_ONLY", diff)

    def test_a_conflicting_base_merge_means_a_full_review(self):
        self.review_at(self.head_a)
        work = self.remote.work
        git("checkout", "-q", "main", cwd=work)
        self.remote.write("src/calc.py", "def divide(a, b):\n    return a / b  # checked upstream\n")
        self.remote.commit("main changes the same line")
        git("push", "-q", str(self.remote.bare), "main", cwd=work)
        git("checkout", "-q", "pr1", cwd=work)
        git("merge", "--no-edit", "main", cwd=work, check=False)  # conflicts
        self.assertTrue((work / ".git" / "MERGE_HEAD").exists())  # a real merge is in progress
        self.remote.write("src/calc.py", FIXED)  # the author resolves it
        head = self.remote.commit("merge main")
        git("push", "-q", "-f", str(self.remote.bare), "HEAD:refs/pull/1/head", cwd=work)
        second, status = self.review_at(head)
        self.assertEqual(status, "posted")
        self.assertEqual(self.rows("SELECT review_kind FROM jobs WHERE id=?", second), [{"review_kind": "full"}])

    def test_a_followup_too_large_for_the_limits_means_a_full_review(self):
        self.svc = self.services(make_config(self.tmp, agents=("claude",), repo_extra={"max_diff_bytes": 10_000}))
        first, status = self.review_at(self.remote.push_pr(1, {"data.txt": "x\n" * 2_500}))  # a 7.5 KB diff
        self.assertEqual(status, "posted")
        work = self.remote.work
        git("checkout", "-q", "pr1", cwd=work)
        git("rm", "-q", "data.txt", cwd=work)  # the PR drops it again
        self.remote.write("other.txt", "y\n" * 1_000)
        head = self.remote.commit("swap the data")
        git("push", "-q", "-f", str(self.remote.bare), "HEAD:refs/pull/1/head", cwd=work)
        git("checkout", "-q", "main", cwd=work)
        # The PR's diff (3 KB) is within the limit; the change since the review (10.5 KB) isn't.
        second, status = self.review_at(head)
        self.assertEqual(status, "posted")
        self.assertEqual(self.rows("SELECT review_kind FROM jobs WHERE id=?", second), [{"review_kind": "full"}])
        self.assertFalse(self.context(second, "since-review.diff").exists())

    def test_a_chain_of_followups_carries_the_open_issues(self):
        self.review_at(self.head_a)
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        (regression,) = self.rows("SELECT id FROM findings WHERE job_id=? AND kind='regression'", second)
        third, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "# tidy\n"}))
        # The first round's finding was fixed in the second; what's open is the second round's regression.
        previous = json.loads(self.context(third, "previous-findings.json").read_text())
        self.assertEqual([p["id"] for p in previous], [regression["id"]])

    def test_unfixed_findings_stay_open(self):
        self.review_at(self.head_a)
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        conn = self.svc.connect()
        with db.transaction(conn):  # say the follow-up found the finding not fixed after all
            conn.execute("UPDATE resolutions SET status='not_fixed' WHERE job_id=?", (second,))
        conn.close()
        (resolution,) = self.rows("SELECT finding_id FROM resolutions WHERE job_id=?", second)
        third, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "# tidy\n"}))
        ids = {p["id"] for p in json.loads(self.context(third, "previous-findings.json").read_text())}
        self.assertIn(resolution["finding_id"], ids)

    def set_resolution(self, job_id, status):
        conn = self.svc.connect()
        with db.transaction(conn):
            conn.execute("UPDATE resolutions SET status=? WHERE job_id=?", (status, job_id))
        conn.close()

    def test_an_unanswered_finding_stays_open(self):
        self.review_at(self.head_a)
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.set_resolution(second, "unchecked")
        (resolution,) = self.rows("SELECT finding_id FROM resolutions WHERE job_id=?", second)
        third, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "# tidy\n"}))
        ids = {p["id"] for p in json.loads(self.context(third, "previous-findings.json").read_text())}
        self.assertIn(resolution["finding_id"], ids)

    def test_a_declined_finding_is_not_asked_about_again(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT id FROM findings WHERE job_id=? AND kind='finding'", first)
        conn = self.svc.connect()
        with db.transaction(conn):
            db.triage(conn, finding["id"], now=self.clock(), disposition="declined")
        conn.close()
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(json.loads(self.context(second, "previous-findings.json").read_text()), [])

    def test_a_retarget_away_and_back_means_a_full_review(self):
        self.review_at(self.head_a)
        conn = self.svc.connect()
        with db.transaction(conn):  # reviewed since against another base, one with changes main doesn't have
            other = db.insert_job(conn, repo=REPO, number=1, head_sha=self.head_a, base_ref="release", agent="claude",
                                  model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, other, status="posted", review_recorded=1, finished_at=self.clock(),
                          base_sha=self.head_a)
        conn.close()
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(self.rows("SELECT review_kind FROM jobs WHERE id=?", second), [{"review_kind": "full"}])

    def test_dropped_edits_are_not_shown_to_the_followup(self):
        row = {"id": 1, "agent": "claude", "severity": "minor", "impact": 2, "title": "t", "file": "a.py", "line": 3,
               "raised_at": "abc", "occurs_when": "w", "detail": "d", "suggestion": "s",
               "edits": json.dumps([
                   {"file": "a.py", "start_line": 3, "end_line": 3, "replacement": "x", "anchor": "moved"},
                   {"file": "a.py", "start_line": 9, "end_line": 9, "replacement": "y", "anchor": "dropped"}])}
        (finding,) = pipeline.PreviousRound("abc", [row]).for_agent()
        self.assertEqual([e["replacement"] for e in finding["proposed_edits"]], ["x"])

    def stacked(self):
        """PR 1 rebuilt on top of another PR's branch, `lower`, and reviewed there. Returns PR 1's head."""
        work, bare = self.remote.work, str(self.remote.bare)
        git("checkout", "-q", "-B", "lower", "main", cwd=work)
        self.remote.write("README.md", "demo, with docs\n")
        self.remote.commit("the PR below")
        git("push", "-q", bare, "lower", cwd=work)
        git("checkout", "-q", "-B", "pr1", "lower", cwd=work)
        self.remote.write("src/calc.py", "def divide(a, b):\n    return a / b if b else 0\n")
        head = self.remote.commit("feature")
        git("push", "-q", "-f", bare, "HEAD:refs/pull/1/head", cwd=work)
        git("checkout", "-q", "main", cwd=work)
        self.review_at(head, base="lower")
        return head

    def squash_the_pr_below(self):
        self.remote.write("README.md", "demo, with docs\n")  # as stacked() made it on `lower`
        squashed = self.remote.commit("the PR below (squashed)")
        git("push", "-q", str(self.remote.bare), "main", cwd=self.remote.work)
        return squashed

    def test_a_retarget_after_the_pr_below_merges_continues_with_a_followup(self):
        stacked = self.stacked()
        self.squash_the_pr_below()
        work = self.remote.work
        git("checkout", "-q", "pr1", cwd=work)  # this PR is retargeted to main, merges it, and fixes its finding
        git("merge", "-q", "-m", "merge main", "main", cwd=work)
        head = self.remote.push_pr(1, {"src/calc.py": FIXED})
        second, _ = self.review_at(head)
        self.assertEqual(self.rows("SELECT review_kind, followup_of FROM jobs WHERE id=?", second),
                         [{"review_kind": "followup", "followup_of": stacked}])
        since = self.context(second, "since-review.diff").read_text()
        self.assertIn("raise ValueError", since)
        self.assertNotIn("README.md", since)  # the merged PR below isn't this PR's change

    def test_a_retarget_to_a_base_without_the_old_ones_changes_means_a_full_review(self):
        self.stacked()  # the PR below never merges; this one moves to main anyway
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(self.rows("SELECT review_kind FROM jobs WHERE id=?", second), [{"review_kind": "full"}])

    def test_a_retarget_without_a_push_carries_the_findings_on(self):
        stacked = self.stacked()
        (finding,) = self.rows("SELECT id FROM findings WHERE kind='finding'")
        self.squash_the_pr_below()
        second, _ = self.review_at(stacked)  # GitHub retargets this PR to main; its head doesn't change
        self.assertEqual(self.rows("SELECT review_kind, followup_of FROM jobs WHERE id=?", second),
                         [{"review_kind": "followup", "followup_of": stacked}])
        self.assertEqual([f["id"] for f in json.loads(self.context(second, "previous-findings.json").read_text())],
                         [finding["id"]])
        self.assertEqual(self.context(second, "since-review.diff").read_text(), "")

    def test_contains(self):
        work = self.remote.work
        contains = lambda sha, other: self.svc.git.contains(work, sha, other)  # noqa: E731
        initial = self.remote.base_sha
        self.assertTrue(contains(self.head_a, initial))  # an ancestor
        git("checkout", "-q", "-B", "lower", "main", cwd=work)
        self.remote.write("README.md", "demo, with docs\n")
        lower = self.remote.commit("the PR below")
        git("checkout", "-q", "main", cwd=work)
        self.assertFalse(contains("main", lower))  # not merged
        squashed = self.squash_the_pr_below()
        self.assertTrue(contains(squashed, lower))  # merged by squash: not an ancestor, but nothing to add
        git("checkout", "-q", "lower", cwd=work)
        self.remote.write("README.md", "demo, with other docs\n")
        later = self.remote.commit("the PR below, changed again")
        git("checkout", "-q", "main", cwd=work)
        self.assertFalse(contains(squashed, later))  # conflicts with what merged
        self.assertFalse(contains(squashed, "f" * 40))  # not in the repository

    def test_a_round_on_the_old_base_gone_from_the_mirror_means_a_full_review(self):
        self.stacked()
        self.squash_the_pr_below()  # so the real old base would count
        conn = self.svc.connect()
        with db.transaction(conn):
            conn.execute("UPDATE jobs SET base_sha=?", ("f" * 40,))
        conn.close()
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(self.rows("SELECT review_kind FROM jobs WHERE id=?", second), [{"review_kind": "full"}])

    def test_only_the_latest_rounds_base_counts(self):
        first, _ = self.review_at(self.head_a)
        conn = self.svc.connect()
        with db.transaction(conn):  # a later full round at the same head on another base
            other = db.insert_job(conn, repo=REPO, number=1, head_sha=self.head_a, base_ref="lower", agent="claude",
                                  model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, other, status="posted", review_recorded=1, finished_at=self.clock() + 1,
                          base_sha=self.remote.base_sha)
            conn.execute("INSERT INTO findings (job_id, kind, position, title) VALUES (?, 'finding', 0, 'other')",
                         (other,))
        conn.close()
        self.clock.advance(60)
        git("push", "-q", str(self.remote.bare), "main:refs/heads/lower", cwd=self.remote.work)
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}), base="lower")
        self.assertEqual([f["title"] for f in json.loads(self.context(second, "previous-findings.json").read_text())],
                         ["other"])

    def test_the_followup_prompt_closes_once(self):
        self.review_at(self.head_a)
        self.svc = self.services(make_config(self.tmp, agents=("claude",),
                                             repo_extra={"extra_instructions": "Prefer the stdlib."}))
        self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        prompt = self.agent_log("claude")[-1]["prompt"]
        self.assertEqual(prompt.count("Return your "), 1)
        self.assertEqual(prompt.count("Prefer the stdlib."), 1)

    def test_findings_export_includes_followup_statuses(self):
        import contextlib, io, os
        from unittest import mock
        from local_autoreviewer import __main__ as cli
        first, _ = self.review_at(self.head_a)
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"STATE_DIRECTORY": str(self.state)}), contextlib.redirect_stdout(out):
            cli.main(["findings", "--json", "--job", str(first), "--kind", "finding"])
        (row,) = json.loads(out.getvalue())
        self.assertEqual([(r["job_id"], r["status"]) for r in row["resolutions"]], [(second, "fixed")])

    def test_a_trial_is_not_a_round(self):
        self.review_at(self.head_a, post=False, source=db.FOREGROUND)
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(self.rows("SELECT review_kind FROM jobs WHERE id=?", second), [{"review_kind": "full"}])

    def test_a_previous_head_gone_from_the_mirror_means_a_full_review(self):
        conn = self.svc.connect()
        with db.transaction(conn):  # a round whose commit was force-pushed away and garbage-collected
            gone = db.insert_job(conn, repo=REPO, number=1, head_sha="f" * 40, base_ref="main", agent="claude",
                                 model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, gone, status="posted", review_recorded=1, finished_at=self.clock())
        conn.close()
        job_id, status = self.review_at(self.head_a)
        self.assertEqual(status, "posted")
        self.assertEqual(self.rows("SELECT review_kind FROM jobs WHERE id=?", job_id), [{"review_kind": "full"}])

    def test_new_code_means_the_followup_reviews_the_whole_pr(self):
        self.review_at(self.head_a)
        self.svc = self.services(make_config(self.tmp, agents=("claude",), claude="newcode"))
        second, status = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(status, "posted")
        prompt = self.agent_log("claude")[-1]["prompt"]
        self.assertIn("## Method: this is the only pass", prompt)
        self.assertIn('"new_code"', self.context(second, "followup.schema.json").read_text())
        (job,) = self.rows("SELECT review_kind, new_code FROM jobs WHERE id=?", second)
        self.assertEqual((job["review_kind"], job["new_code"]), ("followup", 1))
        self.assertEqual([(r["kind"], r["title"]) for r in self.rows(
            "SELECT kind, title FROM findings WHERE job_id=? ORDER BY kind, position", second)],
            [("nit", "Name the new helper."), ("regression", "The fix dropped the docstring"),
             ("test_gap", "No test for the new code.")])
        posted = self.github.posted[-1]
        self.assertIn("#### New findings", posted["body"])
        self.assertIn("#### Test gaps\n- No test for the new code.", posted["body"])
        self.assertEqual(len(posted["comments"]), 2)  # the regression's suggestion and the nit's
        # The next follow-up asks about the regression, not the nit or the test gap.
        third, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "\n"}))
        previous = json.loads(self.context(third, "previous-findings.json").read_text())
        self.assertEqual([p["title"] for p in previous], ["The fix dropped the docstring"])

    def test_the_followup_is_told_what_was_already_raised(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT id FROM findings WHERE job_id=? AND kind='finding'", first)
        conn = self.svc.connect()
        with db.transaction(conn):
            db.triage(conn, finding["id"], now=self.clock(), disposition="declined", note="Callers never pass 0.")
        conn.close()
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(json.loads(self.context(second, "previous-findings.json").read_text()), [])
        raised = json.loads(self.context(second, "already-raised.json").read_text())
        self.assertEqual(sorted((r["kind"], r["text"]) for r in raised),
                         [("finding", "Division by zero is unhandled"), ("nit", "Add a docstring."),
                          ("test_gap", "No test for b == 0.")])
        declined = next(r for r in raised if r["kind"] == "finding")
        self.assertEqual((declined["decision"], declined["note"], declined["reviewer"], declined["raised_at"]),
                         ("declined", "Callers never pass 0.", "claude", self.head_a))
        self.assertNotIn("decision", next(r for r in raised if r["kind"] == "nit"))

    def test_only_the_latest_raised_items_are_shown(self):
        rows = [{"kind": "nit", "agent": "claude", "file": "a.py", "line": n, "raised_at": "abc", "title": f"nit {n}",
                 "disposition": None, "note": None} for n in range(pipeline.MAX_RAISED + 5)]
        shown = pipeline.PreviousRound("abc", [], rows).raised_for_agent()
        self.assertEqual(len(shown), pipeline.MAX_RAISED)
        self.assertEqual((shown[0]["text"], shown[-1]["text"]), ("nit 5", f"nit {pipeline.MAX_RAISED + 4}"))

    def test_a_finding_the_author_declines_is_not_asked_about_again(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT id, title FROM findings WHERE job_id=? AND kind='finding'", first)
        suggestion = next(c for c in self.github.review_comments[(REPO, 1)] if finding["title"] in c["body"])
        self.github.comment(1, "Won't fix: callers never pass 0.", reply_to=suggestion["id"])
        self.github.comment(1, "won't fix, says someone else", author="someone-else")  # not the author: ignored
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        comments = json.loads(self.context(second, "author-comments.json").read_text())
        self.assertEqual([(c["kind"], c["body"]) for c in comments], [("reply", "Won't fix: callers never pass 0.")])
        self.assertIn(finding["title"], comments[0]["thread"])
        self.assertEqual(self.rows("SELECT status FROM resolutions WHERE job_id=?", second), [{"status": "declined"}])
        self.assertIn(f"🙅 Declined by the author: {finding['title']}", self.github.posted[-1]["body"])
        third, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "\n"}))
        previous = json.loads(self.context(third, "previous-findings.json").read_text())
        self.assertNotIn(finding["id"], [p["id"] for p in previous])

    def test_a_followup_goes_on_without_the_authors_comments_if_github_fails(self):
        from local_autoreviewer.github import GitHubError
        self.review_at(self.head_a)
        self.github.fail = GitHubError("HTTP 502")
        second, status = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(json.loads(self.context(second, "author-comments.json").read_text()), [])
        self.github.fail = None
        self.assertEqual(status, "posted")
        self.assertEqual(self.rows("SELECT review_kind FROM jobs WHERE id=?", second), [{"review_kind": "followup"}])

    def test_a_push_during_the_comment_listing_starts_no_review(self):
        self.review_at(self.head_a)
        calls = len(self.agent_log("claude"))
        head_b = self.remote.push_pr(1, {"src/calc.py": FIXED})
        self.github.set_pr(1, head_b)
        conn = self.svc.connect()
        with db.transaction(conn):
            job_id = db.insert_job(conn, repo=REPO, number=1, head_sha=head_b, base_ref="main", agent="claude",
                                   model="opus", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, job_id, status="running", started_at=self.clock())
        conn.close()
        token = pipeline.CancelToken()

        def listing(*args):
            token.cancel("superseded: head moved")
            return []
        from unittest import mock
        with mock.patch.object(pipeline, "_author_comments", side_effect=listing):
            status = pipeline.run_job(self.svc, job_id, token)
        self.assertEqual(status, "cancelled")  # "superseded" when the canceller records why, as the worker does
        self.assertEqual(len(self.agent_log("claude")), calls)  # the reviewer never started
        # Cancelled before the listing: no GitHub calls for it at all.
        conn = self.svc.connect()
        with db.transaction(conn):
            again = db.insert_job(conn, repo=REPO, number=1, head_sha=head_b, base_ref="main", agent="claude",
                                  model="opus", source="manual", not_before=0, now=self.clock(),
                                  rerun=db.next_rerun(conn, REPO, 1, head_b, "main", "claude"))
            db.update_job(conn, again, status="running", started_at=self.clock())
        conn.close()
        early = pipeline.CancelToken()
        early.cancel("shutdown")
        with mock.patch.object(pipeline, "_author_comments") as listing:
            pipeline.run_job(self.svc, again, early)
        listing.assert_not_called()

    def decline_first_finding(self):
        """A first review whose one finding the author declines in its thread, and the follow-up that marks it."""
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT id, title FROM findings WHERE job_id=? AND kind='finding'", first)
        suggestion = next(c for c in self.github.review_comments[(REPO, 1)] if finding["title"] in c["body"])
        self.github.comment(1, "Won't fix: callers never pass 0.", reply_to=suggestion["id"])
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        return finding, second

    def test_a_declined_finding_is_already_raised_for_a_whole_pr_review(self):
        finding, _ = self.decline_first_finding()
        self.svc = self.services(make_config(self.tmp, agents=("claude",), claude="newcode"))
        third, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "\n"}))
        raised = {r["text"]: r for r in json.loads(self.context(third, "already-raised.json").read_text())}
        self.assertEqual((raised[finding["title"]]["decision"], raised[finding["title"]]["note"]), ("declined", "checked"))
        self.assertNotIn(finding["id"], [p["id"] for p in
                                         json.loads(self.context(third, "previous-findings.json").read_text())])

    def test_a_finding_one_reviewer_still_has_open_is_asked_about_again(self):
        finding, second = self.decline_first_finding()
        conn = self.svc.connect()
        with db.transaction(conn):  # the round's other reviewer found it not fixed
            other = db.insert_job(conn, repo=REPO, number=1, head_sha=self.rows(
                "SELECT head_sha FROM jobs WHERE id=?", second)[0]["head_sha"], base_ref="main", agent="codex",
                model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, other, status="posted", review_recorded=1, finished_at=self.clock(),
                          review_kind="followup", followup_of=self.head_a)
            conn.execute("INSERT INTO resolutions (job_id, finding_id, status) VALUES (?, ?, 'not_fixed')",
                         (other, finding["id"]))
        conn.close()
        third, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "\n"}))
        previous = json.loads(self.context(third, "previous-findings.json").read_text())
        self.assertIn(finding["id"], [p["id"] for p in previous])
        raised = json.loads(self.context(third, "already-raised.json").read_text())
        self.assertNotIn(finding["title"], [r["text"] for r in raised])  # asked about, so not listed as closed

    def test_a_saved_followup_keeps_new_code(self):
        self.review_at(self.head_a)
        self.svc = self.services(make_config(self.tmp, agents=("claude",), claude="newcode"))
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        conn = self.svc.connect()
        with db.transaction(conn):  # as if recording had failed
            conn.execute("DELETE FROM resolutions WHERE job_id=?", (second,))
            conn.execute("DELETE FROM findings WHERE job_id=?", (second,))
            db.update_job(conn, second, review_recorded=0, new_code=0)
        try:
            maintenance.record_saved_reviews(conn, self.svc)
            saved = pipeline._saved_review(self.svc, db.get_job(conn, second))
        finally:
            conn.close()
        self.assertEqual(self.rows("SELECT new_code FROM jobs WHERE id=?", second), [{"new_code": 1}])
        self.assertTrue(saved["review"]["new_code"])
        self.assertEqual(saved["review"]["test_gaps"], ["No test for the new code."])

    def thread_of(self, title):
        (comment,) = [c for c in self.github.review_comments[(REPO, 1)] if title in c["body"].split("\n", 1)[0]]
        return f"T{comment['id']}"

    def test_a_fixed_findings_thread_is_resolved(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT title FROM findings WHERE job_id=? AND kind='finding'", first)
        fixed, nit = self.thread_of(finding["title"]), self.thread_of("Add a docstring.")
        self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        # Fixed: its thread is resolved. The nit isn't asked about, and the follow-up's own regression is new.
        self.assertEqual(self.github.resolved, {fixed})
        self.assertNotIn(self.thread_of("The fix dropped the docstring"), self.github.resolved)
        self.assertNotIn(nit, self.github.resolved)

    def test_a_finding_not_fixed_by_every_reviewer_keeps_its_thread(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT id, title FROM findings WHERE job_id=? AND kind='finding'", first)
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.github.resolved.clear()
        conn = self.svc.connect()
        with db.transaction(conn):  # as if a second reviewer of the round had said partly fixed
            head_b = db.get_job(conn, second)["head_sha"]
            other = db.insert_job(conn, repo=REPO, number=1, head_sha=head_b, base_ref="main", agent="codex",
                                  model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, other, posted_with=second, status="posted", review_recorded=1,
                          review_kind="followup", followup_of=self.head_a)
            conn.execute("INSERT INTO resolutions (job_id, finding_id, status) VALUES (?, ?, 'partly_fixed')",
                         (other, finding["id"]))
        try:
            job = db.get_job(conn, second)
            self.assertEqual(pipeline.resolve_fixed_threads(conn, self.svc, job), 0)
            with db.transaction(conn):
                conn.execute("UPDATE resolutions SET status='obsolete' WHERE job_id=?", (other,))
            self.assertEqual(pipeline.resolve_fixed_threads(conn, self.svc, job), 1)  # fixed and obsolete both close
        finally:
            conn.close()
        self.assertEqual(self.github.resolved, {self.thread_of(finding["title"])})

    def test_a_github_failure_leaves_the_threads_open(self):
        from local_autoreviewer.github import GitHubError
        self.review_at(self.head_a)
        self.github.fail_threads = GitHubError("HTTP 403: Resource not accessible by personal access token")
        with self.assertLogs("local_autoreviewer.pipeline", "WARNING") as logs:
            _, status = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(status, "posted")
        self.assertEqual(self.github.resolved, set())
        self.assertTrue(any("they stay open" in line for line in logs.output))

    def test_no_error_while_resolving_reposts_the_review(self):
        self.review_at(self.head_a)
        self.github.fail_threads = RuntimeError("something nobody expected")
        second, status = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(status, "posted")
        self.assertEqual(self.rows("SELECT status FROM jobs WHERE id=?", second), [{"status": "posted"}])
        self.assertEqual(len(self.github.posted), 2)  # the first review and the follow-up, once each

    def test_a_follow_ups_own_regression_is_resolved_when_fixed_later(self):
        self.review_at(self.head_a)
        self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))  # raises the regression, inline
        regression = self.thread_of("The fix dropped the docstring")
        self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "\n"}))  # finds it fixed
        self.assertIn(regression, self.github.resolved)

    def test_a_post_confirmed_by_recovery_resolves_too(self):
        from unittest import mock
        from local_autoreviewer.github import GitHubError
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT title FROM findings WHERE job_id=? AND kind='finding'", first)
        real = self.github.post_review

        def accepted_then_timed_out(*args, **kwargs):
            real(*args, **kwargs)
            raise GitHubError("gh api timed out")
        with mock.patch.object(self.github, "post_review", side_effect=accepted_then_timed_out):
            second, status = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(status, "posting")
        self.assertEqual(self.github.resolved, set())
        conn = self.svc.connect()
        try:
            self.assertEqual(pipeline.recover_post(conn, self.svc, db.get_job(conn, second)), "posted")
        finally:
            conn.close()
        self.assertEqual(self.github.resolved, {self.thread_of(finding["title"])})

    def test_separate_posts_must_agree_and_finish_first(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT id, title FROM findings WHERE job_id=? AND kind='finding'", first)
        head_b = self.remote.push_pr(1, {"src/calc.py": FIXED})
        conn = self.svc.connect()
        with db.transaction(conn):  # the other reviewer of head_b, still running, posting on its own
            other = db.insert_job(conn, repo=REPO, number=1, head_sha=head_b, base_ref="main", agent="codex",
                                  model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, other, status="running")
        conn.close()
        self.svc = self.services(make_config(self.tmp, agents=("claude",), service={"combine_wait": "0s"}))
        second, status = self.review_at(head_b)
        self.assertEqual(status, "posted")
        self.assertEqual(self.github.resolved, set())  # Codex hasn't answered yet
        conn = self.svc.connect()
        try:
            with db.transaction(conn):  # Codex posts on its own, but its answers weren't recorded
                db.update_job(conn, other, status="posted", followup_of=self.head_a, review_kind="followup")
            self.assertEqual(pipeline.resolve_fixed_threads(conn, self.svc, db.get_job(conn, other)), 0)
            with db.transaction(conn):  # once they are: only partly fixed
                db.update_job(conn, other, review_recorded=1)
                conn.execute("INSERT INTO resolutions (job_id, finding_id, status) VALUES (?, ?, 'partly_fixed')",
                             (other, finding["id"]))
            self.assertEqual(pipeline.resolve_fixed_threads(conn, self.svc, db.get_job(conn, other)), 0)
            with db.transaction(conn):
                conn.execute("UPDATE resolutions SET status='fixed' WHERE job_id=?", (other,))
            self.assertEqual(pipeline.resolve_fixed_threads(conn, self.svc, db.get_job(conn, other)), 1)
        finally:
            conn.close()

    def test_a_heading_two_findings_share_waits_for_both(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT * FROM findings WHERE job_id=? AND kind='finding'", first)
        conn = self.svc.connect()
        with db.transaction(conn):  # a second finding with the same title, in another file
            conn.execute("INSERT INTO findings (job_id, kind, position, severity, category, file, line, title)"
                         " VALUES (?, 'finding', 1, 'minor', 'correctness', 'src/other.py', 3, ?)",
                         (first, finding["title"]))
        conn.close()
        # The follow-up finds the first fixed and the second not (fake: only the first previous finding is fixed).
        self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(self.github.resolved, set())
        # The next one finds the second fixed too: now the shared heading's thread goes.
        self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "\n"}))
        self.assertIn(self.thread_of(finding["title"]), self.github.resolved)

    def test_a_sibling_that_ends_without_posting_lets_the_threads_go(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT title FROM findings WHERE job_id=? AND kind='finding'", first)
        head_b = self.remote.push_pr(1, {"src/calc.py": FIXED})
        conn = self.svc.connect()
        with db.transaction(conn):  # the other reviewer of head_b, still running
            other = db.insert_job(conn, repo=REPO, number=1, head_sha=head_b, base_ref="main", agent="codex",
                                  model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, other, status="running")
        conn.close()
        self.svc = self.services(make_config(self.tmp, agents=("claude",), service={"combine_wait": "0s"}))
        second, _ = self.review_at(head_b)
        self.assertEqual(self.github.resolved, set())  # waits for Codex
        conn = self.svc.connect()
        try:
            from unittest import mock
            with mock.patch.object(self.github, "pr_view") as pr_view:
                pipeline._resolve_after_post(conn, self.svc, db.get_job(conn, second))
            pr_view.assert_not_called()  # Codex still active: the sweep would wait, so GitHub isn't asked
            pipeline._finish(conn, self.svc, other, "failed", "agent failed 3 times")  # Codex never posts
        finally:
            conn.close()
        self.assertEqual(self.github.resolved, {self.thread_of(finding["title"])})

    def test_a_full_review_posting_last_lets_the_threads_go(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT title FROM findings WHERE job_id=? AND kind='finding'", first)
        head_b = self.remote.push_pr(1, {"src/calc.py": FIXED})
        conn = self.svc.connect()
        with db.transaction(conn):  # a full review of head_b by the other reviewer, still running
            other = db.insert_job(conn, repo=REPO, number=1, head_sha=head_b, base_ref="main", agent="codex",
                                  model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, other, status="running")
        conn.close()
        self.svc = self.services(make_config(self.tmp, agents=("claude",), service={"combine_wait": "0s"}))
        self.review_at(head_b)
        self.assertEqual(self.github.resolved, set())
        conn = self.svc.connect()
        try:
            with db.transaction(conn):
                db.update_job(conn, other, status="posted", review_recorded=1)
            pipeline._resolve_after_post(conn, self.svc, db.get_job(conn, other))  # as post() does
        finally:
            conn.close()
        self.assertEqual(self.github.resolved, {self.thread_of(finding["title"])})

    def test_an_older_commit_still_being_reviewed_holds_its_findings(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT title FROM findings WHERE job_id=? AND kind='finding'", first)
        head_b = self.remote.push_pr(1, {"src/calc.py": FIXED})
        conn = self.svc.connect()
        with db.transaction(conn):  # Codex's review of head_b: accepted by GitHub, not yet confirmed here
            other = db.insert_job(conn, repo=REPO, number=1, head_sha=head_b, base_ref="main", agent="codex",
                                  model="", source="poll", not_before=0, now=self.clock())
            db.update_job(conn, other, status="posting")
        conn.close()
        self.svc = self.services(make_config(self.tmp, agents=("claude",), service={"combine_wait": "0s"}))
        self.review_at(head_b)  # Claude: fixed
        self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED + "\n"}))  # a later commit's follow-up
        self.assertNotIn(self.thread_of(finding["title"]), self.github.resolved)

    def test_a_github_blip_before_resolving_is_a_warning(self):
        from unittest import mock
        from local_autoreviewer.github import GitHubError
        self.review_at(self.head_a)
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        conn = self.svc.connect()
        try:
            with mock.patch.object(self.github, "pr_view", side_effect=GitHubError("HTTP 502")), \
                    self.assertLogs("local_autoreviewer.pipeline", "WARNING") as logs:
                pipeline._resolve_after_post(conn, self.svc, db.get_job(conn, second))
        finally:
            conn.close()
        self.assertEqual([line.split(":", 1)[0] for line in logs.output], ["WARNING"])  # no traceback

    def test_a_post_recovered_after_a_push_resolves_nothing(self):
        from unittest import mock
        from local_autoreviewer.github import GitHubError
        self.review_at(self.head_a)
        real = self.github.post_review

        def accepted_then_timed_out(*args, **kwargs):
            real(*args, **kwargs)
            raise GitHubError("gh api timed out")
        with mock.patch.object(self.github, "post_review", side_effect=accepted_then_timed_out):
            second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.github.set_pr(1, self.remote.push_pr(1, {"src/calc.py": "def divide(a, b):\n    return a / b\n"}))
        conn = self.svc.connect()
        try:
            self.assertEqual(pipeline.recover_post(conn, self.svc, db.get_job(conn, second)), "posted")
        finally:
            conn.close()
        self.assertEqual(self.github.resolved, set())  # its "fixed" may not hold at the new head

    def test_a_title_over_several_lines_is_matched(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT id, title FROM findings WHERE job_id=? AND kind='finding'", first)
        conn = self.svc.connect()
        with db.transaction(conn):
            conn.execute("UPDATE findings SET title=? WHERE id=?", ("Division by zero\nis unhandled", finding["id"]))
        conn.close()
        root = next(c for c in self.github.review_comments[(REPO, 1)] if finding["title"] in c["body"])
        root["body"] = root["body"].replace(finding["title"], "Division by zero\nis unhandled")
        self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(self.github.resolved, {f"T{root['id']}"})

    def test_a_failure_partway_keeps_what_was_resolved(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT title FROM findings WHERE job_id=? AND kind='finding'", first)
        root = next(c for c in self.github.review_comments[(REPO, 1)] if finding["title"] in c["body"])
        self.github.review_comments[(REPO, 1)].append(dict(root, id=root["id"] + 1000))  # a second thread
        self.github.fail_resolve_after = 1
        with self.assertLogs("local_autoreviewer.pipeline", "WARNING") as logs:
            _, status = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(status, "posted")
        self.assertEqual(len(self.github.resolved), 1)
        self.assertTrue(any("GitHubError: HTTP 502" in line and "they stay open" in line for line in logs.output))

    def test_every_open_thread_of_a_finding_is_resolved_once(self):
        first, _ = self.review_at(self.head_a)
        (finding,) = self.rows("SELECT title FROM findings WHERE job_id=? AND kind='finding'", first)
        root = next(c for c in self.github.review_comments[(REPO, 1)] if finding["title"] in c["body"])
        for n in (1, 2):  # as before 0.7.0: a thread per edit, one of them already resolved by hand
            self.github.review_comments[(REPO, 1)].append(dict(root, id=root["id"] + 1000 * n))
        self.github.resolved.add(f"T{root['id'] + 2000}")
        self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        self.assertEqual(sorted(self.github.resolve_calls), sorted([f"T{root['id']}", f"T{root['id'] + 1000}"]))

    def test_a_saved_followup_is_recorded_as_one(self):
        self.review_at(self.head_a)
        second, _ = self.review_at(self.remote.push_pr(1, {"src/calc.py": FIXED}))
        conn = self.svc.connect()
        with db.transaction(conn):  # as if recording had failed
            conn.execute("DELETE FROM resolutions WHERE job_id=?", (second,))
            conn.execute("DELETE FROM findings WHERE job_id=?", (second,))
            db.update_job(conn, second, review_recorded=0, review_kind="full", followup_of=None)
        try:
            maintenance.record_saved_reviews(conn, self.svc)
        finally:
            conn.close()
        (job,) = self.rows("SELECT review_kind, followup_of FROM jobs WHERE id=?", second)
        self.assertEqual((job["review_kind"], job["followup_of"]), ("followup", self.head_a))
        self.assertEqual(len(self.rows("SELECT * FROM resolutions WHERE job_id=?", second)), 1)


class FollowupFormatTests(unittest.TestCase):
    def test_validate_keeps_one_status_per_known_finding(self):
        raw = {"summary": "s", "regressions": [],
               "resolutions": [{"finding": 1, "status": "fixed", "detail": "a"},
                               {"finding": 1, "status": "not_fixed", "detail": "dup"},
                               {"finding": 9, "status": "fixed", "detail": "unknown"},
                               {"finding": 2, "status": "maybe", "detail": "bad status"}]}
        self.assertEqual(review.validate_followup(raw, {1, 2})["resolutions"],  # 2 had no valid answer
                         [{"finding": 1, "status": "fixed", "detail": "a"},
                          {"finding": 2, "status": "unchecked", "detail": ""}])
        with self.assertRaises(review.ReviewFormatError):
            review.validate_followup({"summary": ""}, set())

    def test_render(self):
        previous = [{"id": 1, "agent": "codex", "severity": "major", "title": "Leaks a handle", "file": "a.py",
                     "line": 3},
                    {"id": 2, "agent": "claude", "severity": "minor", "title": "Typo", "file": "", "line": 0}]
        data = {"summary": "One fixed.", "findings": [], "resolutions": [{"finding": 1, "status": "fixed",
                                                                          "detail": "closed in a with block"}]}
        body = review.render_followup(data, previous, repo="o/r", number=5, sha="b" * 40, previous_sha="a" * 40,
                                      agent="claude", model="opus", job_id=7)
        self.assertIn("for the 2 issue(s) open after [`aaaaaaa`]", body)
        self.assertIn("https://github.com/o/r/blob/" + "a" * 40 + "/a.py#L3", body)  # where it was raised
        self.assertIn("- ✅ Fixed: Leaks a handle _(Codex, major, [`a.py:3`]", body)
        self.assertIn("— closed in a with block", body)
        self.assertIn("- ❔ Not checked: Typo _(Claude Code, minor)_", body)
        self.assertIn("No regressions found.", body)
        self.assertTrue(body.endswith(review.marker(7, "b" * 40, "claude")))

    def test_declined_is_a_status_that_closes_a_finding(self):
        self.assertIn("declined", review.followup_schema()["properties"]["resolutions"]["items"]["properties"]
                      ["status"]["enum"])
        self.assertNotIn("declined", review.OPEN_STATUSES)
        raw = {"summary": "s", "regressions": [], "resolutions": [{"finding": 1, "status": "declined", "detail": "x"}]}
        self.assertEqual(review.validate_followup(raw, {1})["resolutions"][0]["status"], "declined")

    def test_schema_reuses_the_finding_definition(self):
        schema = review.followup_schema()
        full = json.loads(review.schema_path().read_text())["properties"]
        self.assertEqual(schema["properties"]["regressions"]["items"], full["findings"]["items"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        for key in ("test_gaps", "questions", "nits"):  # for a follow-up that found new code
            self.assertEqual(schema["properties"][key]["items"], full[key]["items"])
        self.assertEqual(schema["properties"]["new_code"]["type"], "boolean")

    def test_validate_keeps_new_code_and_its_items(self):
        raw = {"summary": "s", "new_code": True, "regressions": [], "resolutions": [], "test_gaps": ["untested"],
               "questions": ["why?"], "nits": [{"file": "a.py", "line": 1, "text": "Rename", "edits": []}]}
        checked = review.validate_followup(raw, set())
        self.assertEqual((checked["new_code"], checked["test_gaps"], checked["questions"], len(checked["nits"])),
                         (True, ["untested"], ["why?"], 1))
        older = review.validate_followup({"summary": "s", "regressions": [], "resolutions": []}, set())
        self.assertEqual((older["new_code"], older["test_gaps"], older["questions"], older["nits"]),
                         (False, [], [], []))
        self.assertFalse(review.validate_followup(dict(raw, new_code="yes"), set())["new_code"])
        # Without new code there was no review of the whole PR, so whatever the agent put there is dropped.
        without = review.validate_followup(dict(raw, new_code=False), set())
        self.assertEqual((without["test_gaps"], without["questions"], without["nits"]), ([], [], []))

    def test_a_long_followup_drops_its_nits_first(self):
        nits = [{"file": "a.py", "line": 1, "text": "x" * 900} for _ in range(15)]
        data = review.validate_followup({"summary": "s", "new_code": True, "resolutions": [], "regressions": [
            {"severity": "minor", "title": "T", "file": "a.py", "line": 1, "detail": "d" * 5000} for _ in range(12)],
            "nits": nits}, set())
        body = review.render_followup(data, [], repo="o/r", number=5, sha="b" * 40, previous_sha="a" * 40,
                                      agent="claude", model="opus", job_id=7)
        self.assertLessEqual(len(body), review.MAX_BODY)
        self.assertIn("_Nits omitted: review too long._", body)
        self.assertNotIn("<summary>Nits", body)
        self.assertTrue(body.endswith(review.marker(7, "b" * 40, "claude")))

    def test_the_prompt_carries_the_full_review_for_new_code(self):
        prompt = review.build_followup_prompt(previous_sha="a" * 40, previous_count=1, repo="o/r", number=5,
                                              title="T", author="me", base_ref="main", head_sha="b" * 40,
                                              worktree="/w", context="/c")
        self.assertIn("/c/already-raised.json", prompt)
        order = [prompt.index(text) for text in ("3. **New code.**", "## A full review, for job 3",
                                                 "## Method: this is the only pass",
                                                 "## What to cover (a thorough review)", "## Proposed changes",
                                                 "## Severity and impact", "Return your follow-up")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("$", prompt)
        self.assertEqual(prompt.count("Return your "), 1)

    def test_render_new_code(self):
        previous = [{"id": 1, "agent": "codex", "severity": "major", "title": "Leaks a handle", "file": "a.py",
                     "line": 3}]
        data = review.validate_followup(
            {"summary": "Fixed, and adds an export command.", "new_code": True, "resolutions": [
                {"finding": 1, "status": "fixed", "detail": "closed"}],
             "regressions": [{"severity": "minor", "title": "Export ignores --since", "file": "b.py", "line": 2}],
             "test_gaps": ["Nothing tests export."], "questions": ["Why CSV?"],
             "nits": [{"file": "b.py", "line": 1, "text": "Name it export_rows."}]}, {1})
        body = review.render_followup(data, previous, repo="o/r", number=5, sha="b" * 40, previous_sha="a" * 40,
                                      agent="claude", model="opus", job_id=7)
        self.assertIn("The changes since add new code, so the reviewer also reviewed the whole PR.", body)
        self.assertIn("#### New findings\n1. **Export ignores --since**", body)
        self.assertIn("#### Test gaps\n- Nothing tests export.", body)
        self.assertIn("#### Questions\n- Why CSV?", body)
        self.assertIn("<summary>Nits (1)</summary>", body)
        self.assertNotIn("Regressions", body)
        quiet = review.render_followup(dict(data, findings=[]), previous, repo="o/r", number=5, sha="b" * 40,
                                       previous_sha="a" * 40, agent="claude", model="opus", job_id=7)
        self.assertIn("No new issues found.", quiet)

    def test_a_round_names_who_reviewed_the_whole_pr(self):
        def member(job_id, agent, new_code):
            data = review.validate_followup({"summary": f"{agent} checked.", "new_code": new_code,
                                             "regressions": [], "resolutions": []}, set())
            return {"job_id": job_id, "agent": agent, "model": "", "review": data}
        merged = review.merge([member(1, "claude", False), member(2, "codex", True)])
        body = review.render_followup_round(merged, [], repo="o/r", number=5, sha="b" * 40, previous_sha="a" * 40)
        self.assertIn("The changes since add new code, so Codex also reviewed the whole PR.", body)
        self.assertIn("**Codex:** codex checked.", body)
        # Claude's findings would only be regressions, so the round doesn't call them all new findings.
        self.assertIn("No regressions or new issues found.", body)
        regression = {"severity": "minor", "title": "Breaks X", "file": "a.py", "line": 1}

        def heading(*flags):
            members = [member(n, agent, flag) for n, (agent, flag) in enumerate(zip(("claude", "codex"), flags), 1)]
            members[0]["review"] = review.validate_followup({"summary": "s", "new_code": flags[0],
                                                             "regressions": [regression], "resolutions": []}, set())
            text = review.render_followup_round(review.merge(members), [], repo="o/r", number=5, sha="b" * 40,
                                                previous_sha="a" * 40)
            return next(line for line in text.splitlines() if line.startswith("#### "))
        self.assertEqual(heading(True, True), "#### New findings")
        self.assertEqual(heading(False, True), "#### Regressions and new findings")
        self.assertEqual(heading(False, False), "#### Regressions")

    def test_agents_take_the_followup_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), agents=("claude", "codex"))
            paths = pipeline.job_paths(tmp, 1)
            target = Path(tmp, "followup.schema.json")
            target.write_text(json.dumps(review.followup_schema()))
            argv = claude_mod.ClaudeAgent(cfg.agents["claude"], Path(tmp)).invocation(
                unit="u", paths=paths, prompt="p", session_id="s", schema=target).argv
            self.assertIn('"resolutions"', argv[argv.index("--json-schema") + 1])
            argv = codex_mod.CodexAgent(cfg.agents["codex"], Path(tmp)).invocation(
                unit="u", paths=paths, prompt="p", session_id=None, schema=target).argv
            self.assertEqual(argv[argv.index("--output-schema") + 1], str(target))


if __name__ == "__main__":
    unittest.main()
