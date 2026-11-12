# Agent CLI notes

These notes record what the code relies on in the agent CLIs, as verified on 2026-10-30 against **claude-code 2.1.296**, **openai-codex 0.163.0**, systemd 262 and gh 2.102.1, and on 2026-11-11 against **opencode 2.2.7**. Re-check them whenever you change flags or bump the tested versions. `local-autoreviewer doctor` checks that the installed CLIs at least advertise every flag used.

Method: a throwaway repo with planted root and nested `CLAUDE.md` files, an `AGENTS.md`, a `.claude/settings.json` hook that touches a marker file, and a canary file outside the checkout. Every agent ran with an allowlisted environment (`env -i`). `tests/integration/test_real_agents.py` repeats the essentials through the real pipeline.

## Claude Code: `claude -p --restricted --safe-mode --strict-mcp-config --tools Read,Grep,Glob --permission-prompts none`

| Check | Result |
| --- | --- |
| Auth | Works with an existing OAuth (subscription) login (`apiKeySource: none`). Note: `--bare` would refuse OAuth logins, so it isn't used. |
| Tools | The `init` event lists exactly `Glob`, `Grep`, `Read`, and `mcp_servers: []`. |
| Reads outside the working dirs | Denied (`permission_denied`), for example `/etc/hostname` or a sibling directory. Directories passed with `--add-dir` are readable. |
| CLAUDE.md, root and nested | Not auto-loaded. The model saw them only via Glob and treated them as data. |
| Project `.claude/settings.json` hooks | Did not fire. |
| User-level hooks, plugins, status line | Did not run (`--restricted` ignores user/project/local settings files). |
| `--session-id <uuid>` | Honoured. The session is stored under `~/.claude/projects/<cwd-slug>/`. |
| `--output-format json --json-schema` | The result has `structured_output` (validated), `session_id`, `is_error`, `api_error_status`, `usage`, `total_cost_usd`. |
| `-p --resume <id>` | Continues the same session with its context. |

## Codex: `codex exec --ignore-user-config --ignore-rules --sandbox read-only …`

| Check | Result |
| --- | --- |
| Session id | The first `--json` event is `thread.started` with `thread_id`. |
| `-o` + `--output-schema` | `-o` receives the schema-shaped JSON final message. |
| Writes / network from its shell | Both blocked: read-only filesystem, and DNS resolution fails. |
| Reads outside the checkout | **Allowed.** This is the known gap. Reviews are scanned for credentials before posting, and `mode = "inline"` removes the shell. |
| Repo `AGENTS.md` | Not auto-loaded with `project_doc_max_bytes=0`, but the model followed it after reading it with `cat`. This is why agent files are deleted from the checkout. |
| Global `$CODEX_HOME/AGENTS.md` | **Still loaded** despite `--ignore-user-config`, and no config key disables it. Reviews therefore use a dedicated `CODEX_HOME` with its own login. Copying an existing `auth.json` instead would leave two holders of one rotating refresh token. |
| Several processes on one `CODEX_HOME` | Fine: Codex keeps an `auth.lock` beside `auth.json`, so processes sharing the file take turns refreshing it, and concurrent sessions on one home work. So every Codex reviewer, and `tools/replay`, share the reviews' home and run side by side. Only a copy of `auth.json` in another home races. Checked 2026-11-11 on 0.166.0, whose binary has the lock. 0.163.0 has it too: it created `~/.codex/auth.lock` on 2026-10-30. Not checked: two processes refreshing at the same moment, which can't be forced. |
| Extra tools | Code-mode `functions.exec` (exec_command, apply_patch, view_image, image_gen, goals, MCP resources) plus `collaboration.*` (spawning agents). The review disables `goals`, `image_generation`, `multi_agent`, `hooks`, `plugins`, `apps`, `browser_use`, `computer_use`, `daemon_auto_start`. |
| `--disable shell_tool --disable unified_exec` | Codex then **cannot read any file**. Hence `mode = "inline"` puts the diff and changed files in the prompt. |
| `codex sandbox` under `NoNewPrivileges` | Works; Codex's bubblewrap sandbox sets no_new_privs itself. |

