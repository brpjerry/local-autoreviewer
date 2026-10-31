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
        self.assertIn("This is a follow-up, not a new review.", self.agent_log("claude")[-1]["prompt"])
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

    def test_schema_reuses_the_finding_definition(self):
        schema = review.followup_schema()
        finding = json.loads(review.schema_path().read_text())["properties"]["findings"]["items"]
        self.assertEqual(schema["properties"]["regressions"]["items"], finding)
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

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
