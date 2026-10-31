# local-autoreviewer — notes for contributors and coding agents

`local-autoreviewer` reviews its operator's own GitHub PRs with local `claude -p` and `codex exec` sessions. It runs as systemd user units and ships as an Arch package. `README.md` describes the behaviour and the security model. `docs/agent-cli-notes.md` records verified facts about the agent CLIs that the code depends on.

## Rules

- Use only the standard library at runtime, Python ≥ 3.11. The package depends only on `python`, `git`, `github-cli` and `systemd`. Discuss any new runtime dependency first.
- Tests use stdlib `unittest`: `python3 -m unittest discover -s tests -t .` (`pytest` works too). They must stay offline and fast, because they also run in the package's `check()`. Use the fakes in `tests/helpers.py` and `tests/fakes/fake_agent.py`, never a real agent or the GitHub API. The real-CLI test in `tests/integration/` is opt-in.
- Never weaken the agent invocation. The locked parts are:
  - the flags in `agents/claude.py` and `agents/codex.py` (`_locked_flags`, `DISABLED_FEATURES`);
  - the environment allowlist in `agents/base.py`;
  - the agent-file removal and git hardening in `gitops.py`.

  `tests/unit/test_agents.py` and `tests/unit/test_pipeline.py` pin all of these. Configuration must never be able to change them.
- The GitHub token may appear only in the `gh`/`git` subprocess environment (`github.gh_env`). It must never reach an agent, argv or a log line.
- When you change agent CLI flags, re-verify against the installed CLIs, update `docs/agent-cli-notes.md` and the `REQUIRED_FLAGS` that `doctor` checks.
- The SQLite schema only grows: add a migration to `db.MIGRATIONS` and never edit an existing one. Rolling back relies on older builds tolerating newer columns.
- systemd slice names must not contain dashes, because dashes mean nesting. That is why the slice is `autoreviewer.slice`.
- Releases: bump `version` in `pyproject.toml`, add `release-notes/vX.Y.Z.md`, then push the tag `vX.Y.Z`. `.github/workflows/release.yml` builds and checks the pacman package and attaches it, with `SHA256SUMS`, to the GitHub release.
- New source files start with `# SPDX-License-Identifier: GPL-3.0-or-later`.
