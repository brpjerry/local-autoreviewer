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


if __name__ == "__main__":
    unittest.main()