## OpenCode: `opencode run --standalone --agent review --format json`

Verified on 2026-11-11 against **opencode 2.2.7**, with `opencode/deepseek-v4.1-flash-free` and a home of its own (`HOME` and the XDG directories under the state directory). A trap repo held `AGENTS.md`, `CLAUDE.md`, an `opencode.json` granting every permission and naming an instruction file, a `.opencode/agents/review.md` redefining the agent, and skills in `.opencode/skills/`, `.agents/skills/`, the home's `~/.agents/skills/` and its config directory. A canary sat outside the checkout.

| Check | Result |
| --- | --- |
| Auth | Free models (`opencode/*-free`) run with no login. OpenCode Go is the `opencode-go` integration, logged in with an API key (method `key`; `opencode auth login --standalone opencode-go` asks for it and needs a terminal). Logins live in the home's SQLite database: `credential.value` is JSON, `{"type": "key", "key"}` or `{"type": "oauth", "access", "refresh", "expires"}` (the binary's `Credential.Value`). |
| Models | `opencode models --standalone` prints one bare `provider/model` per line, only those available: without a login, the 12 free ones; `opencode-go/glm-5` was then refused as unavailable. |
| Background service | Plain `opencode run` connects to, or starts, a background server under `HOME`. `--standalone`, a global flag every subcommand takes, runs a private server for the one command instead. |
| Concurrency | Two `opencode run --standalone` in one home at once both finished, their runs overlapping. |
| Resume | `opencode --standalone --session <id>` starts the TUI (it ran until stopped; not tried on a real terminal). The top-level command has no `--agent` (with it, it prints its help and exits 1), so the locked config's `default_agent` selects the review agent. `opencode run --session <id>` continues a session with its context. |
| Output | `--format json` prints one event per line: `step_start`, `text` (`part.text`), `tool_use`, `step_finish` (`part.reason`: `tool-calls`, `stop`, `length`; `part.tokens`, `part.cost`), and `error` (`error.type`, `error.message`; exit 1). Every event carries `sessionID`. A prompt on stdin works. |
| Output schema | **No flag.** The schema goes in the prompt; the review is the JSON object in the final message, which the adapter checks against the schema itself. DeepSeek V4.1 Flash answered three full reviews in the schema; a follow-up and the duplicate check weren't tried against the real CLI (the free tier's limit stopped the duplicate check). |
| Config sources | `opencode debug config` lists them. With `OPENCODE_CONFIG_DIR` set, it replaces the global config directory, which opencode creates and leaves empty. With `OPENCODE_CONFIG_PROJECT_DISABLE`/`OPENCODE_DISABLE_PROJECT_CONFIG`, the repo's `opencode.json` and `.opencode/` aren't sources; without them, both are. `OPENCODE_CONFIG_CONTENT` comes last. |
| Instruction files | Not loaded with project config and Claude Code compatibility switched off: a one-word prompt cost 1,461 input tokens with the switches set and 1,480 without them (the 19 are the one-line `AGENTS.md` and `CLAUDE.md`), and no trap word appeared. |
| Permissions | Rules are `{action, resource, effect}`; the last match wins, after opencode's defaults (which `opencode debug agents` shows). With `deny *` then `allow read/grep/glob`, the model is offered `read`, `grep`, `glob` and `skill` only: no shell, edit, write or fetch tool. No rule is left at `ask`, which would need a person. |
| Reads outside the checkout | Denied (`Permission denied: external_directory`) for `read`, `grep` and `glob`, also through `context/../outside`. Allowed in the context directory by an `external_directory` rule for `<context>/*`, nested files included. |
| Skills | Those in the repo's `.opencode/`/`.agents/` and the config directory aren't found; one in the home's `~/.agents/skills/` is found and refused (`Permission denied: skill`), as is the built-in `browser`. |
| Errors | An unknown or unavailable model: `provider.no-route` "Model unavailable", exit 1. The free tier's limit: `provider.free-limit` "Free usage limit reached", which counts as a usage limit. |
| Grep and glob | Use the system `ripgrep`, which Arch's `opencode` package depends on; nothing is downloaded. |

