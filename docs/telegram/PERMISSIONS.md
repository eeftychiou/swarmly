# Adapter × permission matrix

This document records what each CLI adapter is started with at every permission level (`strict`, `standard`, `full`) and what capabilities (file editing, shell execution, network access) are observed and permitted.

Permission levels are configured under `adapter.permissions` in `config.yaml` (default: `full`).

Per the Round 5 scope cut, this matrix documents the **Claude**, **Codex**, and **Antigravity** adapters. Other adapters follow in subsequent rounds.

## Matrix

| Adapter | Level | CLI invocation / flags passed | Edit | Shell | Network | Verification source | Notes |
|---|---|---|---|---|---|---|---|
| `claude` / `claude_thread` | `strict` | `claude -p --output-format json --permission-mode acceptEdits -- {prompt}` | Yes | No | No | verified-live, read-from-code | Accepts workspace file edits without interactive prompt; disables tool access for Bash and PowerShell; no external network access. |
| `claude` / `claude_thread` | `standard` | `claude -p --output-format json --permission-mode acceptEdits --allowedTools Bash(git:*),PowerShell(git:*),Bash(pytest:*),PowerShell(pytest:*),Bash(gh:*),PowerShell(gh:*) -- {prompt}` | Yes | Restricted | Restricted | verified-live, read-from-code | Accepts file edits; shell commands restricted to allowlisted executables (`git`, `pytest`, `gh`, plus any configured `adapter.allow_commands`); network access permitted only through allowlisted commands. |
| `claude` / `claude_thread` | `full` | `claude -p --output-format json --permission-mode bypassPermissions -- {prompt}` | Yes | Yes | Yes | verified-live, read-from-code | Bypasses permission prompts; full shell and network access across the host environment. |
| `codex` | `strict` | `codex exec --skip-git-repo-check --sandbox workspace-write {prompt}` | Yes | Yes (sandboxed) | No | verified-live, read-from-code | Confined to `workspace-write` sandbox; file writes allowed inside workspace; shell runs inside sandbox container; network access disabled by default. |
| `codex` | `standard` | `codex exec --skip-git-repo-check --sandbox workspace-write -c sandbox_workspace_write.network_access=true {prompt}` | Yes | Yes (sandboxed) | Yes | verified-live, read-from-code | Confined to `workspace-write` sandbox; file writes allowed inside workspace; network access explicitly enabled via `-c sandbox_workspace_write.network_access=true`. |
| `codex` | `full` | `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox {prompt}` | Yes | Yes | Yes | verified-live, read-from-code | Sandbox and approvals disabled; direct unrestricted host shell and network execution. |
| `antigravity` / `antigravity_thread` | `strict` | `agy -p {prompt} {model} {effort} --print-timeout 20m --dangerously-skip-permissions` | Yes | Yes | Yes | verified-live, read-from-code | AGY currently lacks granular sub-process sandboxing flags; passes `--dangerously-skip-permissions` at all levels until upstream fine-grained sandboxing is available. |
| `antigravity` / `antigravity_thread` | `standard` | `agy -p {prompt} {model} {effort} --print-timeout 20m --dangerously-skip-permissions` | Yes | Yes | Yes | verified-live, read-from-code | Identical template to `strict`; permission flags remain unchanged. |
| `antigravity` / `antigravity_thread` | `full` | `agy -p {prompt} {model} {effort} --print-timeout 20m --dangerously-skip-permissions` | Yes | Yes | Yes | verified-live, read-from-code | Unrestricted host execution with launch-time approval prompts skipped. |

## Notes and Scope

- **Defaults**: Default permission level across all adapters remains `full` (`DEFAULT_PERMISSIONS = "full"` in `swarmly.telegram.config`). No defaults or adapter codes are modified in this round.
- **Custom command template override**: Setting an explicit `adapter.command` in `config.yaml` bypasses the template generation and overrides all level flags.
- **Hermes and other adapters**: Hermes and remaining adapters are deferred to later rounds per coordinator/admin directive for #140.
