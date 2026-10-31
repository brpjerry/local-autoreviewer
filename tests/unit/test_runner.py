# SPDX-License-Identifier: GPL-3.0-or-later
import os
import signal
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


    def test_interrupt_while_sending_input_stops_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp, "pid")
            inv = Invocation(  # an agent that never reads its input, so writing more than a pipe holds blocks
                argv=[sys.executable, "-c", "import os, sys, time\n"
                      "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)", str(pid_file)],
                cwd=tmp, env={"PATH": os.environ["PATH"]}, stdin=b"x" * (4 << 20), stdout_path=str(Path(tmp, "out")),
                stderr_path=str(Path(tmp, "err")), unit="test-unit")

            def interrupt(signum, frame):
                raise KeyboardInterrupt

            previous = signal.signal(signal.SIGALRM, interrupt)
            signal.setitimer(signal.ITIMER_REAL, 0.5)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    DirectRunner().run(inv, timeout=30)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):  # stopped and reaped
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
