# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts src/ on sys.path)
from local_autoreviewer import __main__ as cli


class CliTests(unittest.TestCase):
    def test_agent_selections_are_checked_against_the_config(self):
        from local_autoreviewer import config
        cfg = config.parse({"agents": {"codex-sol": {"model": "gpt-6.1-sol"}}})
        self.assertEqual(cli._agents_arg(cfg, ["codex-sol", "claude", "codex-sol"]), ["codex-sol", "claude"])
        self.assertIsNone(cli._agents_arg(cfg, None))
        with self.assertRaisesRegex(cli.CliError, "unknown agent 'codex-terra'; configured: claude, codex, opencode, codex-sol"):
            cli._agents_arg(cfg, ["codex-terra"])

    def test_version_reads_the_packaged_version_file(self):
        with tempfile.TemporaryDirectory() as share:
            Path(share, "VERSION").write_text("1.2.3-4\n")
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"LOCAL_AUTOREVIEWER_SHARE": share}), \
                    contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_:
                cli.main(["--version"])
        self.assertEqual(exit_.exception.code, 0)
        self.assertEqual(out.getvalue(), "local-autoreviewer 1.2.3-4\n")

    def test_version_without_a_version_file_is_dev(self):
        with tempfile.TemporaryDirectory() as share:
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"LOCAL_AUTOREVIEWER_SHARE": share}), \
                    contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_:
                cli.main(["--version"])
        self.assertEqual(exit_.exception.code, 0)
        self.assertEqual(out.getvalue(), "local-autoreviewer dev\n")


class OpencodeCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        from local_autoreviewer import config
        from local_autoreviewer.agents import make_agent
        self.cfg = config.parse({"agents": {"opencode": {"model": "opencode/deepseek-v4.1-flash-free"}}})
        self.agent = make_agent(self.cfg.agents["opencode"], self.tmp / "state")
        self.hostile = {"HOME": str(self.tmp / "home"), "PATH": "/usr/bin", "GH_TOKEN": "ghp_x", "GITHUB_TOKEN": "y",
                        "OPENCODE_CONFIG": "/home/u/evil.json", "XDG_DATA_HOME": str(self.tmp / "data"),
                        "TERM": "xterm-256color"}

    def test_the_scan_reads_both_opencode_databases(self):
        import json
        import sqlite3
        state = self.tmp / "state"
        with mock.patch.dict(os.environ, self.hostile, clear=True):
            own, reviews = cli.auth_databases(state)
            self.assertEqual(own, self.tmp / "data/opencode/opencode.db")
            self.assertEqual(reviews, state / "opencode-home/.local/share/opencode/opencode.db")
            self.assertEqual(reviews.parent, Path(self.agent.job_env(cli.pipeline.job_paths(state, 1))[
                "XDG_DATA_HOME"], "opencode"))  # where the reviews' opencode keeps it
            for path, key in ((own, "o" * 20), (reviews, "w" * 20)):
                path.parent.mkdir(parents=True)
                conn = sqlite3.connect(path)
                conn.execute("CREATE TABLE credential (value TEXT)")
                conn.execute("INSERT INTO credential VALUES (?)", (json.dumps({"type": "key", "key": key}),))
                conn.commit()
                conn.close()
            self.assertEqual(sorted(cli.local_secrets(state)), ["o" * 20, "w" * 20])
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home")}, clear=True):
            self.assertEqual(cli.auth_databases(state)[0], self.tmp / "home/.local/share/opencode/opencode.db")

    def test_resume_runs_opencode_in_the_reviews_environment(self):
        job = {"id": 7, "agent": "opencode", "session_id": "ses_1"}
        job_paths = cli.pipeline.job_paths(self.tmp / "state", 7)
        Path(job_paths.worktree).mkdir(parents=True)
        with mock.patch.dict(os.environ, self.hostile, clear=True), \
                mock.patch.object(cli, "_job_and_agent", return_value=(self.cfg, None, job, self.agent)), \
                mock.patch.object(cli, "_ensure_worktree", return_value=job_paths), \
                mock.patch.object(cli.os, "chdir"), mock.patch.object(cli.os, "execvpe") as execvpe, \
                contextlib.redirect_stderr(io.StringIO()):
            cli.cmd_resume(mock.Mock(job=7))
        command, argv, env = execvpe.call_args.args
        self.assertEqual(argv, ["opencode", "--standalone", "--session", "ses_1"])
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "OPENCODE_CONFIG"):
            self.assertNotIn(key, env)
        self.assertEqual(env["XDG_DATA_HOME"], str(self.tmp / "state/opencode-home/.local/share"))
        self.assertEqual(env["TERM"], "xterm-256color")

    def test_opencode_login(self):
        with mock.patch.dict(os.environ, self.hostile, clear=True), \
                mock.patch.object(cli, "load_config", return_value=self.cfg), \
                mock.patch.object(cli.paths, "state_dir", return_value=self.tmp / "state"), \
                mock.patch.object(cli.os, "execvpe") as execvpe, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["opencode-login"])
            cli.main(["opencode-login", "github-copilot"])
        (_, first, env), (_, second, _) = (call.args for call in execvpe.call_args_list)
        self.assertEqual(first, ["opencode", "auth", "login", "--standalone", "opencode-go"])
        self.assertEqual(second[-1], "github-copilot")
        self.assertEqual(env["HOME"], str(self.tmp / "state/opencode-home"))
        self.assertEqual(env["TERM"], "xterm-256color")
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "OPENCODE_CONFIG"):
            self.assertNotIn(key, env)


if __name__ == "__main__":
    unittest.main()
