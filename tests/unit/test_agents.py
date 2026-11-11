# SPDX-License-Identifier: GPL-3.0-or-later
import os
import tempfile
import unittest
from unittest import mock

from tests.helpers import ROOT  # noqa: F401
from local_autoreviewer import config
from local_autoreviewer.agents import make_agent
from local_autoreviewer.agents.base import JobPaths, agent_env, classify_error
from local_autoreviewer.runner import RunResult

PATHS = JobPaths(root="/s/jobs/1", worktree="/s/jobs/1/repo", context="/s/jobs/1/context", out="/s/jobs/1/out")
SECRET_ENV = {"GH_TOKEN": "ghp_x", "GITHUB_TOKEN": "y", "SSH_AUTH_SOCK": "/run/ssh", "OTHER_AGENT_TOKEN": "z",
              "ANTHROPIC_API_KEY": "k", "HOME": "/home/u", "PATH": "/usr/bin", "LANG": "C.UTF-8"}


_STATE = tempfile.TemporaryDirectory(prefix="lar-agents-")
unittest.addModuleCleanup(_STATE.cleanup)
STATE = _STATE.name


def agents(**codex):
    cfg = config.parse({"agents": {"claude": {"model": "opus", "effort": "high"},
                                   "codex": {"model": "m1", "effort": "high", **codex}}})
    return make_agent(cfg.agents["claude"], STATE), make_agent(cfg.agents["codex"], STATE)


