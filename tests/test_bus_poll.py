"""
Tests for the durable bus poller introduced by issue #59.

The new ``bus-poll --once`` CLI subcommand and the in-transport ``check_gitbus()``
share a single ``bus_poll_once`` core. These tests exercise that core, the new
``bus:`` config section (with ``fallback.gitbus_*`` alias precedence), the
freeze semantics, the adapter-level ``verify_delivery`` hook, the offline echo
skip, broadcasts, and the single-writer lock.

All tests run without a Telegram bot token, without aiogram, and without
network calls to Telegram.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional
from unittest.mock import Mock, patch

from swarmly.models import A2AMessage, MessageIntent, MessagePriority, RecipientInfo, SenderInfo
from swarmly.telegram.adapters import EchoAdapter, build_adapter
from swarmly.telegram.adapters.base import AdapterReply, AgentAdapter, IncomingMessage
from swarmly.telegram.channel import AgentChannel, ChannelEvent
from swarmly.telegram.config import AdapterSettings, AgentConfig, ConfigError
from swarmly.telegram import poll as poll_module
from swarmly.telegram.poll import (
    PollOutcome,
    PollSettings,
    acquire_poll_lock,
    bus_poll_once,
    heartbeat_status,
    poll_lock_path,
    release_poll_lock,
)
from swarmly.telegram.wire import Envelope, MessageType


GROUP = -1001234567890
ADMIN_ID = 1234567890


def make_config(**overrides) -> AgentConfig:
    data = {
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "",
                  "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
        "peers": {"Codex": "CodexBot", "Antigravity": "AntigravityBot"},
        "adapter": {"type": "echo"},
        "limits": {"min_seconds_between_messages": 0, "min_seconds_between_agent_turns": 0},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return AgentConfig.from_dict(data)


def bus_message(message_id, sender, recipient, text, intent="QUERY_REQUEST"):
    return A2AMessage(
        protocol="a2a/v1.0", message_id=message_id, timestamp_utc="2026-09-19T00:00:00+00:00",
        sender=SenderInfo(agent_id=sender, model_family="custom", framework="custom"),
        recipient=RecipientInfo(agent_id=recipient),
        intent=MessageIntent(intent), priority=MessagePriority.ROUTINE, signature="",
        payload={"text": text})


class FakeSender:
    def __init__(self) -> None:
        self.sent: List[dict] = []
        self.fail_next = False

    async def __call__(self, payload, reply_to_message_id=None):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("telegram unavailable")
        self.sent.append({"text": payload, "reply_to": reply_to_message_id})
        return payload


class FakeClock:
    """Deterministic injected clock for the settle window (issue #80).

    ``sleep`` records every requested delay and advances ``now`` instead of
    waiting, so a probe can observe time passing without touching the wall clock.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: List[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def no_settle(_seconds: float) -> None:
    """Injected sleep for probe tests: never wait on the real clock (issue #80)."""


# --------------------------------------------------------------------------------------
# Config: bus: section + alias precedence (issue #59 deliverable 3)
# --------------------------------------------------------------------------------------
class TestBusConfigSection(unittest.TestCase):
    """First-class bus: config with fallback.gitbus_* alias precedence."""

    def test_bus_section_defaults(self):
        cfg = make_config()
        # New first-class settings exist with spec defaults.
        self.assertTrue(hasattr(cfg, "bus"))
        self.assertEqual(cfg.bus.poll_minutes, 30)
        self.assertEqual(cfg.bus.ref, "origin/main")
        self.assertTrue(cfg.bus.enabled)
        # Review P0-4: no default bus; unset, nothing is polled.
        self.assertIsNone(cfg.bus.repo)

    def test_bus_section_parses_explicit_keys(self):
        cfg = make_config(bus={"repo": "/tmp/repo", "ref": "origin/dev",
                                "poll_minutes": 7, "enabled": False})
        self.assertEqual(cfg.bus.repo, "/tmp/repo")
        self.assertEqual(cfg.bus.ref, "origin/dev")
        self.assertEqual(cfg.bus.poll_minutes, 7)
        self.assertFalse(cfg.bus.enabled)

    def test_verify_settle_seconds_is_declared_under_bus(self):
        """Issue #80: the probe settle window is a bus: config key."""
        self.assertEqual(make_config().bus.verify_settle_seconds, 3.0)
        self.assertEqual(
            make_config(bus={"verify_settle_seconds": 1.5}).bus.verify_settle_seconds,
            1.5)
        with self.assertRaises(ConfigError):
            make_config(bus={"verify_settle_seconds": "soon"})

    def test_fallback_aliases_are_accepted(self):
        cfg = make_config(fallback={"gitbus_poll_minutes": 12, "gitbus_ref": "origin/alt",
                                     "gitbus_root": "/srv/project"})
        self.assertEqual(cfg.bus.poll_minutes, 12)
        self.assertEqual(cfg.bus.ref, "origin/alt")
        self.assertEqual(cfg.bus.repo, "/srv/project")
        # Backward-compat FallbackSettings still populated for the legacy path.
        self.assertEqual(cfg.fallback.gitbus_poll_minutes, 12)
        self.assertEqual(cfg.fallback.gitbus_ref, "origin/alt")
        self.assertEqual(cfg.fallback.gitbus_root, "/srv/project")

    def test_bus_section_wins_over_fallback_aliases(self):
        """When both sections set the same key, the bus: section takes precedence."""
        cfg = make_config(
            bus={"poll_minutes": 5, "ref": "origin/main", "repo": "/from/bus"},
            fallback={"gitbus_poll_minutes": 99, "gitbus_ref": "origin/legacy",
                      "gitbus_root": "/from/legacy"},
        )
        self.assertEqual(cfg.bus.poll_minutes, 5)
        self.assertEqual(cfg.bus.ref, "origin/main")
        self.assertEqual(cfg.bus.repo, "/from/bus")

    def test_validation_rejects_out_of_range_poll_minutes(self):
        cfg = make_config(bus={"poll_minutes": 200})
        problems = cfg.validate(require_token=False)
        self.assertTrue(any("bus.poll_minutes" in p for p in problems))

    def test_validation_zero_poll_minutes_is_disabled(self):
        cfg = make_config(bus={"poll_minutes": 0})
        problems = cfg.validate(require_token=False)
        # Zero is a valid disabled state, not a validation error.
        self.assertFalse(any("bus.poll_minutes" in p for p in problems))
        self.assertFalse(cfg.bus.enabled)

    def test_validation_negative_poll_minutes_is_rejected(self):
        cfg = make_config(bus={"poll_minutes": -1})
        problems = cfg.validate(require_token=False)
        self.assertTrue(any("bus.poll_minutes" in p for p in problems))

    def test_validation_string_poll_minutes_is_rejected(self):
        with self.assertRaises(ConfigError):
            make_config(bus={"poll_minutes": "soon"})

    def test_validation_names_legacy_key_when_only_fallback_set(self):
        """Review N1 (round 3): name the key the operator actually wrote.

        The resolved value is mirrored into ``fallback``, so a legacy-only bad
        value must report the legacy key — not ``bus.poll_minutes``, which the
        operator never wrote.
        """
        cfg = make_config(fallback={"gitbus_poll_minutes": 500})
        problems = cfg.validate(require_token=False)
        self.assertTrue(any("fallback.gitbus_poll_minutes" in p for p in problems),
                        msg=str(problems))
        self.assertFalse(any("bus.poll_minutes must be" in p for p in problems),
                         msg=str(problems))

    def test_validation_names_bus_key_when_bus_section_set(self):
        cfg = make_config(bus={"poll_minutes": 500})
        problems = cfg.validate(require_token=False)
        self.assertTrue(any("bus.poll_minutes must be" in p for p in problems),
                        msg=str(problems))
        self.assertFalse(any("fallback.gitbus_poll_minutes" in p for p in problems),
                         msg=str(problems))


# --------------------------------------------------------------------------------------
# Single-writer lock (deliverable 4)
# --------------------------------------------------------------------------------------
class TestPollLock(unittest.TestCase):
    """fcntl/msvcrt lock around the load → deliver → save window."""

    def test_lock_path_is_inside_state_dir(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = poll_lock_path(root, "hermes")
            self.assertTrue(str(lock).endswith(".poll.lock"))
            self.assertIn(".agents", str(lock))

    def test_second_acquirer_is_rejected_within_ttl(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            held = acquire_poll_lock(root, "hermes")
            self.assertTrue(held)
            again = acquire_poll_lock(root, "hermes")
            self.assertFalse(again)
            release_poll_lock(root, "hermes")

    def test_lock_released_then_reacquired(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(acquire_poll_lock(root, "hermes"))
            release_poll_lock(root, "hermes")
            self.assertTrue(acquire_poll_lock(root, "hermes"))
            release_poll_lock(root, "hermes")

    def test_expired_lock_can_be_reclaimed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Pretend the previous holder died long ago by stamping the marker file.
            lock = poll_lock_path(root, "hermes")
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(json.dumps({"acquired_at": time.time() - 9999,
                                        "owner": "ghost"}), encoding="utf-8")
            self.assertTrue(acquire_poll_lock(root, "hermes"))
            release_poll_lock(root, "hermes")


# --------------------------------------------------------------------------------------
# Shared poll core: bus_poll_once (deliverables 1, 2, 5, 6, 7, 8)
# --------------------------------------------------------------------------------------
@patch("swarmly.telegram.poll._needs_baseline", new=lambda *_args: False)   # later cycles
class TestBusPollOnce(unittest.TestCase):
    """The shared load → inject → ledger core used by both CLI and transport."""

    def _make(self, *, messages, poll_minutes=10, bus_root=None, telegram=None,
              frozen=False, sender=None, adapter=None, verify_settle_seconds=None):
        bus = {"poll_minutes": poll_minutes,
               "repo": bus_root or tempfile.mkdtemp()}
        if verify_settle_seconds is not None:
            bus["verify_settle_seconds"] = verify_settle_seconds
        cfg = make_config(bus=bus)
        root = Path(cfg.bus.repo)
        # Pull/read mocks so we don't need a real git repo.
        pulled = []
        def puller(_root):
            pulled.append("pull")

        def reader(_root):
            return [(m, True, "ok") for m in messages]

        channel = AgentChannel(
            cfg,
            adapter=adapter or EchoAdapter("Hermes"),
            sender=sender or FakeSender(),
            gitbus_pull=puller,
            gitbus_read=reader,
        )
        channel._frozen = frozen
        return channel, cfg, root, pulled

    def test_disabled_poll_returns_empty(self):
        with TemporaryDirectory() as tmp:
            channel, cfg, root, pulled = self._make(
                messages=[bus_message("m1", "codex", "hermes", "hi")],
                poll_minutes=0, bus_root=tmp)
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertEqual(outcome.delivered, [])
            self.assertEqual(outcome.skipped, "disabled")
            self.assertEqual(pulled, [])

    def test_cycle_writes_a_heartbeat_for_the_dead_man_watch(self):
        """Issue #68: a completed cycle advances .agents/state/<alias>.heartbeat.json."""
        with TemporaryDirectory() as tmp:
            channel, cfg, root, pulled = self._make(
                messages=[bus_message("m1", "codex", "hermes", "hi")], bus_root=tmp)
            outcome = asyncio.run(bus_poll_once(channel))

            heartbeat = Path(tmp) / ".agents" / "state" / "hermes.heartbeat.json"
            self.assertTrue(heartbeat.exists(), "heartbeat file missing after a cycle")
            data = json.loads(heartbeat.read_text(encoding="utf-8"))
            self.assertEqual(data["alias"], "Hermes")
            self.assertGreater(float(data["at"]), 0.0)
            self.assertEqual(data["delivered"], len(outcome.delivered))
            self.assertEqual(data["deferred"], len(outcome.deferred))
            self.assertEqual(data["escalations"], len(outcome.escalations))

    def test_heartbeat_status_is_file_canonical_and_flags_stale(self):
        """Issue #68 contract: the file is canonical; stale when now-at > factor x interval."""
        with TemporaryDirectory() as tmp:
            cfg = make_config(bus={"poll_minutes": 30, "repo": tmp})
            path = Path(tmp) / ".agents" / "state" / "hermes.heartbeat.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()

            path.write_text(json.dumps({"at": now - 60, "alias": "Hermes", "delivered": 3}),
                            encoding="utf-8")
            fresh = heartbeat_status(cfg, now=now)
            self.assertFalse(fresh["stale"])
            self.assertAlmostEqual(fresh["age_seconds"], 60, delta=5)
            self.assertEqual(fresh["threshold_seconds"], 3 * 1800)
            self.assertEqual(fresh["counters"]["delivered"], 3)

            path.write_text(json.dumps({"at": now - 3 * 1800 - 1, "alias": "Hermes"}),
                            encoding="utf-8")
            stale = heartbeat_status(cfg, now=now)
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["detail"], "stale beat")

        with TemporaryDirectory() as tmp2:
            cfg2 = make_config(bus={"poll_minutes": 30, "repo": tmp2})
            missing = heartbeat_status(cfg2, now=time.time())
            self.assertFalse(missing["present"])
            self.assertTrue(missing["stale"])          # never beat => stale
            self.assertEqual(missing["detail"], "no heartbeat recorded yet")

    def test_no_token_is_required(self):
        """No Telegram token AND no sender — the poller must still run cleanly."""
        with TemporaryDirectory() as tmp:
            cfg = make_config(bus={"poll_minutes": 10, "repo": tmp})
            # No sender — the transport is OFFLINE, so the CLI must not need it.
            cfg.identity.bot_token = ""
            msgs = [bus_message("m1", "codex", "hermes", "hi")]

            def puller(_root):
                pass

            def reader(_root):
                return [(m, True, "ok") for m in msgs]

            channel = AgentChannel(
                cfg,
                adapter=EchoAdapter("Hermes"),
                sender=None,                                # no transport at all
                gitbus_pull=puller,
                gitbus_read=reader,
            )
            self.assertFalse(cfg.identity.token_is_present())
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertEqual(outcome.delivered, ["m1"])
            # No Telegram echo attempted — the sender was None.
            self.assertFalse(outcome.telegram_unavailable)

    def test_addressed_messages_are_delivered_via_adapter(self):
        with TemporaryDirectory() as tmp:
            msgs = [bus_message("m1", "codex", "hermes", "first"),
                    bus_message("m2", "codex", "codex", "not mine")]
            channel, cfg, root, _ = self._make(messages=msgs, bus_root=tmp)
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertEqual(outcome.delivered, ["m1"])
            seen = json.loads((root / ".agents" / "state" / "seen-hermes.json")
                              .read_text(encoding="utf-8"))["seen"]
            self.assertIn("m1", seen)

    def test_ledger_persists_across_polls(self):
        with TemporaryDirectory() as tmp:
            msgs = [bus_message("m1", "codex", "hermes", "first")]
            channel, cfg, root, _ = self._make(messages=msgs, bus_root=tmp)
            asyncio.run(bus_poll_once(channel))
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertEqual(outcome.delivered, [])
            self.assertEqual(outcome.skipped, "already_delivered")

    def test_corrupt_ledger_is_quarantined(self):
        with TemporaryDirectory() as tmp:
            state = Path(tmp) / ".agents" / "state"
            state.mkdir(parents=True)
            (state / "seen-hermes.json").write_text("{bad", encoding="utf-8")
            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "after")],
                bus_root=tmp)
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertEqual(outcome.delivered, ["m1"])
            self.assertTrue(list(state.glob("seen-hermes.json.corrupt-*")))

    # -- deliverable 7: offline echo skip -------------------------------------------
    def test_offline_skips_telegram_echo_and_marks_telegram_unavailable(self):
        with TemporaryDirectory() as tmp:
            sender = FakeSender()
            sender.fail_next = True
            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "hello")],
                bus_root=tmp, sender=sender)
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertEqual(outcome.delivered, ["m1"])
            self.assertFalse(outcome.telegram_ok)
            self.assertTrue(outcome.telegram_unavailable)
            # Adapter still received the message; only the echo was skipped.
            self.assertEqual(len(outcome.adapter_replies), 1)
            # No second attempt to queue the echo.
            self.assertEqual(sender.sent, [])
            # The planted token for this delivery is the message id
            # (no reply-store ticket for the bus path).
            self.assertEqual(outcome.tokens.get("m1"), "m1")

    # -- deliverable 5: freeze stays bus-visible -------------------------------------
    def test_freeze_status_transitions_apply_during_poll(self):
        """While frozen, the poller still reads/ledgers and applies FREEZE/RESUME."""
        with TemporaryDirectory() as tmp:
            msgs = [
                bus_message("fr-1", "admin", "hermes",
                            "[A2A v1.1] STATUS to=hermes intent=FREEZE id=fr-1"),
                bus_message("fr-2", "admin", "hermes",
                            "[A2A v1.1] STATUS to=hermes intent=RESUME id=fr-2"),
            ]
            channel, cfg, root, _ = self._make(messages=msgs, bus_root=tmp, frozen=True)
            # Admin-authored envelopes so the freeze check passes the auth gate.
            outcome = asyncio.run(bus_poll_once(channel,
                                                author_admin_id=ADMIN_ID))
            # FREEZE/RESUME control traffic must apply — even while initially frozen.
            self.assertFalse(channel._frozen,
                             "RESUME envelope while frozen must unfreeze")
            self.assertIn("fr-1", outcome.delivered)
            self.assertIn("fr-2", outcome.delivered)

    def test_freeze_defers_actionable_but_not_admin(self):
        """Frozen agent keeps FREEZE/RESUME flowing; defers actionable body messages."""
        with TemporaryDirectory() as tmp:
            actionable = bus_message("a1", "codex", "hermes",
                                     "please do work")
            admin_status = bus_message("s1", "admin", "hermes",
                                       "[A2A v1.1] STATUS to=hermes intent=FREEZE id=s1")
            channel, cfg, root, _ = self._make(
                messages=[actionable, admin_status], bus_root=tmp, frozen=True)
            # Pre-stamp the actionable id as deferred so the test is deterministic.
            deferred_path = root / ".agents" / "state" / "deferred-hermes.json"
            deferred_path.parent.mkdir(parents=True, exist_ok=True)
            deferred_path.write_text(json.dumps({"deferred": ["a1"]}),
                                      encoding="utf-8")
            outcome = asyncio.run(bus_poll_once(channel,
                                                author_admin_id=ADMIN_ID))
            # Admin/STATUS is recorded as delivered; actionable stays deferred.
            self.assertIn("s1", outcome.delivered)
            self.assertNotIn("a1", outcome.delivered)
            self.assertIn("a1", outcome.deferred)

    def test_unfreeze_drains_deferred_messages(self):
        with TemporaryDirectory() as tmp:
            msgs = [
                bus_message("a1", "codex", "hermes", "work now"),
                bus_message("res", "admin", "hermes",
                            "[A2A v1.1] STATUS to=hermes intent=RESUME id=res"),
            ]
            channel, cfg, root, _ = self._make(
                messages=msgs, bus_root=tmp, frozen=True)
            # Stash a previously-deferred actionable.
            deferred_path = root / ".agents" / "state" / "deferred-hermes.json"
            deferred_path.parent.mkdir(parents=True, exist_ok=True)
            # Include 'res' in deferred too — RESUME itself must still apply.
            deferred_path.write_text(json.dumps({"deferred": ["a1", "res"]}),
                                      encoding="utf-8")
            outcome = asyncio.run(bus_poll_once(channel,
                                                author_admin_id=ADMIN_ID))
            self.assertFalse(channel._frozen)
            # Deferred actionable drained (delivered on resume).
            self.assertIn("a1", outcome.delivered)

    def test_deferred_payload_survives_window_rollover(self):
        """A deferred message that left the fetch window still delivers on drain.

        Codex review of #60: deferred ids used to be bare strings re-found in
        the window — once the message rolled out of the fetch window it was
        lost. Payloads are now captured at defer time.
        """
        with TemporaryDirectory() as tmp:
            # Empty window: the deferred message is NOT fetchable anymore.
            channel, cfg, root, _ = self._make(messages=[], bus_root=tmp)
            deferred_path = root / ".agents" / "state" / "deferred-hermes.json"
            deferred_path.parent.mkdir(parents=True, exist_ok=True)
            deferred_path.write_text(json.dumps({"deferred": [
                {"id": "gone-1", "text": "captured before unfreeze",
                 "sender": "codex", "accepted": True}]}), encoding="utf-8")
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertIn("gone-1", outcome.delivered)
            # The drained entry is removed: an empty store is deleted.
            self.assertFalse(deferred_path.exists())

    def test_drained_injections_are_verified(self):
        """Drain path runs the verify hook — no seen-without-verify (#60)."""
        with TemporaryDirectory() as tmp:
            class ProbeNoAdapter(EchoAdapter):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    self.calls = 0

                async def verify_delivery(self, message, *a, **kw):
                    self.calls += 1
                    return AdapterReply(text="", ok=False,
                                        error="no thread record")

            adapter = ProbeNoAdapter("Hermes")
            channel, cfg, root, _ = self._make(
                messages=[], bus_root=tmp, adapter=adapter)
            deferred_path = root / ".agents" / "state" / "deferred-hermes.json"
            deferred_path.parent.mkdir(parents=True, exist_ok=True)
            deferred_path.write_text(json.dumps({"deferred": [
                {"id": "d1", "text": "go", "sender": "codex", "accepted": True}]}),
                encoding="utf-8")
            outcome = asyncio.run(bus_poll_once(channel,
                                                delivery_verify_attempts=2,
                                                settle_sleep=no_settle))
            self.assertIn("d1", outcome.delivered)
            # The verify hook ran on the drained injection, twice (attempts=2),
            # and the clean negatives escalated exactly like the main path.
            self.assertEqual(adapter.calls, 2)
            self.assertIn("d1", outcome.delivery_unknown)
            self.assertGreaterEqual(len(outcome.escalations), 1)

    def test_drained_probe_error_degrades_without_escalation(self):
        with TemporaryDirectory() as tmp:
            class ProbeBoomAdapter(EchoAdapter):
                async def verify_delivery(self, *a, **kw):
                    raise RuntimeError("probe transport died")

            channel, cfg, root, _ = self._make(
                messages=[], bus_root=tmp, adapter=ProbeBoomAdapter("Hermes"))
            deferred_path = root / ".agents" / "state" / "deferred-hermes.json"
            deferred_path.parent.mkdir(parents=True, exist_ok=True)
            deferred_path.write_text(json.dumps({"deferred": [
                {"id": "d2", "text": "go", "sender": "codex", "accepted": True}]}),
                encoding="utf-8")
            outcome = asyncio.run(bus_poll_once(channel, settle_sleep=no_settle))
            self.assertIn("d2", outcome.delivered)
            self.assertEqual(outcome.escalations, [])
            self.assertNotIn("d2", outcome.delivery_unknown)

    def test_legacy_deferred_without_payload_escalates_when_lost(self):
        """A payload-less legacy deferred entry that left the window escalates.

        Nit from codex's delta re-check: it must not vanish silently — the
        BLOCKED escalation (state + bus envelope carrying the keys) records it.
        """
        with TemporaryDirectory() as tmp:
            channel, cfg, root, _ = self._make(messages=[], bus_root=tmp)
            deferred_path = root / ".agents" / "state" / "deferred-hermes.json"
            deferred_path.parent.mkdir(parents=True, exist_ok=True)
            deferred_path.write_text(json.dumps({"deferred": ["lost-legacy"]}),
                                     encoding="utf-8")
            writer_calls = []
            outcome = asyncio.run(bus_poll_once(
                channel, writer=lambda s, r, b: writer_calls.append(b)))
            self.assertIn("lost-legacy", outcome.delivery_unknown)
            self.assertGreaterEqual(len(outcome.escalations), 1)
            self.assertEqual(outcome.escalations[0]["intent"], "BLOCKED")
            self.assertTrue(writer_calls, "escalation must write the bus envelope")
            self.assertIn("message_id=lost-legacy", writer_calls[0])

    # -- deliverable 8: broadcasts via MachineEventClassifier ------------------------
    def test_broadcast_is_delivered_once(self):
        with TemporaryDirectory() as tmp:
            bcast = bus_message("b1", "antigravity", "*", "FYI for everyone")
            channel, cfg, root, _ = self._make(
                messages=[bcast, bcast], bus_root=tmp)
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertEqual(outcome.delivered, ["b1"])
            # Re-poll must not double-deliver.
            again = asyncio.run(bus_poll_once(channel))
            self.assertEqual(again.delivered, [])

    def test_broadcast_machine_class_recorded_without_dispatch(self):
        with TemporaryDirectory() as tmp:
            receipt = bus_message("r1", "codex", "*",
                                   "[A2A v1.1] RECEIPT to=* ref=t-1 id=r-1")
            channel, cfg, root, _ = self._make(messages=[receipt], bus_root=tmp)
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertEqual(outcome.delivered, ["r1"])
            # Machine-class envelope: recorded as state, NOT dispatched to adapter.
            self.assertEqual(outcome.adapter_replies, [])
            self.assertIn("r1", outcome.machine_events)

    # -- deliverable 6: verify_delivery + degrade rule ------------------------------
    def test_probe_error_does_not_escalate(self):
        with TemporaryDirectory() as tmp:
            class ProbeBoomAdapter(EchoAdapter):
                async def verify_delivery(self, *a, **kw):
                    raise RuntimeError("probe transport died")

            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "go")],
                bus_root=tmp, adapter=ProbeBoomAdapter("Hermes"))
            outcome = asyncio.run(bus_poll_once(channel, settle_sleep=no_settle))
            self.assertEqual(outcome.delivered, ["m1"])
            # Probe error → log and continue, NOT an escalation.
            self.assertEqual(outcome.escalations, [])
            self.assertNotIn("m1", outcome.delivery_unknown)

    def test_clean_negative_after_attempts_escalates(self):
        with TemporaryDirectory() as tmp:
            class ProbeNoAdapter(EchoAdapter):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    self.calls = 0

                async def verify_delivery(self, message, *a, **kw):
                    self.calls += 1
                    return AdapterReply(text="", ok=False,
                                        error="no thread record")

            adapter = ProbeNoAdapter("Hermes")
            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "go")],
                bus_root=tmp, adapter=adapter)
            outcome = asyncio.run(bus_poll_once(
                channel, delivery_verify_attempts=3, settle_sleep=no_settle))
            # Three probe attempts, all clean-negative.
            self.assertEqual(adapter.calls, 3)
            self.assertIn("m1", outcome.delivery_unknown)
            # Escalation: BLOCKED envelope + bus write hook called.
            self.assertGreaterEqual(len(outcome.escalations), 1)
            self.assertEqual(outcome.escalations[0]["intent"], "BLOCKED")

    def test_probe_success_marks_known_and_skips_escalation(self):
        with TemporaryDirectory() as tmp:
            class ProbeOkAdapter(EchoAdapter):
                async def verify_delivery(self, message, *a, **kw):
                    return AdapterReply(text="thread ok", ok=True)

            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "go")],
                bus_root=tmp, adapter=ProbeOkAdapter("Hermes"))
            outcome = asyncio.run(bus_poll_once(channel,
                                                 delivery_verify_attempts=3,
                                                 settle_sleep=no_settle))
            self.assertEqual(outcome.delivery_unknown, [])
            self.assertEqual(outcome.escalations, [])
            self.assertIn("m1", outcome.delivery_verified)

    # -- issue #80: settle window before the first verify probe ---------------------
    def test_settle_window_prevents_a_false_escalation(self):
        """Issue #80: the probe must not race the session it just woke.

        The fake clock only lets the probe see the message after the settle
        window elapses; with a settle of 0 the same adapter clean-negatives
        (or, at best, races) and escalates a false ESCALATE_BLOCKER.
        """
        with TemporaryDirectory() as tmp:
            clock = FakeClock()

            class LateProbeAdapter(EchoAdapter):
                async def verify_delivery(self, message, *a, **kw):
                    if clock.now < 3.0:
                        return AdapterReply(ok=False,
                                            error="session has not shown it yet")
                    return AdapterReply(text="thread row present", ok=True)

            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "go")],
                bus_root=tmp, adapter=LateProbeAdapter("Hermes"))
            outcome = asyncio.run(bus_poll_once(
                channel, delivery_verify_attempts=3, settle_sleep=clock.sleep))

            self.assertEqual(clock.sleeps, [3.0])       # the config default window
            self.assertIn("m1", outcome.delivery_verified)
            self.assertEqual(outcome.delivery_unknown, [])
            self.assertEqual(outcome.escalations, [])
            self.assertEqual(outcome.bus_write_failures, 0)

    def test_settle_window_is_config_declared_and_bounded(self):
        """Issue #80: ``bus.verify_settle_seconds`` drives the window and is clamped.

        A window past the cap cannot stall a cycle; 0 disables the settle.
        """
        with TemporaryDirectory() as tmp:
            class ProbeNoAdapter(EchoAdapter):
                async def verify_delivery(self, message, *a, **kw):
                    return AdapterReply(ok=False, error="no record")

            clock = FakeClock()
            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "go")],
                bus_root=tmp, adapter=ProbeNoAdapter("Hermes"),
                verify_settle_seconds=10_000)
            asyncio.run(bus_poll_once(channel, delivery_verify_attempts=1,
                                      settle_sleep=clock.sleep))
            self.assertEqual(clock.sleeps, [poll_module.MAX_VERIFY_SETTLE_SECONDS])

            off = FakeClock()
            channel, cfg, root, _ = self._make(
                messages=[bus_message("m2", "codex", "hermes", "go")],
                bus_root=tmp, adapter=ProbeNoAdapter("Hermes"),
                verify_settle_seconds=0)
            asyncio.run(bus_poll_once(channel, delivery_verify_attempts=1,
                                      settle_sleep=off.sleep))
            self.assertEqual(off.sleeps, [])

    def test_genuine_clean_negative_past_the_settle_window_still_escalates(self):
        """Issue #80: the settle window must not mask a genuinely missing row."""
        with TemporaryDirectory() as tmp:
            clock = FakeClock()

            class ProbeNoAdapter(EchoAdapter):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    self.calls = 0

                async def verify_delivery(self, message, *a, **kw):
                    self.calls += 1
                    return AdapterReply(ok=False, error="no thread record")

            adapter = ProbeNoAdapter("Hermes")
            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "go")],
                bus_root=tmp, adapter=adapter)
            writer_calls = []
            outcome = asyncio.run(bus_poll_once(
                channel, delivery_verify_attempts=2,
                settle_sleep=clock.sleep,
                writer=lambda sender, recipient, body: writer_calls.append(body)))

            self.assertEqual(clock.sleeps, [3.0])       # settle once, then probe
            self.assertEqual(adapter.calls, 2)
            self.assertIn("m1", outcome.delivery_unknown)
            self.assertEqual(len(outcome.escalations), 1)
            self.assertTrue(writer_calls, "a genuine failure must still write the bus")
            self.assertEqual(outcome.bus_write_failures, 0)

    # -- issue #82: failed escalation bus writes are visible ------------------------
    def test_failed_bus_write_is_counted_detailed_and_in_the_heartbeat(self):
        """Issue #82: a writer exception used to be swallowed without a trace."""
        with TemporaryDirectory() as tmp:
            class ProbeNoAdapter(EchoAdapter):
                async def verify_delivery(self, message, *a, **kw):
                    return AdapterReply(ok=False, error="no record")

            def exploding_writer(sender, recipient, body):
                raise RuntimeError("bus push rejected")

            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "go")],
                bus_root=tmp, adapter=ProbeNoAdapter("Hermes"))
            outcome = asyncio.run(bus_poll_once(
                channel, delivery_verify_attempts=1,
                settle_sleep=no_settle, writer=exploding_writer))

            # The escalation is still raised; only its durable record is missing.
            self.assertEqual(len(outcome.escalations), 1)
            self.assertEqual(outcome.bus_writes, [])
            self.assertEqual(outcome.bus_write_failures, 1)
            self.assertEqual(len(outcome.bus_write_failure_details), 1)
            self.assertIn("RuntimeError", outcome.bus_write_failure_details[0])
            self.assertIn("bus push rejected", outcome.bus_write_failure_details[0])

            # Issue #68: the beat must not imply persistence the bus never got.
            heartbeat = Path(tmp) / ".agents" / "state" / "hermes.heartbeat.json"
            data = json.loads(heartbeat.read_text(encoding="utf-8"))
            self.assertEqual(data["escalations"], 1)
            self.assertEqual(data["bus_write_failures"], 1)
            status = heartbeat_status(cfg)
            self.assertEqual(status["counters"]["bus_write_failures"], 1)

    # -- contention: two pollers, exactly one delivers -----------------------------
    def test_two_pollers_only_one_delivers(self):
        with TemporaryDirectory() as tmp:
            msgs = [bus_message("m1", "codex", "hermes", "race me")]
            cfg = make_config(bus={"poll_minutes": 10, "repo": tmp})

            def make_channel():
                def puller(_root):
                    pass

                def reader(_root):
                    return [(m, True, "ok") for m in msgs]

                chan = AgentChannel(
                    cfg,
                    adapter=EchoAdapter("Hermes"),
                    sender=FakeSender(),
                    gitbus_pull=puller,
                    gitbus_read=reader,
                )
                return chan

            chan_a = make_channel()
            chan_b = make_channel()
            repo = Path(cfg.bus.repo)
            alias = cfg.identity.alias

            # Hold the lock for chan_b; chan_a must see "busy" and skip the cycle.
            self.assertTrue(acquire_poll_lock(repo, alias))

            async def run_pair():
                # chan_a runs while chan_b's lock is held → must skip.
                outcome_a = await bus_poll_once(chan_a)
                # Release the held lock so chan_b can run cleanly.
                release_poll_lock(repo, alias)
                outcome_b = await bus_poll_once(chan_b)
                return outcome_a, outcome_b

            a, b = asyncio.run(run_pair())
            # chan_a was the loser; chan_b was the winner.
            self.assertEqual(a.delivered, [])
            self.assertEqual(b.delivered, ["m1"])
            self.assertEqual(a.skipped, "busy")

    def test_corrupt_lock_file_is_reclaimed(self):
        """An unreadable lock marker must never block the poller forever.

        Codex review of #60: the old check-then-write acquire treated a corrupt
        marker as fresh (age computed against ts) and blocked until someone
        deleted it. O_EXCL + stale-reclaim fixes both the race and this liveness
        bug.
        """
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = poll_lock_path(repo, "Hermes")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
            self.assertTrue(acquire_poll_lock(repo, "Hermes"),
                            "corrupt marker must be reclaimed, not block")
            # The reclaimer now owns it: a second acquire is busy.
            self.assertFalse(acquire_poll_lock(repo, "Hermes"))
            release_poll_lock(repo, "Hermes")
            self.assertTrue(acquire_poll_lock(repo, "Hermes"))
            release_poll_lock(repo, "Hermes")

    def test_blocked_escalation_envelope_carries_message_id(self):
        """The PRODUCTION caller passes the message id into the fallback envelope.

        Reviewer finding F1: the production path used to pass
        ``original_message_id=None`` (the IncomingMessage carries no id), so the
        BLOCKED envelope shipped without its ``message_id=`` dedupe line while
        the direct-helper test stayed green.
        """
        with TemporaryDirectory() as tmp:
            class ProbeNoAdapter(EchoAdapter):
                async def verify_delivery(self, message, *a, **kw):
                    return AdapterReply(text="", ok=False, error="no record")

            writer_calls = []
            channel, cfg, root, _ = self._make(
                messages=[bus_message("mid-42", "codex", "hermes", "go")],
                bus_root=tmp, adapter=ProbeNoAdapter("Hermes"))
            outcome = asyncio.run(bus_poll_once(
                channel, delivery_verify_attempts=1,
                settle_sleep=no_settle,
                writer=lambda sender, recipient, body: writer_calls.append(body)))
            self.assertIn("mid-42", outcome.delivery_unknown)
            self.assertTrue(writer_calls, "escalation must write the bus envelope")
            self.assertIn("message_id=mid-42", writer_calls[0])

    def test_empty_cycle_creates_no_deferred_file(self):
        """A cycle with nothing deferred must not create the deferred file.

        Reviewer finding F5: _save_deferred wrote unconditionally, so every
        empty CLI cycle grew an empty ``deferred-<alias>.json`` that the legacy
        check_gitbus never created.
        """
        with TemporaryDirectory() as tmp:
            channel, cfg, root, _ = self._make(messages=[], bus_root=tmp)
            asyncio.run(bus_poll_once(channel))
            self.assertFalse(
                (root / ".agents" / "state" / "deferred-hermes.json").exists())

    def test_transport_compat_preserves_legacy_skip_rules(self):
        """transport_compat=True keeps the legacy check_gitbus skip rules.

        Reviewer finding F8: the branch had zero direct coverage.
        """
        with TemporaryDirectory() as tmp:
            bcast = bus_message("b1", "antigravity", "*", "FYI all")
            own = bus_message("o1", "hermes", "codex", "my own post")
            machine = bus_message("r1", "codex", "hermes",
                                  "[A2A v1.1] RECEIPT to=hermes ref=t-1 id=r-1")
            mine = bus_message("m1", "codex", "hermes", "for hermes")
            channel, cfg, root, _ = self._make(
                messages=[bcast, own, machine, mine], bus_root=tmp)
            outcome = asyncio.run(bus_poll_once(channel, transport_compat=True))
            # Legacy path: broadcasts and own posts are silently skipped; a
            # directly-addressed machine envelope is injected like any other
            # addressed message (legacy had no classifier — review B7), and no
            # machine state is recorded on this path.
            self.assertEqual(outcome.delivered, ["r1", "m1"])
            self.assertEqual(outcome.machine_events, [])

    # -- round 2 (antigravity reconciliation): B1/B2/B4/B5/B6/B8 -------------------
    def test_probe_success_with_empty_text_is_not_inverted(self):
        """A REAL probe reply (ok=True, empty text) must count as verified.

        Review B1: AdapterReply.__bool__ is bool(text) — truthiness scored every
        real success (empty text) as a clean negative and escalated it.
        """
        with TemporaryDirectory() as tmp:
            class ProbeOkEmptyText(EchoAdapter):
                async def verify_delivery(self, message, *a, **kw):
                    return AdapterReply(ok=True, meta={"step_idx": 1})

            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "go")],
                bus_root=tmp, adapter=ProbeOkEmptyText("Hermes"))
            outcome = asyncio.run(bus_poll_once(channel, delivery_verify_attempts=2,
                                                settle_sleep=no_settle))
            self.assertIn("m1", outcome.delivery_verified)
            self.assertEqual(outcome.delivery_unknown, [])
            self.assertEqual(outcome.escalations, [])

    def test_verify_none_reply_degrades_without_escalation(self):
        """None = unknown (not implemented) — never a clean negative (B1/B9)."""
        with TemporaryDirectory() as tmp:
            class ProbeNone(EchoAdapter):
                async def verify_delivery(self, message, *a, **kw):
                    return None

            channel, cfg, root, _ = self._make(
                messages=[bus_message("m1", "codex", "hermes", "go")],
                bus_root=tmp, adapter=ProbeNone("Hermes"))
            outcome = asyncio.run(bus_poll_once(channel, delivery_verify_attempts=3,
                                                settle_sleep=no_settle))
            self.assertIn("m1", outcome.delivered)
            self.assertEqual(outcome.escalations, [])
            self.assertNotIn("m1", outcome.delivery_unknown)

    def test_default_writer_posts_escalation_to_bus(self):
        """Review B2: no caller wires a writer — the default must still reach the bus."""
        with TemporaryDirectory() as tmp:
            class ProbeNoAdapter(EchoAdapter):
                async def verify_delivery(self, message, *a, **kw):
                    return AdapterReply(ok=False, error="no record")

            channel, cfg, root, _ = self._make(
                messages=[bus_message("mid-77", "codex", "hermes", "go")],
                bus_root=tmp, adapter=ProbeNoAdapter("Hermes"))
            with patch("swarmly.telegram.poll._post_bus_envelope") as posted:
                outcome = asyncio.run(bus_poll_once(channel, delivery_verify_attempts=1,
                                                    settle_sleep=no_settle))
            self.assertIn("mid-77", outcome.delivery_unknown)
            self.assertTrue(posted.called, "default writer must post the BLOCKED envelope")
            body = posted.call_args.args[3]
            self.assertIn("BLOCKED", body)
            self.assertIn("message_id=mid-77", body)

    def test_broadcast_freeze_is_refused_without_admin_author(self):
        """Review B4: no author identity ⇒ no admin ⇒ broadcast FREEZE refused."""
        with TemporaryDirectory() as tmp:
            freeze = bus_message("fz-1", "codex", "hermes",
                                 "[A2A v1.1] STATUS to=* intent=FREEZE id=fz-1")
            channel, cfg, root, _ = self._make(messages=[freeze], bus_root=tmp)
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertFalse(channel._frozen,
                             "broadcast FREEZE without admin author must not apply")
            self.assertIn("fz-1", outcome.machine_events)

    def test_broadcast_freeze_applies_with_admin_author(self):
        with TemporaryDirectory() as tmp:
            freeze = bus_message("fz-2", "admin", "hermes",
                                 "[A2A v1.1] STATUS to=* intent=FREEZE id=fz-2")
            channel, cfg, root, _ = self._make(messages=[freeze], bus_root=tmp)
            asyncio.run(bus_poll_once(channel, author_admin_id=ADMIN_ID))
            self.assertTrue(channel._frozen)

    def test_persisted_freeze_applies_in_poll(self):
        """Review B5: the CLI path must honor a persisted freeze (defer, not inject)."""
        with TemporaryDirectory() as tmp:
            channel, cfg, root, _ = self._make(
                messages=[bus_message("a1", "codex", "hermes", "work")],
                bus_root=tmp)
            freeze_file = Path(tmp) / "freeze.json"
            freeze_file.write_text(json.dumps({"frozen": True, "ref": "t-9"}))
            channel._freeze_state_path = lambda: freeze_file
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertNotIn("a1", outcome.delivered)
            self.assertIn("a1", outcome.deferred)

    def test_lock_ttl_derives_from_adapter_timeout(self):
        """Review B6: the lock TTL must outlive a legitimate (slow) injection."""
        with TemporaryDirectory() as tmp:
            channel, cfg, root, _ = self._make(messages=[], bus_root=tmp)
            recorded = {}

            def fake_acquire(repo, alias, **kw):
                recorded.update(kw)
                return True

            with patch("swarmly.telegram.poll.acquire_poll_lock", side_effect=fake_acquire):
                asyncio.run(bus_poll_once(channel))
            # Adapter default timeout is 900s; the lock must exceed 2× that.
            self.assertGreaterEqual(recorded.get("ttl_seconds", 0), 1800.0)

    def test_bus_machine_events_record_state_and_resolve_closed(self):
        """Review B8: bus-path machine traffic updates the token-economy state."""
        with TemporaryDirectory() as tmp:
            hello = bus_message("h1", "antigravity", "hermes",
                                "[A2A v1.1] HELLO to=* intent=ONLINE rev=abc")
            closed = bus_message("c1", "codex", "hermes",
                                 "[A2A v1.1] NOTE to=hermes intent=CLOSED ref=ab12cd id=c1")
            channel, cfg, root, _ = self._make(messages=[hello, closed], bus_root=tmp)
            fake_replies = Mock()
            fake_replies.resolve_prefix.return_value = 1
            channel.replies = fake_replies
            outcome = asyncio.run(bus_poll_once(channel))
            self.assertIn("h1", outcome.machine_events)
            self.assertIn("c1", outcome.machine_events)
            self.assertTrue(channel._peer_last_seen,
                            "machine state surface must update on the bus path")
            fake_replies.resolve_prefix.assert_called_once_with("ab12cd",
                                                                author="CodexBot")


