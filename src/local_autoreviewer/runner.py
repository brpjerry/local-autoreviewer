# SPDX-License-Identifier: GPL-3.0-or-later
"""Run one agent process with limits, a wall-clock timeout and cooperative cancellation.

LocalRunner wraps the process in a transient `systemd-run --user --scope` unit in
autoreviewer.slice, so limits are enforced by the cgroup and a stop kills the whole
tree (including Codex's sandbox children). DirectRunner uses a process group and is
for tests and hosts without a user manager. A future PodmanRunner fits the same shape.
"""

import os
import signal
import subprocess
import time
from dataclasses import dataclass

SLICE = "autoreviewer.slice"  # no dashes: systemd treats them as slice nesting


@dataclass
class RunResult:
    returncode: int
    timed_out: bool
    cancelled: bool
    duration: float


@dataclass
class Invocation:
    argv: list
    cwd: str
    env: dict
    stdin: bytes
    stdout_path: str
    stderr_path: str
    unit: str
    memory_max: str = "3G"
    tasks_max: int = 512


class _Runner:
    poll_interval = 1.0

    def _command(self, inv):
        raise NotImplementedError

    def _stop(self, inv, proc):
        raise NotImplementedError

    def run(self, inv, *, timeout, should_cancel=lambda: False):
        start = time.monotonic()
        with open(inv.stdout_path, "wb") as out, open(inv.stderr_path, "wb") as err:
            proc = subprocess.Popen(self._command(inv), cwd=inv.cwd, env=inv.env, stdin=subprocess.PIPE,
                                    stdout=out, stderr=err, start_new_session=True)
            try:
                proc.stdin.write(inv.stdin)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            timed_out = cancelled = False
            try:
                while True:
                    try:
                        returncode = proc.wait(timeout=self.poll_interval)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if time.monotonic() - start > timeout:
                        timed_out = True
                    elif should_cancel():
                        cancelled = True
                    if timed_out or cancelled:
                        self._stop(inv, proc)
                        try:
                            returncode = proc.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            returncode = proc.wait()
                        break
            except BaseException:  # Ctrl-C: the agent runs in its own session, so stop it here
                self._stop(inv, proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                raise
        return RunResult(returncode, timed_out, cancelled, time.monotonic() - start)


class LocalRunner(_Runner):
    def _command(self, inv):
        return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                f"--unit={inv.unit}", f"--slice={SLICE}",
                "-p", f"MemoryMax={inv.memory_max}", "-p", "MemorySwapMax=0",
                "-p", f"TasksMax={inv.tasks_max}", "--", *inv.argv]

    def _stop(self, inv, proc):
        stop_unit(inv.unit)

    def cleanup(self, unit):
        """Stop a scope left behind by a crashed worker."""
        stop_unit(unit)


class DirectRunner(_Runner):
    poll_interval = 0.1

    def _command(self, inv):
        return list(inv.argv)

    def _stop(self, inv, proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def cleanup(self, unit):
        pass


def stop_unit(unit):
    subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True, timeout=60)


def make_runner():
    if os.environ.get("LOCAL_AUTOREVIEWER_RUNNER") == "direct":
        return DirectRunner()
    return LocalRunner()
