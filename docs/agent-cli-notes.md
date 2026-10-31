# Agent CLI notes

These notes record what the code relies on in the agent CLIs, as verified on 2026-10-30 against **claude-code 2.1.296**, **openai-codex 0.163.0**, systemd 262 and gh 2.102.1. Re-check them whenever you change flags or bump the tested versions. `local-autoreviewer doctor` checks that the installed CLIs at least advertise every flag used.

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
| Extra tools | Code-mode `functions.exec` (exec_command, apply_patch, view_image, image_gen, goals, MCP resources) plus `collaboration.*` (spawning agents). The review disables `goals`, `image_generation`, `multi_agent`, `hooks`, `plugins`, `apps`, `browser_use`, `computer_use`, `daemon_auto_start`. |
| `--disable shell_tool --disable unified_exec` | Codex then **cannot read any file**. Hence `mode = "inline"` puts the diff and changed files in the prompt. |
| `codex sandbox` under `NoNewPrivileges` | Works; Codex's bubblewrap sandbox sets no_new_privs itself. |

## systemd: `systemd-run --user --scope`

| Check | Result |
| --- | --- |
| `MemoryMax`, `TasksMax` | Applied to the scope's cgroup (`memory.max`, `pids.max`). |
| OOM enforcement | Needs `MemorySwapMax=0`. Without it, swap (for example zram) absorbs the overrun. With it, a 200 MiB allocation under a 64 MiB limit is killed (exit 137). |
| Kill tree | `systemctl --user stop <scope>` kills every process in it, including a `setsid` child. |
| Slice naming | Dashes nest slices: `foo-bar.slice` lives under `foo.slice`. Hence `autoreviewer.slice`. |

## Duplicate check

Before the reviews of a commit are posted as one, the agent whose review finished last runs once more to find the items both reviews share. It is the same invocation as a review, with the same locked flags, in the same checkout and context directory. Only the prompt (`prompts/duplicates.md`, with the items inline) and the output schema (`prompts/duplicates.schema.json`) differ, and the output goes to `out/duplicates/`. The schema follows the same rules as the review schema: every object has `additionalProperties: false` and lists all its properties in `required`, which Codex's `--output-schema` needs.

## Footprint

A small review took about 25 s with Claude Haiku and about 28 s with Codex at low reasoning effort. Larger PRs with stronger models take minutes; the default timeout is 20 minutes.

## Not yet verified

- The exact shape of usage-limit errors from each CLI. Parsing is defensive: `api_error_status == 429` or usage-limit wording becomes a 30-minute deferral.
