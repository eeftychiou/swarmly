# Command authorization map

This inventory covers commands and directives that Swarmly acts on. “Admin” means the configured `agent.admin_user_id`, checked with `AgentConfig.is_admin`; Telegram group-admin status is not enough. Telegram commands must also address this bot (or be a permitted reply/mention). Commands addressed to another bot are ignored.

| Command / directive | Plane | Who may trigger it | Check location | Refusal test |
|---|---|---|---|---|
| `/ping`, `/status`, `/diag`, `/where`, `/role`, `/round`, `/wakes`, `/usage` (read only) | Telegram | Any member, addressed to this bot | `channel.AgentChannel._handle_command` | Addressing/filtering cases in `tests/test_telegram_channel.py`; wake mutation refusal in `tests/test_wake.py::test_only_the_admin_cancels` |
| `/memory`, `/memory clear` | Telegram | Admin only | `channel.AgentChannel._memory_command` | `tests/test_lessons.py::TestTheMemoryCommand::test_anyone_else_is_refused` |
| `/model <name>`, `/reasoning <level>` | Telegram | Admin to change; anyone may read | `channel.AgentChannel._model_command`, `_reasoning_command` | `tests/test_model_switch.py::test_only_the_admin_switches`; `tests/test_reasoning_command.py::test_only_the_admin_sets_it` |
| `/cli[!] COMMAND` | Telegram | Admin only, addressed to this bot | `channel.AgentChannel._handle_native`; command allowlist and confirmation in `native.plan` | `tests/test_cli_passthrough.py::test_a_peer_who_is_not_the_admin_is_refused`; unsafe/confirmation cases in `tests/test_native.py` |
| `/cleanup` | Telegram | Admin only; explicit confirmation for deletion | `channel.AgentChannel._handle_cleanup`; `cleanup.Cleanup.handle_args` | `tests/test_cleanup.py` non-admin and confirmation cases |
| `/admit`, `/remove`, `/sync` | Telegram | Admin only | `channel.AgentChannel._handle_roster` | `tests/test_roster.py::test_only_the_admin_admits`; `test_only_the_admin_starts_a_sync` |
| `/topic #N…` | Telegram | Admin only; a peer bot's is ignored | `channel.AgentChannel._handle_topic_pin` | `tests/test_topic_sprawl.py::TestWorkFollowsItsTopic::test_only_the_admin_moves_items_and_other_bots_apply_it_silently` |
| `/pause`, `/resume` | Telegram | Admin only | `channel.AgentChannel._handle_pause` | `tests/test_telegram_ductor_parity.py::test_only_the_admin_pauses_and_a_bare_pause_from_others_is_silent` |
| `/hold`, `/release` | Telegram | Admin only | `channel.AgentChannel._handle_hold` | `tests/test_hold.py::HoldTests::test_only_the_admin_holds` |
| `/stop` | Telegram | Admin only; peer bots are silently consumed | `channel.AgentChannel._handle_stop` | `tests/test_telegram_ductor_parity.py::test_only_the_admin_can_stop_a_turn` |
| Signed FREEZE/RESUME envelopes | Bus | Authenticated sender; broadcast control also requires the configured admin identity when `require_admin_for_broadcast` is enabled | `poll.poll_once` freeze-control branch, `cfg.is_admin(author_admin_id)` | `tests/test_bus_poll.py::test_broadcast_freeze_is_refused_without_admin_author`; signed-header mismatch cases in `tests/test_signed_envelope.py` |
| Other addressed bus messages | Bus | Accepted GitBus sender; addressed to this agent. A bus writer does not gain Telegram admin rights. | `poll._accepted_items`, `poll.poll_once`; verification in `swarmly/swarm/envelope.py` | `tests/test_bus_poll.py`, `tests/test_signed_envelope.py` |
| Issue/PR mention, assignment, review request, or activity notification | GitHub | Matching repository notification can request a turn; it cannot authorize privileged transport actions | `github.watch.GitHubWatcher` filters and deduplicates notifications; turn input is handled by `channel.AgentChannel` | `tests/test_github_watch.py` filtering tests; there is no GitHub-to-admin identity mapping |
| GitHub CLI/API actions during a turn | GitHub | The agent process, constrained by adapter permissions and its configured token; not an admin command channel | Adapter command permissions; `github.watch` uses read-only API calls | `tests/test_adapter_permissions.py`; `tests/test_github_watch.py` token isolation |
| #117 dashboard inline-button callbacks | Telegram callback | Admin per #117; second tap for destructive/state-changing actions | **Not implemented in this checkout.** No callback-query handler or authorization function exists under `swarmly/telegram/`. | No callback refusal test exists. Tracked by #117, whose acceptance requires a non-admin press refusal. |

## Scope and gaps

`swarmly-telegram` CLI subcommands are local operator entry points, controlled by host access and process invocation. GitHub polling is read-only; notification-triggered turns do not inherit GitHub's identity as transport-admin authority.

The #117 callback surface is absent here, so this inventory records the gap rather than naming a nonexistent check or test. #117 must add a non-admin refusal test with its callback implementation. No permission defaults change in this PR.
