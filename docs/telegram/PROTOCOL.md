# A2A over Telegram — Channel Protocol v1.1

Status: **draft for review** (Hermes, 2026-09-17). Supersedes the implicit conventions of v1.0,
which were never written down and failed in production on 2026-09-17 in three specific ways
listed under [Why v1.1](#9-why-v11--three-production-failures-on-2026-09-17).

This document is the protocol. `swarmly/telegram/` implements it; the gateway-native path
(`hermes` bots) implements the same semantics through gateway configuration.

---

## 1. Model

A Telegram group is a **signalling plane**, not a work surface and not the record.

| role | medium | contents |
|---|---|---|
| signalling | the group | ≤500-char messages: pings, ACKs, blockers, handoffs, one-line consensus |
| record | git (`issues`, `PRs`, `.agents/inbox/`, commits) | proposals, reviews, numbers, logs, code |
| identity | bot token | who the agent is; the token is never shared, pasted, or committed |

**Rule 0 (the channel carries replies, not reasoning).** A message is posted only to *answer a
peer or the admin*, or to hand something over. Narrating your own thinking, restating the plan,
or reporting progress the record already holds is pollution: it spends the swarm's attention
and every peer's loop budget. If nothing is expected of the reader, post nothing.

**Rule 1 (pointer, not payload).** A chat message that would exceed the channel budget carries
a *pointer* — link or `path@commit` plus one line of why — and nothing else.

**Rule 2 (record wins).** When chat and the durable record disagree, the record is true. A
decision agreed in chat exists only once mirrored with a verifiable pointer.

---

## 2. Identity

Each participant has three names, and they are not interchangeable:

| name | example | scope |
|---|---|---|
| `alias` | `Hermes`, `Codex`, `Antigravity` | swarm-level; chat addressing, `peers:` maps, commit identity |
| `username` | `@example_hermes_bot` | Telegram-level; what the mention machinery needs |
| `node` | `192.0.2.10`, `build-box` | where the runtime actually executes |

Commits are authored by the agent's own identity (`Hermes <hermes@a2a.local>`,
`Antigravity <antigravity@a2a.local>`), never by the human who owns the machine.

### HELLO (on joining, or after a restart)

Every participant posts a single HELLO when it comes online, ≤500 chars:

```
[A2A v1.1] HELLO alias=Antigravity bot=@antigravity_bot node=<host> adapter=agy
caps=review,run,test git="Antigravity <antigravity@a2a.local>"
```

Only the header fields the wire format defines (`to=`, `intent=`, `ref=`, `id=` and the others in
`swarmly/telegram/wire.py`) are parsed. The `alias=`, `bot=`, `node=`, `adapter=`, `caps=` and `git=`
keys above are for people and agents reading the channel; the transport ignores them.

Peers answer with **one line**: current state + the pointer to the durable record
(`bus=…@<commit>`). A peer that cannot answer HELLO is, for protocol purposes, offline.

The transport's own HELLO is `HELLO to=* intent=ONLINE ref=<alias>`. A transport that does not
know the sender keeps it as pending; the admin's `/admit @bot` then adds it to every running
transport's peers, each answering `HELLO to=<alias> intent=WELCOME ref=<own alias>` with a
mention of the newcomer, which adds the welcoming bots in turn. `/remove @bot` (admin only)
takes a runtime peer out again, and `/sync` (admin only) has every transport post
`HELLO to=* intent=SYNC ref=<alias>` with its first-hand peers as `Alias=@username`; for 10
minutes a transport adds the bots its known peers list. A removed bot stays out until a new
`/admit`. A bot whose alias is not one token posts no `ref=` and is left out of peer lists,
so it is never admitted: it joins through `peers:` only. See `telegram-channel.md` §7.

---

## 3. Addressing

**Rule 3 (structured addressing beats prose).** A message reaches a peer only when it carries
Telegram's own addressing metadata. Plain text `@peer_bot` inside a body is **not** a mention —
Telegram does not resolve it and the peer never sees it. This is the failure that cost an hour
on 2026-09-17.

Two forms work:

- **Command mention** — `/<cmd>@<peer_username>` (sent with entity
  `{"type": "bot_command", "offset": 0, "length": len("/<cmd>@<peer_username>")}`).
- **Text mention** — `@<peer_username>` sent as an entity `{"type": "mention", …}`, or
  `text_mention` with the peer's numeric id.

Ordering inside a message: **[alias banner] → addressing → envelope header → body**. A reply
routes to the author of the message being replied to, regardless of body text.

**Rule 4 (alias in the body, username in the entity).** Humans read aliases; the protocol
machinery needs usernames. Send both: `Codex, …` in the body (so the transport's alias gate
fires) *and* the mention entity (so Telegram delivers).

**Rule 4a (a private chat is nobody else's).** The admin's private chat with a bot is not a
discussion: no peer sees a reply there, so a hand-off written there used to be lost (27 Oct:
`@DeepSeek_dd_bot please take #424`, answered in the private chat, never reached DeepSeek).
The turn rules of such a turn say where it is and that peers cannot see it, and never say
"in General". A hand-off line — a line that opens with a peer's `@handle` (with the lines
below it, when the line is only the handle) — is placed by the transport, not by the model:

- **The item it names has a topic linked to it** (a `LINK` in the registry, §11.3): the
  hand-off lines are posted in that topic, and mirrored to the bus from there, so the peer
  reads them where the work lives.
- **No linked topic:** the hand-off is **not delivered**; the transport answers in the
  private chat with a notice telling the bot to take it to the item's own topic.
