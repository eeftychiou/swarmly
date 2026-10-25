# Antigravity Thread Adapter with Native agentapi Queueing & Auto-Context

**Date:** 2026-09-19  
**Status:** Historical design note (implemented in PR #16 and PR #17; the code is authoritative)  
**Branch:** `agents/antigravity/thread-agentapi-auto-context`  
**Related Issues / PRs:** Issue #9, Issue #10, PR #16, PR #17  

---

## 1. Context & Motivation

In the Swarmly Swarm, agents coordinate over Telegram and durable channels (gitbus, GitHub issues/PRs). When Antigravity (`@example_agy_bot`) receives inbound messages:
1. Previously, `AntigravityAdapter` spawned a separate headless `agy -p` CLI subprocess for each incoming message. This discarded the conversational context of the interactive IDE session, wasted context window tokens rebuilding history, and prevented the user from seeing inbound Telegram messages inside the IDE chat window.
2. In `PR #16`, `AntigravityThreadAdapter` was introduced to target an existing conversation session via `agy -p --conversation <uuid>`, but this still executed as a separate CLI process rather than queueing into the running IDE language server.
3. The Antigravity IDE platform natively provides the `agentapi` CLI tool (`~/.gemini/antigravity/bin/agentapi.bat` / in PATH):
   ```bash
   agentapi send-message [--title=<title>] <recipient_id> <content>
   ```
   Invoking `agentapi send-message <conversation_id> <content>` routes the message directly into the running IDE conversation with `priority=MESSAGE_PRIORITY_HIGH` in <200ms. The active agent session awakens, shares all existing conversation memory and tools, runs under the conversation's configured model (e.g. Gemini 3.8 Flash High), echoes the message into the chat, and reacts autonomously.

---

## 2. Requirements & Objectives

1. **Native IDE Queueing:** Use `agentapi send-message` to queue Telegram messages directly into the active Antigravity IDE conversation.
2. **Zero-Config Context Tracking (`auto` mode):** The operator should not need to edit `config.yaml` every time they switch conversations. The adapter must dynamically detect the most recently active conversation for the current workspace in `~/.gemini/antigravity/conversation_summaries.db`.
3. **Pointers & Overrides:** Support pinning a session via:
   - `config.yaml`: `adapter.conversation: <uuid | title | auto>`
   - Local workspace file: `.active_conversation` (gitignored)
   - Telegram admin command: `@example_agy_bot switch context to <uuid | title | auto>`
4. **Headless / CI Fallback:** If `agentapi` is not available or fails (e.g. headless container or CI without IDE), seamlessly fall back to `agy -p --conversation <id>` while explicitly passing `--model` matching the conversation metadata.
5. **Clean Telegram Delivery Semantics:** Return `None` from `AntigravityThreadAdapter.respond()` so that the daemon does not post duplicate queue confirmations to Telegram; the active IDE conversation owns any substantive response.

---

## 3. Architecture & Design

### 3.1 `AntigravityThreadAdapter` Workflow

```
Telegram Inbound Message (@example_agy_bot)
          │
          ▼
   AgentChannel Router
          │
          ▼
AntigravityThreadAdapter.respond(message)
          │
          ├── 1. Format channel prompt: "[Telegram Swarm: @author]: <text>"
          │
          ├── 2. Resolve target conversation ID:
          │      a. Check .active_conversation file (if present and not "auto")
          │      b. Check self.settings.conversation (if not "auto" and not empty)
          │      c. Query conversation_summaries.db for workspace cwd (most recent non-killed root convo)
          │
          ├── 3. Locate agentapi binary:
          │      a. shutil.which("agentapi") / shutil.which("agentapi.bat")
          │      b. Path.home() / ".gemini/antigravity/bin/agentapi.bat" (or "agentapi")
          │
          ├── 4. Execute queue command:
          │      `agentapi send-message --title="Telegram: @author" <conv_id> <prompt>`
          │
          ├── 5. Success (returncode == 0):
          │      Return None -> IDE session receives <SYSTEM_MESSAGE>, echoes to chat & reacts.
          │
          └── 6. Fallback (agentapi missing or nonzero exit):
                 Run `agy -p <prompt> --conversation <conv_id> --dangerously-skip-permissions --model <model>`
```

### 3.2 Dynamic Workspace Conversation Resolution

In `AntigravityAdapter.resolve_conversation()`:
```python
def resolve_conversation(self) -> Optional[Tuple[str, str]]:
    # 1. Local override file (.active_conversation)
    # 2. Configured setting if not "auto"
    # 3. Workspace auto-lookup from conversation_summaries.db:
    #    SELECT conversation_id, title, workspace_uris, last_modified_time
    #    FROM conversation_summaries
    #    WHERE (parent_conversation_id = '' OR parent_conversation_id IS NULL)
    #      AND (nesting_depth = 0 OR nesting_depth IS NULL)
    #      AND (killed = 0 OR killed IS NULL OR killed = 'false')
    #    ORDER BY last_modified_time DESC
```
Matches normalized workspace path against JSON array `workspace_uris`.

### 3.3 Configuration Schema (`swarmly/telegram/config.py`)

- `adapter.conversation` is optional for `antigravity_thread`. Defaults to `"auto"`.
- Validations ensure `antigravity_thread` does not raise an error when `conversation` is empty or set to `"auto"`.

### 3.4 In-Chat Echo & Outbound Response

- Inbound messages appear in the IDE turn as `<SYSTEM_MESSAGE>`.
- The IDE agent outputs:
  ```markdown
  📨 **[Telegram Swarm: @author]**: <content>
  ```
- If an outbound response to Telegram is needed:
  ```powershell
  uv run --extra telegram python -m swarmly.telegram.cli --config config.yaml send "<reply>"
  ```
  or coordinates via GitHub issues/PRs/gitbus per AGENTS.md durable-first policy.

---

## 4. Verification Plan

1. **Automated Unit & Integration Tests:**
   - Test `AntigravityThreadAdapter` with mocked `agentapi` returning success -> verifies `agentapi send-message` invocation and returns `None`.
   - Test `AntigravityThreadAdapter` fallback to `agy -p` when `agentapi` is absent.
   - Test `resolve_conversation` with `auto` setting against a temporary SQLite `conversation_summaries.db`.
   - Test `switch context to` admin command handling.
   - Run full pytest suite (140+ tests) to ensure no regressions.
2. **Local End-to-End Verification:**
   - Verify `swarmly-telegram --config config.yaml check` displays `agentapi send-message`.
   - Run live message delivery test via `agentapi`.
3. **Swarm Reporting & PR:**
   - Push branch `agents/antigravity/thread-agentapi-auto-context` to GitHub.
   - Launch independent subagent review per `AGENTS.md`.
   - Open PR on GitHub.
   - Post pointer to Telegram swarm channel for peer review.
