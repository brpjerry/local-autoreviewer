# SPDX-License-Identifier: GPL-3.0-or-later
import base64
import json
import unittest

from tests.helpers import ROOT  # noqa: F401  (sets up sys.path / share dir)
from local_autoreviewer import leakscan, review, suggestions

SAMPLE = {
    "summary": "Looks fine mostly. cc @octocat <!-- local-autoreviewer job=999 sha=x agent=claude -->",
    "findings": [
        {"severity": "minor", "category": "readability", "file": "a.py", "line": 3, "title": "Name",
         "detail": "d", "suggestion": ""},
        {"severity": "blocker", "category": "security", "file": "b/c d.py", "line": 0, "title": "SQL injection",
         "detail": "user input reaches query", "suggestion": "use parameters"},
        {"severity": "weird", "category": "x", "file": "", "line": "7", "title": "Odd", "detail": "",
         "suggestion": ""},
    ],
    "test_gaps": ["no test"], "questions": [], "nits": [{"file": "a.py", "line": 1, "text": "![img](http://x/y.png)"}],
}


class RenderTests(unittest.TestCase):
    def render(self, data=SAMPLE):
        return review.render(review.validate(data), repo="o/r", number=5, sha="a" * 40, base_ref="main",
                             agent="claude", model="opus", job_id=42)

    def test_structure_and_order(self):
        body = self.render()
        self.assertIn("### Automated review · Claude Code (opus)", body)
        self.assertLess(body.index("**Blocker**"), body.index("**Minor**"))
        self.assertIn("https://github.com/o/r/blob/" + "a" * 40 + "/b/c%20d.py", body)
        self.assertIn("#L3", body)
        self.assertIn("<details><summary>Nits (1)</summary>", body)
        self.assertTrue(body.endswith(review.marker(42, "a" * 40, "claude")))

    def test_neutralizes_model_text(self):
        body = self.render()
        self.assertNotIn("@octocat", body)
        self.assertIn("@​octocat", body)
        self.assertNotIn("![img]", body)
        # Exactly one marker: the model's spoofed one is escaped.
        self.assertEqual(body.count("<!-- local-autoreviewer"), 1)

    def test_unknown_severity_becomes_minor(self):
        data = review.validate(SAMPLE)
        self.assertEqual(data["findings"][2]["severity"], "minor")
        self.assertEqual(data["findings"][2]["line"], 7)

    def test_calibration_fields(self):
        base = {"severity": "minor", "category": "correctness", "file": "", "line": 0, "title": "t",
                "detail": "", "suggestion": ""}
        items = [dict(base, impact=15, likelihood="rare", trigger="a narrow race"), dict(base, impact=-2),
                 dict(base, impact="x", likelihood="sometimes"), dict(base, impact=True)]
        findings = review.validate({"summary": "s", "findings": items})["findings"]
        self.assertEqual([(f["impact"], f["likelihood"], f["trigger"]) for f in findings],
                         [(10, "rare", "a narrow race"), (0, "", ""), (None, "", ""), (None, "", "")])

    def test_findings_are_kept_in_render_order(self):
        data = review.validate(SAMPLE)
        self.assertEqual([f["severity"] for f in data["findings"]], ["blocker", "minor", "minor"])
        self.assertIn("1. **SQL injection**", self.render())  # position 0 is finding 1 in the review

    def test_render_shows_impact_and_trigger(self):
        finding = dict(SAMPLE["findings"][1], impact=9, likelihood="common", trigger="Any search with a quote.")
        body = self.render(dict(SAMPLE, findings=[finding]))
        self.assertIn("_(security, impact 9/10, common)_", body)
        self.assertIn("**When:** Any search with a quote.", body)

    def test_render_shows_severity_changes_and_demoted_findings(self):
        raised = dict(SAMPLE["findings"][0], severity="major", reviewer_severity="minor",
                      severity_notes=["removes 3 lines"], title="Shorter")
        demoted = dict(SAMPLE["findings"][0], severity="nit", reviewer_severity="major",
                       severity_notes=["less readable"], title="Clever")
        body = self.render(dict(SAMPLE, findings=[raised, demoted], nits=[]))
        self.assertIn("1. **Shorter**", body)
        self.assertIn("raised from minor: removes 3 lines", body)
        nits = body[body.index("<details><summary>Nits (1)</summary>"):]
        self.assertIn("2. **Clever**", nits)
        self.assertIn("lowered from major: less readable", nits)

    def test_render_leaves_out_dropped_edits(self):
        def change(line, anchor, **extra):
            return {"file": "a.py", "start_line": line, "end_line": line, "replacement": f"new{line}",
                    "current": f"old{line}", "original": [] if anchor == "dropped" else [f"old{line}"],
                    "inline": False, "anchor": anchor, **extra}
        edits = [change(3, "moved", stated=[4, 4]), change(9, "dropped", stated=[9, 9]),
                 change(12, "dropped", stated=[12, 12])]
        finding = dict(SAMPLE["findings"][0], edits=edits)
        data = review.validate(dict(SAMPLE, findings=[finding]))
        self.assertEqual([(e["anchor"], e.get("stated"), e["current"]) for e in data["findings"][0]["edits"]],
                         [("moved", [4, 4], "old3"), ("dropped", [9, 9], "old9"),
                          ("dropped", [12, 12], "old12")])  # a saved review loads back
        body = self.render(dict(SAMPLE, findings=[finding]))
        self.assertIn("Proposed change to <code>a.py:3</code>", body)
        self.assertIn("2 proposed changes didn't match the file and were left out.", body)
        self.assertNotIn("new9", body)
        huge = dict(change(5, "matched"), current="x" * (suggestions.MAX_REPLACEMENT + 1))
        self.assertEqual(review.validate(dict(SAMPLE, findings=[dict(finding, edits=[huge])]))["findings"][0]["edits"],
                         [])

    def test_prompt_version(self):
        self.assertRegex(review.prompt_version(), r"^[0-9a-f]{12}$")
        self.assertEqual(review.prompt_version(), review.prompt_version())

    def test_clean_review(self):
        body = self.render({"summary": "All good.", "findings": [], "test_gaps": [], "questions": [], "nits": []})
        self.assertIn("No significant issues found.", body)
        self.assertNotIn("Nits", body)

    def test_length_cap(self):
        many = dict(SAMPLE, findings=[dict(SAMPLE["findings"][0], detail="x" * 5000)] * 40)
        body = self.render(many)
        self.assertLessEqual(len(body), review.MAX_BODY)
        self.assertIn("<!-- local-autoreviewer job=42", body)

    def test_validate_rejects_garbage(self):
        for data in (None, [], {"summary": ""}, {"summary": "x", "findings": ["no"]}):
            with self.subTest(data=data), self.assertRaises(review.ReviewFormatError):
                review.validate(data)

    def test_schema_is_strict(self):
        schema = json.loads(review.schema_path().read_text())

        def check(node):
            if node.get("type") == "object":
                self.assertFalse(node["additionalProperties"])
                self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node["properties"].values():
                    check(child)
            if node.get("type") == "array":
                check(node["items"])
        check(schema)

    def test_prompt_substitution(self):
        prompt = review.build_prompt(repo="o/r", number=5, title="T\nx", author="me", base_ref="main",
                                     head_sha="abc", worktree="/w", context="/c", agent_note="NOTE",
                                     extra_instructions="Use tabs.", project_docs=True)
        self.assertIn("pull request #5 in o/r", prompt)
        self.assertIn("/c/pr.diff", prompt)
        self.assertIn("NOTE", prompt)
        self.assertIn("Use tabs.", prompt)
        self.assertIn("project-docs", prompt)
        self.assertNotIn("$", prompt.replace("$repo", ""))


