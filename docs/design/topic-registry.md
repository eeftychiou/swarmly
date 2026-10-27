# Design: transport bus vs token space — the topic registry

Oct 24, 2026

Approved by the admin on 24 Oct 2026 after review in comments. The implementation plan is §6; Claude implements it, and each phase is tested on the swarm's own bots.

## Principle

Two layers, kept strictly apart:

|  | **Transport bus** (code, no tokens) | **Token space** (the agents' CLIs) |
| --- | --- | --- |
| Holds | bot status and stats, the topic registry, routing, queues, rules enforcement, drift | the work: reasoning, code, reviews, answers |
| Lives in | each bot's transport process + shared state (bus/state backend) | one CLI session per (topic, agent) |
| Telegram surfaces | **General** (status, notices, admin commands) and **📡 Swarm pulse** (pulses, machine lines) | **work topics only** |

**No model turn ever runs in General or in Swarm pulse.** Everything a model sees comes from a work topic, through that topic's session.

## 1. The topic registry (the transport's single source of truth)

Today each bot keeps its own topic map, and links to GitHub are *guessed* from `#N` mentions (ref-learning, topic moves). That gets replaced by one explicit record per topic, written only by code:

```json
{
  "topic_id": 10134,
  "chat_id": -1001234567890,
  "title": "Stability round",
  "status": "open",
  "opened_by": "admin",
  "opened_at": "2026-10-23T23:12:41Z",
  "closed_at": null,
  "last_activity": "2026-10-24T08:55:34Z",
  "links": [
    {"id": "L1", "provider": "github", "kind": "issue",
     "ref": "https://github.com/o/r/issues/237", "role": "tracking",
     "added_by": "admin", "added_at": "2026-10-23T23:12:41Z",
     "updated_by": "admin", "updated_at": "2026-10-23T23:12:41Z"},
    {"id": "L2", "provider": "github", "kind": "pr",
     "ref": "https://github.com/o/r/pull/256", "role": "work",
     "status": "open", "phase": "review", "phase_at": "2026-10-24T13:49:00Z",
     "assignees": [
       {"agent": "DeepSeek", "as": "developer", "assigned_by": "Claudebot",
        "assigned_at": "2026-10-24T10:02:00Z", "last_activity": "2026-10-24T13:49:00Z"},
       {"agent": "Reasonix", "as": "reviewer", "assigned_by": "Claudebot",
        "assigned_at": "2026-10-24T13:50:00Z", "last_activity": null}
     ],
     "added_by": "DeepSeek", "added_at": "2026-10-24T13:49:00Z",
     "updated_by": "Claudebot", "updated_at": "2026-10-24T13:50:00Z"},
    {"id": "L3", "provider": "gdrive", "kind": "doc",
     "ref": "https://drive.google.com/…", "title": "Dashboard UX notes",
     "role": "reference",
     "added_by": "admin", "added_at": "2026-10-24T14:00:00Z",
     "updated_by": "admin", "updated_at": "2026-10-24T14:00:00Z"}
  ],
  "agents": {
    "Claudebot": {
      "role": "coordinator",
      "runtime": "claude",
      "model": "claude-opus-5-5",
      "effort": "high",
      "model_source": "profile",
      "session": {"id": "c0a1…", "started_at": "2026-10-23T23:12:42Z",
                  "turns": 41, "rotations": 0},
      "linked_by": "transport",
      "linked_at": "2026-10-23T23:12:41Z",
      "history": [
        {"at": "2026-10-24T09:00:00Z", "field": "effort", "from": "medium",
         "to": "high", "by": "admin"}
      ]
    },
    "DeepSeek": {
      "role": "developer",
      "runtime": "reasonix",
      "model": "deepseek-flash",
      "effort": "low",
      "model_source": "profile",
      "session": {"id": "20261024-…-deepseek-flash", "started_at": "…",
                  "turns": 12, "rotations": 1},
      "linked_by": "Claudebot",
      "linked_at": "2026-10-23T23:12:42Z",
      "history": []
    }
  }
}
```

### 1a. Data definition and data dictionary

An agent's entry in a topic (its **binding**) fully defines how that agent behaves *in that topic*: its role, its runtime, model and effort, and its session. Model and effort can change mid-topic (`/model@bot`, `/reasoning@bot` in the topic); every change is recorded in `history`, so the record always says what the agent was running at any moment. A binding holds configuration and identity, never live availability: whether the agent is busy, paused, frozen or DOWN comes from its pulse, read live, and is never stored here.

Two records exist: the **agent profile**, one per bot in the swarm, holding its defaults (on disk, in that bot's `config.yaml`); and the **topic record** with its bindings, one per topic, in the registry. A binding starts from the profile and can override it for that topic. **Precedence: a value set in the topic registry wins over the profile default from `config.yaml`.** A field the binding leaves unset falls back to the profile, and `model_source` records which one applied (`profile` or `topic`).

**Topic record**

| Field | Type | Req. | Written by | Meaning |
| --- | --- | --- | --- | --- |
| `topic_id` | integer | yes | transport | Telegram forum thread id; the key. |
| `chat_id` | integer | yes | transport | The Telegram group the topic lives in. |
| `title` | string | yes | transport | Topic name as shown in Telegram. |
| `status` | enum `open`, `closed` | yes | transport | Closed topics route nothing; their sessions are retired. |
| `opened_by` | string | yes | transport | `admin`, or the alias of the coordinator that opened it. |
| `opened_at` / `closed_at` | ISO-8601 UTC | yes / no | transport | When it opened; when it closed (null while open). |
| `last_activity` | ISO-8601 UTC | yes | transport | Last message routed in the topic. |
| `links` | list of Link | yes (may be empty) | transport | The work items and references this topic is about (any provider). |
| `agents` | map alias → Binding | yes | transport | The agents linked to the topic, and how each behaves here. |

**Link**

A link with `role: work` is a **work item**: a piece of work the swarm assigns and tracks. `tracking` and `reference` links are only pointers, and the work-item fields stay empty for them.

| Field | Type | Req. | Written by | Meaning |
| --- | --- | --- | --- | --- |
| `id` | string | yes | transport | Stable id within the topic (`L1`, `L2`, …), used by `ASSIGN:`, `PHASE:` and `/phase`. |
| `provider` | enum `github`, `gitlab`, `gdrive`, `cloud`, `local`, `topic` | yes | transport | Where the item lives. `topic` means there is no external tracker: the topic itself is the work item. |
| `kind` | enum `issue`, `pr`, `doc`, `folder`, `file`, `task` | yes | transport | What the reference points to. |
| `ref` | string | yes | transport | How to reach it: a full URL (GitHub, GitLab, Drive), a path (`local`), or a short id (`topic`, e.g. `T-10134-1`). Replaces the GitHub-only `url`. |
| `title` | string | no | transport | Human label, needed when `ref` is not self-explanatory. |
| `role` | enum `tracking`, `work`, `reference` | yes | transport | How the item relates to the topic: `tracking` the round or umbrella item; `work` an item worked here; `reference` input material (spec, dataset, prior PR). It does **not** say how far the work has got: that is `phase`. |
| `status` | enum `open`, `closed`, `abandoned` | for `work` | lease holder | Whether the item is still live. `closed` comes from the provider (merged, issue closed) or the coordinator; `abandoned` only from the coordinator or the admin. |
| `phase` | enum `research`, `design`, `development`, `review`, `qa`, `done`, `blocked` | for `work` | lease holder | Where the work on this item stands. |
| `phase_at` | ISO-8601 UTC | with `phase` | lease holder | When the phase last changed. |
| `blocked_on` | string | with `blocked` | lease holder | What it waits for: another item's `id`, an agent, or `admin`. |
| `assignees` | list of Assignment | for `work` | lease holder | Which agents work on it, and as what (below). Empty means unassigned: the coordinator sees it as open work. |
| `updated_at` | ISO-8601 UTC | yes | transport | Last change to this link, any field. |
| `updated_by` | string | yes | transport | Who made that change. |
| `added_by` | string | yes | transport | `admin`, the coordinator's alias, or the developer whose PR opened from the topic. |
| `added_at` | ISO-8601 UTC | yes | transport | When the link was added. |

**Assignment** (one per agent on a work item)

| Field | Type | Req. | Written by | Meaning |
| --- | --- | --- | --- | --- |
| `agent` | alias | yes | lease holder | The agent. Assigning it links it to the topic too, if it isn't already. |
| `as` | role name | yes | lease holder | What it does on this item: `developer`, `reviewer`, `qa-tester`, `researcher`, … Chosen when assigned, from who is available; not tied to the bot. |
| `assigned_by` | string | yes | lease holder | The coordinator's alias or `admin`. |
| `assigned_at` | ISO-8601 UTC | yes | lease holder | When. |
| `last_activity` | ISO-8601 UTC | no | that agent's transport | The agent's last turn on this item. Lets the coordinator see a stalled assignment. |

Every change to `status`, `phase` or `assignees` is also appended to the topic's `history`. When an assignee is paused, frozen or DOWN, the stand-in is still computed live (#247/#255); it is shown, never stored here.

**Working without GitHub.** A link is a pointer to wherever the work lives, through a small provider adapter: GitHub and GitLab (issues, PRs, CI), Google Drive or another cloud (docs, folders, data files), a local path, or nothing at all. With `provider: topic` the topic is its own work item: the transport gives it a short id, and phases and history are kept in the registry alone. Only providers with events (GitHub, GitLab) feed notifications, the CI watch and automatic phase changes; the others are changed by hand.

**Phases.** `phase` is per work item, because one topic can hold several items at different stages (one PR in review while the next issue is still in research). It changes in three ways:

- **From provider events, where there are any:** a PR opened from the item → `review`; checks green and approved → `qa`; merged or closed → `done`.
- **By the coordinator's line** `PHASE: <ref> <phase>` (for example `PHASE: #256 qa`), recorded by the transport.
- **By the admin's** `/phase <ref> <phase>`.

The phase tells the coordinator what kind of agent the item needs next (`research` → a researcher, `qa` → a tester). That choice is made at the time, from who is available, as roles are: no bot is tied to a phase. The dashboard and `/topics` show each item's phase.

**Binding (an agent in a topic)**

| Field | Type | Req. | Written by | Meaning |
| --- | --- | --- | --- | --- |
| `role` | enum `coordinator`, `developer`, `reviewer`, `qa-tester`, `researcher` | yes | transport, on the coordinator's assignment or the admin's `/role` | The role this agent plays **in this topic**. The coordinator role always follows the lease. |
| `runtime` | enum `claude`, `claude_thread`, `codex`, `reasonix`, `antigravity`, … | yes | transport (from the profile) | The CLI adapter the agent runs on. |
| `model` | string | yes | transport | Model id in use in this topic, as the CLI names it (for example `claude-opus-5-5`, `deepseek-flash`). |
| `effort` | enum `low`, `medium`, `high`, `xhigh`, `max`, or `default` | yes | transport | Reasoning effort in this topic; `default` = the CLI's own default. |
| `model_source` | enum `profile`, `topic` | yes | transport | `profile`: inherited from the agent profile; `topic`: overridden here with `/model` or `/reasoning`. |
| `session.id` | string | no (none before the first turn) | the agent's transport | The CLI session id for this topic. |
| `session.started_at` | ISO-8601 UTC | no | the agent's transport | When the current session began. |
| `session.turns` | integer | yes | the agent's transport | Turns run in the current session. |
| `session.rotations` | integer | yes | the agent's transport | How often the session was handed over to a fresh one (size limit, drift reset). |
| `linked_by` | string | yes | transport | Who linked the agent: `transport` (the coordinator, linked when the topic opens), the coordinator's alias (by assignment), or `admin` (`/link`). |
| `linked_at` | ISO-8601 UTC | yes | transport | When it was linked. |
| `history` | list of Change | yes (may be empty) | transport | Every change to `role`, `model` or `effort` in this topic. |

**Change**

| Field | Type | Meaning |
| --- | --- | --- |
| `at` | ISO-8601 UTC | When it changed. |
| `field` | enum `role`, `model`, `effort` | What changed. |
| `from` / `to` | string | Old and new value. |
| `by` | string | `admin`, the coordinator's alias, or `transport` (for example the lease moving). |

**Agent profile (one per bot, swarm-wide)**

| Field | Type | Req. | Written by | Meaning |
| --- | --- | --- | --- | --- |
| `alias` | string | yes | config | The bot's name in the swarm (`Claudebot`). |
| `handle` | string | yes | config | Its Telegram username (`@Claude_obot`). |
| `runtime` | enum (as in Binding) | yes | config | Its CLI adapter. |
| `default_model` / `default_effort` | string / enum | yes | config, `/model@bot`, `/reasoning@bot` outside a topic | What a new binding starts with. |
| `capabilities` | list of role names | yes | config | Roles it may be given; by default every role except coordinator, which the lease decides. |
| `role_preference` | role name or null | no | config | A starting preference for the first split of work; never binding. *Also in the profile:* `max_turns` and `max_age` (session rotation limits, §Agent stats and drift). A binding's `role` is the agent's role in the topic as a whole; an assignment's `as` is what it does on one work item, and can differ. |

- **Where it lives:** in shared swarm state, through a `StateStore` API whose backend is pluggable (the lease and owed table move behind the same API). **Proposed default: Telegram itself**, through the Swarm pulse topic and a pinned snapshot (§1b, "Telegram as the store"), so the swarm needs no gitbus. gitbus and a shared folder remain optional backends. Every bot's transport reads it, and writes only through the store.
- **Who writes what:**
  - **Opening a topic** (admin, or the coordinator's `NEW_TOPIC:`): the transport creates the record and links the coordinator.
  - **Links to GitHub:** added when a PR or issue is opened from the topic, or named in the coordinator's `LINK: #N` line, or by the admin's `/topic #N`. **No guessing** from arbitrary mentions.
  - **Agents:** linked by the coordinator's assignment (`ASSIGN: @bot <work>`, or an existing hand-off), recorded by the transport. The admin can override with `/link @bot` and `/unlink @bot` in the topic.
  - **Sessions:** each agent's transport records the session id its CLI returns for that topic.
  - **Close:** when the tracking item closes, or by the admin's `/cleanup`. Closing retires the sessions.
- **What it replaces:** `_ref_topics` / ref-learning, `_topic_elsewhere` moves, the per-bot `topics-<alias>.json` as source of truth (it stays only as a cache), and the guessed topic in GitHub notifications.
- **Shown:** on the dashboard (topics, their links, linked agents and whether each is busy) and in `/topics`.

**Agent stats and drift**

**Today (#247, live):** each bot's transport keeps a drift ledger in a local file (`drift-<alias>.json`, last 300 rows), shown by `/drift@bot`. Rule violations the transport detects are scored by severity, over a 6-hour window:

- score ≥ 3: a rule reminder is added to the bot's next turn;
- score ≥ 6, or two hard (severity 3) violations: a **drift reset**. The session for that topic is retired, and the next turn starts a fresh session that reads the rulebook again.

Three things are missing. Only the bot itself can see its ledger: the coordinator and the dashboard can't. Outcomes such as failed PRs are not counted, only rule breaks. And the 6-hour window forgets slow drift: a session that runs for days and breaks a rule twice a day never reaches the threshold. The design moves the ledger into the store, adds outcome signals, and changes how the score is counted:

**Score per session, not per time window.** A violation counts against the **session it happened in** (the agent's session for that topic). It keeps counting for as long as that session lives, however many days that is. The count starts again only when the session is rotated. A violation with no topic (for example a secret in a push) counts against every session of that agent. There is no 24-hour reset of anything: `resets_24h` below only counts how many resets happened, so that a bot that keeps drifting straight after a reset is flagged to the admin (3 or more a day).

**Rotation by age as well as by drift.** Long sessions drift even without recorded violations, so every session is also rotated with a handover when it reaches a limit from the agent profile: `max_turns` (Reasonix does this today) or `max_age` (proposed default: 24 h of activity). A rotation is planned, done between turns and never mid-task, and costs one handover turn.

**Drift reset = handover rotation.** When the score reaches the threshold (or at the age/turn limit):

1. **The agent writes the handover.** The transport gives the old session one last turn: "Write your handover", in a fixed format: the work items and their state, decisions made, open questions, and next steps. No commentary.
2. **The transport checks and stores it.** The handover is saved with the session (`session.handover` in the binding, plus `last_rotation` {`at`, `reason`: `drift` | `turns` | `age` | `admin`}). Facts from the registry (links, assignments, phases, the lease) are put at its top and **win over the handover's own claims**, because a drifting session may have wrong ideas about the state of the work.
3. **The new session starts from:** the rulebook (its role prompt, read fresh), then the rule reminder listing the rules it broke (for a drift reset), then the registry facts, then the handover.
4. **Fallback:** if the old session can't write a handover (it times out, or the agent is frozen), the transport builds one from the registry and the topic's recent messages, as today's recap does.

The admin can force one with `/rotate@bot` in a topic.

**Agent stats record** (one per agent, in the `StateStore`; written only by **that agent's own transport**, like `session.*`)

| Field | Type | Req. | Meaning |
| --- | --- | --- | --- |
| `agent` | alias | yes | Whose stats. |
| `sessions` | map topic\_id → {`score`, `hard`, `since`} | yes | Drift per live session: the sum of severities and the number of hard violations since that session started or was last rotated. A reset or rotation is decided per session. |
| `resets_24h` | int | yes | How many drift resets happened in the last 24 h. It is only a count (3 or more flags the agent to the admin); nothing is reset on a 24-hour clock. |
| `last_reset` | {`at`, `topic_id`} | no | When and where the last drift reset happened. |
| `violations` | list of Violation | yes | The last 300 rows (below); the per-session scores are computed from them. |
| `counters` | map | yes | Running totals since the bot joined: `turns`, `prs_opened`, `prs_merged`, `prs_closed_unmerged`, `ci_red_on_own_pr`, `changes_requested`, `bounced_messages`, `violations_by_kind`, `rotations_by_reason`. Shown on the dashboard. |
| `updated_at` | ISO-8601 UTC | yes | Last change. |

**Violation**

| Field | Type | Req. | Meaning |
| --- | --- | --- | --- |
| `kind` | enum (see below) | yes | What happened. |
| `severity` | 1–3 | yes | From the rule table; 3 is hard. |
| `rule` | string | yes | The rulebook line it breaks, quoted in the reminder. |
| `evidence` | string (≤300) | no | What the transport saw. |
| `topic_id` | int | no | Where. A reset retires the session of this topic. |
| `link_id` | string | no | The work item (`L2`), when there is one. |
| `reported_by` | alias | yes | Which transport detected it (itself, or the lease holder's watcher). |
| `at` | ISO-8601 UTC | yes | When. |

**Kinds.** These are recorded today: `addressed_frozen_peer` (3), `merge_gate_refused` (3), `merge_gate_bypassed` (2), `secret_in_push` (3), `foreign_force_push` (3), `coordinator_code_push` (2), `duplicate_reply` (1), `deploy_instruction` (2). Proposed new **outcome** kinds, weights for your review:

- `claimed_ready_while_red` (2): asked for review or merge while its own PR's CI was red;
- `pr_closed_unmerged` (1): its PR was closed without a merge and not superseded by its own new PR;
- `review_ignored` (2): pushed again for review without addressing the changes requested;
- `stalled_assignment` (1): an item assigned to it had no `last_activity` for 2 h while it was available.

A red CI on its own in-progress branch is *not* drift: that's normal development. It only counts once the agent claims the work is done.

**Who records it.** The single-writer rule still holds. The agent's own transport writes its stats. When another transport sees the event (for example, the lease holder's repo-wide CI watch sees a PR go red after its author asked for review), it sends a `DRIFT <alias> <kind> <evidence>` line in Swarm pulse. The agent's transport records it, exactly as the role guard's inbox works today. The coordinator reads everyone's stats from the store to decide who gets which work, and the dashboard shows them.

### 1b. How the shared state is updated and kept in sync, and split brain

The registry lives in shared state that every bot's transport reads. Four rules keep it consistent without a central server.

**1. One owner per field: no two bots ever write the same value.**

| Store key | What it holds | Only writer |
| --- | --- | --- |
| `lease` | the coordinator claims, one winner per term | any bot may claim; the store's compare-and-set picks one |
| `topic/<id>` | topic header, links (with status, phase, assignees), bindings (`role`, `model`, `effort`, `linked_by`, `history`) | the **lease holder** (the acting coordinator's transport), applying the admin's commands and the coordinator's `LINK:`/`ASSIGN:`/`PHASE:` lines |
| `topic/<id>/agent/<alias>` | that binding's `session.*` (id, turns, rotations, handover, last\_rotation) and `last_activity` per work item | **that agent's own transport** |
| `agent/<alias>/stats` | agent stats and drift | **that agent's own transport** |
| `agent/<alias>/profile` | the agent profile, published from its `config.yaml` | **that agent's own transport**; the admin's `/model@bot`, `/reasoning@bot` outside a topic go through it |
| `owed` | the owed / WAIT table | the lease holder |

Readers join these keys into the records of §1a: an assignment's `last_activity`, for example, is read from the agent's own key, not written into the lease holder's.

A bot that needs a lease-holder field changed (for example a developer whose PR should be linked) sends a request line; the lease holder applies it. Since keys are disjoint by owner, a merge is a union: there is no conflict to resolve.

**2. Every write is a compare-and-set, stamped with the lease term.**

- Each record carries `version` (incremented per write), `term` (the writer's lease term) and `writer`.
- On gitbus, a write is a commit pushed with lease: if the ref moved, the push is refused, and the writer fetches, re-applies its change on the new tip and pushes again, with backoff (#235). Nothing is ever overwritten blindly.
- Readers fold all writes and keep, for each lease-holder field, the value with the highest `(term, version)`.

**3. Split brain is prevented by the lease, and fenced by the term.**

- The lease is itself a compare-and-set in the store: for any term, exactly one claim wins. Two bots can both *believe* they coordinate only if one of them cannot see the store, and that bot cannot write either.
- **Fencing:** before every write, and before every side effect (a merge, a role change), the transport re-reads the lease fresh (`lease_fence`, already in code). If this bot no longer holds the current term, it does not write.
- **Stale writes are ignored:** a write stamped with an older term than the current lease is discarded by every reader's fold, even if it reached the store late. So a deposed coordinator that wakes up mid-write cannot overwrite its successor.
- **Partition from the store: fail closed.** A bot that cannot reach the store cannot renew its lease. The lease lapses after its TTL (10 min), the successor claims the next term, and the cut-off bot stops acting as coordinator and refuses merges. It never invents state.

**4. Synchronisation: pull on a clock, push a nudge.**

- Every transport keeps a local cache of the registry and refreshes it on each pulse (every 2 min), and always fresh before it writes or acts.
- After a write, peers learn of it at once instead of at their next pulse: on the Telegram backend the write *is* a line in Swarm pulse, so every bot receives it as it happens; on the other backends the writer posts a one-line `STATE <key> v<n>` nudge there. This happens outside token space.
- A bot that restarts reads the store first and acts only after the read succeeds.

**Backends.** The same four rules hold for every backend. On **gitbus**: ref compare-and-set plus retry (what exists today). On a **shared folder**: an atomic rename with a version check.

**Telegram as the store (proposed default).** gitbus exists today only because Telegram is not durable for bots: a bot cannot read chat history, and updates it missed while offline are kept by Telegram for 24 hours at most. Two pieces close that gap.

- **The op log is the Swarm pulse topic.** Every write is one machine line there: `STATE <key> v<base>→v<new> t<term> <change>`. The bots already see each other's lines there (bot-to-bot mode, as for pulses). In a supergroup, message ids rise strictly, so every bot sees the same order.
- **Compare-and-set by order:** for a given key and base version, the line with the lowest message id wins. Every reader applies the same rule, so all reach the same result without talking. A loser sees it lost, re-reads and retries on the new version. The lease claim is the same: the first `LEASE t<n>` line wins term n.
- **The snapshot is a pinned file.** The lease holder keeps the whole state as a JSON file attached to a pinned message in Swarm pulse (Telegram allows files far larger than the state). It records the version and the last message id it covers. A snapshot from an older term is ignored, as any stale write is. Telegram's `getChat` returns only the chat's *most recently sent* pinned message, so each snapshot is posted as a **new** message, pinned, and the previous snapshot unpinned; the dashboard stays pinned too but is only ever edited in place, never re-sent, so the snapshot is always the newest pin. This is re-done after every few changes and at least every pulse. (Verified on the live bots in phase 0 before anything depends on it.)
- **A bot that starts or rejoins, however long it was away** (an hour, or a week), reads the pinned snapshot (`getChat` returns the pinned message), then applies the `STATE` lines after it from its pending updates. The 24-hour limit only decides whether it can also replay those individual lines. The snapshot itself is always current, at most one pulse old, so a long absence never keeps a bot out. Until that read succeeds, the bot acts on nothing.

  **Each bot also keeps its last state on its own disk**, as a cache and a backup, not as the truth. When it rejoins, it merges as follows:
  - **Swarm-wide fields** (topics, links, assignments, the lease): the pinned snapshot wins. A local copy has an older (term, version), so it is ignored, exactly like a stale write. The bot never pushes its old view of the swarm over the current one. That would bring back assignments and topics the swarm has since moved past.
  - **Its own fields** (its `session.*` for each topic, its availability): the bot announces them with `STATE` lines, as usual, because it is their only writer. Sessions for topics that closed while it was away are retired.
  - **Its work:** items still assigned to it show up in the snapshot, and the coordinator decides whether to keep them with it or give them to someone else. Its local copy shows what it was doing, so it can say "back, I had L2" in that topic.

  **Recovery when the pin itself is lost** (deleted, or a new group): every bot's local copy is a candidate. Each bot posts its copy's (term, version), and the one with the highest pair is restored and re-pinned by the lease holder. The swarm recovers from any single bot's disk.
- **Fail closed, as before:** if Telegram is unreachable, nothing can be written or renewed, so the lease lapses and no one acts on stale state.
- **Limits:** Telegram limits how fast a bot can post in a group (about 20 messages a minute). State changes are few and are batched into one line when several happen together, so this stays well under the limit. The log adds lines to Swarm pulse, which is transport-only and never routed to a CLI.

What this gives up compared with gitbus: no history older than Telegram keeps, apart from the snapshot. The registry's own `history` fields keep what matters for the dashboard.

## 2. Routing (the transport decides who gets a message)

A human or bot message in a **work topic**:

1. The transport looks up the topic in the registry.
2. **Recipients:**
   - the **coordinator (lease holder) gets every message** in the topic;
   - other **linked agents** get a message only when it **names them** (@mention) or **hands them work** (a hand-off or an ASSIGN line);
   - a mention of an agent that **isn't linked** links it (recorded) if the author is the coordinator or the admin; otherwise the transport answers "X isn't linked to this topic", and the coordinator decides.
3. Each recipient's message goes to **that agent's session for this topic**. If the session is busy, it's **queued** and delivered when free, folded with anything else that queued. (This is what exists now, keyed by the registry instead of the mention heuristics.)
4. **Unavailable recipients** (paused, frozen, DOWN): not delivered. There's a transport notice in the topic, the sender gets a bounce turn, and the stand-in is addressed (#247/#255, unchanged).

Rationale for "coordinator gets everything, others when named": it keeps the cost at about one turn per message, not N, and the coordinator dispatches anyway (#227).

## 3. General and Swarm pulse are transport-only

- **General:**
  - **Allowed:** transport posts (status changes, deploy/online notices, refusals that aren't tied to a topic, round open/close announcements, admin command answers) and **admin commands** (`/pause`, `/hold`, `/role`, `/swarm`, `/usage`, `/drift`, `/cleanup`, …).
  - **Never:** a model turn.
  - **A person typing work in General:** the transport **opens a topic** for it (title from the request), records it in the registry, links the coordinator, delivers the request there as the first message, and answers in General: "↪ opened topic «…» for this, coordinator Claudebot". (Question 3 below.)
  - **A bot's reply** can't be posted in General: replies are always posted in the topic of the turn, and never moved to another topic by the items they name.
  - **Nothing from token space**, whatever route it takes (a reply, a brief, a report-back summary, a role's reason, a peer's answer from the bus, a bus pointer's preview, an agent's own `send`), is posted in General or Swarm pulse. The send path refuses text marked as token space, and the bus poller, `post` and `send` check the same rule before they send. A report-back for a request typed in General leaves its summary in the topic and puts only a link in General. A bus answer to a mirrored hand-off is posted in the topic the hand-off was seen in.
- **📡 Swarm pulse:** pulses, FROZEN/RESUMED, presence and the pinned dashboard. Nothing in it is ever routed to a CLI, and nothing said there ties an item to it. (That's already true today; it becomes a hard rule with a test.)

## 4. One session per (topic, agent), for every adapter

| Adapter | Today | After |
| --- | --- | --- |
| Reasonix (DeepSeek, Reasonix) | session per topic, rotated after N turns with a handover | unchanged, id recorded in the registry |
| Codex | thread per topic | unchanged, id recorded |
| Antigravity | conversation per topic | unchanged, id recorded |
| **Claude** | **fresh `claude -p` every turn** + transport recap | **`claude_thread`**: one resumable session per topic (`--resume`), like the others. The adapter already exists. |

- The recap stays only as the **seed** of a new session (first turn, or after a rotation or reset).
- The drift reset (#247) retires a session through the registry.

## 5. What gets removed or simplified

- **Ref-learning and topic moves** (#193/#198/#223): replaced by explicit links.
- The **mention-based "should I answer"** heuristics in each bot (`routing decision …`): replaced by registry routing in the transport.
- **Turns in General**, including transport turns falling back to General (the #257 class of bug), become impossible by construction.
- **NEW\_TOPIC parsing** stays, but it writes the registry.

## 6. Implementation plan

**Who and how.** Claude implements every phase. Each phase is one or two PRs on `eeftychiou/swarmly`, branch `claude/registry-p<n>`; the swarm reviews them and the coordinator merges, as for transport changes so far. CI must be green on Linux and Windows. After a merge, Claude deploys to the four Windows bots when they are idle (the usual idle check), and the admin reinstalls Reasonix on CT100 with the `pipx` command Claude gives him.

**Testing on the swarm's own bots.** There is no second swarm: a bot token can only be polled by one process, so tests run on the live bots. To keep that safe:

- every phase ships **behind a flag** in `config.yaml` (`state.backend: off | telegram` and `state.mode: shadow` for the store and the registry, `limits.general_turns: allow | never` for phase 4, and one more per later phase), first in **shadow** (it records and compares, nothing acts on it) and only then **on**, one flag at a time;
- live tests run in a dedicated **🧪 Swarm test** topic, with scripted messages from the admin account or a transport self-test, never in the round's work topics;
- each phase has **acceptance checks** (below) run on the live bots, and a written result in the PR before the flag is turned on;
- every flag can be switched back to `off` without a deploy (config change and bot restart), and every phase keeps today's path working until its cleanup.

Codex is out of quota until 18 Nov and Antigravity is rate-limited, so the live checks rely on Claudebot, DeepSeek and Reasonix, and test frozen/paused peers with the ones that are frozen anyway.

### Phase 0: verify the Telegram assumptions (1 PR, no behaviour change)

A `swarmly selftest store` command, run by each bot on start and on demand:

- every bot, Reasonix on CT100 included, sees the others' machine lines in Swarm pulse (bot-to-bot mode);
- a line has the same message id for every bot, and one bot's lines get rising ids in the order it sent them; lines may *arrive* in a different order at different bots (the fold sorts by id), and how often that happens is measured;
- a bot can post a JSON file, pin it, unpin the previous one, and every other bot reads it back through `getChat` while the dashboard is also pinned;
- post rate stays under the limit with a burst of 10 changes batched into one line.

**Done when:** all four results are green on all five bots. If the pin route fails, the fallback (the snapshot's message id kept in the Swarm pulse topic's own dashboard line, re-read on start) is designed before phase 1.

### Phase 1: the StateStore (1–2 PRs; shadow)

New package `swarmly/state/`:

- `records.py`: dataclasses for every record of §1a (topic, link, assignment, binding, change, profile, agent stats, violation), with JSON round-trip and validation;
- `keys.py`: the key schema and the owner of each key (§1b table); a write to a key the bot does not own is refused in code;
- `fold.py`: the fold rules: highest (term, version), lowest message id wins a compare-and-set, stale terms dropped, joins across keys;
- `telegram_store.py`: the op log in Swarm pulse, the pinned snapshot, the local cache `state-<alias>.json`, start-up read, rejoin merge and pin-loss recovery (§1b);
- `git_store.py` and `dir_store.py`: the same API over today's gitbus lanes and a shared folder.

**Tests:** compare-and-set races between two writers, a deposed lease holder writing late, a bot rejoining after 2 h and after 8 days, the pin deleted, Telegram unreachable (fail closed), a write by the wrong owner refused. The fold is tested to give the same state for any order of delivery.

**Live check:** the store runs in shadow on all bots for 24 h, writing a heartbeat key each pulse; every bot's state hash agrees (shown by `/state@bot`).

### Phase 2: the registry, populated in shadow (1 PR)

- The lease holder writes topic records from today's events: topics opened, titles, the links today's logic finds (marked `added_by: legacy` so they are easy to tell from explicit ones), the coordinator's binding.
- Each bot writes its own `topic/<id>/agent/<alias>`: session ids returned by every adapter, turns, rotations; and publishes its profile (runtime, model, effort, capabilities).
- `/topics` and a Topics section in the dashboard and the Mini App read the registry.

**Live check:** for 24 h the registry agrees with today's topic map and session files for every topic the swarm works in; differences are logged and fixed.

### Phase 3: lease and owed table on the store (1–2 PRs)

- Lease claims become `LEASE t<n>` writes; `lease_fence` reads the store. Owed rows move to the `owed` key.
- First in shadow: both the gitbus lease and the store lease run, and every term's winner is compared. Then the store becomes authoritative, and gitbus is no longer needed for anything (pulse already falls back to Telegram).

**Live checks:** failover drill (stop Claudebot; the next bot claims the term within the TTL; restart Claudebot; it fails back), a simulated partition (a test flag makes one bot's store unreachable: it must stop coordinating and refuse merges), and a 6-hour idle run with no lease flapping. Then one bot runs with gitbus switched off entirely.

### Phase 4: General and Swarm pulse transport-only (1 PR)

- No model turn in General or Swarm pulse, by construction (a guard at the one place turns start, with a test).
- Work typed in General opens a topic, records it, links the coordinator and delivers the request there.
- A reply is always posted in the topic of its turn.

**Live check:** the admin types a task in General; a topic opens and the coordinator answers there; a peer-back, CI wake and bounce turn after a restart all land in work topics.

### Phase 5: work items, links and phases (1–2 PRs)

- `LINK:`, `ASSIGN:`, `PHASE:` lines from the coordinator; `/topic`, `/link`, `/unlink`, `/phase` for the admin; request lines from other bots.
- Provider adapters: GitHub (events drive status and phase, PR opened → review, merged → done), `topic` (no tracker), and plain pointers for Drive, cloud and local paths.
- GitHub notifications and the repo-wide CI watch find their topic through the links; ref-learning and topic moves are switched off by the flag (removed in phase 9).

**Live check:** one small round run on the live swarm with explicit links only: an issue, its PR, a review and a merge move the item through phases on the dashboard, with no notification in a wrong topic.

### Phase 6: routing from the registry (1 PR)

- The coordinator gets every message in a topic; other linked agents when named or handed work; an unlinked mention links (coordinator or admin) or is answered by the transport.
- Queues keyed by (topic, agent), as now.
- Behind `state.routing: registry` (default `legacy`, `registry` once the store is on). Refinements from the implementation review: the coordinator takes a *person's* unaddressed message, but not a peer bot's unaddressed post (that would ping-pong up to the peer-turn cap); it stays out of a message addressed to agents that will take it (linked, or named by the coordinator or the admin), and takes one whose named agents were refused. A reply to a bot addresses that bot. `@all` reaches linked agents only, and an unlinked one posts no notice for it. Until phase 9 an agent with turns in the topic counts as linked, unless `/unlink` took it off since. Swarm pulse and closed topics are never routed by the registry; a mention alone never creates a topic record.

**Live check:** in the test topic, scripted messages give exactly the expected turns per bot (counted from the logs); a mention of an unlinked bot by a peer is refused with a notice.

### Phase 7: one session per topic for every adapter (1 PR)

- Claudebot moves to `claude_thread` (one resumable session per topic); its recap becomes only the seed of a new session.
- Sessions retire on topic close and on rotation, through the registry.

**Live check:** two topics worked in parallel by Claudebot keep separate sessions (ids in `/topics`), and a restart resumes both.

### Phase 8: drift v2 and handover rotation (1–2 PRs)

- The ledger moves to `agent/<alias>/stats`; scores per session, no time window; counters.
- Outcome kinds and `DRIFT` lines from other transports (the CI watch, the stalled-assignment check).
- Rotation by drift, `max_turns`, `max_age` and `/rotate@bot`: the agent writes its handover, the transport puts the registry facts on top and stores it, the new session loads rulebook, reminder, facts and handover; the fallback handover when none comes.
- 3 or more resets in a day flags the agent to the admin in General.

**Live check:** `/rotate@bot` on each runtime produces a handover and a new session that continues the work correctly; a scripted violation series triggers a reminder, then a reset with handover; `/drift` and the dashboard show every agent's stats.

### Phase 9: cleanup and release (1 PR)

- Remove ref-learning, topic moves, mention heuristics and the shadow paths; make the flags' `on` the defaults.
- Update PROTOCOL.md, AGENTS.md, the role prompts and README (public-ready: setup without gitbus, Telegram-only swarm).

**Done when:** the swarm runs a full round on the new path with gitbus off, and an idle night with no stall.

**Order and size.** Phases 0–3 come first because everything else reads the store. Phases 4–8 each depend only on 1–2 and can follow in this order, the order chosen to fix the worst failures first (turns in General, wrong-topic notifications, drift). About 12–15 PRs in all.

## Decisions (24 Oct)

The admin agreed with the proposals:

1. **Routing:** the coordinator gets every message in a topic; other linked agents only when named or handed work.
2. **Linking:** the coordinator links agents by assignment, the transport records it; the admin can `/link` and `/unlink`.
3. **Work typed in General:** the transport opens a topic for it and hands it to the coordinator there.
4. **Bot-to-bot DMs and cross-topic mentions:** forbidden; a message always belongs to one topic.
5. **Store:** the registry is built straight on the Telegram store; gitbus stays as an optional backend; the lease and the owed table move onto the same store.
6. **Drift:** scored per session; resets and age/turn limits rotate the session with an agent-written handover; outcome kinds with the weights proposed above.
