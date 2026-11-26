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
import base64

from local_autoreviewer import gitops, leakscan
from local_autoreviewer.github import GitHub, GitHubError, TokenFile

PAT = "github_pat_" + "A1b2C3d4E5" * 8 + "xy"  # as long as a real fine-grained token


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

    def test_a_rewrite_in_place_is_read_again(self):
        self.write("token-aaa")
        tokens = TokenFile(self.path)
        tokens.get()
        inode, mtime = self.path.stat().st_ino, self.path.stat().st_mtime_ns
        self.path.write_text("token-bbb\n")  # the same inode and size
        os.utime(self.path, ns=(mtime + 10**9, mtime + 10**9))
        self.assertEqual((self.path.stat().st_ino, tokens.get()), (inode, "token-bbb"))

    def test_a_deleted_file_is_reported_once_and_a_new_one_is_picked_up(self):
        self.write("old-token")
        tokens = TokenFile(self.path)
        tokens.get()
        self.path.unlink()
        with self.assertLogs("local_autoreviewer.github", "ERROR") as logged:
            self.assertEqual([tokens.get() for _ in range(3)], ["old-token"] * 3)
        self.assertEqual(len(logged.records), 1)
        self.assertIn("missing", logged.output[0])
        self.write("new-token")
        self.assertEqual(tokens.get(), "new-token")

    def test_the_leak_check_keeps_a_token_refused_for_its_mode(self):
        self.write("old-token")
        tokens = TokenFile(self.path)
        tokens.get()
        self.write(PAT, mode=0o644)
        with self.assertLogs("local_autoreviewer.github", "ERROR"):
            self.assertEqual(tokens.get(), "old-token")
        self.path.unlink()
        with self.assertLogs("local_autoreviewer.github", "ERROR"):
            tokens.get()
        self.assertTrue(any(hit.startswith("known-secret-") for hit in leakscan.scan(f"leaked {PAT}", tokens.seen)))

    def test_gh_runs_with_the_token_in_the_file_and_redacts_that_one(self):
        self.write("ghp_first")
        github = GitHub(TokenFile(self.path), Path(self._tmp.name) / "gh-config")

        def fail(args, *, env, **kwargs):
            self.write("ghp_second")  # replaced while gh runs
            return subprocess.CompletedProcess(args, 1, "", f"bad credentials {env['GH_TOKEN']}")

        with mock.patch.object(subprocess, "run", side_effect=fail) as run:
            for sent in ("ghp_first", "ghp_second"):
                with self.assertRaises(GitHubError) as caught:
                    github.viewer_login()
                self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], sent)
                self.assertIn("bad credentials ***", str(caught.exception))
                self.assertNotIn(sent, str(caught.exception))

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
        header = svc.github.git_env()["GIT_CONFIG_VALUE_0"]  # git first, before any API request reads the file
        self.assertNotEqual(header, old_header)
        self.assertEqual(base64.b64decode(header.split()[-1]).decode(), "local-autoreviewer:fj-new")
        new = svc.github.forgejo("forge.example")
        self.assertEqual((old.token, new.token), ("fj-old", "fj-new"))
        (self.forgejo_dir / "forge.example").unlink()  # the leak check still knows the token it no longer reads
        self.assertLessEqual({"fj-old", "fj-new"}, set(svc.secrets()))

    def test_the_leak_check_remembers_a_forgejo_token_it_only_saw_in_the_directory(self):
        self.write(self.token_path, "ghp_old")
        self.write(self.forgejo_dir / "other.example", "fj-unused-token")
        self.write(self.forgejo_dir / "NOTES", "tokens for the forges")
        svc = cli.build_services(config_mod.parse({}))
        self.assertLessEqual({"fj-unused-token", "tokens for the forges"}, set(svc.secrets()))
        for name in ("other.example", "NOTES"):
            (self.forgejo_dir / name).unlink()
        self.assertIn("fj-unused-token", svc.secrets())
        self.assertNotIn("tokens for the forges", svc.secrets())  # not a token: forgotten with its file

    def test_a_host_without_a_usable_token_leaves_git_working(self):
        self.write(self.token_path, "ghp_old")
        svc = cli.build_services(config_mod.parse({}))
        with self.assertRaises(GitHubError):
            svc.github.forgejo("forge.example")
        self.assertEqual(svc.github.git_env(), {})
        self.assertEqual(self.git_token(svc), "ghp_old")


if __name__ == "__main__":
    unittest.main()
