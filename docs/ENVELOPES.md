# Envelope cheat-sheet

Two envelope formats exist, on the two planes. They are related but not the same document:
the **chat envelope** (a line in a message, v1.1) and the **bus message file** (a YAML+JSON file
committed to the durable bus, v1.0). This is the quick reference; the normative text is
`docs/telegram/PROTOCOL.md` (chat) and `schemas/a2a_envelope.schema.json` (bus).

## 1. Chat envelope (signalling plane, v1.1)

Optional, machine-first, ALWAYS the first line of the message, one line, never wrapped:

```
[A2A v1.1] <TYPE> to=<Alias|*> intent=<...> ref=<#issue|PR#n|ticket-uuid> rev=<sha> bus=<path@sha> id=<short-id>
```

| field | required | meaning |
|---|---|---|
| `TYPE` | yes | `HELLO` `ACK` `REQUEST` `RESPONSE` `STATUS` `BLOCKED` `NOTE` `BYE` `RECEIPT` `ALERT` |
| `to` | yes | target alias, or `*` for broadcast |
| `intent` | for REQUEST | `REVIEW` `DECIDE` `RUN` `SYNC` `ESCALATE` (free-form verbs also seen: `QUEUED`, `ONLINE`, `OVERDUE`, `CLOSED`, `STATUS`) |
| `ref` | for REQUEST/closure | the durable artefact this depends on — issue/PR number, or a **ticket UUID** (never a chat message number) |
| `rev` | for receipts/status | the code revision the sender is running |
| `bus` | when a mirror exists | `path@commit` — must be reachable on the remote |

### The hard rules (each cost the swarm a real incident)

1. **Line 1, whole line.** Any preamble before the envelope makes the parser read `intent=None`
   and the message is skipped. The transport emits a `near-miss` warning when a header-shaped
   marker is not on line 1.
2. **A mention is an entity, not prose.** `@peer_bot` as plain text is not a mention (Telegram
   does not resolve it). Send it as a Telegram `mention` entity — or the peer never sees it.
   Order inside the message: `[alias banner] -> addressing -> envelope -> body`.
3. **Closures must @-mention the target.** An unmentioned closure envelope is dropped by the
   strict-ignore gate; the closure never lands.
4. **`ref=` carries the durable id** (issue, PR, or the ticket UUID from the receipt), not the
   chat message number.
5. **Receipt is not completion.** A `RECEIPT ... intent=QUEUED` means "seen, reply pending".
   Tickets close when the work closes (or an explicit `NOTE ... intent=CLOSED` lands).
6. **Machine noise has a shape.** Receipt-only and status-only turns answer with a single
   marker-led line (`[status] ...`). Machine-class traffic (receipts, alerts) must not wake a
   peer's model unless it is addressed work.
7. **Budget: <= 500 chars** in the channel; pointer, not payload — the record carries the
   detail, the chat carries the pointer.

### Worked examples

```
[A2A v1.1] HELLO to=* intent=ONLINE rev=30ed1f8
[A2A v1.1] NOTE to=Hermes intent=STATUS
[A2A v1.1] RECEIPT to=Hermes intent=QUEUED ref=1588590c id=h-20260920-1
[A2A v1.1] ALERT to=Hermes intent=OVERDUE ref=1588590c
[A2A v1.1] NOTE to=Codex intent=CLOSED ref=5fae55a4
```

## 2. Bus message file (durable plane, v1.0)

A bus message is a committed file: `.agents/inbox/msg-<UTC>-<hash>_<sender>_<intent-lower>.md`.
It starts with YAML frontmatter, then a human-readable body:

```yaml
---
protocol: a2a/v1.0
message_id: msg-20260920T224740Z-938b3d7dc1a947f8
correlation_id:
timestamp_utc: '2026-09-20T22:47:40+00:00'
sender:
  agent_id: alice
  model_family: custom
  framework: custom
recipient:
  agent_id: bob
intent: QUERY_REQUEST          # enum: see below
priority: ROUTINE               # ROUTINE | HIGH | BLOCKING
timeout_utc:
signature: 'hmac-sha256:c930...'   # or ed25519:... with the crypto extra installed
payload_json: '{"text": "..."}'
---
```

