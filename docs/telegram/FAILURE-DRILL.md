# Failure drill

A live rehearsal of what the swarm does when a bot dies, so the recovery path is something the
admin has *seen* work rather than something the code claims. Run it at the end of a round, or
after any change to the lease, the heartbeat, the WAKE timers or the round state.

Everything here is done through the group and the aliases — no local paths, no per-host detail.
The in-process version of the same seven drills runs in the test suite (`tests/test_failover.py`,
built on `tests/swarm_group.py`); this file is the human one.

**Before you start**

- A round is open with one item left, small enough to finish in ten minutes (a docs fix, a
  one-line test change).
- Every bot is up (`/swarm` answers with `coordinator: <alias>` and every peer `up`).
- You are the admin: `/role`, `/pause` and `/resume` are yours, and nobody else's.

**Timings that matter**

| Value | Default | Where it comes from |
| --- | --- | --- |
| heartbeat | 2 minutes | `limits.heartbeat_minutes` |
| a peer is DOWN | 6 minutes (3 missed beats) | `limits.heartbeat_minutes` × `limits.peer_down_after` |
| lease TTL / takeover | 10 minutes | `limits.takeover_minutes` |
| lease renewal | every 300 seconds | `limits.lease_renew_seconds` |
| claim settle window | 30 seconds | `limits.lease_settle_seconds` |

## 1. Kill the coordinator

1. Note which alias `/swarm` names as the coordinator, then stop that bot with no goodbye
   (disable its scheduled task and end the process — not `/pause`, not `/bye`).
2. Within its next release interval the group sees **no** lease renewal. Watch the topic: at
   about the TTL (10 minutes) the successor posts

   `🔄 coordinator lease: <alias> is acting coordinator at term N+1 (the lease lapsed; expires HH:MMZ).`

   Exactly one bot posts that line, and it is the first live peer in the succession order —
   reviewer, then QA, then developer.
3. Ask the new acting coordinator to finish the round item. It merges when CI is green:
   `gh pr checks <n>` shows seven passing checks, then the merge lands under the acting
   coordinator's own identity.
4. Confirm the old coordinator merged nothing: no second merge commit, and the PR shows one
   merger. A bot whose bus connection is gone cannot pass `lease_fence`, so a merge attempt
   from it is refused even while its own view of the world says it still leads.

**Expected**: one takeover line, one merge, one round report at the end of the round.

## 2. Bring the coordinator back

1. Start the killed bot again. It posts a HELLO, then reads the lease.
2. It does **not** resume leading: `/swarm` still names the acting coordinator, and the
   returned bot answers as a peer. Its first message in the topic says it is back; it does not
   claim the role.
3. Give it a small piece of work; it does it as a peer.
4. Hand the role back deliberately:

   ```
   /role @<coordinator_handle> coordinator
   ```

   All bots apply the same command, so they agree. The returned bot writes a new term (N+2) and
   the previous acting coordinator yields; `/swarm` names one coordinator again.

**Expected**: no double merge at any point, one coordinator before and after `/role`.

## 2b. Move the lease deliberately: hand-over and fail-back (#154)

Nothing has to break for this one — it is the drill for an *explicit* move, and it is the same
operation whether you use the dashboard or `/role`.

**Hand the lease to another bot**

1. In the control topic, press **Hand lease** on the pinned dashboard. It answers with one
   button per bot (never the current holder), then asks again.
2. Press the bot that should coordinate, then **Confirm**. That is one press: the holder yields
   its term and posts `CONTROL swarm.lease target=<new holder> value=take by=<your id>`.
3. The new holder answers in the channel:
   `🔄 coordinator lease: <alias> is acting coordinator at term N+1 (handed over by <old>; expires HH:MMZ).`
   It pins its own dashboard; the previous one is unpinned, never edited.
4. `/swarm` names the new coordinator, at the new term. Nothing merges for the old one: its
   yield is in the past, and `lease_fence` refuses it from then on.

**Fail it back**