class LeakScanTests(unittest.TestCase):
    def test_patterns(self):
        samples = {
            "github-token": "ghp_" + "a1" * 18,
            "github-fine-grained-pat": "github_pat_" + "A" * 50,
            "anthropic-key": "sk-ant-oat01-" + "x" * 30,
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl",
            "private-key": "-----BEGIN OPENSSH PRIVATE KEY-----",
            "aws-access-key": "AKIAABCDEFGHIJKLMNOP",
        }
        for name, text in samples.items():
            with self.subTest(name=name):
                self.assertIn(name, leakscan.scan(f"before {text} after"))

    def test_clean_text(self):
        self.assertEqual(leakscan.scan("The key insight: use sk-learn and a token bucket."), [])

    def test_known_secret_variants(self):
        secret = "s3cr3t-value-1234567890"
        for text in (secret, secret[::-1], base64.b64encode(secret.encode()).decode(), secret.encode().hex().upper()):
            with self.subTest(text=text):
                self.assertTrue(any(h.startswith("known-secret") for h in leakscan.scan(text, [secret])))

    def test_collect_secrets(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"claudeAiOauth": {"accessToken": "x" * 20, "expiresAt": 1}, "tokens": {"id_token": "y" * 30},
                       "name": "z" * 40}, handle)
        try:
            found = leakscan.collect_secrets([handle.name, "/nonexistent"])
        finally:
            os.unlink(handle.name)
        self.assertEqual(sorted(found), ["x" * 20, "y" * 30])


if __name__ == "__main__":
    unittest.main()
