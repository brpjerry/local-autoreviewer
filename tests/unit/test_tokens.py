# SPDX-License-Identifier: GPL-3.0-or-later
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT  # noqa: F401  (puts src/ on sys.path)
from local_autoreviewer import __main__ as cli
from local_autoreviewer import config as config_mod
from local_autoreviewer import gitops
from local_autoreviewer.github import GitHub, GitHubError, TokenFile


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "github-token"

    def write(self, value, mode=0o600):
        temp = self.path.with_suffix(".new")
        temp.write_text(value + "\n")
        temp.chmod(mode)
        os.replace(temp, self.path)  # a new inode, as an editor or `mv` leaves it

    def test_a_missing_token_fails_the_first_read(self):
        with self.assertRaisesRegex(GitHubError, "missing"):
            TokenFile(self.path).get()

    def test_a_replaced_token_is_used_without_a_restart(self):
        self.write("old-token")
        tokens = TokenFile(self.path)
        github = GitHub(tokens, Path(self._tmp.name) / "gh")
        self.assertEqual(github.env()["GH_TOKEN"], "old-token")
        self.write("new-token")
        with self.assertLogs("local_autoreviewer.github", "INFO"):
            self.assertEqual(github.env()["GH_TOKEN"], "new-token")
        self.assertEqual(tokens.seen, ["old-token", "new-token"])

    def test_an_unusable_change_keeps_the_last_good_token_until_fixed(self):
        self.write("old-token")
        tokens = TokenFile(self.path)
        tokens.get()
        self.write("new-token", mode=0o644)
        with self.assertLogs("local_autoreviewer.github", "ERROR") as logged:
            self.assertEqual(tokens.get(), "old-token")
            self.assertEqual(tokens.get(), "old-token")
        self.assertEqual(len(logged.records), 1)  # once per version of the file
        self.assertNotIn("new-token", logged.output[0])
        self.path.chmod(0o600)  # only the mode changes
        self.assertEqual(tokens.get(), "new-token")


class WorkerServicesTests(unittest.TestCase):
    """The services the worker builds once follow a replaced token everywhere it goes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.token_path, self.forgejo_dir = tmp / "github-token", tmp / "forgejo-tokens"
        self.forgejo_dir.mkdir()
        for target, value in ((cli.paths, "state_dir"), (cli.paths, "token_file"), (cli.paths, "forgejo_token_dir"),
                              (cli, "resolve_token_owner"), (cli, "resolve_forgejo_owners"), (cli, "make_runner"),
                              (cli, "local_secrets")):
            patcher = mock.patch.object(target, value)
            self.addCleanup(patcher.stop)
            setattr(self, value, patcher.start())
        self.state_dir.return_value = tmp / "state"
        self.token_file.return_value = self.token_path
        self.forgejo_token_dir.return_value = self.forgejo_dir
        self.resolve_token_owner.return_value = "alice"
        self.local_secrets.return_value = []

    def write(self, path, value):
        temp = path.with_suffix(".new")
        temp.write_text(value + "\n")
        temp.chmod(0o600)
        os.replace(temp, path)

    def git_token(self, svc):
        with mock.patch.object(gitops.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            svc.git.run(["status"])
        return run.call_args.kwargs["env"]["GH_TOKEN"]

    def test_gh_git_and_the_leak_check_use_the_token_as_it_is_now(self):
        self.write(self.token_path, "ghp_old")
        svc = cli.build_services(config_mod.parse({}))
        self.assertEqual((svc.github.env()["GH_TOKEN"], self.git_token(svc)), ("ghp_old", "ghp_old"))
        self.write(self.token_path, "ghp_new")
        self.assertEqual((svc.github.env()["GH_TOKEN"], self.git_token(svc)), ("ghp_new", "ghp_new"))
        self.assertLessEqual({"ghp_old", "ghp_new"}, set(svc.secrets()))

    def test_a_replaced_forgejo_token_gets_a_client_and_a_git_header_of_its_own(self):
        self.write(self.token_path, "ghp_old")
        self.write(self.forgejo_dir / "forge.example", "fj-old")
        svc = cli.build_services(config_mod.parse({}))
        old = svc.github.forgejo("forge.example")
        old_header = svc.github.git_env()["GIT_CONFIG_VALUE_0"]
        self.write(self.forgejo_dir / "forge.example", "fj-new")
        new = svc.github.forgejo("forge.example")
        self.assertEqual((old.token, new.token), ("fj-old", "fj-new"))
        self.assertNotEqual(svc.github.git_env()["GIT_CONFIG_VALUE_0"], old_header)
        (self.forgejo_dir / "forge.example").unlink()  # the leak check still knows the token it no longer reads
        self.assertLessEqual({"fj-old", "fj-new"}, set(svc.secrets()))


if __name__ == "__main__":
    unittest.main()