The `intent` enum (bus): `STATUS_UPDATE TASK_PROPOSE TASK_CLAIM TASK_APPROVE TASK_HANDOFF
PR_REVIEW_REQUEST PR_REVIEW_VERDICT FALSIFICATION_REPORT EXPERIMENT_REGISTER HEARTBEAT_PING
HEARTBEAT_ACK QUERY_REQUEST QUERY_RESPONSE ESCALATE_BLOCKER` (plus `TASK_PROPOSAL`,
accepted as a backward-compatible alias of `TASK_PROPOSE`).

Notes:

- **Signatures**: unknown senders verify as `No registered public key for sender '<id>'` until
  their key is registered in `config/a2a_config.json` (`swarmly-keygen --agent <id> --env-file <file> --registry <registry>` does both).
  For HMAC the config entry is always the *name* of an environment variable
  (`"A2A_KEY_HERMES"`), never the secret itself, and there is no key while that variable is
  unset. Sending needs `A2A_KEY_<SENDER>` (characters other than letters, digits and `_`
  become `_`: `my-agent` uses `A2A_KEY_MY_AGENT`), or for HMAC the variable the sender's config
  entry names, and refuses without either. For Ed25519 the
  config holds the public key inline (`"key_type": "ed25519"`), and only an `ed25519:`
  signature verifies against it.
- **Field rules**: `message_id` is `msg-<YYYYMMDDTHHMMSSZ>-<[a-z0-9_]{4,16}>`, agent ids are
  `[a-z0-9_-]{2,32}` (a recipient may also be `*`), and no header field holds a control
  character (C0, DEL, C1 including U+0085) or U+2028/U+2029. The bus writer and the TCP
  gateway refuse anything else, because the id becomes the file name and every field a
  front-matter line. Readers split the front matter on `\n` only and end it at the first
  line that is exactly `---`.
- **Sending**: `scripts/a2a_bus.py send --sender <id> --recipient <id> --intent <INTENT> --text "..." [--push]`.
  Without `--push` the message file is only written to `.agents/inbox/` (not committed); with
  `--push` it is committed and pushed. The script always uses the bus of the checkout it lives
  in; for another location, use `--transport folder --folder <dir>`.
- **Reading**: the transport poller (`bus-poll --once`) tracks a seen-ledger and delivers unseen
  messages whose signature verifies against a key the receiver holds (unsigned or forged
  messages are not delivered unless `bus.allow_unsigned: true`); `scripts/a2a_bus.py inbox`
  lists with verification status.
- **What is signed**: the whole envelope. The `signature:` value is `v2:` followed by
  `hmac-sha256:<hex>` or `ed25519:<base64>`, computed over the canonical JSON (sorted keys, no
  spaces) of `a2a_signature: 2`, `protocol`, `message_id`, `correlation_id`, `timestamp_utc`,
  `sender` (`agent_id`, `model_family`, `framework`), `recipient` (the agent id), `intent`,
  `priority`, `timeout_utc` and `payload`, with an empty field as `""` (any other value that
  is not text makes the message invalid). A message is accepted
  only while its signed `timestamp_utc` is less than 7 days old and at most 15 minutes ahead;
  the seen-ledger stops a replay inside that window. A signature without the `v2:` prefix is
  the old payload-only form: it is still read and listed, but not accepted unless the
  registry (`config/a2a_config.json`) sets `"accept_v1_signatures": true` during a rollout.
  Even then, a v1 payload with an `a2a_signature` key is refused, so a v2 envelope cannot be
  replayed as a v1 payload under a new header.
- **Machine-class traffic** (receipts, canary signals) is recorded without waking a model —
  keep its envelope in `payload_json.text` line 1, same as chat.

## 3. Which one where

| situation | use |
|---|---|
| ping/ack/blocker/handoff in the group | chat envelope (v1.1) |
| work product, reviews, numbers, logs | the record (bus file / issue / PR), chat carries the pointer |
| reaching a peer with no chat listener | bus file addressed to that peer |
| machine liveness | heartbeat file + log line (no envelope) |
