# local-autoreviewer

`local-autoreviewer` reviews your own GitHub pull requests automatically with **local** [Claude Code](https://docs.claude.com/en/docs/claude-code), [Codex](https://github.com/openai/codex) and [OpenCode](https://opencode.ai) sessions, and posts each review back to the PR. Claude uses the `claude` login you already have on the machine, and Codex and OpenCode log in once for reviews, so reviews draw on those subscriptions rather than on API keys. OpenCode's free models need no login at all.

It needs no inbound connectivity: a systemd timer polls GitHub every 2 minutes, so it works behind NAT on a home server or workstation.

```text
poll.timer → poll → events → ingest (your PRs, no drafts, dedupe, settle, supersede) → job queue
worker → fetch PR head → checkout (agent files removed) → claude -p / codex exec / opencode run (limited scope)
       → render → token scan
       → wait for the other agent's review → duplicate check → one post
```

For each update to one of your open PRs on a configured repo, every enabled agent reviews it (Claude Code and Codex by default), and the reviews are posted together as one.
- **Format:** a `COMMENT` review pinned to the reviewed commit.
- **One post:** a finished review waits for the other agent's review of the same commit: for as long as that one runs, and for up to `combine_wait` (30 minutes) while it waits its turn. The agent that finishes last then checks both reviews for duplicates: an issue both reported appears once, credited to both, and every item says which reviewer found it. Where the other reviewer proposed a different fix for it, that fix is shown under it too, in the review body only, so two one-click suggestions never compete for the same lines. A proposed change that isn't also a one-click suggestion is shown up to 400 lines; one that is may be shortened to 80 in the body. A review too long for GitHub shortens every change to 80 lines first, then leaves out its nits, and only then is cut, before the first finding, heading or list item that doesn't fit. A review is posted on its own when the other one failed or comes later than that, and with `combine_wait = "0s"`. If the duplicate check fails, both reviews are posted in full, with a note.
- **More reviewers:** besides `claude`, `codex` and `opencode`, you can add any number of reviewers of each type, each with its own model and effort. Add an `[agents.codex-sol]` table with `model = "gpt-6.1-sol"`, then list `"codex-sol"` in `agents`. The part of the name before the first dash is the reviewer's type, and the type alone picks the CLI and its locked flags. All the reviews of a commit are posted as one, and each is named in it ("Codex sol"). Every reviewer runs side by side with the others; reviewers of one type share that type's login. Each reviewer has its own daily cap.
- **OpenCode:** `[agents.opencode]` takes a `model` in OpenCode's `provider/model` form, such as `opencode/deepseek-v4.1-flash-free` (free, no login) or a model of your [OpenCode Go](https://opencode.ai) subscription after `local-autoreviewer opencode-login`. `effort`, if set, is appended as the model's variant (`provider/model#high`). `opencode` has no output-schema flag, so the schema goes in the prompt, and the review is the JSON object in the model's final message, checked against that schema; one that doesn't parse or match is retried like any failed review. Free models have a usage limit, which defers reviews like any other.
- **Trivial PRs:** a reviewer may judge a PR trivial (it only changes strings, comments or docs, bumps a version or renames something) and end the review early, after checking the change itself. The review says so.
- **Coverage:** correctness, security, design, performance, missing tests and readability. Findings are ranked Blocker / Major / Minor, and nits are collapsed.
- **Fixes:** each finding comes with the exact proposed change. Each finding or nit with a change within the PR's diff gets one inline comment, so one thread to reply in: that first change is a GitHub suggested change, which you apply with **Commit suggestion**, and the rest of its fix is shown in the same comment as diffs (a suggestion can only replace its own comment's lines). The review body shows every change as a diff too. Each change carries the text it replaces, so one whose line numbers are a little off is moved to where that text is, and one whose text isn't in the file is left out rather than applied to the wrong lines.
- **Severity:** reviewers rate each finding's impact from 0 to 10, its likelihood and its trigger, and severity follows from impact. Then two adjustments apply to anything below Blocker: a fix that removes lines outside the tests (`test_paths`) moves the finding up a level, and a fix the reviewer rates less readable moves it down two, as far as Nit.
- **Links:** every finding links to the file and line at that commit.
- **Pushes:** a burst of pushes produces one review. A push during a review cancels it in favour of the new commit.
- **Follow-ups:** after the first review, later commits get a follow-up instead of a full review. Each agent checks whether the previous round's issues (both agents' findings, or a previous follow-up's open ones) are fixed, partly fixed, not fixed, obsolete, or declined. It reads what you wrote on the PR since the first review (replies in its threads, comments and reviews; nobody else's), and a finding you said you won't fix is declined, with your reason, and not asked about again. Once the follow-up is posted, the review threads of a finding every reviewer of the round found fixed or obsolete are resolved, so its stale suggestion collapses on the PR; if GitHub refuses, they stay open. It also reports regressions in what changed since. If the new commits add new code (behaviour none of the previous findings asked for, rather than their fixes and the tests for them), the follow-up also reviews the whole PR as if it were new, with the full review's method, and reports test gaps, questions and nits as well. It is told what earlier rounds raised that it isn't asked about (nits, test gaps, questions, and findings you triaged away, with your note), so it doesn't post them again. The post says who reviewed the whole PR. A previous commit that is gone from the mirror means a full review again, and so does a retarget, unless the old base's changes are all in the new one. That usually covers a stacked PR whose PR below has merged, even by squash, so it carries on with follow-ups. If the PR below changed the same lines again after the last review, it's a full review.
- **Feedback:** every review ends by asking for feedback: a PR comment that starts with **Review feedback**, or a reply to one of its inline suggestions. When the PR closes, the next poll collects that feedback, and again a day later; it is kept, tied to the review it answers, for tuning the prompts (see [Review data](#review-data)).

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
  - `opencode`: tested with OpenCode 2.2.7.

  `local-autoreviewer doctor` checks that the installed CLIs support every flag the tool relies on.
- **Usage terms:** reviews run under your own Claude, ChatGPT and OpenCode subscriptions. Make sure automated use of them is acceptable under the terms that apply to you. Free OpenCode models can come with other data terms than paid ones; check them before pointing one at private code.

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
3. **Log the agents in for reviews.** For Codex, run `local-autoreviewer codex-login`. Reviews use their own `CODEX_HOME`, because a global `~/.codex/AGENTS.md` would otherwise be loaded into every review, even with `--ignore-user-config`. Log in there rather than copying `~/.codex/auth.json`: Codex processes sharing one `auth.json` take turns refreshing it, but a copy races the original for the rotating refresh token.

   **OpenCode** reviews run in a home of their own (`~/.local/state/local-autoreviewer/opencode-home/`), so your own opencode config, instructions and sessions stay out of them. Free models need no login. For OpenCode Go, run `local-autoreviewer opencode-login` and paste your Go API key when asked. It logs in to the `opencode-go` integration; name another one to log in to it instead.

   Claude uses your existing login.
4. **Check** with `local-autoreviewer doctor`.
5. **Try one PR** in the foreground, without posting:
   ```sh
   local-autoreviewer review you/repo#12
   ```
   Each agent's review is written to `~/.local/state/local-autoreviewer/jobs/<id>/out/review.md`, and the two merged as they would be posted to the last job's `out/round.md`. Add `--post` to post them.
6. **Enable:**
   ```sh
   systemctl --user enable --now local-autoreviewer-worker.service local-autoreviewer-poll.timer
   ```
   With `bootstrap = "baseline"`, the open PRs of a newly added repo are recorded but not reviewed; their next update triggers a review. To review one right away, run `local-autoreviewer enqueue you/repo#12`.

## Daily use

| Command | What it does |
| --- | --- |
| `local-autoreviewer status` | Recent jobs, their state and review links (`--json` for scripts) |
| `local-autoreviewer enqueue owner/repo#N [--agent codex-sol] [--force]` | Queue a review; with `--force`, review the same commit again |
| `local-autoreviewer review owner/repo#N [--post]` | Review now, in the foreground (see below) |
| `local-autoreviewer resume <job>` | Continue the review's agent session interactively, in its checkout |
| `local-autoreviewer ask <job> "why is X a problem?"` | Send one follow-up question to the review session |
| `local-autoreviewer poll --dry-run` | Show which events the next poll would produce |
| `local-autoreviewer findings [owner/repo#N] [--untriaged] [--json]` | List what reviews reported; `--json` exports it with each review's metadata |
| `local-autoreviewer triage ID… [--impact 0-10] [--disposition fixed\|declined\|…] [--commit SHA] [--note …]` | Record what was done about findings |
| `local-autoreviewer feedback [owner/repo#N] [--collect] [--json]` | Feedback on the reviews, collected when PRs close; `--collect` fetches it now; `--json` exports it with the reviews it answers |
| `journalctl --user -u local-autoreviewer-worker -f` | Watch the worker |

- **Foreground reviews.** `review` waits out retries of up to 5 minutes and cancels a job that would wait longer, such as one hitting a usage limit. Without `--post` it is a trial, which doesn't stop the service from reviewing that commit. With `--post`, a post interrupted by Ctrl-C is finished by the worker, and a run cancelled before its review is ready lets the service review that commit as it otherwise would have.
- **Pausing.** Set `paused = true` in the config. The worker picks this up without a restart, and running reviews finish, including posts that were interrupted or failed.
- **Daily cap.** `max_reviews_per_day` limits reviews per agent, which protects your interactive budget. The short duplicate check before a combined post is part of its review and doesn't count separately. Usage-limit errors defer the job for 30 minutes without using up a retry.
- **Plan reserve.** Before a review starts, the service reads its agent's plan usage limits, as the agents' own usage displays do. If any of them has less than `min_limit_left` percent left (10 by default), the review is skipped: nothing runs or posts for it, the job ends `skipped` with the limit in its reason, and the other reviewers' reviews of the commit post without it. A skipped review doesn't count towards `max_reviews_per_day`. A later push is checked again. A limit on one model only counts for a reviewer running that model. Readings are kept for 10 minutes per login; when they can't be read, the review runs. `doctor` shows each reviewer's limits.
  - Claude: `api.anthropic.com/api/oauth/usage`, the endpoint behind `/usage`, with your Claude login's access token. The token is never refreshed, which would rotate it out from under Claude Code: an expired one means unknown limits until Claude next runs.
  - Codex: the `account/rateLimits/read` request to a short-lived `codex app-server` in the reviews' `CODEX_HOME`, which starts no thread and runs no model.
  - OpenCode: `opencode api --standalone get /api/experimental/usage/limits` in the reviews' home: OpenCode Go's rolling, weekly and monthly meters and budgets. They count only for reviewers on Go models (`opencode-go/…`), so a free model's reviews are never skipped for them.
- **Resuming.** `resume` opens a normal interactive session with your full config. Run it in any terminal or multiplexer pane. Changes made there land in the review checkout, not your own clone.
- **Retention.** Job checkouts are kept for `retain_jobs` (14 days by default); the review output and the recorded review data stay. After that, `resume` recreates the checkout as long as the commit can still be fetched.

## Review data

Every review that passes the credential scan is also recorded in the state database, so you can see over time what the reviewers find and what is worth acting on:
- **Per review:** the agent, model, effort, a hash of the prompt and schema, the summary, whether the reviewer judged the PR trivial, whether a follow-up found new code and reviewed the whole PR (`new_code`), and which job's post carried it (`posted_with`).
- **Per item:** each finding with the reviewer's own ratings, plus test gaps, questions and nits. Reviewers rate each finding with a `severity`, an `impact` from 0 (matters only for something like a cosmic-ray bit flip) to 10 (the app would not work), a `likelihood` and a `trigger` (what has to happen). Also recorded: the proposed edits (whether each was a one-click suggestion, and whether it matched its stated lines, was moved or was left out), the readability rating, the net change in non-test lines, and the severity before and after the adjustments. Follow-ups record each previous finding's status (`resolutions`) and their regressions (kind `regression`, which for one with `new_code` includes the problems its review of the whole PR found), so you can see which fixes worked without triaging by hand. Each review is kept as its reviewer wrote it; an item the duplicate check merged into another reviewer's has `duplicate_of` set, and isn't asked about again in a follow-up.
- **Afterwards:** `triage` records your own impact rating, what was done (`fixed`, `declined`, `false-positive`, `duplicate`, `deferred`), the fixing commit and a note.
- **Feedback:** when a reviewed PR closes, merged or not, the next poll collects the feedback given on its reviews, and does so once more a day later for feedback given after the merge:
  - PR comments, and PR reviews other than the service's own, with a line that starts with `Review feedback` (also as a heading or in bold);
  - every reply in the thread of one of the review's inline suggestions;
  - other inline comments with such a line.

  Each item is tied to the review it answers: a reply to its thread's review, anything else to the latest review posted before it. `feedback --json` exports them with that review's jobs (agent, model, effort, prompt version) and the file that holds the review as posted. Each collection replaces the PR's previous one, so feedback deleted or untagged in the meantime goes. A collection that fails is retried by the next polls, up to 5 times. A PR that is reopened and closed again is collected again. PRs that closed before this existed are collected too, a few per poll, and so are PRs reviewed by hand that closed before a poll saw them.

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

**OpenCode** runs `opencode run --standalone --agent review` with a config fixed in code (`OPENCODE_CONFIG_CONTENT`) and a home of its own:
- its permission rules deny every tool, then allow only `read`, `grep` and `glob`, and reads outside the checkout only in the context directory; so it has no shell, no edits and no web access;
- skills, plugins, MCP servers and web search are denied by policy too, and language servers, formatters, snapshots, sharing and update checks are off;
- your opencode config, `AGENTS.md`, instructions and skills are not loaded, nor are the PR's: project config and Claude Code compatibility are switched off, and the global config directory is an empty one of the job's own;
- `--standalone` runs a private server for the review instead of opencode's background service;
- `resume` reopens a review's session in that home, environment and config, not yours.

**The checkout:**
- The PR's own agent files are deleted: `CLAUDE.md`, `AGENTS.md`, `.claude/`, `.codex/`, `.agents/`, `.opencode/`, `.mcp.json` and `opencode.json(c)`, case-insensitively, at any depth. The base branch's `CLAUDE.md`/`AGENTS.md` can be passed along as reference text (`project_docs`).
- Symlinks are checked out as plain files.
- Git runs with hooks, LFS smudge, fsmonitor, external diff and your `~/.gitconfig` disabled.
- Nothing from the PR is built or installed. The agents are told not to run anything; Claude has no way to.

**Reading plan limits** (see Plan reserve) sends your Claude access token only to Anthropic's usage endpoint, from the service's own process; it never reaches an agent, argv or a log.

**Running the agents:**
- Agents get an allowlisted environment, with no `GH_TOKEN` and no `SSH_AUTH_SOCK`.
- Each agent runs in a `systemd-run --user --scope` under `autoreviewer.slice`: 3 GiB and 512 tasks per review, 6 GiB in total, at lower CPU and IO weight.
- A timeout or a newer push stops the scope, which kills the whole process tree.

**Before posting:**
- The duplicate check runs the agent with the same locked-down flags and config as its review, in the same checkout.
- The review is rebuilt from structured output. HTML comments are escaped, @mentions neutralized, embedded images dropped, and the body capped at 65,536 characters.
- It is scanned for credential patterns (each review, and again the merged post), and for exact, base64, hex and reversed copies of the GitHub token and the local Claude, Codex and opencode credentials (opencode's are read from its database, yours and the reviews'). A match marks the job `blocked`, and nothing is posted.
- The PR head is re-checked immediately before posting, so a review of a superseded commit is never posted.

## Files

| Path | Contents |
| --- | --- |
| `~/.config/local-autoreviewer/config.toml` | Configuration; reloaded when it changes |
| `~/.config/local-autoreviewer/github-token` | Fine-grained token (0600) |
| `~/.local/state/local-autoreviewer/state.sqlite3` | PR snapshots, events, jobs |
| `~/.local/state/local-autoreviewer/status.json` | Snapshot written by the worker |
| `~/.local/state/local-autoreviewer/jobs/<id>/` | `repo/` checkout and `context/` (pr.diff, pr.json, …), both removed after `retain_jobs`; `out/` (agent output, review.json, review.md, the PR's pr.json once the context goes, and for the job that posted several reviews round.md and the duplicate check's output), kept |
| `~/.local/state/local-autoreviewer/mirrors/` | One bare mirror per repo |
| `~/.local/state/local-autoreviewer/codex-home/` | Codex login and sessions used for reviews |
| `~/.local/state/local-autoreviewer/opencode-home/` | OpenCode login, sessions and caches used for reviews (its `HOME`) |
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
LOCAL_AUTOREVIEWER_REAL_AGENTS=claude,codex,opencode python3 -m unittest tests.integration.test_real_agents
```

To run the service from this repository instead of the installed package, so a change goes live in seconds:

```sh
tools/live setup          # a separate worktree at main, with the user units pointed at it
tools/live deploy [ref]   # move it to another commit (default: main); the worker restarts on it once idle
local-autoreviewer-live status   # the CLI from the live worktree
tools/live teardown       # back to the installed package (install one at least as new first)
```

The live worktree is separate from the checkout you edit, so work in progress never runs.

To see how a prompt change would have reviewed an earlier commit, replay it. Git data comes from the service's local mirror, the review runs in a scratch state directory and posts nothing, and only the PR's metadata is read from GitHub (the agents still use their normal online connections). The title and description are the ones the service reviewed that commit with, where it kept them, because a description updated later can describe the fixes the review prompted; `--live-description` uses the current ones:

```sh
tools/replay owner/repo#N <commit> [--base-sha SHA] [--agent AGENT] [--prompts <dir with review.md and review.schema.json>] [--json] [--live-description]
```

Contributor and agent conventions are in [AGENTS.md](AGENTS.md).

Not built yet:
- a webhook receiver (HMAC-verified, behind a tunnel) as a second trigger next to polling;
- inline line comments;
- a GitHub App identity;
- a container runner, which would be needed before reviewing untrusted PRs.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
