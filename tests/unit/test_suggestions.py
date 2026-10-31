# SPDX-License-Identifier: GPL-3.0-or-later
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT  # noqa: F401  (puts src/ on sys.path)
from local_autoreviewer import review, suggestions
from local_autoreviewer.suggestions import DEFAULT_TEST_PATHS

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 a
+b
 c
 d
@@ -20,2 +21,0 @@
-x
-y
diff --git a/old.py b/old.py
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-gone
-gone
diff --git a/new.py b/new.py
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+hello
"""


def edit(file, start, end, replacement, **extra):
    return {"file": file, "start_line": start, "end_line": end, "replacement": replacement, **extra}


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.worktree = Path(self._tmp.name, "repo")
        (self.worktree / "src").mkdir(parents=True)
        (self.worktree / "tests").mkdir()
        (self.worktree / "src/app.py").write_text("a\nb\nc\nd\n")
        (self.worktree / "tests/test_app.py").write_text("t1\nt2\n")
        Path(self._tmp.name, "outside.py").write_text("secret\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_diff_hunks(self):
        self.assertEqual(suggestions.diff_hunks(DIFF), {"src/app.py": [(1, 4)], "new.py": [(1, 1)]})

    def test_test_paths(self):
        for path, expected in (("tests/test_app.py", True), ("pkg/tests/x.py", True), ("src/test_util.py", True),
                               ("web/app.test.js", True), ("spec/a_spec.rb", True), ("src/app.py", False),
                               ("src/contest.py", False), ("src/latest_tests.py", False)):
            with self.subTest(path=path):
                self.assertEqual(suggestions.is_test_path(path, DEFAULT_TEST_PATHS), expected)

    def test_check_edit(self):
        checked = suggestions.check_edit(edit("src/app.py", 2, 3, "B"), self.worktree)
        self.assertEqual(checked["original"], ["b", "c"])
        for bad in (edit("../outside.py", 1, 1, "x"), edit("src/missing.py", 1, 1, "x"),
                    edit("src/app.py", 4, 5, "x"), edit("/etc/hostname", 1, 1, "x")):
            with self.subTest(edit=bad):
                self.assertIsNone(suggestions.check_edit(bad, self.worktree))

    def place(self, *args, **extra):
        checked = suggestions.check_edit(edit(*args, **extra), self.worktree)
        return checked and (checked["start_line"], checked["end_line"], checked["anchor"], checked.get("stated"))

    def test_edits_are_anchored_by_their_current_text(self):
        self.assertEqual(self.place("src/app.py", 2, 3, "B", current="b\nc"), (2, 3, "matched", None))
        self.assertEqual(self.place("src/app.py", 3, 4, "B", current="b\nc\n"), (2, 3, "moved", [3, 4]))
        self.assertEqual(self.place("src/app.py", 4, 5, "D", current="d"), (4, 4, "moved", [4, 5]))  # past the end
        self.assertEqual(self.place("src/app.py", 1, 1, "A", current="zzz"), (1, 1, "dropped", [1, 1]))
        self.assertEqual(self.place("src/app.py", 4, 4, "D", current="c\nd\ne"), (4, 4, "dropped", [4, 4]))
        self.assertEqual(self.place("src/app.py", 1, 1, "A"), (1, 1, "unverified", None))  # an older review
        self.assertIsNone(self.place("src/app.py", 4, 5, "D"))
        checked = suggestions.check_edit(edit("src/app.py", 2, 3, "B", current="b\nc"), self.worktree)
        self.assertEqual(checked["original"], ["b", "c"])
        dropped = suggestions.check_edit(edit("src/app.py", 1, 1, "A", current="zzz"), self.worktree)
        self.assertEqual(dropped["original"], [])

    def test_anchoring_prefers_the_nearest_copy_and_ignores_trailing_whitespace(self):
        (self.worktree / "src/lib.py").write_text("return x\nq\n\ny = 2  \nreturn x\n")
        self.assertEqual(self.place("src/lib.py", 5, 5, "r", current="return x"), (5, 5, "matched", None))
        self.assertEqual(self.place("src/lib.py", 4, 4, "r", current="return x"), (5, 5, "moved", [4, 4]))
        self.assertEqual(self.place("src/lib.py", 2, 2, "r", current="return x"), (1, 1, "moved", [2, 2]))
        self.assertEqual(self.place("src/lib.py", 4, 4, "y = 3", current="y = 2"), (4, 4, "matched", None))
        self.assertEqual(self.place("src/lib.py", 2, 2, "", current=""), (3, 3, "moved", [2, 2]))  # a blank line
        checked = suggestions.check_edit(edit("src/lib.py", 4, 4, "y = 3", current="y = 2"), self.worktree)
        self.assertEqual(checked["original"], ["y = 2  "])  # the file's text, for the shown diff and loc_delta

    def test_anchoring_looks_only_near_the_stated_lines(self):
        (self.worktree / "src/long.py").write_text("target\n" + "pad\n" * 100 + "tail\n")  # tail is line 102
        near, far = 1 + suggestions.ANCHOR_WINDOW, 2 + suggestions.ANCHOR_WINDOW
        self.assertEqual(self.place("src/long.py", near, near, "t", current="target"), (1, 1, "moved", [near, near]))
        self.assertEqual(self.place("src/long.py", far, far, "t", current="target")[2], "dropped")
        near, far = 102 - suggestions.ANCHOR_WINDOW, 101 - suggestions.ANCHOR_WINDOW
        self.assertEqual(self.place("src/long.py", near, near, "t", current="tail"), (102, 102, "moved", [near, near]))
        self.assertEqual(self.place("src/long.py", far, far, "t", current="tail")[2], "dropped")

    def test_dropped_edits_count_for_nothing(self):
        edits = [suggestions.check_edit(e, self.worktree) for e in
                 (edit("src/app.py", 1, 2, "", current="a\nb"), edit("src/app.py", 1, 3, "x\ny", current="nope"))]
        self.assertEqual(suggestions.loc_delta(edits, DEFAULT_TEST_PATHS), -2)
        data = {"findings": [self.finding("minor", [edit("src/app.py", 1, 1, "", current="nope")])], "nits": []}
        out = suggestions.apply(data, worktree=self.worktree, hunks={"src/app.py": [(1, 4)]},
                                test_paths=DEFAULT_TEST_PATHS)
        (finding,) = out["findings"]
        self.assertEqual((finding["loc_delta"], finding["severity"]), (0, "minor"))  # not raised for removing a line
        self.assertEqual([(e["anchor"], e["inline"]) for e in finding["edits"]], [("dropped", False)])
        self.assertEqual(suggestions.comments(out, review.comment_label(out)), [])

    def test_loc_delta_ignores_tests(self):
        edits = [suggestions.check_edit(e, self.worktree) for e in
                 (edit("src/app.py", 1, 3, "abc"), edit("tests/test_app.py", 1, 1, "t1\nt1b\nt1c"))]
        self.assertEqual(suggestions.loc_delta(edits, DEFAULT_TEST_PATHS), -2)
        self.assertEqual(suggestions.replacement_lines(""), [])
        self.assertEqual(suggestions.replacement_lines("x\n"), ["x"])

    def test_severity_modifiers(self):
        cases = [
            (("minor", -3, "same"), "major"), (("major", -1, "same"), "blocker"), (("minor", 2, "same"), "minor"),
            (("major", 0, "worse"), "nit"), (("minor", 0, "worse"), "nit"), (("major", -1, "worse"), "minor"),
            (("minor", -1, "worse"), "nit"), (("blocker", -5, "worse"), "blocker"), (("minor", 0, "better"), "minor"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(suggestions.adjust(*args)[0], expected)
        self.assertEqual(suggestions.adjust("minor", -2, "worse")[1], ["removes 2 lines", "less readable"])
        self.assertEqual(suggestions.adjust("blocker", -2, "worse")[1], [])

    def test_fence_cannot_be_closed_early(self):
        block = suggestions.fence("suggestion", "x = '```'\n````")
        self.assertTrue(block.startswith("`````suggestion\n"))
        self.assertTrue(block.endswith("\n`````"))
        self.assertEqual(suggestions.fence("suggestion", ""), "```suggestion\n```")  # deletes the lines

    def finding(self, severity="minor", edits=(), readability="same", title="t"):
        return {"severity": severity, "readability": readability, "title": title, "edits": list(edits)}

    def test_apply_adjusts_resorts_and_marks_inline(self):
        data = {"findings": [self.finding("major", [edit("src/app.py", 2, 2, "B")], readability="worse", title="m"),
                             self.finding("minor", [edit("src/app.py", 1, 3, "abc")], title="n"),
                             self.finding("minor", [edit("../outside.py", 1, 1, "x")], title="o")],
                "nits": [{"text": "t", "edits": [edit("tests/test_app.py", 1, 1, "T")]}]}
        hunks = {"src/app.py": [(1, 4)]}
        out = suggestions.apply(data, worktree=self.worktree, hunks=hunks, test_paths=DEFAULT_TEST_PATHS)
        self.assertEqual([(f["title"], f["reviewer_severity"], f["severity"]) for f in out["findings"]],
                         [("n", "minor", "major"), ("o", "minor", "minor"), ("m", "major", "nit")])
        self.assertEqual(out["findings"][1]["edits"], [])  # the edit outside the checkout was dropped
        self.assertTrue(out["findings"][0]["edits"][0]["inline"])
        self.assertFalse(out["nits"][0]["edits"][0]["inline"])  # tests/ isn't part of this diff

    def test_comments_for_inline_edits(self):
        data = {"findings": [self.finding("minor", [edit("src/app.py", 2, 3, "B\nC", original=["b", "c"], inline=True),
                                                    edit("src/app.py", 9, 9, "z", original=["q"], inline=False)])],
                "nits": [{"text": "a nit", "edits": [edit("src/app.py", 1, 1, "A", original=["a"], inline=True)]}]}
        found = suggestions.comments(data, review.comment_label(data))
        self.assertEqual(len(found), 2)
        self.assertEqual({key: found[0][key] for key in ("path", "start_line", "line", "side", "start_side")},
                         {"path": "src/app.py", "start_line": 2, "line": 3, "side": "RIGHT", "start_side": "RIGHT"})
        self.assertIn("**1. t**", found[0]["body"])
        self.assertIn("```suggestion\nB\nC\n```", found[0]["body"])
        self.assertNotIn("start_line", found[1])  # a one-line suggestion
        self.assertIn("**Nit:** a nit", found[1]["body"])


if __name__ == "__main__":
    unittest.main()