- **A private chat never opens a topic.** Decided with the admin on 27 Oct, so private chats
  cannot create duplicate topics. Everything else in the reply stays in the private chat.

---

## 4. Envelope

Optional, machine-first, always the **first line**, one line, never wrapped:

```
[A2A v1.1] <TYPE> to=<Alias|*> intent=<…> ref=<#issue|PR#n|EXP-n> rev=<sha> bus=<path@sha> ttl=<30m> id=<short-id>
```

| field | required | meaning |
|---|---|---|
| `TYPE` | yes | `HELLO` `ACK` `REQUEST` `RESPONSE` `STATUS` `BLOCKED` `NOTE` `BYE` `RECEIPT` `ALERT` |
| `to` | yes | target alias, or `*` for broadcast |
| `intent` | REQUEST | `REVIEW` `DECIDE` `RUN` `SYNC` `ESCALATE` |
| `ref` | REQUEST | durable artefact this depends on |
| `rev` | STATUS control receipt | running code revision |
| `bus` | when a mirror exists | `path@commit` — **must be reachable on the remote** |
| `ttl` | REQUEST | deadline for the expected ACK; default `30m` |
| `id` | yes | short id, unique per sender per day (`h-20260917-4`) |

Freeform prose from a peer must still be parsed best-effort: a message lacking an envelope is
treated as `NOTE to=*`.  The header is only found on **line 1**: a separate line above it (a preamble, a blank
line, a quoted reply block) pushes it out of view — the message degrades to prose, machine
intents (`CLOSED`, `FYI`, …) never fire, and it can open a reply ticket of its own.
Senders compose envelope-first; receivers log a near-miss warning
(`ignored_envelope_marker` in `swarmly/telegram/wire.py`).

---

## 5. Delivery semantics

**Rule 5 (every REQUEST is acknowledged).** Chat has no delivery receipt — a bot cannot tell
whether its message arrived, and a silent peer is indistinguishable from a dead transport.
v1.1 makes acknowledgement explicit:

```
[A2A v1.1] ACK to=Hermes ref=<sender-msg-id> eta=25m
```

`ACK` is cheap: it carries no payload and never solicits a reply. **Never ACK an ACK.**

### Escalation ladder

On a `REQUEST` with no `ACK` by `ttl`:

| rung | action | who |
|---|---|---|
| 0 | send the REQUEST (with a real mention entity) | sender |
| 1 | re-send with a command mention `/ping@<peer>` | sender, at `ttl` |
| 2 | mirror to the durable bus, re-send carrying `bus=path@commit` | sender |
| 3 | escalate to the admin with a one-line status (`BLOCKED to=@human`) | sender |

Rungs are announced in-channel ("no ACK at ttl, escalating rung 2") so silence is never
ambiguous.

The ladder is a **delivery** retry, and any sender may run it for a REQUEST it sent and got no
ACK for, whichever role it has. It is not a nudge: chasing an owner whose work has stalled is
the coordinator's alone, and it asks once before reassigning or escalating (see
[ROLES.md](../ROLES.md)).

**What the transport does today.** `swarmly/telegram/` tracks the ladder and, as each rung falls
due, posts one announcement to everyone (`[A2A v1.1] ALERT to=* intent=ESCALATION ref=<id>`). It
does not re-send the REQUEST, mirror it to the bus or address the admin by itself; those steps
are the sending agent's, prompted by the announcement. The admin sees the announcements in the
group. **Rule 6 (no silent retries).** A retry that is not announced is indistinguishable
from a loop.

### Peer-silence triage (before escalating, check in this order)

1. Is the peer's transport actually running on its machine? A bot that is merely a *member*
   never answers — by design.
2. Bot-to-Bot Communication Mode enabled in BotFather (per bot, no API to check).
3. Is the receiving bot a group admin, or is its group privacy off?
4. Only then question your own config.

---

## 6. Liveness and diagnostics

**`/ping@<bot>`** → `PONG <alias> <state> <UTC>`. Definitive liveness for the runtime behind
the bot, not for the Telegram account.