1. Press **Fail back**. The target is the coordinator the swarm names
   (`membership.state.coordinator`, else a peer whose role is `coordinator`, else this bot's own
   role) — not a choice you make at the button, so the role returns to the bot you configured.
2. If the pressed bot holds the lease it hands it over exactly as above; if the *named*
   coordinator is the one pressing and the lease has lapsed, it claims the next term itself.

**Expected**

- Exactly one coordinator at every moment: the new claim is term N+1, and a yielded or lapsed
  term cannot be revived by claiming it again (the fold keeps it dead).
- One `CONTROL swarm.lease` line per move, naming the new holder, `value=take`, and your user id.
- One dashboard in the topic: the new holder's. A press on the retired pin answers
  "<alias> coordinates now" and does nothing.
- Two refusals you should see if you try: **Hand lease** from a bot whose lease has lapsed
  answers "does not hold the lease any more", and **Fail back** in a swarm that names no
  coordinator answers "no coordinator is named here".

## 3. Two successors at once

Only meaningful with three or more bots. Kill the coordinator and let two peers in the
succession order claim at the same moment (restart both, or start them together).

**Expected**: two claim files at the same term are written, and the fold still names one holder —
the earlier write, ties broken by succession rank then alias. The other bot yields, and after
its next fetch it stops acting. There is never a second merge, and a bot that lost the fold
never posts a takeover line.

## 4. Freeze a developer mid-PR

1. Hand a small PR to a developer and wait until it says the branch is pushed.
2. `/pause @<developer_handle>`, and leave the hand-off unanswered for one release interval.
3. The topic gets one line: `<alias> has been silent since HH:MM (no pulse and no message for N
   minutes). DOWN is advisory: the lease still decides who coordinates.` — once per episode, not
   once per pass.
4. The bot that handed the work over is asked what happens to it and reassigns the item. The
   round state moves the item to the new owner.
5. `/resume @<developer_handle>`: one `🟢 <alias> is back (a pulse was heard again).` line, and
   nothing else.

## 5. Freeze the reviewer mid-review

1. Ask the reviewer to review an open PR: `@<reviewer_handle> review #<n>`.
2. Pause it before it answers: `/pause @<reviewer_handle>` (an operator pause; a quota freeze
   does the same thing by itself).
3. Wait one release interval. The bot that handed the work over gets a turn saying the reviewer
   is frozen and cannot answer, and reassigns the review to a peer whose role fits — the
   coordinator never takes the review itself in silence.
4. `/resume @<reviewer_handle>`: the reviewer comes back and answers nothing it missed.

**Expected**: one reassignment turn, one new reviewer, no review from the coordinator of its own
merge.

## 6. Kill everything

1. Note the armed timers (`/wakes` on any bot) and the round state (`/round`).
2. Stop every bot with no goodbye. Wait a minute.
3. Start them all again.

**Expected**: each bot's `WAKE` timers are still armed with the same ids and the same `until`;
`/round` prints the same items, owners and verdicts; the lease does not go backwards; no timer
fires twice, and a firing that was cut off mid-turn is replayed once, saying so.

## 7. A WAKE check that never goes green

1. Ask a bot to wait on something that stays pending: `WAKE in=1m every=1m until=5m check="gh pr
   checks <closed-or-pending-pr>" then="merge it if green"`.
2. Each firing re-arms while the check exits "still pending". At `until` the timer expires and the
   bot gets **one** turn: the wait is over, it decides whether to escalate or re-arm, and says
   which.
3. `NO_REPLY` to that turn ends the chain. There is no infinite loop: after the escalation turn
   nothing more fires unless a new `WAKE` is armed.

**Expected**: re-arms until `until`, then one turn, then silence.

## Recording the drill

Post the result in the topic: the drill number, the aliases involved, and what you saw against
the expected line above. Anything that did not match is a bug, not a curiosity — file it, and
link the issue from the round's tracking issue. The safe upgrade and revert path for the
transport itself is in [UPGRADE.md](UPGRADE.md); the channel's delivery rules are in
[PROTOCOL.md](PROTOCOL.md).
