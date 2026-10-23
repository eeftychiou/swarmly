# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A promise that says when it wants looking at is now looked at then (`#213` review). `WAKE` rows
  carry their `in=` as the row's own `deadline`, but the stall ladder measured only `updated_at`:
  a bot told "look again in ten minutes" was first reminded at `limits.stall_minutes` (thirty by
  default) and, with `stall_minutes: 0`, never. `owed_guard.stalls` now takes the row's deadline
  first — one rung when it passes, spent afterwards so a single old deadline cannot climb three
  rungs in three beats — and a ladder that is off still fires the moment a row named. `every=`
  and `until=` are refused with a pointer (nothing repeats and nothing expires: write one `WAIT`
  per thing watched) instead of being dropped silently — `WakeSpec.until_explicit` tells an
  explicit `until=` from the one `parse_line` derives from `in=`, so the refusal cannot hit a
  line that named none. With the ladder off, a spent deadline ends at that one reminder and is
  not chased to the coordinator or the admin: that is what "ladder off" means, and the docs say
  so. `in=` is required by the `WAKE` grammar, so every row this opens has a moment and there is
  no deadline-less case to flag.

- A round-state update that carried **only** a `repo` change wrote no segment at all (`#185`
  review): `fold` took `update.repo`, but `_unchanged` — the check that keeps a repeated turn
  from adding a commit to the bus — never compared it, so the field was folded when it arrived
  beside something else and dropped when it arrived alone. It is compared now, so a repo change
  is written and reaches the bus.

- A full busy queue **holds** a message instead of dropping it (`#184`): past
  `limits.busy_queue_max` the message goes to the durable inbox and is taken, oldest first, as
  each turn ends, so every message is answered in arrival order. The topic is told once per
  window that the bot is behind, with the depth, and `stats.busy_held` counts what used to be
  silently dropped. Holding is bounded by `limits.busy_held_max` (default 50): at the ceiling
  the oldest are kept and the new arrival is dropped with a word in the topic, and a held
  message's inbox record is cleared only once its turn actually exists.

### Removed

- The **WAKE timer path** (`#215`, the last of `#165`): `_wake_loop`, `_wake_tick`, the per-timer
  store, the per-firing claims and settlement window, adoption and replays, `WakeStore`,
  `WakeTimer`, `make_timer`, `decide`, `run_check`, `classify` and `_kill_group`. No turn starts
  from a clock any more: a `WAKE` line becomes a row (`wait_line.from_wake`) and a wake comes only
  from a fact — a row's observation changing (`#163`), a hand-off nobody answered, an orphaned item
  whose next actor cannot be derived (`#164`) — with the stall ladder as the backstop. `/wakes`
  stays as a pointer to `/owed`, `/status` drops `wakes=`, and `limits.wake_max`,
  `wake_tick_seconds`, `wake_check_seconds`, `wake_max_attempts`, `wake_claim_settle_seconds` and
  `wake_check_allow` are gone (`wake_max_per_reply` stays, as the bound on `WAKE` lines per reply).
  The timer tests went with the code — `tests/test_wake.py` keeps the parsing, allowlist and
  migration coverage (21, from 133) — and the drill steps only a timer could exercise are dropped
  while `test_failover` keeps the round-state half of the full-outage drill.

- The coordinator's watchdog (`#165`). `check_coordinator_watch` gave an idle coordinator a turn
  because *time had passed* — the one remaining turn started by a clock rather than a fact. It is
  gone, together with `_note_turn_for_watch`, its `coordinator-watch` state file and
  `limits.coordinator_watch_minutes`; a config that still sets the limit gets an `unknown key,
  ignored` note from `swarmly-telegram check`. What replaces it is the orphan invariant (`#164`):
  an open item whose next actor cannot be derived is a row, and the guard's ladder reminds its
  debtor, then the coordinator, then the admin — so the wake names the item it is about. The wake
  loop stays for one more slice, for timers armed before the migration; it no longer checks in on
  anyone.

### Changed

