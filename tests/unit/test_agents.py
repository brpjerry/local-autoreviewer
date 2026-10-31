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

    def test_codex_inline_mode_disables_shell(self):
        _, codex = agents(mode="inline")
        argv = codex._locked_flags()
        disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
        self.assertTrue({"shell_tool", "unified_exec"} <= disabled)


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