# --------------------------------------------------------------------------------------
# Adapter-level verify_delivery hook interface (deliverable 6)
# --------------------------------------------------------------------------------------
class TestVerifyDeliveryHook(unittest.TestCase):
    def test_default_returns_none(self):
        adapter = EchoAdapter("Hermes")
        result = asyncio.run(adapter.verify_delivery(IncomingMessage(
            text="x", author_alias="X")))
        self.assertIsNone(result)

    def test_codex_thread_verify_returns_unknown_when_no_thread_record(self):
        from swarmly.telegram.replies import ReplyStore
        from swarmly.telegram.adapters.codex import CodexThreadAdapter

        with TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "replies.sqlite3"
            store = ReplyStore(store_path)
            settings = AdapterSettings(
                type="codex_thread", conversation="session-uuid",
                reply_state_path=str(store_path),
            )
            adapter = CodexThreadAdapter("Codex", settings)
            # Plant a token in response_instructions; probe looks it up by id.
            token = "token-no-such"
            incoming = IncomingMessage(
                text="hello", author_alias="Hermes",
                response_instructions=f"[BUS_DELIVERY_TOKEN={token}]")
            # No record for the token → None (unknown → degrade; review B9: the
            # reply store cannot positively confirm a bus injection, so a miss
            # must not become a clean negative / false escalation).
            result = asyncio.run(adapter.verify_delivery(incoming))
            self.assertIsNone(result)

    def test_codex_thread_verify_ok_when_ticket_recorded(self):
        """When the reply store has a record keyed by the planted token, ok=True."""
        from swarmly.telegram.replies import ReplyStore
        from swarmly.telegram.adapters.codex import CodexThreadAdapter

        with TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "replies.sqlite3"
            store = ReplyStore(store_path)
            settings = AdapterSettings(
                type="codex_thread", conversation="session-uuid",
                reply_state_path=str(store_path),
            )
            adapter = CodexThreadAdapter("Codex", settings)
            # Insert a row keyed by the planted token id (the probe's lookup key).
            token = "abcdef0123456789"
            with store._db() as db:
                db.execute(
                    """INSERT INTO replies
                       (id, chat_id, message_id, recipient, original, created,
                        deadline, state)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
                    (token, GROUP, 42, "codex", "hello", 1000.0, 2000.0))
            incoming = IncomingMessage(
                text="hello", author_alias="Hermes",
                response_instructions=f"[BUS_DELIVERY_TOKEN={token}]")
            result = asyncio.run(adapter.verify_delivery(incoming))
            self.assertIsNotNone(result)
            self.assertTrue(result.ok)

    def test_codex_probe_ignores_token_quoted_in_free_text(self):
        """Review N2 (round 3): only an id-keyed row counts.

        A ticket that *quotes* the token in text/original/detail must not score
        delivered — a false positive suppresses the escalation for a message
        that was never delivered.
        """
        from swarmly.telegram.replies import ReplyStore
        from swarmly.telegram.adapters.codex import CodexThreadAdapter

        with TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "replies.sqlite3"
            store = ReplyStore(store_path)
            settings = AdapterSettings(
                type="codex_thread", conversation="session-uuid",
                reply_state_path=str(store_path),
            )
            adapter = CodexThreadAdapter("Codex", settings)
            token = "msg-20260920T101010Z-deadbeefdeadbeef"
            with store._db() as db:
                db.execute(
                    """INSERT INTO replies
                       (id, chat_id, message_id, recipient, original, created,
                        deadline, state)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                    ("unrelated-ticket-uuid", GROUP, 42, "codex",
                     f"a ticket quoting the token {token} in its body",
                     1000.0, 2000.0))
            incoming = IncomingMessage(
                text="hello", author_alias="Hermes",
                response_instructions=f"[BUS_DELIVERY_TOKEN={token}]")
            result = asyncio.run(adapter.verify_delivery(incoming))
            self.assertIsNone(result,
                              "a free-text quote must not score delivery (N2)")

    def test_antigravity_thread_probe_step_type_101_filter_is_mandatory(self):
        """The probe MUST filter on step_type=101 (verified 1:1 against agentapi acks).

        The ``steps`` table has no direction/author column; a LIKE-alone match
        false-positives on the agent's own tool-argument echoes (step_type=15).
        step_type=101 is the only inbound record. Issue #59 spec amendment.
        """
        from swarmly.telegram.adapters.antigravity import AntigravityThreadAdapter, _ConversationDb

        with TemporaryDirectory() as tmp:
            conv_id = "test-conv-101"
            db_path = Path(tmp) / f"{conv_id}.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE steps (
                    idx INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_type INTEGER NOT NULL,
                    step_payload TEXT NOT NULL
                )""")
            token = "TOKEN-m1"
            # An inbound record (101) — this IS the delivery signal.
            conn.execute(
                "INSERT INTO steps (step_type, step_payload) VALUES (101, ?)",
                (f"agentapi ack inbound message {token}",))
            # Tool-argument echo (15) — same token substring; must NOT count.
            conn.execute(
                "INSERT INTO steps (step_type, step_payload) VALUES (15, ?)",
                (f"tool-call {token} arg",))
            conn.commit()
            conn.close()

            # Point the adapter at the test database via a custom conversation_id.
            settings = AdapterSettings(
                type="antigravity_thread", conversation=conv_id,
            )
            adapter = AntigravityThreadAdapter("Antigravity", settings)
            # Patch the conversation DB lookup so the adapter opens the temp file.
            import sqlite3 as _sqlite
            def _fake_open(conversation_id):
                return _ConversationDb(_sqlite.connect(f"{tmp}/{conversation_id}.db"))
            adapter._open_conversation_db = _fake_open      # type: ignore[attr-defined]

            incoming = IncomingMessage(text="hello", author_alias="Hermes")
            # Inject the token in the planted-token marker the poller uses.
            incoming.response_instructions = f"[BUS_DELIVERY_TOKEN={token}]"
            result = asyncio.run(adapter.verify_delivery(incoming))
            self.assertIsNotNone(result)
            self.assertTrue(result.ok,
                            msg=f"step_type=101 row should match; got {result}")

    def test_antigravity_probe_tool_echo_alone_is_not_delivery(self):
        """A token present ONLY in a step_type=15 tool-echo must NOT be delivery.

        Reviewer finding (discrimination): the companion test inserts both row
        kinds and only asserts the happy path. This one removes the 101 row, so
        a probe that ignored the step_type filter would false-positive here.
        """
        from swarmly.telegram.adapters.antigravity import AntigravityThreadAdapter, _ConversationDb

        with TemporaryDirectory() as tmp:
            conv_id = "echo-only-conv"
            db_path = Path(tmp) / f"{conv_id}.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE steps (
                    idx INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_type INTEGER NOT NULL,
                    step_payload TEXT NOT NULL
                )""")
            token = "TOKEN-ECHO-ONLY"
            conn.execute(
                "INSERT INTO steps (step_type, step_payload) VALUES (15, ?)",
                (f"tool-call {token} arg",))
            conn.commit()
            conn.close()

            settings = AdapterSettings(
                type="antigravity_thread", conversation=conv_id,
            )
            adapter = AntigravityThreadAdapter("Antigravity", settings)
            import sqlite3 as _sqlite
            adapter._open_conversation_db = lambda cid: _ConversationDb(  # type: ignore[attr-defined]
                _sqlite.connect(f"{tmp}/{cid}.db"))

            incoming = IncomingMessage(text="hello", author_alias="Hermes")
            incoming.response_instructions = f"[BUS_DELIVERY_TOKEN={token}]"
            result = asyncio.run(adapter.verify_delivery(incoming))
            self.assertIsNotNone(result)
            self.assertFalse(result.ok,
                             "a tool-echo row alone must never count as delivery")

    def test_antigravity_thread_probe_clean_negative_on_no_row(self):
        """Clean negative after N attempts ⇒ adapter returns ok=False; escalate elsewhere."""
        from swarmly.telegram.adapters.antigravity import AntigravityThreadAdapter, _ConversationDb

        with TemporaryDirectory() as tmp:
            conv_id = "empty-conv"
            db_path = Path(tmp) / f"{conv_id}.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE steps (
                    idx INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_type INTEGER NOT NULL,
                    step_payload TEXT NOT NULL
                )""")
            conn.commit()
            conn.close()

            settings = AdapterSettings(
                type="antigravity_thread", conversation=conv_id,
            )
            adapter = AntigravityThreadAdapter("Antigravity", settings)
            import sqlite3 as _sqlite
            adapter._open_conversation_db = lambda cid: _ConversationDb(  # type: ignore[attr-defined]
                _sqlite.connect(f"{tmp}/{cid}.db"))

            incoming = IncomingMessage(text="hello", author_alias="Hermes")
            incoming.response_instructions = "[BUS_DELIVERY_TOKEN=TOKEN-NOPE]"
            result = asyncio.run(adapter.verify_delivery(incoming))
            self.assertIsNotNone(result)
            self.assertFalse(result.ok)

    def test_antigravity_immutable_fallback_miss_raises_not_negative(self):
        """Review N1 (round 3): a WAL-only row missed by the immutable fallback
        must raise (probe error → degrade), never become the clean negative that
        escalates to a false BLOCKED.

        The fallback is exercised for real: `sqlite3.connect` is patched so the
        `mode=ro` URI fails (as it does when read permission on the directory is
        denied), leaving the `immutable=1` path; the row is committed to the
        -wal only, with the writer connection still open — the live-session case
        this probe exists for.
        """
        from swarmly.telegram.adapters.antigravity import AntigravityThreadAdapter
        import sqlite3 as _sqlite
        from unittest.mock import patch as _patch

        with TemporaryDirectory() as tmp:
            conv_id = "wal-conv"
            db_path = Path(tmp) / f"{conv_id}.db"
            writer = _sqlite.connect(str(db_path))
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY AUTOINCREMENT, "
                "step_type INTEGER NOT NULL, step_payload TEXT NOT NULL)")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            token = "TOKEN-WALONLY"
            writer.execute(
                "INSERT INTO steps (step_type, step_payload) VALUES (101, ?)",
                (f"inbound {token}",))
            writer.commit()          # committed to the -wal only; writer stays open
            try:
                settings = AdapterSettings(
                    type="antigravity_thread", conversation=conv_id)
                adapter = AntigravityThreadAdapter("Antigravity", settings)
                adapter._conversation_db_paths = (   # type: ignore[attr-defined]
                    lambda cid: [db_path])

                real_connect = _sqlite.connect

                def _connect(uri, *a, **kw):
                    if "mode=ro" in str(uri):
                        raise _sqlite.OperationalError(
                            "simulated: mode=ro unavailable")
                    return real_connect(uri, *a, **kw)

                incoming = IncomingMessage(text="hello", author_alias="Hermes")
                incoming.response_instructions = f"[BUS_DELIVERY_TOKEN={token}]"
                with _patch("sqlite3.connect", side_effect=_connect):
                    with self.assertRaises(RuntimeError):
                        asyncio.run(adapter.verify_delivery(incoming))

                # Control: the normal mode=ro reader DOES see the committed row,
                # so the raise above is about the fallback's blindness to the
                # WAL — not about a missing row.
                result = asyncio.run(adapter.verify_delivery(incoming))
                self.assertIsNotNone(result)
                self.assertTrue(result.ok)
            finally:
                writer.close()


class TestPlantedTokenContract(unittest.TestCase):
    """Issue #59 spec amendment (1): the injection carries a unique planted token.

    The token is the delivery-probe key for every adapter store. Default: the
    message/envelope id. When a reply-store ticket id is in scope, that is
    already unique and already in the prompt, so it is reused.
    """

    def test_default_token_is_message_id(self):
        from swarmly.telegram.poll import planted_token_for
        from swarmly.models import A2AMessage, MessageIntent, MessagePriority, RecipientInfo, SenderInfo
        msg = A2AMessage(
            protocol="a2a/v1.0", message_id="m-42",
            timestamp_utc="2026-09-19T00:00:00+00:00",
            sender=SenderInfo(agent_id="codex", model_family="custom", framework="custom"),
            recipient=RecipientInfo(agent_id="hermes"),
            intent=MessageIntent.QUERY_REQUEST, priority=MessagePriority.ROUTINE,
            signature="", payload={"text": "hi"},
        )
        self.assertEqual(planted_token_for(message=msg, ticket_id=None), "m-42")

    def test_ticket_id_overrides_message_id(self):
        """When a reply ticket id is in scope it is already unique; reuse it."""
        from swarmly.telegram.poll import planted_token_for
        from swarmly.models import A2AMessage, MessageIntent, MessagePriority, RecipientInfo, SenderInfo
        msg = A2AMessage(
            protocol="a2a/v1.0", message_id="m-42",
            timestamp_utc="2026-09-19T00:00:00+00:00",
            sender=SenderInfo(agent_id="codex", model_family="custom", framework="custom"),
            recipient=RecipientInfo(agent_id="hermes"),
            intent=MessageIntent.QUERY_REQUEST, priority=MessagePriority.ROUTINE,
            signature="", payload={"text": "hi"},
        )
        self.assertEqual(planted_token_for(message=msg, ticket_id="ticket-9"), "ticket-9")


# --------------------------------------------------------------------------------------
# Outbound fallback envelopes carry message_id + dedupe keys (deliverable 7)
# --------------------------------------------------------------------------------------
class TestOutboundFallbackContract(unittest.TestCase):
    def test_fallback_envelope_carries_message_id_and_dedupe_keys(self):
        from swarmly.telegram.poll import build_fallback_envelope
        env = build_fallback_envelope(
            sender="Hermes", recipient="Codex",
            original_message_id=42,
            envelope_id="env-7", ticket_id="ticket-9",
            body="hi")
        # Header carries the protocol-level dedupe keys.
        self.assertIn("id=env-7", env)
        self.assertIn("ref=ticket-9", env)
        # Body contains the dedupe markers (in the contract lines appended).
        self.assertIn("dedupe=envelope:env-7", env)
        self.assertIn("dedupe=ticket:ticket-9", env)
        self.assertIn("message_id=42", env)


# --------------------------------------------------------------------------------------
# CLI: bus-poll --once (deliverable 1)
# --------------------------------------------------------------------------------------
class TestBusPollCLI(unittest.TestCase):
    def _run_cli(self, argv, env=None):
        # Portable cwd: the repo root this test file lives in (codex review of
        # PR #60: a hardcoded host path failed off this host — WinError 267).
        repo_root = Path(__file__).resolve().parents[1]
        # The no-token proof must actually run WITHOUT any Telegram credential
        # in the environment — scrub every TELEGRAM*/BOT_TOKEN* variable
        # (codex review of PR #60: nothing scrubbed them before).
        base_env = {k: v for k, v in os.environ.items()
                    if "TELEGRAM" not in k and "BOT_TOKEN" not in k}
        proc = subprocess.run(
            [sys.executable, "-m", "swarmly.telegram.cli", *argv],
            capture_output=True, text=True,
            cwd=str(repo_root),
            env=env or base_env,
            timeout=120,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_bus_poll_once_runs_without_token(self):
        """The proof command from the spec — exit 0, no token, no Telegram."""
        with TemporaryDirectory() as tmp:
            # Config with NO bot token and a tiny gitbus root containing no
            # messages — should still exit 0.
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text(json.dumps({
                "agent": {"name": "HermesBot", "alias": "Hermes",
                          "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
                "peers": {"Codex": "CodexBot"},
                "adapter": {"type": "echo"},
                "bus": {"poll_minutes": 10, "repo": tmp},
                "limits": {"min_seconds_between_messages": 0},
            }), encoding="utf-8")
            rc, out, err = self._run_cli(["--config", str(cfg_path),
                                           "bus-poll", "--once"])
            self.assertEqual(rc, 0,
                             msg=f"bus-poll --once failed: stdout={out!r} stderr={err!r}")
            self.assertIn("bus-poll", out.lower() + err.lower())

    def test_quiet_suppresses_the_human_summary(self):
        """Scheduler wrappers want silence on success (PR #74 review, F2)."""
        with TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text(json.dumps({
                "agent": {"name": "HermesBot", "alias": "Hermes",
                          "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
                "peers": {"Codex": "CodexBot"},
                "adapter": {"type": "echo"},
                "bus": {"poll_minutes": 10, "repo": tmp},
                "limits": {"min_seconds_between_messages": 0},
            }), encoding="utf-8")
            rc, out, err = self._run_cli(["--config", str(cfg_path),
                                           "bus-poll", "--once", "--quiet"])
            self.assertEqual(rc, 0, msg=f"quiet run failed: stderr={err!r}")
            self.assertEqual(out.strip(), "", msg=f"expected silence, got: {out!r}")

    def test_cli_summary_prints_bus_write_failures(self):
        """Issue #82: a swallowed escalation write is visible in the summary."""
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from swarmly.telegram import cli as cli_module

        cfg = make_config(bus={"repo": tempfile.mkdtemp()})
        outcome = PollOutcome()
        outcome.escalations.append({"intent": "BLOCKED", "envelope_id": "poll-m1",
                                    "ticket_id": "m1", "message_id": "m1"})
        outcome.bus_write_failures = 1
        outcome.bus_write_failure_details.append("poll-m1: RuntimeError: nope")

        async def fake_poll_once(channel, **kwargs):
            return outcome

        buffer = io.StringIO()
        with patch("swarmly.telegram.poll.bus_poll_once", new=fake_poll_once), \
                redirect_stdout(buffer):
            rc = asyncio.run(cli_module._bus_poll(
                cfg, SimpleNamespace(once=True, json=False, quiet=False)))
        self.assertEqual(rc, 0)
        self.assertIn("bus_write_failures=1", buffer.getvalue())
        self.assertIn("BUS WRITE FAILED: poll-m1: RuntimeError: nope",
                      buffer.getvalue())

        buffer = io.StringIO()
        with patch("swarmly.telegram.poll.bus_poll_once", new=fake_poll_once), \
                redirect_stdout(buffer):
            asyncio.run(cli_module._bus_poll(
                cfg, SimpleNamespace(once=True, json=True, quiet=False)))
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["bus_write_failures"], 1)
        self.assertEqual(payload["bus_write_failure_details"],
                         ["poll-m1: RuntimeError: nope"])

    def test_bus_poll_reports_rejected_messages(self):
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from swarmly.telegram import cli as cli_module

        cfg = make_config(bus={"repo": tempfile.mkdtemp()})
        outcome = PollOutcome()
        outcome.rejected.append({"id": "m9", "sender": "mallory", "reason": "unsigned\x1b[2J"})

        async def fake_poll_once(channel, **kwargs):
            return outcome

        for as_json in (True, False):
            buffer = io.StringIO()
            with patch("swarmly.telegram.poll.bus_poll_once", new=fake_poll_once), \
                    redirect_stdout(buffer):
                asyncio.run(cli_module._bus_poll(
                    cfg, SimpleNamespace(once=True, json=as_json, quiet=False)))
            if as_json:
                self.assertEqual(json.loads(buffer.getvalue())["rejected"][0]["id"], "m9")
            else:
                self.assertIn("rejected=1", buffer.getvalue())
                self.assertNotIn("\x1b", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()


# Issue #106: ``AgentConfig.from_dict`` merges the developer's real ``.env`` into the
# process-global ``os.environ``; the guard keeps this module's tests from leaking it.
from tests import envguard  # noqa: E402 - last on purpose: the wrapper stays two lines


def load_tests(loader, tests, pattern):
    return envguard.guard(loader, tests, pattern)


class HeartbeatTransportBlockTests(unittest.TestCase):
    """Issue #123: the beat's ``transport`` block -- written by the transport tick,
    preserved by the scheduled poller, classified by ``heartbeat-check``.

    The incident these pin: ``heartbeat-check`` reported ``fresh beat, age=27s``
    through two 25-minute blackouts, because the scheduled poller kept writing
    beats while the transport could not complete a cycle.
    """

    def _beat_path(self, tmp):
        return Path(tmp) / ".agents" / "state" / "hermes.heartbeat.json"

    def _write_beat(self, tmp, payload):
        path = self._beat_path(tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_transport_tick_writes_the_block_and_status_reads_alive(self):
        import socket
        with TemporaryDirectory() as tmp:
            cfg = make_config(bus={"poll_minutes": 30, "repo": tmp})
            outcome = PollOutcome()
            poll_module._write_heartbeat(Path(tmp), "Hermes", outcome, transport={
                "pid": os.getpid(), "host": socket.gethostname(),
                "started_at": time.time() - 5, "endpoint_ok": True})
            data = json.loads(self._beat_path(tmp).read_text(encoding="utf-8"))
            self.assertEqual(data["transport"]["pid"], os.getpid())
            status = heartbeat_status(cfg, now=time.time())
            self.assertFalse(status["stale"])
            self.assertEqual(status["transport_state"], "alive")

    def test_poller_write_preserves_the_transport_block(self):
        """If the poller erased the block, 'transport dead' would silently read as
        'unknown (legacy)' exactly when the distinction matters."""
        with TemporaryDirectory() as tmp:
            outcome = PollOutcome()
            poll_module._write_heartbeat(Path(tmp), "Hermes", outcome, transport={
                "pid": 4242, "host": "other-host", "started_at": 0.0, "endpoint_ok": True})
            before = json.loads(self._beat_path(tmp).read_text(encoding="utf-8"))
            block_at = before["transport"]["at"]
            time.sleep(0.01)
            poll_module._write_heartbeat(Path(tmp), "Hermes", outcome)   # poller-only
            after = json.loads(self._beat_path(tmp).read_text(encoding="utf-8"))
            self.assertIn("transport", after)
            self.assertEqual(after["transport"]["at"], block_at)
            self.assertGreater(float(after["at"]), float(before["at"]))

    def test_block_marks_dead_when_pid_is_gone(self):
        import socket
        with TemporaryDirectory() as tmp:
            cfg = make_config(bus={"poll_minutes": 30, "repo": tmp})
            now = time.time()
            self._write_beat(tmp, {
                "at": now, "alias": "Hermes",
                "transport": {"at": now - 5, "pid": 424242,
                              "host": socket.gethostname(),
                              "started_at": 0.0, "endpoint_ok": True}})
            with patch("swarmly.telegram.poll.pid_is_alive", return_value=False):
                status = heartbeat_status(cfg, now=now)
            self.assertFalse(status["stale"])
            self.assertEqual(status["transport_state"], "dead")
            self.assertIn("424242", status["transport_detail"])

    def test_stale_block_marks_dead_even_while_the_beat_is_fresh(self):
        """The blackout shape: the transport stops COMPLETING cycles; the scheduled
        poller keeps the beat fresh and carries the stale block over."""
        with TemporaryDirectory() as tmp:
            cfg = make_config(bus={"poll_minutes": 30, "repo": tmp})
            now = time.time()
            self._write_beat(tmp, {
                "at": now, "alias": "Hermes",
                "transport": {"at": now - 3 * 1800 - 10, "pid": 424242,
                              "host": "other-host", "started_at": 0.0,
                              "endpoint_ok": True}})
            status = heartbeat_status(cfg, now=now)
            self.assertFalse(status["stale"])
            self.assertEqual(status["transport_state"], "dead")

    def test_endpoint_false_marks_degraded(self):
        import socket
        with TemporaryDirectory() as tmp:
            cfg = make_config(bus={"poll_minutes": 30, "repo": tmp})
            now = time.time()
            self._write_beat(tmp, {
                "at": now, "alias": "Hermes",
                "transport": {"at": now - 5, "pid": os.getpid(),
                              "host": socket.gethostname(),
                              "started_at": 0.0, "endpoint_ok": False}})
            status = heartbeat_status(cfg, now=now)
            self.assertFalse(status["stale"])
            self.assertEqual(status["transport_state"], "degraded")

    def test_legacy_beat_without_block_is_absent_not_falsely_alive(self):
        with TemporaryDirectory() as tmp:
            cfg = make_config(bus={"poll_minutes": 30, "repo": tmp})
            now = time.time()
            self._write_beat(tmp, {"at": now, "alias": "Hermes"})
            status = heartbeat_status(cfg, now=now)
            self.assertEqual(status["transport_state"], "absent")
            self.assertIsNone(status["transport"])

    def test_heartbeat_check_exit_codes_split_on_the_block(self):
        """Acceptance (#123): fresh beat + dead/degraded transport must exit 3;
        a healthy block must exit 0."""
        import socket
        import contextlib
        from types import SimpleNamespace
        from swarmly.telegram.cli import _heartbeat_check
        args = SimpleNamespace(factor=3.0, json=True, alert=False)
        with TemporaryDirectory() as tmp:
            cfg = make_config(bus={"poll_minutes": 30, "repo": tmp})
            now = time.time()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self._write_beat(tmp, {
                    "at": now, "alias": "Hermes",
                    "transport": {"at": now - 5, "pid": os.getpid(),
                                  "host": socket.gethostname(),
                                  "started_at": 0.0, "endpoint_ok": False}})
                self.assertEqual(_heartbeat_check(cfg, args), 3)

                self._write_beat(tmp, {
                    "at": now, "alias": "Hermes",
                    "transport": {"at": now - 5, "pid": os.getpid(),
                                  "host": socket.gethostname(),
                                  "started_at": 0.0, "endpoint_ok": True}})
                self.assertEqual(_heartbeat_check(cfg, args), 0)

                self._write_beat(tmp, {"at": now, "alias": "Hermes"})   # legacy
                self.assertEqual(_heartbeat_check(cfg, args), 0)