- A **parent issue closes with its last slice** (round 5.5 `#227`): the poller reads the same
  refs a tracking issue's body lists — the checklist, or "split into #a, #b" — and once every
  one of them is merged (a pull request) or closed (an issue) it closes the parent itself, with
  one line naming the slices (`GitHubClient.close_issue`, comment first). The state of each
  slice decides, never a ticked box: a checklist ticked over an unmerged PR keeps the parent
  open, and an item `gh` could not answer for counts as not landed. The close is a round end
  like a hand close, so the same poll queues the coordinator's report turn, and a parent the
  transport closed is not closed twice. The refs must be **stated, not mentioned**: a
  checklist or a "split into #a, #b" line closes the parent, while a body that only names
  numbers in prose (`#189`'s own shape) closes nothing — that reading would have closed #189
  with #228 and #226 still open. The line is posted once; a close that keeps failing is
  retried without re-posting it. Item states are read with `ref_state` — one `gh api`
  call for an issue *or* a pull request — because this now runs once per poll rather than only
  when an issue closes.

- **Only the lease holder dispatches** (round 5.5 `#227`): the reviewer, developer, QA and
  researcher role texts now say they never brief or assign another bot — they report to the
  coordinator, and the coordinator hands the work out — and the coordinator's text says it is
  the one dispatcher. The 22 Oct cases were a reviewer sending a peer to a PR and a developer
  posting a wake that pinged three bots. `docs/ROLES.md` carries both rules.

- `WAKE` no longer arms a timer (round 5.5 `#165`, second slice). A `WAKE` line is read as the
  row that replaces it — the bot owes itself a look at what the check watched, with the check
  and the action written down and the item the check named, so the guard's ladder (`#164`) and
  the wakes (`#163`) carry it on facts — and the ack names the `WAIT` line to write next time.
  Nothing new enters the timer path; timers armed before this release still fire until they
  expire, and the firing machinery, `/wakes`, the adoption rules and
  `check_coordinator_watch` are removed in the next slice. The developer and coordinator role
  texts now say to declare a `WAIT` where they used to say to arm a `WAKE`.

### Added

- The `WAIT` line (round 5.5 `#165`, first slice): a reply says what it waits on — `WAIT review
  #133 head 38f1977 from Reasonix`, `WAIT ci #173 head 9fb2f21`, `WAIT answer on the Drive
  credentials` — and the transport opens the matching row (verdict, CI, merge, progress, the
  admin's answer) with no timer and no check. A malformed line is refused in the reply with the
  shape that would work, and the line is stripped either way. An `answer` row is keyed by the
  question it asks, so two questions to the admin in one topic are two debts rather than one
  (`#209` review, folded in here because the merge landed at `32205e1`). The coordinator
  watchdog and the separate hand-off reminders are the rest of `#165`.
- A verdict is kept **once per head** (`#185`): the round state already holds the fact, so a
  repeated `review`/`qa` verdict — same `verdict`, `sha` and `by`, even with a newer `at` — writes
  no new segment and is logged as suppressed instead of restated. A verdict on a new head, or
  another bot's verdict, is still written. (The `NO_REPLY` half of the issue landed in `#180`:
  `drop_stray_silence` removes the sentinel anywhere in a reply, and a reply that is only it posts
  nothing.)
  a full queue holds more than it drops, so `busy_dropped=0` alone reads as "nothing was lost"
  while a message waits in the held queue. The `/status` line grows by one token, and the test
  that bounds its length moves with the token set.

- The head under review is frozen (`#183`): a message that claims a PR's head is checked against
  the PR's `headRefOid` and a mismatch is one notice, never a closed row; a verdict still open on
  a head the item no longer has is flagged once as a rewrite under review (the #173 case, where a
  rebuild dropped a fix); and `ready_blockers()` answers a "ready" claim with GitHub's
  `mergeable` fact and the head the verdicts were given on.

### Fixed

- The `/owed` line says the item once and with one preposition (`… owes a review verdict on
  22e68c5 for #170`), and a sweep no longer resets an observation another source made: it
  refreshes the gap it derives for a progress row, and leaves a `ci` row's polled value alone
  (`#199`). An orphan flag row also closes when its item leaves the round, not only when the
  item finds an actor (`#199`).

### Added

- A peer's message that asks several bots at once is one hand-off **per bot** (`#181`): each
  reads the shared context plus its own lines, and none of them refuses with "a peer asks
  another bot to hand this bot the work" -- the line that dropped a request eight times in
  round 5. Two asks on one line are split within the line, a line that *opens* with a run of
  handles is one shared sentence kept whole, a line that only mentions another bot is context
  for everyone, and only the first sentence after a handle counts as its ask --
  a question two sentences later belongs to the bot it is addressed to. A message that asks one
  bot, and merely names another as who should get the work, is still that one bot's.
- An open item whose next actor cannot be derived is **flagged** on its row (`⚑ no next actor: …`
  in `/owed`) instead of looking like one more coordinator debt, and the coordinator is still
  named so the item is not lost (`#194`, the second #164 invariant). The flag is computed on the
  table as the closing facts leave it, so an item only CI speaks for is flagged rather than
  looking live; it clears when the item finds its actor; and the same row's gap moves with the
  item instead of freezing at the day it opened. `/owed` says the item once, not twice.
- Invariants, deadlines and a wake budget (`#164`): an open round item nothing owes opens a row
  for the coordinator carrying the concrete gap (the #146 case, where no WAKE was armed), every
  open PR names its next actor or is flagged, a row no fact has moved for
  `limits.stall_minutes` climbs a three-rung ladder once per rung — a reminder in code, one
  coordinator turn, then the admin — and a round's coordinator wakes are capped per day by
  `limits.round_wake_budget`, pausing them and telling the admin instead of spending through it.
  The notice is said once per round per day, not once per process; the budget counts against the
  round written last (by its own `term`/`seq`/`updated_at`, not the last name); and both settings
  are validated with the rest of `limits`.
- Waking on news (`#163`, first slice): a row wakes the bot that owes it only when the value
  observed for it differs from the value that last woke anyone (`Row.woken`, `wakes`), and the
  wake names the delta -- "a result on c166987 on #146: pending to green" -- instead of "check
  again". A wake needs a bot behind it: `CI` and `admin` rows are observed, never woken, a
  debtor off the roster gets no turn, and a bot wakes only the debts it owes itself -- a peer's
  row is on the table too, but starting a turn for it would spend the news on the wrong bot. An
  escalation is posted by the coordinator alone, the swarm's one voice to the admin. A wake nobody acts on is nudged once after
  `WAKE_GRACE` (ten minutes) and escalated to the admin **once** after `MAX_WAKES`, in
  General -- news afterwards starts the count again. A bot's claim the facts do not support is
  a notice, never a closed row, and a seven-character SHA that the forty-character head agrees
  with is not a contradiction. The transport half wakes in the discussion the row names
  (General for a debt no topic owns), **holds** the wake while that topic already has a turn
  running (a queued wake usually settles the same debt), starts at most two turns a tick, and
  writes what woke whom, and when, onto the row so a restart cannot wake the same bot about
  the same observation twice.
- The expectations table's other five sources (`#174`): a sweep on the heartbeat tick opens a
  new head's CI result and both verdicts, the developer's fix row after a fix-first or QA fail,
  the coordinator's merge row once CI, review and QA agree on one head, progress on unticked
  items, and the admin's question row -- each bound to the fact that opened it. A fact the round
  state already carries closes its row in the same pass, and a beat writes only the rows that
  changed.
- The expectations table (`#162` slice 1): rows saying who owes what — a debtor, what is owed,
  the item and (when it is about one commit) the head, who waits, the fact that opened it, the
  last value seen and a deadline. Facts open and close them, never a bot's word; they are kept
  per writer under `.agents/owed/<alias>/` on the bus and folded when read, so a successor
  inherits the open ones; `/owed` lists them without a model turn. Hand-offs are the first
  source wired (`#174` wires the rest).
- Stuck detection from the CLI's own output (round 5.5 `#161`): the adapter behind each topic
  reads its CLI's stdout and stderr as they arrive and records when the last byte landed, so a
  turn that is running but not progressing shows as `stuck (silent Nm)` in the snapshot, the
  pulse and `/status` — past `limits.turn_silence_minutes` (default 5) — and back to `busy` when
  output resumes. `limits.turn_silence_kill_minutes` (default 0, off) stops such a turn through
  the `/stop` path and tells its topic once. Liveness only: it starts no turn and touches no
  WAKE. A run started with `--output-format stream-json` is read too: the reply is its
  `result` event, and a stream that ended before that event is a failed turn, not a partial
  answer posted as prose.

### Changed

- A WAKE that comes due while its topic already has a turn running is neither claimed nor
  run: it stays armed and is evaluated, expiry included, on the first tick after that turn
  ends (the tick reports `held: turn running`). A running turn often settles the same item,
  so the second turn queued behind it was a paid "nothing changed" (`#160`).

## [0.11.0] - 2026-10-21

### Added

- Self-wake timers: a reply line `WAKE in=10m every=5m until=2h check="gh pr checks 75"
  then="merge it"` arms a timer that the transport runs with no model turn. The check is a
  read-only command from an allowlist, and its exit code decides: go fires one turn, pending
  re-arms, an error or expiry starts one turn. Firings are claimed on the bus so two bots
  never fire the same timer (`#81`). A bot fires its own timers even when it does not hold
  the coordinator lease (`#96`), and timers fire on a git bus (`#100`).
- Acting coordinator: the coordinator role is a lease on the bus with terms, so another bot
  takes over when the coordinator is gone, and every side effect is fenced on the current
  lease (`#83`).
- Durable round state: the round's items and their state live on the bus and survive a
  restart (`#86`).
- `/cleanup`, short-lived notices, and reaping orphaned provider CLIs (`#63`, `#64`, `#68`).
- The online notice names the model and reasoning effort the bot runs at (`#95`), as the CLI
  really runs them: `/model`, then the bot's config, then the CLI's own settings, then the
  model its last turn reported (`#99`).
- Heartbeats: each bot writes a pulse to its own git ref on the bus with no model turn, peers
  that stop pulsing are marked DOWN, and a busy peer's hand-off clock restarts instead of
  drawing a reminder (`#82`).
- A turn cut off by a die-off is resumed once after the restart, never in a loop (`#84`).
- Nothing taken for a turn is lost to a restart: queued messages and unsent replies are kept
  on disk and replayed on the next start. A hand-off to the coordinator gets a reminder from
  its sender, and an idle coordinator with no WAKE gets a check-in turn (`#101`).
- An in-process multi-bot harness with seven failure drills, and a runbook for the live
  drills (`#85`).
- A round now ends with a report to the human in the round's topic, and closing the round's
  tracking issue is what triggers it: closing sends no GitHub notification, so the
  notifications backstop also remembers which issues carrying `github.tracking_label`
  (default `tracking`) were open and wakes the **coordinator** in the project's bound topic
  with a *post the round report now* turn when one closes. One report per closure -- an issue
  that stays closed is quiet, a reopened-and-closed one is a new round end -- and a closure
  whose turn fails stays queued, backing off and then given up loudly like a failed
  notification turn, instead of losing the round end. Only an agent whose `agent.role` is
  `coordinator` is woken, and `tracking_label: ""` disables the watch (`#25`).
- CI wake: while an agent waits on a pull request's checks, the GitHub poll
  (`github.poll_minutes`) watches that PR's check runs and wakes the agent with exactly one
  turn when they all reach a terminal state -- pass or fail, with the failing check names on a
  failure. The agent is watched for a PR it names in a reply (a `.../pull/<n>` link, or a bare
  `#<n>` in a project topic). The watch and the result reported live in
  `.agents/state/gh_ci-<alias>.json`, keyed by head commit and check state, so a restart
  neither forgets a watch nor wakes twice for a run, while a new push is reported again
  (`#24`). A pull request whose rollup is still empty stays pending rather than waking at
  once, and a `statusCheckRollup` entry in the `EXPECTED` state counts as still running rather
  than as passed. The PR's own state is read with the rollup, so a reply that names an
  already-merged or closed PR (a merge notice, an upgrade call) registers no wake: the check
  results arrived for work nothing waits on, and the watch is dropped instead. A watch
  registered by a reply during a wake survives that poll, and a wake in a topic with no
  context id is refused instead of running in the shared General session (`#24` review).

### Changed

- The developer role ends every turn with a push or a WAKE, opens a draft pull request on
  its first turn, and never force-pushes another agent's branch (`#97`).
- Only the admin deploys, and a tracking issue closes only when every item is done (`#65`,
  `#66`).
- The coordinator's role text now says to act in the turn that brings the last missing piece:
  when that turn produces the review merge, the QA pass or the green CI, the coordinator
  merges, ticks the checklist and hands out the next assignment before the turn ends, instead
  of promising to act later ("I'll ping you then"), which is how a round stalls (`#26`).

- `/status` reports the revision the transport **runs** (`rev=`), resolved once at process
  start, and compares it with the checkout's own `HEAD`: after a deploy repoints the checkout
  under a live transport, the process keeps its code and the status answers
  `rev=<running> checkout=<head> drift=restart-needed` until it is restarted. Every envelope
  the process emits carries the same running revision, so the online notice cannot claim a
  revision the process is not executing (`#32`).

- The reply budget is a setting now, not a slogan: `limits.reply_chars` (default 500) is what
  the turn rules ask for and what a reply ticket is held to, and `projects.<key>.reply_chars`
  gives one project's topics their own — a plan-shaped topic can ask for 1500 while the rest of
  the swarm stays at 500. A topic with no override, and every reply ticket (it carries no
  topic), uses the global number; an explicit value above `max_message_chars` is refused at
  load, while the default is clamped to it. A reply ticket carries the budget of the turn that
  raised it, so a topic allowed 1500 is not refused for following its own rules, and the
  ticket's CLI hint states that same number; gitbus turns are held to the global one too. Both
  example configs and `docs/telegram-channel.md` show it (`#31`).

- The reviewer's role text asks for one review pass that lists **every** blocker, instead of
  reporting them one per round-trip, so the author can fix them all in one push. And only the
  coordinator nudges a peer that has gone quiet — once, before it reassigns or escalates;
  every other role reports the stall to the coordinator instead of chasing the peer, so a
  stalled agent is not pinged by three bots at once (`#33`).

- `docs/telegram/PROTOCOL.md` and `docs/ROLES.md` say where the line runs: the ACK-less
  delivery retry (re-send a REQUEST, `/ping` at its `ttl`, mirror to the bus, escalate) belongs
  to whoever sent that REQUEST, while chasing an owner whose *work* has stalled is the
  coordinator's alone. `ROLES.md` calls the two role-wide rules "cross-role" and loses a stray
  blank line (`#55`).

- The role prompts an agent actually receives (`developer`, `reviewer`, `qa-tester`,
  `researcher`) carve the ACK-less delivery retry out of "do not chase a peer": a REQUEST of
  theirs still without an ACK is re-sent, `/ping`ed at its `ttl` and escalated up the ladder —
  a retry is not a nudge. `ROLES.md` uses the same word (`#57`).

- Rung 1 of the retry carve-out in the role prompts reads as the single step `PROTOCOL.md`
  defines — "at `ttl`, re-send the REQUEST with `/ping`" — rather than as two, and the
  coordinator's test now refuses the carve-out in its own prompt, where it would blur the
  rule rather than permit anything (`#59`).

### Fixed

- A bot whose provider says the quota is gone *and* when it comes back freezes on the first
  failure, instead of after `limits.quota_freeze_threshold` failed runs: the peers get
  `FROZEN` while the work can still move, and the notice of the failed turn now replies to the
  message that failed. A plain `429`/rate limit still takes the threshold however long its
  `Retry-After` is, so a half-minute blip does not hand the bot's work away (`#51`).

- Topic memory keeps each brief whole and applies its length cuts only when a recap is
  rendered, so a task posted at length still reads as the full task in the stored record
  rather than being truncated on the way in (`#30`).

- A bare alias in prose names a bot without addressing it: the alias now counts as an
  address only where it reads as one — `@Codex` anywhere, or an alias opening a line as its
  addressee (`Codex, …`, `Hermes: …`, `Codex and Antigravity, …`). Before, "Codex said the
  diff is fine" was enough to append `@codex_bot` to the message, and that appended handle is
  what the peer's transport routes on, so a report that mentioned a peer in passing pulled
  the peer into another bot's exchange. A run of handles closing a message that already
  addresses another bot is now read as the transport's delivery trailer, not as the
  recipient; a hand-off's own handles (issue #27) are in the body and still count (`#28`).

- Every @handle in a peer bot's hand-off counts as addressed, on one line or over several,
  whether it opens the message or sits in a list of three or more bots: a list a bot writes
  is a hand-off, not a roster, so nothing in it is second-guessed. A mention that is still
  dropped (a peer asking another bot to hand the work on, or the bot-to-bot cooldown) is
  reported back to the sender, naming the handle and the reason, so a lost hand-off never
  reads as one that was taken; the report is per sender, so a second peer's dropped hand-off
  inside the same window is still answerable (`#27`).

- A bot frozen on quota no longer goes silent and stays frozen for good. The freeze ends by
  itself at the reset time the provider's error gives (else after `quota_retry_minutes`), and
  a person who addresses the frozen bot is told why and how to resume. `/status` shows the
  freeze, `/model@<bot> <name>` lifts a quota freeze to try the other model (instead of
  promising a turn that never came), and `/resume` tells the peers. Peers keep track of frozen
  bots, show them in `/status`, and every turn's prompt names them so work goes to a bot that
  can take it.

- A turn run from the bus uses the `/model` and `/reasoning` choice of the topic it is about
  (General's when it names none) instead of the CLI's default model.
- A reply that arrived as `↪ (k/n)` parts is joined back into the one message the sender
  wrote before the turn runs, whatever order the parts arrive in, and the turn runs from part
  1: its message id, its reply target and its raw message are the ones the answer goes back
  to. Parts that never all arrive are delivered as what arrived, with a note naming the
  missing ones, instead of being held forever or answered as fragments. Each part's marker
  carries the reply's own token (`(2/3 #9fa0c1)`), so two replies by one sender that
  interleave can never be joined into one mixed message. The token is what a part is joined on:
  a part with no token cannot be attributed to a reply, so a sender that writes none (a
  transport older than this one) is not joined — its part 1 runs its turn as before, and its
  later parts stay continuations that start no turn, never two messages in one turn. A token
  that contradicts itself (another total, different text at an index already held) goes out as
  what arrived rather than joining two replies into one. One cap, `MAX_REPLY_PARTS` (20), bounds
  both ends:
  `limits.max_reply_parts` above it is clamped at load, and a part header above it is refused
  as ordinary text, so no sender can name a total that reserves the receiver's memory; a number
  written with more digits than the marker accepts (three) does not match it at all, so the
  sender's number is never converted at a size the receiver cannot bound. Buffers
  and their timers are bounded and cancelled when a reply is delivered, are not allocated for
  a bot whose messages are ignored anyway (`limits.strangers: ignore`), so numbered fragments
  cannot leave live timers behind. A fragment whose first part never came keeps its `↪ (k/n)`
  marker when the window closes, so it stays a continuation and never starts a turn of its own,
  even when its text names the bot. Parts can arrive while a held reply is being delivered, so
  the buffer is read again after every await: a part that opened a buffer meanwhile is joined
  with its reply instead of being overwritten and lost (`#29`).

## [0.10.1] - 2026-10-17

A correctness round: the suite is hermetic on hosts that run an agent, and the bugs that each
cost a live turn are fixed. No default permission level and no adapter flag changed.

### Added

- A hermetic test suite: `tests/gitenv.py` scrubs the inherited agent environment (guard
  hooks, `SWARMLY_ROLE`, the agent's commit identity) from every git spawn -- including
  per-alias guard directories (`guard-<alias>`) on `PATH` -- an `envguard` pytest plugin
  applies it to the whole run, and CI proves the scrub end to end by running the suite inside
  a fake guard environment (`#7`, `#14`). `tests/test_ci_workflows.py` fails the suite when a
  workflow file stops parsing, instead of leaving GitHub to reject it before any job starts
  (`#7`, `#14`).
- The suite's git-spawn guard reads the AST, so a git call cannot slip past it: literal and
  aliased argv, loop targets, `launch = subprocess.run`, `shell=True` command lines,
  `os.system`/`os.popen`, and an `env=` that only *mentions* `git_env`, splats
  `os.environ` back in, or reaches the helper conditionally are all rejected (`#7`, `#14`).

### Fixed

- A corrupt agent registry now fails loudly instead of loading as an empty registry, which
  silently dropped every peer it named (`#8`, `#15`).
- The Antigravity adapter parses `agy -c` as `--continue`, a flag with no value, instead of
  reading the next token as a conversation id (`#9`, `#16`).
- The Antigravity model-name regex accepts the documented mixed-case engine names, and the
  model reaches `agy` as one unquoted token on Windows as well -- the quoted token the
  command template produced matched no model (`#10`, `#17`, `#21`).
- `init --home-channel` works on a fresh config: the value lands in
  `agent.home_channel_id` instead of the command exiting 2 and asking for a hand edit
  (`#11`, `#18`).
- The GitHub notifications backstop no longer marks a notification read when the turn
  failed. A failed turn leaves it unread so the next poll retries it, with per-thread
  exponential backoff and a loud give-up after three attempts (`#12`, `#19`).
- An agent that was offline while peers joined catches up on them: members already in the
  roster become its peers without `/admit` (`#5`).

## [0.10.0] - 2026-10-16

The one-command join.

### Added

- Join the swarm with one command: an agent takes its alias, role and peers from the roster
  instead of a hand-edited peer list, and roles ship as playbooks (`#3`).
- `docs/JOIN.md` documents the join, including what the live Telegram test changed (`#4`).

### Changed

- Membership trust is tightened: handles appended to a field no longer leak into other
  membership fields, the join launcher checks what it launches, and `/swarm` stays quiet
  while a join is in flight (`#3`, `#4`).
- `/swarm` counts a peer's `SEEN` as online, so a live agent is no longer reported as
  offline between heartbeats (`#3`).

### Removed

- Hermes Agent is no longer presented as a supported, tested runtime (`#2`).

## [0.9.0] - 2026-10-12

First public release.

### Added

- Multi-agent orchestration for coding-agent CLIs (Claude Code, Codex, Antigravity,
  Reasonix, Hermes) over Telegram forum topics and a signed git bus.
- Two planes that need no public IP and no open ports: a durable plane (gitbus in
  `.agents/inbox/`, a synced folder, or Google Drive) and Telegram as the instant plane.
- Signed message envelopes with an offline-capable record that catches up on its own, an
  optional TCP gateway for agents on the same network, and topic-to-project routing with
  GitHub as the durable plane.
- Roles: coordinator, developer, reviewer, QA tester, researcher, or your own.

[Unreleased]: https://github.com/eeftychiou/swarmly/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/eeftychiou/swarmly/compare/v0.10.0...v0.11.0
[0.10.1]: https://github.com/eeftychiou/swarmly/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/eeftychiou/swarmly/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/eeftychiou/swarmly/releases/tag/v0.9.0
