# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan usage limits: reading them per agent type, and deciding which reviews to skip."""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.helpers import agent_command  # noqa: F401  (puts src/ on sys.path)
from local_autoreviewer import config, limits
from local_autoreviewer.agents import make_agent
from local_autoreviewer.agents.claude import claude_limits
from local_autoreviewer.agents.codex import codex_limits
from local_autoreviewer.agents.opencode import opencode_limits
from local_autoreviewer.limits import Limit, Unavailable

NOW = 1_800_000_000
LATER = NOW + 3600

# Shaped like a real /api/oauth/usage reply (2026-11-11), with made-up figures.
CLAUDE_USAGE = {
    "five_hour": {"utilization": 12.0, "resets_at": "2027-01-15T10:20:00.586156+00:00"},
    "seven_day": {"utilization": 64.0, "resets_at": "2027-01-18T01:00:00.586177+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 12, "resets_at": "2027-01-15T10:20:00.586156+00:00",
         "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 64, "resets_at": "2027-01-18T01:00:00.586177+00:00",
         "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 57, "resets_at": "2027-01-18T00:59:59.586352+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
}
# Shaped like a real account/rateLimits/read reply (codex-cli 0.166.0), with made-up figures.
CODEX_REPLY = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 41, "windowDurationMins": 10080,
                                                   "resetsAt": 1800003600}, "secondary": None},
    "rateLimitsByLimitId": {"codex": {"limitId": "codex", "normalModelSlug": None, "primary": {
        "usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 1800003600}, "secondary": None}},
}


class LimitTests(unittest.TestCase):
    def test_left_and_reset(self):
        self.assertEqual(Limit("weekly", 93.0, LATER).left(NOW), 7.0)
        self.assertEqual(Limit("weekly", 93.0, NOW - 1).left(NOW), 100.0)  # the window has reset since
        self.assertEqual(Limit("weekly", 120.0).left(NOW), 0.0)

    def test_low(self):
        weekly, fable = Limit("weekly", 85.0, LATER), Limit("weekly (Fable)", 95.0, LATER, "Fable")
        self.assertIsNone(limits.low([weekly, fable], "opus", 10, NOW))  # Fable's own limit isn't Opus's
        self.assertIs(limits.low([weekly, fable], "claude-fable-5-1", 10, NOW), fable)
        self.assertIs(limits.low([weekly, fable], "opus", 20, NOW), weekly)
        self.assertIsNone(limits.low([fable], "", 10, NOW))  # the default model: a model's own limit isn't known
        self.assertIsNone(limits.low([Limit("weekly", 100.0, LATER)], "opus", 0, NOW))  # 0: don't check
        self.assertIsNone(limits.low(Unavailable("offline"), "opus", 10, NOW))  # unknown isn't low
        self.assertIsNone(limits.low([Limit("weekly", 90.0, LATER)], "opus", 10, NOW))  # exactly 10% left
        self.assertIn("weekly limit 7% left, resets ", limits.describe(Limit("weekly", 93.0, LATER), NOW))

    def test_epoch(self):
        self.assertEqual(limits.epoch("1970-01-01T00:01:40+00:00"), 100)
        self.assertEqual(limits.epoch(1800003600), 1800003600)
        for value in (None, "soon", -5, float("nan"), True):
            self.assertEqual(limits.epoch(value), 0, value)


class ReadTests(unittest.TestCase):
    def setUp(self):
        limits._readings.clear()
        self.addCleanup(limits._readings.clear)

    def test_one_reading_per_login_for_a_while(self):
        agent = mock.Mock()
        agent.limits_key.return_value = "codex:/home"
        agent.read_limits.side_effect = [[Limit("weekly", 50.0)], [Limit("weekly", 60.0)]]
        self.assertEqual(limits.read(agent, NOW)[0].used, 50.0)
        self.assertEqual(limits.read(agent, NOW + limits.FRESH - 1)[0].used, 50.0)
        self.assertEqual(limits.read(agent, NOW + limits.FRESH)[0].used, 60.0)
        self.assertEqual(agent.read_limits.call_count, 2)

    def test_a_failed_reading_is_kept_too(self):
        agent = mock.Mock()
        agent.limits_key.return_value = "claude"
        agent.read_limits.side_effect = Unavailable("HTTP 429")
        with self.assertLogs("local_autoreviewer.limits", "INFO") as logs:
            self.assertIsInstance(limits.read(agent, NOW), Unavailable)
        self.assertIn("claude: can't read the plan's usage limits (HTTP 429)", logs.output[0])
        self.assertIsInstance(limits.read(agent, NOW + 60), Unavailable)
        self.assertEqual(agent.read_limits.call_count, 1)  # not asked again on every review

    def test_an_unexpected_failure_counts_as_unknown(self):
        agent = mock.Mock()
        agent.limits_key.return_value = "codex:/home"
        agent.read_limits.side_effect = RuntimeError("boom")
        with self.assertLogs("local_autoreviewer.limits", "WARNING") as logs:
            reading = limits.read(agent, NOW)
        self.assertIsInstance(reading, Unavailable)
        self.assertEqual(str(reading), "unexpected RuntimeError")
        self.assertIn("reviews run without the check", logs.output[0])

    def test_reviews_starting_together_share_one_reading(self):
        started, calls = threading.Event(), []

        def slow():
            calls.append(1)
            started.wait(5)
            return [Limit("weekly", 10.0)]
        agent = mock.Mock(read_limits=slow, limits_key=lambda: "codex:/home")
        threads = [threading.Thread(target=limits.read, args=(agent, NOW)) for _ in range(3)]
        for thread in threads:
            thread.start()
        started.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(calls), 1)


class ParserTests(unittest.TestCase):
    def test_claude(self):
        found = claude_limits(CLAUDE_USAGE)
        self.assertEqual([(l.name, l.used, l.model) for l in found],
                         [("5-hour", 12.0, ""), ("weekly", 64.0, ""), ("weekly (Fable)", 57.0, "Fable")])
        self.assertEqual(found[1].resets_at, limits.epoch("2027-01-18T01:00:00.586177+00:00"))
        # Without the limits list, the two windows.
        older = {key: CLAUDE_USAGE[key] for key in ("five_hour", "seven_day")}
        self.assertEqual([(l.name, l.used) for l in claude_limits(older)], [("5-hour", 12.0), ("weekly", 64.0)])
        self.assertEqual(claude_limits({"limits": "x", "five_hour": None}), [])

    def test_codex(self):
        (weekly,) = codex_limits(CODEX_REPLY)
        self.assertEqual((weekly.name, weekly.used, weekly.resets_at, weekly.model), ("weekly", 41.0, 1800003600, ""))
        reply = json.loads(json.dumps(CODEX_REPLY))
        reply["rateLimitsByLimitId"]["codex"]["secondary"] = {"usedPercent": 40, "windowDurationMins": 300}
        reply["rateLimitsByLimitId"]["sol"] = {"normalModelSlug": "gpt-6.1-sol", "primary": {"usedPercent": 95}}
        reply["rateLimitsByLimitId"]["cloud"] = {"primary": {"usedPercent": 99}}  # another product's quota
        self.assertEqual([(l.name, l.used, l.model) for l in codex_limits(reply)],
                         [("weekly", 41.0, ""), ("5-hour", 40.0, ""), ("plan (gpt-6.1-sol)", 95.0, "gpt-6.1-sol")])
        # Only rateLimits, under another id; and a plan that is blocked outright.
        only = {"rateLimits": {"limitId": "pro", "primary": {"usedPercent": 20, "windowDurationMins": 10080}},
                "ordinaryUsageAllowed": False}
        self.assertEqual([(l.name, l.used) for l in codex_limits(only)], [("weekly", 20.0), ("plan", 100.0)])
        renamed = {"rateLimits": {"limitId": "pro"}, "rateLimitsByLimitId": {"pro": {"primary": {"usedPercent": 30}}}}
        self.assertEqual([l.used for l in codex_limits(renamed)], [30.0])

    def test_opencode(self):
        self.assertEqual(opencode_limits({"location": {}, "data": None}), [])  # free models: none reported
        # As the operator's usage bar reads an OpenCode Go reply (epoch milliseconds).
        reply = {"data": {"go": {"rolling": {"percent": 40, "resetsAt": 1_800_000_000_000},
                                 "weekly": {"percent": 50, "limited": True},
                                 "monthly": {"percent": 20, "status": "rate-limited"}},
                          "budgets": [{"ruleID": "r1", "per": "monthly", "limitDollars": 20, "spentDollars": 19},
                                      {"limitDollars": 0, "spentDollars": 1}]}}
        found = opencode_limits(reply)
        self.assertEqual([(l.name, l.used, l.resets_at) for l in found],
                         [("Go rolling", 40.0, 1_800_000_000), ("Go weekly", 100.0, 0), ("Go monthly", 100.0, 0),
                          ("Go monthly budget", 95.0, 0)])
        # The reply is OpenCode Go's usage: it counts for Go models only, not for a free or another provider's.
        for model, low in (("opencode-go/glm-5", True), ("opencode/deepseek-v4.1-flash-free", False),
                           ("anthropic/claude-x", False), ("", False)):
            with self.subTest(model=model):
                self.assertEqual(limits.low(found, model, 10, NOW) is not None, low)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def agent(self, kind, **table):
        cfg = config.parse({"agents": {kind: table}})
        return make_agent(cfg.agents[kind], self.tmp / "state")

    def test_claude_uses_the_login_without_refreshing_it(self):
        home = self.tmp / "home"
        (home / ".claude").mkdir(parents=True)
        credentials = home / ".claude/.credentials.json"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch("urllib.request.urlopen", return_value=response) as urlopen, \
                mock.patch("json.load", return_value=CLAUDE_USAGE):
            with self.assertRaisesRegex(Unavailable, "no Claude login"):
                self.agent("claude").read_limits()
            credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123", "refreshToken": "r",
                                                                 "expiresAt": (time.time() - 60) * 1000}}))
            with self.assertRaisesRegex(Unavailable, "expired"):
                self.agent("claude").read_limits()
            self.assertFalse(urlopen.called)  # an expired token isn't refreshed or sent
            credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123", "refreshToken": "r",
                                                                 "expiresAt": (time.time() + 600) * 1000}}))
            self.assertEqual(len(self.agent("claude").read_limits()), 3)
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.anthropic.com/api/oauth/usage")
            self.assertEqual(request.get_header("Authorization"), "Bearer tok-123")
            urlopen.side_effect = urllib.error.HTTPError(request.full_url, 429, "busy", {}, None)
            with self.assertRaisesRegex(Unavailable, "HTTP 429") as raised:
                self.agent("claude").read_limits()
            self.assertNotIn("tok-123", str(raised.exception))
            urlopen.side_effect = None
            with mock.patch("json.load", side_effect=http.client.IncompleteRead(b"{")), \
                    self.assertRaisesRegex(Unavailable, "IncompleteRead"):  # a body cut short
                self.agent("claude").read_limits()

    def test_codex_asks_its_app_server(self):
        command, _ = agent_command(self.tmp, "codex", "ok")
        codex = self.agent("codex", command=command)
        (weekly,) = codex.read_limits()
        self.assertEqual((weekly.name, weekly.used), ("weekly", 93.0))
        for mode, why in (("appserver_error", "refused account/rateLimits/read"),
                          ("appserver_noise", "isn't JSON")):
            with self.subTest(mode=mode):
                command, _ = agent_command(self.tmp, "codex", mode)
                with self.assertRaisesRegex(Unavailable, why):
                    self.agent("codex", command=command).read_limits()
        command, _ = agent_command(self.tmp, "codex", "appserver_hang")
        with mock.patch("local_autoreviewer.agents.codex.app_server_call.__defaults__", (1,)), \
                self.assertRaisesRegex(Unavailable, "timed out"):
            self.agent("codex", command=command).read_limits()
        with self.assertRaisesRegex(Unavailable, "can't start"):
            self.agent("codex", command=str(self.tmp / "missing")).read_limits()

    def test_a_codex_app_server_gone_before_the_request(self):
        """The send fails, and closing stdin then flushes the same bytes into the same broken pipe: still
        Unavailable, with both pipes closed."""
        from local_autoreviewer.agents import codex as codex_mod
        read_end, write_end = os.pipe()
        os.close(write_end)
        stdin = mock.Mock(flush=mock.Mock(side_effect=BrokenPipeError), close=mock.Mock(side_effect=BrokenPipeError))
        stdout = os.fdopen(read_end, "rb")
        proc = mock.Mock(stdin=stdin, stdout=stdout)
        with mock.patch("subprocess.Popen", return_value=proc), \
                self.assertRaisesRegex(Unavailable, "BrokenPipeError"):
            codex_mod.app_server_call("codex", {}, self.tmp, "account/rateLimits/read")
        stdin.close.assert_called_once()
        self.assertTrue(stdout.closed)

    def test_opencode_asks_a_private_server(self):
        opencode = self.agent("opencode", model="opencode/deepseek-v4.1-flash-free")
        reply = json.dumps({"data": {"go": {"weekly": {"percent": 91}}}})
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=reply)) as run:
            self.assertEqual([(l.name, l.used) for l in opencode.read_limits()], [("Go weekly", 91.0)])
        self.assertEqual(run.call_args.args[0], ["opencode", "api", "--standalone", "get",
                                                 "/api/experimental/usage/limits"])
        self.assertEqual(run.call_args.kwargs["env"]["HOME"], str(self.tmp / "state/opencode-home"))
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="")), \
                self.assertRaisesRegex(Unavailable, "exited 1"):
            opencode.read_limits()


