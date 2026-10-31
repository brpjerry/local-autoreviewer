# local-autoreviewer

`local-autoreviewer` reviews your own GitHub pull requests automatically with **local** [Claude Code](https://docs.claude.com/en/docs/claude-code) and [Codex](https://github.com/openai/codex) sessions, and posts each review back to the PR. It uses the `claude` and `codex` logins you already have on the machine, so reviews draw on those subscriptions rather than on API keys.

It needs no inbound connectivity: a systemd timer polls GitHub every 2 minutes, so it works behind NAT on a home server or workstation.

```text
poll.timer → poll → events → ingest (your PRs, no drafts, dedupe, settle, supersede) → job queue
worker → fetch PR head → checkout (agent files removed) → claude -p / codex exec (limited scope) → render → token scan → post
```

For each update to one of your open PRs on a configured repo, every enabled agent posts one review: Claude Code and Codex by default.
- **Format:** a `COMMENT` review pinned to the reviewed commit.
- **Coverage:** correctness, security, design, performance, missing tests and readability. Findings are ranked Blocker / Major / Minor, and nits are collapsed.
- **Fixes:** each finding comes with the exact proposed change. Changes within the PR's diff are posted as GitHub suggested changes, which you apply with **Commit suggestion**; the review body shows every change as a diff too.
- **Severity:** reviewers rate each finding's impact from 0 to 10, its likelihood and its trigger, and severity follows from impact. Then two adjustments apply to anything below Blocker: a fix that removes lines outside the tests (`test_paths`) moves the finding up a level, and a fix the reviewer rates less readable moves it down two, as far as Nit.
- **Links:** every finding links to the file and line at that commit.
- **Pushes:** a burst of pushes produces one review. A push during a review cancels it in favour of the new commit.

> **Security model: built for your own PRs.**
>
> By default only PRs authored by the token's owner are reviewed. The agents run on the host as your user. They run without a container, but with locked-down CLI flags (see [What protects what](#what-protects-what)).
>
> That is proportionate when you wrote the code. It is not a sandbox for hostile input: a malicious PR could try to steer an agent through prompt injection. Do not add other people to `authors`, or `enqueue` untrusted PRs, unless you accept that.

## Requirements

- **System:** Linux with a systemd user manager (`systemd-run --user`). Enable lingering (`loginctl enable-linger`) if reviews should run while you're logged out.
- **Tools:** Python ≥ 3.11, `git`, and [`gh`](https://cli.github.com/).
- **Agents:** at least one of them, logged in:
  - `claude`: tested with Claude Code 2.1.296. It needs `--restricted` and `--safe-mode`.
  - `codex`: tested with Codex CLI 0.163.0.

  `local-autoreviewer doctor` checks that the installed CLIs support every flag the tool relies on.
- **Usage terms:** reviews run under your own Claude and ChatGPT subscriptions. Make sure automated use of them is acceptable under the terms that apply to you.

## Install

**Arch Linux:** each [release](https://github.com/brpjerry/local-autoreviewer/releases) carries a pacman package and `SHA256SUMS`:

```sh
sha256sum -c SHA256SUMS
sudo pacman -U local-autoreviewer-*-any.pkg.tar.zst
```

To build it yourself from a checkout, see [packaging/arch/README.md](packaging/arch/README.md). The package installs the `local-autoreviewer` command and the systemd **user** units, and enables nothing.

**Other distributions:** run it from a checkout with `bin/local-autoreviewer`; it uses only the standard library. Copy the units from `systemd/` to `~/.config/systemd/user/` and change `ExecStart=` to the checkout's `bin/local-autoreviewer`.

## Setup

1. **Create a GitHub token.** Create a fine-grained token at <https://github.com/settings/personal-access-tokens>:
   - repository access: only the repos you want reviewed;
   - permissions: *Metadata: read*, *Contents: read*, *Pull requests: read and write*.

   Save it with mode 0600 (paste it, then press Ctrl-D):
   ```sh
   install -Dm600 /dev/stdin ~/.config/local-autoreviewer/github-token
   ```
   The token is passed only to the `gh` and `git` processes this tool starts. It is never written to `gh`'s own config and never reaches an agent. Reviews are posted as the token's owner.
2. **Configure.** Copy `config.example.toml` (installed in `/usr/share/doc/local-autoreviewer/`) to `~/.config/local-autoreviewer/config.toml` and list your repos under `[[repo]]`. Check it with `local-autoreviewer config-check`.
3. **Log Codex in for reviews:** run `local-autoreviewer codex-login`. Reviews use their own `CODEX_HOME`, for two reasons:
   - a global `~/.codex/AGENTS.md` would otherwise be loaded into every review, even with `--ignore-user-config`;
   - sharing one `auth.json` between review and interactive sessions risks refresh-token races.

   Claude uses your existing login.
4. **Check** with `local-autoreviewer doctor`.
5. **Try one PR** in the foreground, without posting:
   ```sh
   local-autoreviewer review you/repo#12
   ```
   The review is written to `~/.local/state/local-autoreviewer/jobs/<id>/out/review.md`. Add `--post` to post it.
6. **Enable:**
   ```sh
   systemctl --user enable --now local-autoreviewer-worker.service local-autoreviewer-poll.timer
   ```
   With `bootstrap = "baseline"`, the open PRs of a newly added repo are recorded but not reviewed; their next update triggers a review. To review one right away, run `local-autoreviewer enqueue you/repo#12`.

## Daily use

| Command | What it does |
| --- | --- |
| `local-autoreviewer status` | Recent jobs, their state and review links (`--json` for scripts) |
| `local-autoreviewer enqueue owner/repo#N [--agent codex] [--force]` | Queue a review; with `--force`, review the same commit again |
| `local-autoreviewer review owner/repo#N [--post]` | Review now, in the foreground (see below) |
| `local-autoreviewer resume <job>` | Continue the review's agent session interactively, in its checkout |
| `local-autoreviewer ask <job> "why is X a problem?"` | Send one follow-up question to the review session |
| `local-autoreviewer poll --dry-run` | Show which events the next poll would produce |
| `local-autoreviewer findings [owner/repo#N] [--untriaged] [--json]` | List what reviews reported; `--json` exports it with each review's metadata |
| `local-autoreviewer triage ID… [--impact 0-10] [--disposition fixed\|declined\|…] [--commit SHA] [--note …]` | Record what was done about findings |
| `journalctl --user -u local-autoreviewer-worker -f` | Watch the worker |

- **Foreground reviews.** `review` waits out retries of up to 5 minutes and cancels a job that would wait longer, such as one hitting a usage limit. Without `--post` it is a trial, which doesn't stop the service from reviewing that commit. With `--post`, a post interrupted by Ctrl-C is finished by the worker, and a run cancelled before its review is ready lets the service review that commit as it otherwise would have.
- **Pausing.** Set `paused = true` in the config. The worker picks this up without a restart, and running reviews finish, including posts that were interrupted or failed.
- **Daily cap.** `max_reviews_per_day` limits reviews per agent, which protects your interactive budget. Usage-limit errors defer the job for 30 minutes without using up a retry.
- **Resuming.** `resume` opens a normal interactive session with your full config. Run it in any terminal or multiplexer pane. Changes made there land in the review checkout, not your own clone.
- **Retention.** Job checkouts are kept for `retain_jobs` (14 days by default); the review output and the recorded review data stay. After that, `resume` recreates the checkout as long as the commit can still be fetched.

## Review data

Every review that passes the credential scan is also recorded in the state database, so you can see over time what the reviewers find and what is worth acting on:
- **Per review:** the agent, model, effort, a hash of the prompt and schema, and the summary.
- **Per item:** each finding with the reviewer's own ratings, plus test gaps, questions and nits. Reviewers rate each finding with a `severity`, an `impact` from 0 (matters only for something like a cosmic-ray bit flip) to 10 (the app would not work), a `likelihood` and a `trigger` (what has to happen). Also recorded: the proposed edits (and whether each was a one-click suggestion), the readability rating, the net change in non-test lines, and the severity before and after the adjustments.
- **Afterwards:** `triage` records your own impact rating, what was done (`fixed`, `declined`, `false-positive`, `duplicate`, `deferred`), the fixing commit and a note.

`findings --json` exports everything, including the job's commit, so the rounds of review on a PR can be reconstructed. Housekeeping also records reviews saved before this existed, and an expired job loses its checkout but keeps its `out/` directory.

## What protects what

**Claude** runs with `--restricted --safe-mode --strict-mcp-config --tools Read,Grep,Glob --permission-prompts none`:
- it has no tool that runs commands and no web access;
- its file tools are confined to the checkout and the context directory;
- your hooks, plugins, MCP servers and CLAUDE.md are not loaded.

[docs/agent-cli-notes.md](docs/agent-cli-notes.md) records how this was verified.

**Codex** runs with `--sandbox read-only --ignore-user-config --ignore-rules` and approval set to `never`:
- web search is disabled, as are hooks, plugins, apps, browser and computer use, multi-agent, goals and image generation;
- its shell can read files but cannot write or reach the network.

The known gap is that Codex **can read outside the checkout**. Set `[agents.codex] mode = "inline"` to remove its shell; the diff and changed files are then put in the prompt instead.

**The checkout:**
- The PR's own agent files are deleted: `CLAUDE.md`, `AGENTS.md`, `.claude/`, `.codex/`, `.agents/` and `.mcp.json`, case-insensitively, at any depth. The base branch's `CLAUDE.md`/`AGENTS.md` can be passed along as reference text (`project_docs`).
- Symlinks are checked out as plain files.
- Git runs with hooks, LFS smudge, fsmonitor, external diff and your `~/.gitconfig` disabled.
- Nothing from the PR is built or installed. The agents are told not to run anything; Claude has no way to.

**Running the agents:**
- Agents get an allowlisted environment, with no `GH_TOKEN` and no `SSH_AUTH_SOCK`.
- Each agent runs in a `systemd-run --user --scope` under `autoreviewer.slice`: 3 GiB and 512 tasks per review, 6 GiB in total, at lower CPU and IO weight.
- A timeout or a newer push stops the scope, which kills the whole process tree.

**Before posting:**
- The review is rebuilt from structured output. HTML comments are escaped, @mentions neutralized, embedded images dropped, and the body capped at 65,536 characters.
- It is scanned for credential patterns, and for exact, base64, hex and reversed copies of the GitHub token and the local Claude and Codex credentials. A match marks the job `blocked`, and nothing is posted.
- The PR head is re-checked immediately before posting, so a review of a superseded commit is never posted.

## Files

| Path | Contents |
| --- | --- |
| `~/.config/local-autoreviewer/config.toml` | Configuration; reloaded when it changes |
| `~/.config/local-autoreviewer/github-token` | Fine-grained token (0600) |
| `~/.local/state/local-autoreviewer/state.sqlite3` | PR snapshots, events, jobs |
| `~/.local/state/local-autoreviewer/status.json` | Snapshot written by the worker |
| `~/.local/state/local-autoreviewer/jobs/<id>/` | `repo/` checkout and `context/` (pr.diff, pr.json, …), both removed after `retain_jobs`; `out/` (agent output, review.json, review.md), kept |
| `~/.local/state/local-autoreviewer/mirrors/` | One bare mirror per repo |
| `~/.local/state/local-autoreviewer/codex-home/` | Codex login and sessions used for reviews |
| `~/.claude/projects/<job checkout>/` | Claude sessions (for `resume`) |

Run one worker per state directory. A second worker sharing it could, for example, finish the same interrupted post twice.

## Development

```sh
python3 -m unittest discover -s tests -t .      # or: pytest
bin/local-autoreviewer --help                  # runs straight from the checkout
```

The tests run offline. They use fake agent executables (`tests/fakes/fake_agent.py`), a fake GitHub API, and a local bare repo laid out like GitHub (`refs/pull/N/head`).

The opt-in `tests/integration/test_real_agents.py` runs the real CLIs and uses a little subscription quota:

```sh
LOCAL_AUTOREVIEWER_REAL_AGENTS=claude,codex python3 -m unittest tests.integration.test_real_agents
```

To run the service from this repository instead of the installed package, so a change goes live in seconds:

```sh
tools/live setup          # a separate worktree at main, with the user units pointed at it
tools/live deploy [ref]   # move it to another commit (default: main); the worker restarts on it once idle
local-autoreviewer-live status   # the CLI from the live worktree
tools/live teardown       # back to the installed package (install one at least as new first)
```

The live worktree is separate from the checkout you edit, so work in progress never runs.

To see how a prompt change would have reviewed an earlier commit, replay it. Git data comes from the service's local mirror, the review runs in a scratch state directory and posts nothing, and only the PR's metadata is read from GitHub (the agents still use their normal online connections):

```sh
tools/replay owner/repo#N <commit> [--base-sha SHA] [--agent claude|codex] [--prompts <dir with review.md and review.schema.json>] [--json]
```

Contributor and agent conventions are in [AGENTS.md](AGENTS.md).

Not built yet:
- a webhook receiver (HMAC-verified, behind a tunnel) as a second trigger next to polling;
- inline line comments;
- a GitHub App identity;
- a container runner, which would be needed before reviewing untrusted PRs.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
