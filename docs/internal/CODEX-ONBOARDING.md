# Codex Swarm Onboarding

This is the reusable Swarmly setup for an OpenAI Codex instance. Project-specific
guides live in the target repository (for example its own onboarding docs); this document
stays generic.

## Runtime contract

Use the local Codex CLI and the Python environment where swarmly is installed:

~~~text
codex --version
codex --help
codex exec --sandbox workspace-write "reply with the single word: ok"
codex queue --help
~~~

Use codex exec for a prompt answered in Telegram (the codex adapter keeps one session per
forum topic). Use codex queue --thread <UUID-or-name>
--message <text> when a Telegram message must inherit the context of an already-open Codex
task. The codex_thread adapter is the opt-in path for that stateful route.

## Install

Windows PowerShell:

~~~powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[telegram]"
~~~

Linux or macOS:

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[telegram]'
~~~

Keep the Telegram bot token and A2A signing key in an operator-owned env file. Do not put
secrets in the YAML configuration or commit them.

## Configure

Copy config/telegram.codex.example.yaml to an ignored local config and set:

~~~yaml
agent:
  alias: Codex
  bot_token_env: A2A_TELEGRAM_BOT_TOKEN_CODEX

adapter:
  type: codex_thread
  cwd: /path/to/the/working/repository
  conversation: <CODEX_SESSION_UUID_OR_EXACT_SESSION_NAME>
  timeout_seconds: 30
~~~

The conversation value is a Codex session UUID or exact session name. codex_thread queues the
addressed Telegram prompt intact into that task and returns a bounded queue receipt. This confirms
acceptance, not completion. Each prompt supplies a durable reply ticket and the exact command
to submit an answer or blocker (`reply ID --text ... --wait 45`). A local final answer is NOT
a Telegram reply. Check `reply-status ID`; only `sent` with a Telegram message ID proves delivery.
For a pure ACK/status with no requested action, resolve with `reply ID --skip "reason"` and
post nothing. Mixed status plus an action still requires a reply. Pending tickets alert after
600 seconds by default and survive restarts. See [reply lifecycle](../telegram-channel.md#codex-task-bridge--type-codex_thread).
Structured ``REQUEST`` messages use the protocol ACK path after queue acceptance instead. It does
not pass a model, sandbox, or other execution override: the destination Codex task governs its
own model and permissions.
Every incoming message containing the exact `@example_codex_bot` username is queued, including status
or acknowledgment wording. The `codex`
adapter remains available for deployments that want `codex exec` answers posted to Telegram:
it keeps one Codex session per forum topic (General starts fresh each time).

## Validate and run

Always validate before starting the daemon:

~~~powershell
./.venv/Scripts/python.exe -m swarmly.telegram.cli --config ./config/telegram.codex.local.yaml check
./.venv/Scripts/python.exe -m swarmly.telegram.cli --config ./config/telegram.codex.local.yaml whoami
./scripts/run_codex_telegram.ps1 -Config ./config/telegram.codex.local.yaml -EnvFile ./.env
~~~

~~~bash
./.venv/bin/python -m swarmly.telegram.cli --config ./config/telegram.codex.local.yaml check
./scripts/run_codex_telegram.sh config/telegram.codex.local.yaml .env
~~~

The launcher runs check and then keeps the Telegram polling process in the foreground. It
uses the local virtualenv first, then an explicitly available system Python only as a fallback.
For a service manager, invoke the same module with the absolute path to the chosen interpreter.

## Swarm behavior

- Addressed Telegram messages only: keep respond_without_mention false.
- Keep the daemon running; it consumes no LLM tokens while idle.
- Keep the default pacing (sends at least 3 s apart, at most 18 per minute; a 60-second
  cooldown between bot-to-bot turns in General) and strict routing. Ambient or alias-only bot
  chatter remains suppressed; an exact `@example_codex_bot` mention is always queued into this task.
- Put detailed work in GitHub PRs or the durable Git bus, not Telegram.
- Work on agents/codex/<topic> branches and never commit directly to main.
- Do not deploy, release or merge to main without maintainer authorization.

## Windows access-denied workaround

On some managed Windows hosts, asyncio subprocess pipes can fail with WinError 5. The
codex_thread adapter intentionally runs codex queue through subprocess.run inside a worker
thread. Keep this adapter-specific implementation; do not change Hermes, Antigravity, custom,
or the codex exec adapter to use it.
