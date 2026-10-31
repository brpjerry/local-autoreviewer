# SPDX-License-Identifier: GPL-3.0-or-later
import tomllib
import unittest

from tests.helpers import ROOT
from local_autoreviewer import config


class ConfigTests(unittest.TestCase):
    def test_example_config_parses(self):
        cfg = config.load(ROOT / "config.example.toml")
        repo = cfg.repo("YOUR-USER/your-scratch-repo")
        self.assertEqual(repo.agents, ("claude", "codex"))
        self.assertEqual(repo.authors, ("@token-owner",))
        self.assertEqual(cfg.service.review_timeout, 1200)
        self.assertEqual(cfg.service.retain_jobs, 14 * 86400)
        self.assertEqual(cfg.agents["claude"].model, "opus")

    def test_repo_overrides_defaults(self):
        cfg = config.parse({"defaults": {"agents": ["claude"], "post": False},
                            "repo": [{"name": "a/b", "agents": ["codex"]}, {"name": "a/c"}]})
        self.assertEqual(cfg.repo("a/b").agents, ("codex",))
        self.assertFalse(cfg.repo("a/b").post)
        self.assertEqual(cfg.repo("a/c").agents, ("claude",))

    def test_unknown_keys_rejected(self):
        for data in ({"service": {"typo": 1}}, {"repo": [{"name": "a/b", "mounts": ["/"]}]},
                     {"agents": {"claude": {"tools": "Bash"}}}, {"agents": {"gemini": {}}}, {"extra": 1}):
            with self.subTest(data=data), self.assertRaises(config.ConfigError):
                config.parse(data)

    def test_invalid_values(self):
        bad = [
            {"service": {"settle": "soon"}},
            {"service": {"bootstrap": "all"}},
            {"repo": [{"name": "not a repo"}]},
            {"repo": [{"name": "a/b"}, {"name": "A/B"}]},
            {"defaults": {"agents": ["claude", "claude"]}},
            {"agents": {"claude": {"mode": "inline"}}},
            {"agents": {"codex": {"memory_max": "lots"}}},
            {"defaults": {"max_changed_files": True}},
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(config.ConfigError):
                config.parse(data)

    def test_durations(self):
        self.assertEqual(config.parse_duration("90s", "k"), 90)
        self.assertEqual(config.parse_duration("2h", "k"), 7200)
        self.assertEqual(config.parse_duration(45, "k"), 45)

    def test_toml_types(self):
        data = tomllib.loads('[defaults]\nmax_diff_bytes = 1_000\n[[repo]]\nname = "x/y"\n')
        self.assertEqual(config.parse(data).repo("x/y").max_diff_bytes, 1000)


if __name__ == "__main__":
    unittest.main()
