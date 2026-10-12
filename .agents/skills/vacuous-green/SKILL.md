---
name: vacuous-green
description: >-
  Use when a test is green and you are about to trust it - before claiming a fix
  works, before approving a test in review, or when a test has never once failed.
  Covers tests that bypass the path production takes, absence assertions with no
  positive control, and fixtures that never fire under the command CI runs.
---

# Vacuous Green

## Overview

A passing test is not evidence. It is evidence only once you have shown it can
fail for the right reason, arriving the way production arrives.

**Core principle:** validate the test before you validate the code.

*Format after the skills in [obra/superpowers](https://github.com/obra/superpowers) (MIT);
the "Verify RED" step quoted below is from its test-driven-development skill. See
`THIRD_PARTY_NOTICES.md`.*

This is the sibling of TDD's *"Verify RED — watch it fail"*. That asks **did it
fail?** This asks **did it fail for the intended reason, on the path real callers
take?** A test can go red for a typo, a missing import, or an inert fixture — and
go green afterwards having proven nothing.

In this repository the stakes are concrete: `main` is the transport every agent
runs, so a vacuous guard ships a false assurance to the whole swarm.

## The Iron Laws

```
1. NEVER trust a test that has never been red for the intended reason.
2. NEVER test a code path production does not take.
3. NEVER assert absence without first observing the dependent event happen.
```

## When to Use

- Before claiming a fix works, or that a suite "proves" a guard.
- When reviewing someone else's tests (AGENTS.md §1 impartial review).
- When a test has been green since the moment it was written.
- When a fixture, hook, or isolation layer was added to make a test trustworthy.

---

## Anti-Pattern A: Testing a Path Production Never Takes

**The violation:**

```python
# ❌ BAD: builds the dataclass directly, bypassing the loader
def test_rejects_non_finite_window(self):
    with self.assertRaises(ConfigError):
        ChannelLimits(busy_notice_window_seconds=float("nan"))
```

Production never calls the constructor with raw YAML. `AgentConfig.from_dict`
**coerces first**, at `swarmly/telegram/config.py:331-342`:

```
AgentConfig.from_dict
  -> _limit_defaults = ChannelLimits()
  -> _limits_coerced[key] = type(getattr(_limit_defaults, key))(value)   # line 337
  -> limits = ChannelLimits(**_limits_coerced)                           # line 342
  -> ChannelLimits.__post_init__ ... isinstance(window_raw, bool)        # line 194
```

**Why this is wrong:**

- The coercion on line 337 runs **before** the `__post_init__` guard on line 194.
  It is `type(default)(value)` — so a YAML `bool` for a float field becomes
  `float(True) == 1.0`, and a string for a bool field becomes `bool("no") == True`.
  By the time the guard executes, the type it rejects can no longer be observed
  through the loader.
- The test enters at `__post_init__`; production enters at `from_dict`. The test
  *is* red when the guard is reverted — so it survives red-green review while the
  real path stays broken. This is the most dangerous shape: it looks diligent.
- Generalised: **a guard placed after a coercion step is not validated where it
  sits.**

Measured through the loader on this head (`AgentConfig.from_dict`), the defect is
still live even though the `nan` case *does* raise:

```
limits.busy_notice_window_seconds = nan        ConfigError (finite number)   ← guard fires
limits.busy_notice_window_seconds = 'abc'      ConfigError (must be a float) ← coercion fires
limits.busy_notice_window_seconds = True       ACCEPTED -> 1.0               ← DEFECT
limits.respond_without_mention    = 'no'       ACCEPTED -> True              ← DEFECT
limits.respond_without_mention    = 'false'    ACCEPTED -> True              ← DEFECT
limits.commands_enabled           = 'off'      ACCEPTED -> True              ← DEFECT
```

Note the trap: `nan` fails, so a test built on raw-constructor `nan` *looks* like
it covers the loader. It does not — the values that matter are the ones the
coercion converts into something valid.

```python
# ✅ GOOD: enter through the same door production uses
def test_rejects_non_finite_window_via_loader(self):
    with self.assertRaises(ConfigError):
        AgentConfig.from_dict({
            "agent": {"alias": "probe", "name": "Probe", "bot_token": "x"},
            "limits": {"busy_notice_window_seconds": float("nan")},
        })
```

The `agent` block is not decoration. Drop it and `assertRaises(ConfigError)` is
still satisfied — by *"agent.alias is required"* — so the test passes for a reason
unrelated to windows:

```
guard intact    loader + nan  + agent block -> RED (the window)          ← real
guard intact    loader + nan, no agent block -> RED (agent.alias required) ← vacuous
guard removed   loader + nan  + agent block -> GREEN                     ← as expected
guard removed   loader + nan, no agent block -> RED                      ← caught nothing
```

Measured. This is why "enter through the loader" is not sufficient on its own:
the loader path must also be the path that raises.

**But entering through the loader does not by itself catch the defect.** With a
valid payload, `nan` still fails on the guard, and removing the guard makes it
pass — which reads as a correct red-green cycle. The values this pattern is
actually about are the ones coercion *launders into something valid*, and those
are only caught by asserting on the boundary:

```python
# ⚠️ RED today - and that is the point. The defect is real, so this test fails
# on current main. It goes green only when the loader stops laundering `true`
# into 1.0. Do not "fix" it by relaxing the assertion.
def test_bool_window_is_not_silently_accepted(self):
    with self.assertRaises(ConfigError):
        AgentConfig.from_dict({
            "agent": {"alias": "probe", "name": "Probe", "bot_token": "x"},
            "limits": {"busy_notice_window_seconds": True},
        })
```

This is a third kind of test, distinct from the two above: not "green and
vacuous", but "red and honest". A test that documents an open defect must be
observed **failing**, and a reviewer seeing it pass should ask what changed.

Both examples above are worth keeping — the unit test pins the guard, the loader
test pins the composite, and the boundary test pins the actual defect. The loader
and boundary tests are the evidence about production.

### Gate Function

```
BEFORE trusting any test:
  Ask: "What entry point does production use for this behaviour?"
       (from_dict / from_file / the CLI parser / handle_event / the public API)
  Ask: "Does this test enter through that same entry point?"

  IF the test constructs the object or calls an internal helper directly:
    STOP - add (or rewrite to) a test that enters through the production entry
    point, THEN re-run the red-green cycle on the rewritten test

  Ask: "Does anything ELSE in this payload raise the same exception?"
       assertRaises(Exception) passes on any failure, including a typo in the
       fixture. A valid payload is part of the assertion.

  Ask: "Is there a mutation that makes THIS test fail?"
       IF no input change can turn it red: the test is decorative.
```

An `assertRaises` is only as specific as its payload. The two examples above were
both wrong on first writing for exactly these reasons, which is why the rule is
here rather than implied.

---

## Anti-Pattern B: Absence Without a Positive Control

**The violation:**

```python
# ❌ BAD: asserts nothing leaked - but never shows it COULD have
def test_fixture_cleans_up_environment(self):
    AgentConfig.from_dict(payload)
    self.assertNotIn("A2A_PROBE_LEAK", os.environ)
    # green. But green because the leak was cleaned up, or because the .env that
    # was supposed to cause it registered a mangled key instead? The test cannot
    # tell those apart, and neither can you.
```

**Why this is wrong:**

- An absence assertion passes for *every* reason the prerequisite failed to
  occur. It cannot distinguish "the cleanup worked" from "the thing never
  happened."
- Measured in this repo: `tests/envguard.py` wraps each test so `os.environ` is
  restored afterwards, and it attaches through a module-level `load_tests` hook.
  A probe written to exercise it printed
  `PROBE_LEAK_OBSERVED = 'leaked'` *and* `PROBE_VAR_VISIBLE_IN_LATER_TEST = False`
  — the positive control is what makes the second line meaningful. Without it the
  same green line would have been produced by a probe that never ran.
- Setup artifacts *you wrote yourself* are the most common source. Measured here:
  a `.env` written with a UTF-8 BOM does not fail. `load_env` strips each line but
  `str.strip()` does not remove `\ufeff`, so the key is registered under a **mangled
  name** (`\ufeffA2A_PROBE_LEAK`) while the name the test asks about stays absent.
  The negative assertion is then green because the variable never existed under that
  name — not because anything was cleaned up. No error is raised anywhere.

**The fix:**

Keep the control and the assertion in the **same** test, and make the test
provide the effect it depends on. Driving the subject directly keeps it
self-contained — no `.env` on disk, no cross-test ordering, no reliance on
whatever any earlier test happened to do:

```python
# ✅ GOOD: one test; the prerequisite is created here, not borrowed from another test
def test_the_guard_restores_the_environment(self):
    observed = {}

    class Leaky(unittest.TestCase):
        def runTest(self):
            os.environ["A2A_PROBE_LEAK"] = "leaked"
            observed["during"] = os.environ.get("A2A_PROBE_LEAK")

    envguard.wrap(Leaky()).run(unittest.TestResult())

    self.assertEqual(observed["during"], "leaked")   # positive control
    self.assertNotIn("A2A_PROBE_LEAK", os.environ)    # and it was restored
```

Delete the `envguard.wrap(...)` and the first assertion still passes while the
second fails — which is the mutation that shows the assertion is load-bearing.
A version that borrows its prerequisite from a different test method passes or
fails depending on execution order, so it proves nothing in either direction.

### Gate Function

```
BEFORE writing assertNotIn / assertFalse / assertIsNone / count == 0:
  Ask: "For this to be meaningful, what must have HAPPENED first?"
  Add an assertion that the prerequisite happened.
  Watch the absence assertion FAIL when you remove the thing under test.

  Ask: "Can this test run ALONE and still mean something?"
  IF it borrows its prerequisite from another test:
    STOP - it is order-dependent. Create the prerequisite in this test.
  IF the prerequisite cannot be made to happen here:
    STOP - the test is testing nothing. Fix the setup or delete the test.
```

**Hygiene:** when a fixture file must be byte-exact, do not write it with a tool
that adds a BOM. Assert on the raw bytes (`Path.read_bytes()[:3]`) rather than
trusting the writer.

---

## Anti-Pattern C: A Fixture That Never Fires

**The violation:**

```
"I moved the isolation hook into tests/__init__.py, so it now covers every module."
```

**Why this is wrong:**

- Discovery hooks differ by invocation. Measured in this repo, at a known head,
  against the command CI actually runs (`python -m unittest discover tests -v`,
  from `.github/workflows/tests.yml:26`):
  - a **per-module** `load_tests` in the test file itself — **fires**;
  - a **package-level** `tests/__init__.py:load_tests` — **does not fire**
    (instrumented with a marker; the marker never printed);
  - `tests/conftest.py` — **never fires** under `unittest` at all. pytest calls it;
    `unittest discover` does not. A repo that isolates via `conftest.py` while CI
    runs `unittest` has no isolation in CI, and no error either.
- `tests/envguard.py` (issue #106) is the working mechanism here, and its
  docstring records both halves of this: it is attached by a two-line module-level
  `load_tests` wrapper, and it is **inert under pytest** because pytest never
  calls `load_tests`. One mechanism, two runners, opposite outcomes.
- Every one of these failures is silent. The suite is green; the guard is absent.

The measurement, run verbatim as `python -m unittest discover tests -v` in a
project root, with a probe module and a probe `.env` added temporarily:

```
RUN 1: module-level load_tests  (tests/test_zzz_probe.py)
    PROBE_MODULE_LOAD_TESTS_FIRED
    test_a_prerequisite_actually_happens ... PROBE_LEAK_OBSERVED = 'leaked'
    test_b_next_test_sees_a_clean_environment ... PROBE_VAR_VISIBLE_IN_LATER_TEST = False
    Ran 418 tests in 11.109s

RUN 2: package-level load_tests  (tests/__init__.py, probe module removed)
    PROBE_PACKAGE_LOAD_TESTS_FIRED present: False
    Ran 416 tests in 10.777s
```

Read it as three separate facts, because each one alone is a trap:

1. `PROBE_MODULE_LOAD_TESTS_FIRED` — the per-module hook really does run.
2. `PROBE_LEAK_OBSERVED = 'leaked'` — the prerequisite really happened. **Without
   this line, `PROBE_VAR_VISIBLE_IN_LATER_TEST = False` means nothing**; see
   Anti-Pattern B.
3. `PROBE_PACKAGE_LOAD_TESTS_FIRED present: False` — the package-level hook is
   absent, the file still imported and the suite still passed, and nothing
   complained. A hook that fails to attach is indistinguishable from a hook that
   was never needed.

**The fix:**

```
✅ Prove the fixture fires under the EXACT command CI runs:
   1. Add a marker:  print("FIXTURE_FIRED", file=sys.stderr)
   2. Run CI's command verbatim (read it from .github/workflows - don't guess)
   3. See the marker. THEN rely on the fixture.
   4. Remove the marker; keep the proof in the PR body.
```

### Gate Function

```
BEFORE relying on any fixture, hook, plugin, or autouse behaviour:
  Ask: "What command does CI actually run?"
       (read the workflow file - do not assume it matches your local command)
  Ask: "Which test runner is that command using?"
       (a pytest hook is invisible to unittest, and vice versa)
  Ask: "Have I SEEN this fixture fire under that command?"

  IF unproven:
    STOP - instrument it with a marker and run CI's command verbatim.
    A guard you have not watched fire is not a guard.
```

---

## Red Flags

- A guard whose test builds the object directly instead of loading it
- A validation that runs *after* a coercion/transform step, tested only in isolation
- `assertNotIn` / `assertFalse` / `assertIsNone` with no positive control nearby
- A test that only means something when another test ran first
- An assertion whose subject is never created in the test that asserts on it
- "The fixture handles that" — never observed firing
- A pytest-only hook (`conftest.py`, `autouse` fixtures) as the isolation layer
  while CI runs `unittest`
- A test that has never been red for the intended reason
- No test in the diff that goes red when the fix is reverted
- A setup file whose first byte you never checked
- Tests added with the fix, green on their first ever run

## Quick Reference

| Anti-Pattern | Fix |
|--------------|-----|
| Test bypasses the loader/entry point | Enter through the same door production uses |
| Guard sits after a coercion step | Test the composite, or test the boundary |
| Absence assertion, no positive control | Assert the event fired, then assert absence |
| Absence assertion depends on another test | Create the prerequisite in the same test |
| Fixture never proven to fire | Instrument with a marker, run CI's command verbatim |
| Test only ever green | Revert the fix; watch it go red for the right reason |
| BOM/encoding-sensitive setup | Assert on the raw bytes, not the writer's promise |

## Evidence Pattern (how to prove it in the PR)

Report it as a table, not a claim. Format (illustrative):

```
mutation                          new test alone    PR-head test file
seen = handled + ignored + 10     RED 13 != 3       GREEN (invisible)
token hardcoded to 1              RED 1 != 3        GREEN (invisible)
```

Run each mutation against the **new test alone** so the failure is attributable
to the new assertion rather than a neighbouring test. A mutation the new test
misses but the file catches is still a finding — record it.

For fixture claims, the equivalent evidence is the marker output pasted into the
PR body, next to the workflow line and the command it was run with.

**This applies to the examples in this file too.** An illustrative snippet nobody
executed is a vacuous test: it asserts that the technique works while never having
run it. Every code sample here was run as written, and the ones claiming to catch
something were run twice — once as written (must pass) and once with the guard
removed (must fail).

## The Bottom Line

**A green test you have not validated is a claim, not evidence.**

Before trusting it: name the path it takes, show the event it depends on, and
revert the fix to watch it fail. Then — and only then — call it passing.
