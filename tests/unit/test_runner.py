# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import ROOT  # noqa: F401  (puts src/ on sys.path)
from local_autoreviewer.runner import DirectRunner, Invocation


class RunnerTests(unittest.TestCase):
    def test_interrupt_stops_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp, "pid")
            inv = Invocation(
                argv=[sys.executable, "-c", "import os, sys, time\n"
                      "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)", str(pid_file)],
                cwd=tmp, env={"PATH": os.environ["PATH"]}, stdin=b"", stdout_path=str(Path(tmp, "out")),
                stderr_path=str(Path(tmp, "err")), unit="test-unit")

            def ctrl_c_once_running():
                if pid_file.exists() and pid_file.read_text():
                    raise KeyboardInterrupt
                return False

            with self.assertRaises(KeyboardInterrupt):
                DirectRunner().run(inv, timeout=30, should_cancel=ctrl_c_once_running)
            pid = int(pid_file.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            with self.assertRaises(ProcessLookupError):  # stopped and reaped, not left running
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
