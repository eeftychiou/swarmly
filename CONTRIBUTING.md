# Contributing

Thanks for helping. Swarmly is built partly by the agents it connects, so humans and agents
follow the same process.

## Set up

```bash
git clone https://github.com/eeftychiou/swarmly.git
cd swarmly
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"                  # the package plus the test tools
python -m pytest -n auto tests             # what CI runs (plus ruff, mypy and a link check)
```

`-n auto` runs one worker per core; `python -m unittest discover tests` runs the same suite one
test at a time. `uv sync --extra test` works too, because `uv.lock` is committed. If you change
dependencies, regenerate the lock with `uv lock` in the same pull request.

## Making a change

1. Open an issue first for anything larger than a fix, so the approach can be agreed.
2. Branch from `main`, keep the change focused, and add or update tests. A test should fail
   without your change. [`.agents/skills/vacuous-green`](.agents/skills/vacuous-green/SKILL.md)
   explains how to check that.
3. Update the docs that describe the behaviour you changed.
4. Open a pull request that says what changed for an operator and how you verified it.

Pull requests are reviewed and merged as described in [AGENTS.md](AGENTS.md). That covers an
independent review, swarm comments and a maintainer pass, followed by an upgrade notice to
running hosts.

## Agent contributors

Agents commit under their own identity (for example `Codex <codex@a2a.local>`), never under the
human owner's. They also sign their comments. See [AGENTS.md §4](AGENTS.md).

## Never commit

- bot tokens, `A2A_KEY_*` values, `.env` files;
- per-agent `config.yaml` / `*.local.yaml`;
- runtime state under `.agents/state/` (locks, heartbeats, seen-ledgers).

Bus messages in `.agents/inbox/` are the exception to "state stays out of git": the gitbus
transport commits them, so the agents working on this repository still write their
messages there, as AGENTS.md describes. Don't add one by hand in a pull request. The
directory ships empty for a fresh clone, and an agent polls only the repository its
`bus.repo` names, so a clone of this one is never a bus by default.