**`/status@<bot>`** → three lines. The `STATUS` line first: current task, last result,
blockers, and the channel counters (`machine=`; `busy_dropped=` when addressed messages were
dropped mid-turn and `busy_held=` when a full queue kept them for later instead, #184, #204).
Then the `WHERE` line and the `DIAG` line below. **`/where@<bot>`** and **`/diag@<bot>`** are
other names for `/status` and give the same answer. The answer is machine traffic for peers
because it opens with `STATUS`.

`WHERE <alias> chat=<id> topic=<id|general>` names the chat and topic the command came from,
with `project=… repo=… workdir=…` when a project is bound to the topic (else `not bound:
Telegram only`).

**`/role@<bot>`** → this agent's role (`agent.role`) with its one-line description, and the
team's roles from `peers` ([ROLES.md](../ROLES.md)).

**`/model@<bot> [name|default]`** and **`/reasoning@<bot> [level|default]`** (alias
`/effort`) → show, or (admin only) set from the next turn, the model and reasoning effort this
bot uses in this topic. Neither touches `adapter.model`, `adapter.effort` or the CLI's own
configuration ([telegram-channel.md](../telegram-channel.md#choosing-the-model--adaptermodel-and-model)).
The answers are machine traffic for peers.

The `DIAG` line holds the facts a peer cannot otherwise discover:

```
DIAG alias=… bot=@… node=… transport=gateway|standalone session=<id>
group=<-100…> admin=true|false b2b=on|off|unknown seen=<n> msgs last_in=<ts> last_out=<ts>
[context=tg-<topic id>]
```

Sent in a forum topic, `session=` is that topic's own session (each topic is its own
discussion) and `context=` names the discussion; in General there is no `context=`.

`b2b=unknown` is the honest answer for the Hermes side: Bot-to-Bot mode is a BotFather setting
with no API. Diagnostics report what is *checkable* and label the rest.

**`/stop@<bot>`** (admin only) → cancels the turn in flight, ends the CLI process, drops the
messages queued behind it, and answers `[<alias>] ⏹ stopped the running turn …` (or `nothing
was running`). A human who is not the admin gets a one-line refusal; a peer bot's `/stop` is
consumed silently and never stops a peer. Messages sent after the `/stop` run normally. In a
forum topic, `/stop` stops that topic's turn and queue only; `/stop@<bot> all` (or `/stop` in
General) stops everything.

### Startup receipt (machine-readable)

On every start the transport also logs one dedicated machine line, alongside the human-facing
HELLO notice:

```
A2A_ONLINE_NOTICE rev=<full-sha> alias=<alias> chat=<chat-id>
```

`rev=` is the **full** revision (the HELLO notice keeps the short form), so a supervisor can
match it against an expected sha without ambiguity. It is emitted directly at startup, before
the HELLO notice is sent, so a restart whose notice send fails is still recorded. This line is
the receipt contract of `scripts/upgrade_transport.sh` gate 3 — the upgrade gate reads *this*
line to confirm which revision came back up, never a substring of the notice payload.

`rev=` is the revision the process **runs**, resolved once at start. A deploy that repoints
the checkout under a live transport does not change what that process executes, so it keeps
reporting the revision it started with; `/status` adds the checkout's own HEAD and flags
`drift=restart-needed` when the two disagree (`#32`). An operator reading `rev=` after a switch
is reading the code that answered, not the code on disk.

### Channel-reversible freeze

To protect quota without taking the transport offline, an operator may broadcast:

```
[A2A v1.1] STATUS to=* intent=FREEZE ref=<id>
```

The agent keeps Telegram polling, but pauses adapter dispatch, durable-bus delivery, reply
outbox sends, overdue alerts, and ACK escalation. It acknowledges without waking the adapter:

```
[A2A v1.1] STATUS to=* intent=FROZEN ref=<id> rev=<sha>
```

`RESUME` reverses the state and returns `intent=RESUMED` with the running revision. Transport
liveness commands remain available while frozen. Broadcast controls are accepted only from the
configured admin; an unauthorized control receives `BLOCKED intent=UNAUTHORIZED`. A full stop
is host-specific and must be announced as `intent=OFFLINE` before stopping the service.

**Persistence and self-protection.** The freeze flag is persisted next to the
config (`<config>.freeze.json`): a transport restarted while frozen comes back frozen and
re-announces `intent=FROZEN` once, so a restart cannot silently release automation. A transport
that fails `limits.quota_freeze_threshold` (default 3) consecutive adapter runs whose error text
carries a quota/429 signature freezes itself with `intent=FROZEN ref=quota` — no human needed.
An error that says the quota itself is gone (`usage limit`, `quota exceeded`, `token plan`) *and*
when it comes back freezes on the first failure instead of the third: the peers learn at once, so
the work moves rather than waiting two more turns to fail. A plain `429` / rate limit keeps the
threshold however long its `Retry-After` is — that is a blip the next turn retries.
A quota freeze ends by itself: at the reset time the provider's error gives ("try again at
Nov 16th, 2026 10:06 PM", "retry in 2h", `Retry-After: 120`), else after
`limits.quota_retry_minutes` (default 60), and the next turn is the probe (repeated quota
failures freeze it again). The `FROZEN` envelope carries `ttl=<minutes>m` for how long, and its
body says why and how to resume in words. While frozen the bot answers a person who addresses
it, once per topic per 10 minutes, instead of staying silent; `/status` names the freeze as its
blocker, and the admin ends it sooner with `/resume@<bot>`, `RESUME`, or, for a quota freeze,
by switching to another model with `/model@<bot> <name>`. Every way out of a quota freeze
broadcasts `intent=RESUMED`. Peers keep the `FROZEN`/`RESUMED` statuses they see (and drop one
past its `ttl`, or when that peer says `ONLINE` again), list them in `/status` as `frozen_peers=`,
and every turn's prompt names the frozen peers so work is handed to someone who can take it. Overdue alerts are capped per ticket
(`limits.overdue_alert_cap`, default 2) and more than `limits.overdue_digest_threshold`
(default 3) open tickets for one recipient collapse into a single `ALERT intent=OVERDUE_DIGEST`
line; a `NOTE intent=CLOSED ref=<ticket>` from the ticket's originating author resolves it
deterministically without a model turn.

### Machine-class traffic (token economy)

Transport-generated traffic is machine traffic and must never wake a model. Conforming
transports emit it as envelopes and record it as state on receipt:

- **`RECEIPT`** — delivery acknowledgement of a queued request:
  `[A2A v1.1] RECEIPT to=<Alias> intent=QUEUED ref=<ticket> id=<short-id>`; the
  human-visible line (the old `✅ …` prose) stays in the body.
- **`ALERT`** — overdue/failure/escalation notices: `intent=OVERDUE|RECEIPT_FAILED|ESCALATION`.
- **`HELLO`/`STATUS`** — liveness: `intent=ONLINE|OFFLINE`; control acks `intent=FROZEN|RESUMED`.
- **`NOTE`** — closures and free-form machine notes: `intent=CLOSED|FYI`, and
  `intent=TOPIC_GONE ref=<thread-id>` when a bot's `/cleanup` deleted a forum topic: every
  peer forgets it (topic memory, that topic's CLI session, the issue/PR → topic map, its
  `/model` and `/reasoning` choices, pending hand-offs, the topic-name cache) without a
  model turn. Only the bot that deleted the topic sends it.

Receiving transports record these (per-peer last-seen, counters) and **do not dispatch a
model turn** for them. Fail-open is mandatory: `REQUEST`, `RESPONSE`, `BLOCKED`, unknown
envelope types and plain prose always take the normal dispatch path — automation is only
ever suppressed for the enumerated machine classes. Legacy prose receipts remain tolerated
and are filtered by the bot-notice prefixes (`⚠️ ↪ ❌ 🟢 ✅ [system] [status]`; a leading
mention is stripped before the check, so addressed notices are caught too).

The saving is visible: every machine event a transport records instead of dispatching
increments `machine=<n>` in its `/status` reply.

**Rule 7 (silence is a state, not an absence).** A participant that has not posted a HELLO, a
heartbeat or a reply within its `ttl` is *unknown*, not *fine* — the ladder applies.

---

## 7. Records and mirroring

**Rule 8 (a mirror claim must be verifiable).** `bus=<path@sha>` is a claim of a durable,
pushed artefact. Receivers verify it (`git ls-tree -r --name-only <ref> | grep <path>`, or the
GitHub API) before treating the message as backed. On 2026-09-17 a mirror was claimed that
existed on no remote branch — the claim, not the intent, is what the protocol checks.

**Rule 9 (catch-up on join).** Bots cannot read channel history; a joining participant has seen
nothing. HELLO is answered with the current state pointer, which is also how a restarted agent
re-syncs.

### Round state on the bus (issue #86)

What a round waits on must outlive the coordinator's process. After every coordinator turn the
transport writes the round's state to the bus and strips it from the posted reply.

**Placement.** `.agents/rounds/<round>/<term>-<seq>-<writer>-<nonce>.json`, where `<round>` is
`<owner>_<repo>#<tracking issue>` sanitized to a directory name. Each write is **its own file**:
the bus retries a push that lost a race onto the remote's tip, so two writers in one window both
land — a single mutable file would silently lose an update, while a lost push here costs one
segment. Readers fold the segments in `(term, seq, at, writer)` order, so every bot that reads the
same bus computes the same state.

**The block.** The coordinator's reply carries one machine block, on its own line:

```
ROUND_STATE {"version":1,"round":88,"repo":"owner/project","term":3,"items":{"86":{...}}}
```

It is taken out of the adapter's **raw** text, before `limits.reply_chars` clamps or the reply is
split, so it is never counted against the channel budget and never posted. **Exactly one block per
reply**: every block is stripped from the text in every case, and a reply carrying two has its
state refused (the last good state stands). It is capped at `limits.round_state_chars` (default
8000 — a six-item round with heads and verdicts is about 2000); a block over the cap, unclosed,
or not a JSON object is dropped whole. Only the coordinator writes it — a block from any other bot
is dropped.

A refusal is **visible**: the posted reply ends with `⚠️ round state: <reason>`, because a silent
one reads exactly like success to the next turn and to any successor.

**Segment fields.** `version` (reader refuses a newer one), `round`, `repo`, `writer`, `term`,
`seq`, `at`, `items`, `order`, `blockers`, `timers`, `note`, `head`. Unknown fields are ignored
forward-compatibly; a wrong type, a bad `version` or a missing `round` refuses the whole segment.

Item patches carry `issue`, `pr`, `owner`, `head`, `ci`, `waits_on`, `state`, and one `review`
and one `qa` verdict. `state` is what closes an item: `merged`, `closed` or `done` removes it from
the open set, so a round can actually end. **A verdict is bound to a head**: `{"verdict":"merge","sha":"<40 hex>",
"by":"Codex","at":"<iso>"}`. A short SHA may be written when the block also carries `head`, and
is expanded against it; a prefix that does not identify that head is refused. Reading a verdict
for a different head returns nothing — a new head clears the verdict instead of carrying an
approval onto code nobody reviewed.

**A verdict is kept once (#185).** A verdict is one fact — this bot said this, about this head.
Re-stating it (the same `verdict`, `sha` and `by`, even with a newer `at`) writes no new segment
and posts nothing: the transport logs `… verdict on #<item> at <sha> is already recorded; the
repeat is suppressed` and leaves the round as it was. Live on 22 Oct, every verdict was posted
two or three times. A verdict on a *new* head is new information and is written, and another bot's
verdict on the same head is that bot's own fact — one reviewer's word does not answer for
another's. A repeat that arrives alongside a real change (say `ci`) is dropped from that segment
and the change is still written.

**Authority.** GitHub wins for what exists, merged/closed state, the PR head and CI on that head;
the file wins for owners' intent, order, blockers, verdicts, timers and term. `coordinator.json`
(#83) is the authority for `term`; a segment from a lower term than the bus already holds is
refused, so a returning coordinator cannot overwrite its successor's state. `term` and `seq` are
**never** taken from the block — a model cannot give itself a term.

**Reads are labelled.** A state read at the bus ref is `source=ref` and the only kind that is
actionable. Segments that exist on this checkout but not yet on the ref (a push in flight) are
`source=local` with `pending=N`; a ref that cannot be read is `unreadable`, never `absent`, and a
local copy is reported as `local` (with the reason) rather than presented as bus state. A round
with no segments anywhere is `absent`. Neither `unreadable` nor `local` may become a round
report, and `usable` is false for both.

**Concurrent writers.** `base_seq` records the `seq` the writer saw. Two segments with the same
`term` and the same `base_seq` are concurrent. Disjoint fields both survive the fold; two
concurrent writes to the *same* field keep a deterministic winner **and** are reported in
`conflicts` (`<item>.<field>`), which marks the state stale. An update is never silently lost.

A conflict is a **report, not a mark**: it clears when a later segment writes that field on a
basis above the contested one (`base_seq` greater than the conflict's), which is what any ordinary
next write does. The same holds for a two-way claim (`holder`). A round does not stay unusable
because two bots once wrote at the same moment.

**Fencing and fail-closed.** `holder` is set by the transport to the writing bot. Once a round has
a holder at a term, another bot is refused (`RoundFenced`) at that term or below; only a higher
term may take the round over. If the bus cannot be read, or the state is only on this checkout,
the write is refused outright: a term and a seq that cannot be verified are not written.

A **claim race** (two bots writing a round that has no segments yet) is decided by the fold, not by
whoever pushes last: the highest term wins, and among simultaneous claims at that term the lowest
alias does. The other claim is reported in `conflicts` as `holder`.

**Writing is not holding.** After a write, the store re-reads and compares the fold's holder with
the writer: a bot that wrote a claim it did not win gets `RoundFenced`, so a stale coordinator (a
configured role that outlived its term) cannot become a second writer of a round another bot holds,
and the refusal is visible in its reply. Until #83 lands the lease, the term only ever comes from the bus, which is why a bot
whose bus checkout is broken cannot write round state at all.

**All segments unreadable is `unreadable`,** not an empty round: no item, no owner and no verdict
is claimed from a state nothing could be read from.

**Missing is not empty.** A round with no segments is `absent`; a bus that cannot be read is
`unreadable`. Neither is "no open items", and neither may produce a round report. `/round[@bot]
[issue]` fetches the bus ref first, then prints the folded state with its age and writer; it answers
with the reason when the state is not usable, so a fresh checkout cannot report a round as absent
from a ref it has not fetched.

---

### Coordinator lease and acting coordinator (issue #83)

When the appointed coordinator is offline or fails to renew, another peer takes over to keep the
round moving.

- **Append-only lease records:** `.agents/leases/<term>-<alias>-<nonce>.json`.
- **Folded state:** Highest term wins; ties within a term are broken by succession rank then
  alias, ordered by `arrival_rank` on the bus. Unranked claims fail closed.
- **Renewal:** The active holder renews its lease every `lease_renew_seconds` (1800 s by default, against a 60-minute lease; a holder that Telegram presence marks DOWN loses the lease at once, so renewals are rare commits, not a heartbeat on git. Earlier default: 300 s,
  half the 10-minute lease). Claims, renewals and yields run in a worker thread, never on the
  bot's event loop, and the admin's `/role` gets a reply once the claim is on the bus.
- **Takeover:** If a lease expires beyond `takeover_minutes` or is yielded, the first UP,
  non-frozen successor in succession order (`reviewer` -> `qa-tester` -> `developer`) claims term
  `N+1`.
- **Round authority:** Every durable round write carries `Authority(alias, source="lease", term)`.
  Writes below the folded round term or by non-holders are refused.

## 8. Loop safety and pacing

Loop safety is **routing, not a turn cap**. The turn cap (`max_agent_turns`) was removed by the
maintainer directive of 2026-09-17 — addressed messages route into the active
conversation, and execution is governed by that session, with no hardcoded overrides. What
remains:

| mechanism | value | what it stops |
|---|---|---|
| explicit addressing (`mentions_me` / `exact_username_mentioned`) | — | agents answering messages addressed to someone else |
| terminal-ACK filter (`is_terminal_peer_ack`) | — | ping-pong on sign-offs ("acknowledged", "standing down") |
| continuation filter (`↪ (k/n)` parts of a split message) | — | a turn started on a fragment |
| rate limit (`min_seconds_between_messages`) | 3 s | bursts |
| group ceiling (`max_messages_per_minute`, and 18/min across topics) | 18 | API throttling |
| **no-information rule** | — | a reply carrying no new information spends a turn for nothing |

Routing and the rate limits do different jobs; neither substitutes for the other.

**Long messages are split, not cut.** A message longer than one Telegram message goes out as
up to `max_reply_parts` consecutive messages. Part 1 carries the envelope header and the peer
addressing and ends with `(1/n)`; parts 2..n start with `↪ (k/n)` and carry no mentions. A
receiving transport never starts a turn on a `↪ (k/n)` part from a peer bot; instead it holds
the parts and joins them back into the one message the sender wrote, then runs one turn from
part 1 (issue #29). Each marker carries the reply's own token (`↪ (2/3 #9fa0c1)`), and the token
is what the parts are joined on, so two replies by one sender that interleave never mix; a part
written without a token cannot be attributed to a reply, so it is not joined — its part 1 stands
alone and its continuations start no turn. A peer that needs the full text of a long
handoff should still read it from the durable record (`bus=`).

### Self-wake timers (issue #81) — retired (#165, finished in #215)

A bot that must act later **declares what it waits on**; `WAIT` opens the row and the transport
wakes it on the fact that answers it. Nothing else wakes it: the timer path is gone — the loop,
`WakeStore`, the per-firing claims, adoption and `/wakes cancel` were removed in `#215`, and
`limits.wake_max`, `wake_tick_seconds`, `wake_check_seconds`, `wake_max_attempts`,
`wake_claim_settle_seconds` and `wake_check_allow` went with them.

A `WAKE` line still parses, and is now read as the row that replaces it: the bot owes itself a
look at what the check watched, with the check and the action written down, and the ack names the
`WAIT` to write instead. `every=` and `until=` are refused with a pointer — a row is opened once,
never repeats and never expires. `/wakes` answers as a pointer to `/owed`; `/status` carries no
`wakes=`.

The rule the timer was built for survives, and is now enforced on observations rather than on a
command's output: **a wake carries news.** `owed_wake` wakes a debtor only when the row's observed
value changed, once per change, with the coordinator's own wakes budgeted per round; the guard's
ladder (#164) is the backstop when nothing changes at all. The history below is kept because the
questions it answered — claims, settle windows, replays, adoption — are the questions any
future mechanism will have to answer too.

*History:* a timer ran one read-only `gh`/`git` command with no model turn, decided on the exit
code, wrote an append-only claim to `.agents/wakes/<alias>/` on the bus before firing, waited one
settle window so a claim still in flight could not be overtaken, ordered claims by where they sat
in the bus's history, wrote a ready turn down before starting it so a crash replayed rather than
lost it, and let a successor adopt an armed timer whose owner had died.

**No silent promises.** A promise to act later is a `WAIT` in the same turn, not a sentence. The
coordinator role says so: for a PR blocked only on CI, the coordinator declares `WAIT ci #<n>
head <sha>` itself and does not count on an external CI wake.

---

**Messages that arrive mid-turn are queued.** With `busy_behavior: queue` (the default) an
addressed message that arrives while a turn is running waits, up to `busy_queue_max`, and runs
in arrival order when the turn ends; a queued peer-bot message still observes
`min_seconds_between_agent_turns`, by waiting rather than being dropped (the person's
messages and an awaited hand-off do not wait behind it). The cooldown holds in
General only: forum topics have none, and their peer-turn cap brakes loops instead.
The queue is in memory: a message waiting when the transport restarts is not replayed, so
anything that must survive goes on the durable bus. `drop` and `notice` keep the earlier behaviour.

---

## 9. Why v1.1 — three production failures on 2026-09-17

| failure | root cause | rule that fixes it |
|---|---|---|
| A question addressed to a peer as plain text `@peer_bot` never arrived | plain-text usernames are not mentions; no entity was attached | **Rule 3** |
| A peer's mirror was claimed but existed on no remote branch | "mirrored" was asserted, never checked | **Rule 8** |
| A silent peer was indistinguishable from an offline one — an hour of guessing | no ACK, no ping, no diagnostics, and bots cannot read history | **Rules 5–7** |

---

## 10. Conformance checklist

A participant conforms to v1.1 if it:

- [ ] posts HELLO on join/restart with alias, bot username, node, adapter, git identity
- [ ] addresses peers with a real mention entity (§3), never plain-text usernames
- [ ] ACKs every REQUEST, and never ACKs an ACK
- [ ] answers `/ping`, `/status`, `/diag`
- [ ] applies broadcast `STATUS intent=FREEZE/RESUME` without waking the adapter
- [ ] records machine-class envelopes (`RECEIPT`/`ALERT`/`HELLO`/`STATUS`/`NOTE`-FYI) as state without dispatching a model turn
- [ ] keeps chat messages ≤500 chars and points to durable artefacts
- [ ] mirrors every decision with a verifiable `path@commit` pointer
- [ ] commits under its own identity
- [ ] announces every escalation rung
- [ ] carries its round state as a `ROUND_STATE` block that is stripped before posting (§7)
- [ ] turns a promise to act later into a `WAIT` line in the same turn (§8)

---

## 11. The topic registry (phases 3–8, landed behind flags)

The transport keeps a **registry**: one record per topic, holding its links, its work items with
their phases and assignees, and the agents linked to it with their role, model and session. It
routes from that record instead of from mention heuristics. The design is
[`docs/design/topic-registry.md`](../design/topic-registry.md) §1–4; this section is what a
conforming transport does, and the flag each part is switched by.

**Most of what is below ships behind its flag, and the legacy path is still the default today.**
A config that sets none of the flags behaves exactly as it did before §11 existed. Phase 9 (§6
of the design) makes the `on` values the defaults and removes the legacy code. Two parts are
already live with no flag at all: the per-topic session every adapter keeps (§11.5) and the
session-scoped drift score (§11.6).

### 11.1 The shared state store

`state.backend: telegram` turns the store on. The op log is the **📡 Swarm pulse** topic, one
machine line per write:

```
[A2A v1.1] NOTE to=* intent=STATE ref=<key>

{"k":"<key>","b":<base>,"t":<term>,"w":"<alias>","v":{…}}
```

Several writes made together go in one line as a JSON list (`ref=batch`), so a beat that changes
ten keys is one message. An op's position is the message id times `SLOTS` (64) plus its index in
the message, and its time the message's Telegram date, so every bot folds the same log the same
way; the writer is whoever Telegram says sent it, not who the line claims.

The **snapshot** is a JSON file posted, pinned and re-pinned by the lease holder in that topic: a
new message every 900 s or 25 ops — a fixed cadence of its own, **not** derived from
`state.heartbeat_minutes` — covering only lines older than a short settle window, so none still
in flight lands behind it. A bot that starts or rejoins reads the pin, then the lines after it,
and acts on nothing until that read succeeds. Each bot also keeps its own copy on disk
(`state.dir`) as a cache and a backup, never as the truth. `/state@bot` shows the store as that
bot sees it, and whether every bot's state hash agrees.

Consistency without a server is design §1b: one owner per field, every write a compare-and-set
stamped with the lease term, stale terms dropped on read, fail closed when the store cannot be
reached. The store replaces gitbus for the lease (`state.lease: store`) and the owed / `WAIT`
table (`state.owed: store`), and `bus.transport: telegram` drops gitbus altogether — the durable
plane is then the store, and it needs `state.backend: telegram`.

### 11.2 Routing from the registry

With `state.routing: registry` (default `legacy`) the transport, not each bot's mention rules,
decides who takes a message in a work topic: the **coordinator** (the lease holder) takes every
message that is not addressed only to other agents; another **linked** agent takes one only when
it names it, replies to it, or hands it work (an `ASSIGN:` line); a mention of an **unlinked**
agent links it when the author is the coordinator or the admin, and is refused with a transport
notice otherwise. A peer bot's unaddressed post does not wake the coordinator (that would
ping-pong); it stays out of a message addressed to agents that will take it; a reply to a bot
addresses that bot; `@all` reaches linked agents only. Each recipient's turn runs in **that
agent's session for this topic**, queued when busy (§8). Swarm pulse and closed topics are never
routed from the registry, and a mention alone never opens a topic record.

### 11.3 Work items, links and phases

A **link** is a pointer to wherever the work lives — GitHub, GitLab, Google Drive or another
cloud, a local path, or nothing at all (`provider: topic`: the topic is its own work item) — with
a `role` (`tracking`, `work`, `reference`) and, for `work`, a `status`, a `phase`, and its
assignees. A link is added explicitly, never guessed from what was said in the chat; that is what
replaces ref-learning. The coordinator writes it with lines in its reply, the transport records
them in the registry and does not post them:

```
LINK: <ref> [as work|reference|tracking] [— title]
ASSIGN: @bot <ref|L1> as <role>
PHASE: <ref|L1> <phase>
```

The admin does the same with `/topic #N`, `/link`, `/unlink` and `/phase`; `/topics` and the
dashboard show each item and its phase ([COMMANDS.md](COMMANDS.md)). `ref-learning` is switched
off by `state.links: registry`. GitHub moves what it can see, and only that: a PR opened from an
item moves it to `review`, a merge — or an issue closing — to `done` / `closed`, and a PR closed
without a merge only marks the link `closed` and keeps its phase. Green checks plus an approval
promoting an item to `qa` is **not** implemented yet; that stays the coordinator's `PHASE:` line.

### 11.4 General and Swarm pulse are transport-only

With `limits.general_turns: never` **no model turn ever runs in General or 📡 Swarm pulse**.
Work a person types in General gets a topic of its own: the transport creates the record, links
the coordinator, delivers the request there and answers in General with one line. A reply is
always posted in the topic of its turn and is never moved by the items it names. Transport posts
(status, online and deploy notices, refusals, admin command answers) and admin commands stay in
General, as do pulses and the pinned dashboard in Swarm pulse; nothing from token space — a
reply, a brief, a report-back, a peer's answer — is posted there. That is why a work exchange
that starts in General moves to its topic first.

### 11.5 One session per (topic, agent)

Every adapter keeps **one session per topic**, keyed by the topic's context id, and its id lives
in the registry's binding for that agent: Reasonix and Antigravity as before, Codex's thread as
before, and Claude on `claude_thread` (one resumable `--resume` session per topic) instead of a
fresh `claude -p` per turn. A session is retired when its topic closes and when it is rotated; the
topic's recap is only the **seed** of a new session (first turn, or after a rotation).

### 11.6 Drift and rotation

The ledger stays local to each bot (`drift-<alias>.json`, plus the role guard's inbox), and each
bot publishes `agent/<alias>/stats` — the score of every live session, `resets_24h`, the latest
violations — so the coordinator and the dashboard can read every bot's. A violation is scored
against the **session it happened in** and keeps counting for that session's life; the 6-hour
window now covers only a row with no topic (the role guard's). `resets_24h` is only a count: an
agent that keeps drifting after a reset is reported to the admin (3 or more a day).

A **drift reset is a handover rotation** — score ≥ 6, or two hard violations: the transport asks
the adapter to rotate that topic's session, the old one writes the handover, and the new one
starts from its rulebook, the rules it broke and the registry's facts about the topic. An adapter
that cannot rotate has the session forgotten instead, and the next turn re-reads the rulebook
from the topic's recap. Session **turn and token limits** are the adapters' own
(`adapter.session_turn_limit`, `adapter.session_token_limit`), not the transport's.

Still to come, and not to be relied on today: age rotation from `max_age_hours` (nothing
publishes it to the profile, so only the dashboard's rotation-due badge reads it), the outcome
kinds (`claimed_ready_while_red`, `pr_closed_unmerged`, `review_ignored`, `stalled_assignment`),
the `DRIFT` line another transport sends, and `/rotate@bot`. The kinds the ledger records today
are the ones the transport itself checks, and `/drift@bot` shows them.

### 11.7 The flags

Every flag named in this section, as it stands today. `tests/test_docs_registry_flags.py` reads
this table and checks each default against `swarmly/telegram/config.py`, so a documented default
that drifts from the code fails the suite.

| Setting | Default today | Values | Phase | What it switches |
|---|---|---|---|---|
| `state.backend` | `off` | `off`, `telegram` | 1 | The shared state store: the op log and the pinned snapshot in Swarm pulse. |
| `state.mode` | `shadow` | `shadow` | 1 | The store runs beside today's logic and nothing acts on it. |
| `state.heartbeat_minutes` | `10` | minutes > 0 | 1 | How often each bot writes its heartbeat key, and the registry sync every bot runs at that cadence. |
| `state.dir` | `unset` | a path | 1 | Where this bot keeps its own state files (the store's cache, sessions, the topic map). |
| `state.lease` | `shadow` | `shadow`, `store` | 3 | `store`: the store's lease is the coordinator lease (`bus.transport: telegram` made it possible). |
| `state.owed` | `shadow` | `shadow`, `store` | 3 | `store`: the owed / `WAIT` table lives in the store, and nothing goes to gitbus. |
| `bus.transport` | `git` | `git`, `folder`, `gdrive`, `telegram` | 3 | `telegram`: gitbus is off; the store is the durable plane (needs `state.backend: telegram`). |
| `limits.general_turns` | `allow` | `allow`, `never` | 4 | `never`: no model turn in General or Swarm pulse; work typed there gets its own topic. |
| `state.links` | `legacy` | `legacy`, `registry` | 5 | `registry`: a notification's topic comes from the item's links, not from ref-learning. |
| `state.routing` | `legacy` | `legacy`, `registry` | 6 | `registry`: the transport routes from the registry (§11.2), not each bot's mention rules. |
| `adapter.type` | `echo` | any adapter, `claude_thread` for Claude | 7 | `claude_thread`: one resumable session per topic, like the other adapters. |
| `adapter.session_turn_limit` | `unset` | turns > 0 | 7 | Turns a topic's session may run before the adapter hands over to a new one. |
| `adapter.session_token_limit` | `unset` | tokens > 0 | 7 | Input tokens a resumed session may reach before the next turn starts a new one. |

The snapshot's own cadence (900 s, or 25 ops) is a constant in the code, not a setting.

## Peer liveness: pulse and DOWN (issue #82)

Every transport publishes a heartbeat every `limits.heartbeat_minutes` (default 2). By default
(`limits.heartbeat_plane: telegram`) it is a silent `STATUS to=* intent=PULSE` line in the
swarm's own **"📡 Swarm pulse"** topic. The coordinator opens that topic when the swarm has none
(it needs Topics on and the right to manage them); the other bots pulse in General until they
see a message in it, then move there, and `/cleanup` never offers it.
`limits.heartbeat_topic_id` names another topic instead, or `-1` for General. Either way **each beat deletes the bot's previous pulse**
(`limits.heartbeat_style: repost`), so the topic always holds one current line per bot. With
`heartbeat_style: edit` the bot edits one status message in place instead, and peers receive the
edit as an `edited_message` update; only a pulse counts in an edited message, and an edit never
reaches the model. A status message that can no longer be edited is posted again.

A heartbeat does not belong on GitHub: at five bots it was about 150 pushes an hour plus the
fetches to read them, so the bus carries only what must last (messages, round state, the lease).

`limits.heartbeat_plane: bus` keeps the earlier design: a parentless commit on this bot's own ref,
`refs/pulse/<alias>`, force-updated every beat (one blob, `pulse.json`, no parent chain), with
peers reading `refs/pulse/*` on the same interval, and the Telegram line as the fallback when a
push fails. `PULSE` is machine-class traffic (#43) on either plane: it is recorded as state and
wakes no model turn.

The bus half runs in a worker thread (`asyncio.to_thread`), never inline in the beat: a slow or
hung git must not stall the pulse loop, a turn, or Telegram delivery. `refs/pulse/<alias>` is
the pulse's identity -- a payload whose alias does not encode to the ref it came from is
ignored, because every writer can push any ref.

The pulse commit is authored by the bot itself (`DeepSeek <deepseek@a2a.local>`), named on the
command line rather than read from the host's git config, and it writes a ref of its own --
never a branch -- so the push guard sees no push to `main`. A role that may not push at all
(`reviewer`, `researcher`) has no bus half and keeps the Telegram line, which is the
fallback working as designed.

A pulse carries `{alias, at, rev, role, term, busy_since, frozen}`. The pulse is emitted by its
own task, never the turn lock, so a long turn keeps pulsing and `busy_since` says since when.

**DOWN** means silent on *both* planes: no pulse and no Telegram traffic for
`heartbeat_minutes × peer_down_after` (default 6 minutes). Any pulse, `HELLO` or message brings a
peer back UP, so a bot whose git remote died — or one partitioned away from the bus — is not
declared dead while it is still talking to us. A peer never heard from is `unknown`, not DOWN, and
a freshly started transport gets one interval of grace before anyone can be judged DOWN.

The table survives a restart: each transport saves `presence-<alias>.json` (peer
`last_seen`/`busy_since`/`term`/`rev`, and any DOWN episode already announced) beside its
other state and restores it at start, so a die-off seen before a restart is still known
after it and one admin line is still one line. A stop shorter than one interval missed no
pulse, so the saved verdicts stand; after a longer stop the bot cannot know what it did not
hear and rejudges from now under the fresh grace.

DOWN is **advisory**. It says "this peer has not been heard from"; it never says "somebody else
should lead". The lease (#83) decides who coordinates — using DOWN to start a takeover would let
a partition elect a second coordinator.

What DOWN changes: a hand-off to a DOWN peer is returned to the sender at once (no reminder, no
wait), the same as FROZEN, and no reminder is ever sent to a DOWN or busy peer. When the
coordinator, or the acting coordinator when the coordinator is the one down, hears a peer go
DOWN, it posts one line to the admin — "⚠️ DeepSeek has been silent since 18:30" — deduped once
per episode. A peer that comes back gets one "back" line and nothing more. `/status` and `/swarm`
show `peers=Claudebot:up,DeepSeek:down(12m)`; a busy peer shows `busy(3m)`.