class ConfigTests(unittest.TestCase):
    def test_min_limit_left(self):
        self.assertEqual(config.parse({}).service.min_limit_left, 10)
        self.assertEqual(config.parse({"service": {"min_limit_left": 0}}).service.min_limit_left, 0)
        for bad in (-1, 101, "10%", True):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.parse({"service": {"min_limit_left": bad}})


class DoctorTests(unittest.TestCase):
    def test_limits_line(self):
        from local_autoreviewer import __main__ as cli
        cfg = config.parse({"agents": {"codex-sol": {"model": "gpt-6.1-sol"}}})
        agent = mock.Mock()
        now = int(time.time())
        cases = [([Limit("weekly", 59.0, now + 3600), Limit("plan (other)", 99.0, 0, "gpt-other")], True,
                  "codex-sol: weekly limit 41% left, resets "),
                 ([Limit("weekly", 95.0, now + 3600)], False, "Under 10%: its reviews are skipped"),
                 (Unavailable("HTTP 429"), True, "usage limits unknown (HTTP 429)"),
                 ([], True, "no usage limits reported"),
                 ([Limit("Go weekly", 99.0, now + 3600, "opencode-go/")], True,
                  "none of its plan's usage limits are on its model")]
        for reading, ok, text in cases:
            with self.subTest(text=text), mock.patch.object(cli.limits, "read", return_value=reading):
                line = cli._limits_line(cfg, "codex-sol", agent)
                self.assertEqual(line[0], ok)
                self.assertIn(text, line[1])
                self.assertNotIn("other", line[1])  # another model's limit isn't shown
                self.assertEqual(line[2:], () if ok else (True,))  # a warning, not a failure


if __name__ == "__main__":
    unittest.main()
