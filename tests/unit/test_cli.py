# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_autoreviewer import __main__ as cli


class CliTests(unittest.TestCase):
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
                    contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                cli.main(["--version"])
        self.assertEqual(out.getvalue(), "local-autoreviewer dev\n")


if __name__ == "__main__":
    unittest.main()
