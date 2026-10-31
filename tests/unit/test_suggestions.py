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
