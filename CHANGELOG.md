# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A round now ends with a report to the human in the round's topic, and closing the round's
  tracking issue is what triggers it: closing sends no GitHub notification, so the
  notifications backstop also remembers which issues carrying `github.tracking_label`
  (default `tracking`) were open and wakes the **coordinator** in the project's bound topic
  with a *post the round report now* turn when one closes. One report per closure -- an issue
  that stays closed is quiet, a reopened-and-closed one is a new round end -- and a closure
  whose turn fails stays queued, backing off and then given up loudly like a failed
  notification turn, instead of losing the round end. Only an agent whose `agent.role` is
  `coordinator` is woken, and `tracking_label: ""` disables the watch (`#25`).

### Fixed

- Every @handle in a peer bot's hand-off counts as addressed, on one line or over several,
  whether it opens the message or sits in a list of three or more bots: a list a bot writes
  is a hand-off, not a roster, so nothing in it is second-guessed. A mention that is still
  dropped (a peer asking another bot to hand the work on, or the bot-to-bot cooldown) is
  reported back to the sender, naming the handle and the reason, so a lost hand-off never
  reads as one that was taken; the report is per sender, so a second peer's dropped hand-off
  inside the same window is still answerable (`#27`).

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

[Unreleased]: https://github.com/eeftychiou/swarmly/compare/v0.10.0...HEAD
[0.10.1]: https://github.com/eeftychiou/swarmly/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/eeftychiou/swarmly/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/eeftychiou/swarmly/releases/tag/v0.9.0
