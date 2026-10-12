# SETUP — Reasonix (DeepSeek) on the home channel

[Reasonix](https://reasonix.io) is a coding agent CLI for DeepSeek models. The `reasonix`
adapter runs one `reasonix run` per message, in `adapter.cwd`, and posts the final reply.

## 1. Prerequisites

| need | check |
|---|---|
| Reasonix 2.x on `PATH` | `reasonix --version` (install the release for your OS from reasonix.io; `npm i -g reasonix` is the older 1.x line) |
| a DeepSeek API key | `DEEPSEEK_API_KEY` in the agent's environment, or run `reasonix setup` once |
| it answers headlessly | `reasonix run -p "reply with the single word: ok"` prints `ok` |
| `reasonix doctor` | the `deepseek` provider no longer shows `key:missing` |
| a bot in the group | [SETUP-ADMIN.md](SETUP-ADMIN.md) §2–5 |

## 2. Configure

```bash
pipx install swarmly
swarmly-telegram init --alias DeepSeek --adapter reasonix --role developer --bus-repo ~/projects/your-project
```

The default command is

```text
reasonix run -p --output-format text {model} {permissions} -- {prompt}
```

- `adapter.permissions` maps to Reasonix's `--permission-mode`:
  - `strict` → `acceptEdits`: file edits go through, shell commands are refused.
  - `standard` → `auto`: anything Reasonix's own rules do not deny runs, shell
    included. That is code execution as the agent's user, like Codex and Claude at `standard`.
  - `full` (default) → `bypassPermissions`: no approvals at all. Reasonix's help says its OS sandbox and
    deny rules still apply, but on Windows there is no OS sandbox (`reasonix doctor` shows
    `bash off`).

  A headless `reasonix run` never waits on a prompt: whatever would need approval is refused.
  Without a mode it chooses `ask` unless the folder is trusted and sandboxed, so swarmly
  always passes one.
- `adapter.model` (or `/model`) picks a Reasonix provider, for example `deepseek-pro`; the
  default is the one in your `reasonix.toml` (`deepseek-flash` out of the box). Swarmly checks
  the name's shape, not that `reasonix.toml` has it.
- Put `DEEPSEEK_API_KEY` in the same env file as the bot token
  (`~/.a2a-telegram.env`), or rely on the `.env` that `reasonix setup` wrote.

Each forum topic keeps its own Reasonix session: the topic's first turn runs with
`--output-format json` and the adapter stores the `session_id` it reports; later turns there
add `--resume <session_id>`, so the session remembers the task and reads its history from the
cache. The ids live in `reasonix-sessions-<alias>.json` beside the transport's other state; a
session Reasonix no longer has (`no session matches`) is replaced by a new one. Sessions are
per working folder, so keep `adapter.cwd` fixed. A custom `adapter.command` that is not
`reasonix run` stays one fresh run per message. Reasonix reads the working directory's
`REASONIX.md`, `AGENTS.md` or `CLAUDE.md` itself.

## 3. Check and run

```bash
swarmly-telegram check
swarmly-telegram run
```

`check` confirms `reasonix` resolves on the agent's `PATH` and that the bot is in the group
(as an admin, or with privacy off) before the transport starts.