## systemd: `systemd-run --user --scope`

| Check | Result |
| --- | --- |
| `MemoryMax`, `TasksMax` | Applied to the scope's cgroup (`memory.max`, `pids.max`). |
| OOM enforcement | Needs `MemorySwapMax=0`. Without it, swap (for example zram) absorbs the overrun. With it, a 200 MiB allocation under a 64 MiB limit is killed (exit 137). |
| Kill tree | `systemctl --user stop <scope>` kills every process in it, including a `setsid` child. |
| Slice naming | Dashes nest slices: `foo-bar.slice` lives under `foo.slice`. Hence `autoreviewer.slice`. |

## Plan usage limits

Read on 2026-11-11 with the logins reviews use, as the operator's usage bar reads them. Nothing here starts a session or runs a model.

| Agent | Source | Reply |
| --- | --- | --- |
| Claude | `GET https://api.anthropic.com/api/oauth/usage`, `Authorization: Bearer <claudeAiOauth.accessToken>` from `~/.claude/.credentials.json`, `anthropic-beta: oauth-2025-04-20`. Not a public API: it is what `/usage` shows. Rate limited (`429` with `Retry-After`). | A `limits` list: `{kind: session \| weekly_all \| weekly_scoped, percent, resets_at (ISO 8601), scope: null or {model: {display_name}}}`, and the same windows as `five_hour`/`seven_day` `{utilization, resets_at}`. Seen: a session, a weekly and a one-model weekly limit. |
| Codex (0.166.0) | `codex app-server --listen stdio://`: `initialize`, then `account/rateLimits/read` (JSON-RPC lines). | `rateLimitsByLimitId.codex` (and `rateLimits`): `primary`/`secondary` `{usedPercent, windowDurationMins, resetsAt (epoch s)}`, `normalModelSlug`, `ordinaryUsageAllowed`. Some plans have only a weekly window (10080 min), as `primary`. |
| OpenCode (2.2.7) | `opencode api --standalone get /api/experimental/usage/limits` | Without an OpenCode Go sign-in, `{"data": null}`. With one, as the usage bar reads it, `data.go.{rolling,weekly,monthly}` `{percent, limited, status, resetsAt (epoch ms)}` and `data.budgets` `{limitDollars, spentDollars}`; not seen here. |

## Duplicate check

Before the reviews of a commit are posted as one, the agent whose review finished last runs once more to find the items both reviews share. It is the same invocation as a review, with the same locked flags (and for opencode the same config), in the same checkout and context directory. Only the prompt (`prompts/duplicates.md`, with the items inline) and the output schema (`prompts/duplicates.schema.json`) differ, and the output goes to `out/duplicates/`. The schema follows the same rules as the review schema: every object has `additionalProperties: false` and lists all its properties in `required`, which Codex's `--output-schema` needs.

## Footprint

A small review took about 25 s with Claude Haiku and about 28 s with Codex at low reasoning effort, and about 195 s with opencode's free DeepSeek V4.1 Flash (5 steps, 13k reasoning tokens). Larger PRs with stronger models take minutes; the default timeout is 20 minutes.

## Not yet verified

- The exact shape of usage-limit errors from each CLI. Parsing is defensive: `api_error_status == 429` or usage-limit wording counts as a usage limit. Wording that says the plan is used up ("usage limit", "limit reached", "quota", "free limit") skips the review; anything else, such as "rate limit exceeded", is deferred for 30 minutes. Seen: OpenCode's free tier answers `provider.free-limit` "Free usage limit reached".