class ArgvTests(unittest.TestCase):
    def test_claude_locked_flags(self):
        claude, _ = agents()
        inv = claude.invocation(unit="u", paths=PATHS, prompt="p", session_id="sid")
        argv = inv.argv
        for flag in ("-p", "--restricted", "--safe-mode", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--add-dir") + 1], PATHS.context)
        self.assertEqual(argv[argv.index("--session-id") + 1], "sid")
        self.assertIn("--json-schema", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertEqual(inv.cwd, PATHS.worktree)
        self.assertEqual(inv.stdin, b"p")

    def test_codex_locked_flags(self):
        _, codex = agents()
        argv = codex.invocation(unit="u", paths=PATHS, prompt="p", session_id=None).argv
        self.assertEqual(argv[1], "exec")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("project_doc_max_bytes=0", argv)
        self.assertIn('web_search="disabled"', argv)
        disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
        self.assertTrue({"hooks", "plugins", "multi_agent", "apps"} <= disabled)
        self.assertNotIn("shell_tool", disabled)
        self.assertNotIn("danger-full-access", " ".join(argv))
        self.assertEqual(argv[-1], "-")

    def test_further_reviewers_run_their_types_locked_flags(self):
        cfg = config.parse({"agents": {"codex-sol": {"model": "gpt-6.1-sol", "effort": "xhigh"},
                                       "claude-sonnet": {"model": "sonnet"}}})
        claude, codex = agents()
        sol = make_agent(cfg.agents["codex-sol"], STATE)
        sonnet = make_agent(cfg.agents["claude-sonnet"], STATE)
        self.assertIs(type(sol), type(codex))
        self.assertIs(type(sonnet), type(claude))
        self.assertEqual(sol.home, codex.home)  # every Codex reviewer uses the reviews' one login
        self.assertEqual(sol._locked_flags(), codex._locked_flags())
        self.assertEqual(sonnet._locked_flags(PATHS), claude._locked_flags(PATHS))
        argv = sol.invocation(unit="u", paths=PATHS, prompt="p", session_id=None).argv
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="xhigh"', argv)
        argv = sonnet.invocation(unit="u", paths=PATHS, prompt="p", session_id="sid").argv
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_codex_inline_mode_disables_shell(self):
        _, codex = agents(mode="inline")
        argv = codex._locked_flags()
        disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
        self.assertTrue({"shell_tool", "unified_exec"} <= disabled)

    def test_codex_inline_mode_keeps_the_latest_author_comments(self):
        import json
        from pathlib import Path
        from local_autoreviewer.agents import codex as codex_mod
        _, codex = agents(mode="inline")
        with tempfile.TemporaryDirectory() as tmp:
            paths = JobPaths(root=tmp, worktree=f"{tmp}/repo", context=f"{tmp}/context", out=f"{tmp}/out")
            Path(paths.context).mkdir()
            Path(paths.context, "pr.diff").write_text("diff")
            Path(paths.context, "files.txt").write_text("")
            comments = [{"kind": "comment", "body": f"note {n} " + "é" * 2900} for n in range(40)]
            comments[-1]["body"] = "Won't fix 2: the caller closes it."
            Path(paths.context, "author-comments.json").write_text(json.dumps(comments, indent=2))
            text = codex.inline_context(paths)
        self.assertIn("## author-comments.json", text)
        self.assertIn("Won't fix 2: the caller closes it.", text)  # the latest survives the cut
        self.assertNotIn("note 0 ", text)
        self.assertIn("oldest comments left out: size limit", text)
        self.assertNotIn("\\u00e9", text)  # not spent on escapes
        self.assertEqual(codex_mod._latest_comments("not json", 10), ("not json", ""))
        # The numbering of a review whose first comment is left out goes to the next comment answering it.
        numbered = {"id": 9, "commit": "c", "numbered_findings": ["1. **Leak**", "2. **Race**"]}
        kept = [{"kind": "comment", "body": "x" * 500, "review_id": 9, "review": numbered},
                {"kind": "comment", "body": "won't fix 2", "review_id": 9}]
        shown, _ = codex_mod._latest_comments(json.dumps(kept), 400)
        self.assertEqual(json.loads(shown), [dict(kept[1], review=numbered)])


class EnvTests(unittest.TestCase):
    def test_env_is_allowlisted(self):
        with mock.patch.dict(os.environ, SECRET_ENV, clear=True):
            claude, codex = agents()
            for env in (claude.invocation(unit="u", paths=PATHS, prompt="", session_id="s").env, codex.env()):
                for key in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "OTHER_AGENT_TOKEN", "ANTHROPIC_API_KEY"):
                    self.assertNotIn(key, env)
                self.assertEqual(env["HOME"], "/home/u")
            self.assertEqual(codex.env()["CODEX_HOME"], f"{STATE}/codex-home")

    def test_agent_env_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            env = agent_env()
        self.assertEqual(env["TERM"], "dumb")
        self.assertIn("/usr/bin", env["PATH"])


class ParseTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = JobPaths(root=self._tmp.name, worktree=self._tmp.name, context=self._tmp.name,
                              out=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text):
        with open(f"{self._tmp.name}/{name}", "w") as handle:
            handle.write(text)

    def test_claude_rate_limit(self):
        claude, _ = agents()
        self.write("agent.stdout", '{"type":"result","subtype":"success","is_error":true,"api_error_status":429,'
                                   '"result":"limit","session_id":"s"}')
        result = claude.parse(RunResult(1, False, False, 1.0), self.paths, "s")
        self.assertEqual(result.error_kind, "rate_limit")

    def test_claude_garbage_output(self):
        claude, _ = agents()
        self.write("agent.stdout", "")
        self.write("agent.stderr", "Error: something broke")
        result = claude.parse(RunResult(1, False, False, 1.0), self.paths, "s")
        self.assertEqual(result.error_kind, "error")
        self.assertIn("something broke", result.error)

    def test_codex_success(self):
        _, codex = agents()
        self.write("agent.stdout", '{"type":"thread.started","thread_id":"t1"}\n'
                                   '{"type":"turn.completed","usage":{"input_tokens":3}}\n')
        self.write("review.json", '{"summary":"ok","findings":[],"test_gaps":[],"questions":[],"nits":[]}')
        result = codex.parse(RunResult(0, False, False, 1.0), self.paths, None)
        self.assertEqual(result.session_id, "t1")
        self.assertEqual(result.review["summary"], "ok")
        self.assertIsNone(result.error_kind)

    def test_classify(self):
        self.assertEqual(classify_error("You've hit your usage limit"), "rate_limit")
        self.assertEqual(classify_error("HTTP 429 Too Many Requests"), "rate_limit")
        self.assertEqual(classify_error("segfault"), "error")


if __name__ == "__main__":
    unittest.main()
