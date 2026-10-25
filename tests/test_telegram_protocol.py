"""
Tests for the A2A Telegram channel protocol v1.1 (docs/telegram/PROTOCOL.md).

The protocol exists because three things failed in production on 2026-09-17: a plain-text
@username that was never a mention (no delivery), a mirror claim that existed on no remote
branch, and a silent peer that could not be distinguished from a dead one. Each test below
pins one of the rules that fix those.

Run: python -m unittest tests.test_telegram_protocol -v
"""

from __future__ import annotations

import unittest

from swarmly.telegram.acks import AckTracker, Escalation, parse_ttl
from swarmly.telegram.commands import diag_reply, parse_command, ping_reply, status_reply
from swarmly.telegram.wire import (
    CHANNEL_BUDGET_CHARS,
    Envelope,
    MessageType,
    format_message,
    has_explicit_envelope,
    ignored_envelope_marker,
    is_within_budget,
    mention_entities,
    parse_message,
)


class TestEnvelope(unittest.TestCase):
    def test_header_round_trips_through_parse(self):
        env = Envelope(type=MessageType.REQUEST, to="Codex", intent="RUN",
                       ref="#148", rev="abc1234", bus=".agents/inbox/x.md@abc1234",
                       ttl="30m", id="h-20260917-4")
        parsed, body = parse_message(format_message(env, "please reproduce the anchor"))
        self.assertEqual(parsed.type, MessageType.REQUEST)
        self.assertEqual(parsed.to, "Codex")
        self.assertEqual(parsed.intent, "RUN")
        self.assertEqual(parsed.ref, "#148")
        self.assertEqual(parsed.rev, "abc1234")
        self.assertEqual(parsed.bus, ".agents/inbox/x.md@abc1234")
        self.assertEqual(parsed.ttl, "30m")
        self.assertEqual(parsed.id, "h-20260917-4")
        self.assertEqual(body, "please reproduce the anchor")
        legacy = Envelope(MessageType.REQUEST, "Codex", "RUN", "#148", "bus", "30m", "legacy")
        self.assertEqual((legacy.bus, legacy.ttl, legacy.id, legacy.rev),
                         ("bus", "30m", "legacy", None))

    def test_a_message_without_an_envelope_is_a_broadcast_note(self):
        parsed, body = parse_message("Codex, are you up?")
        self.assertEqual(parsed.type, MessageType.NOTE)   # best-effort, per spec §4
        self.assertEqual(parsed.to, "*")
        self.assertEqual(body, "Codex, are you up?")

    def test_only_an_explicit_envelope_is_authoritative_for_note_semantics(self):
        explicit = format_message(Envelope(type=MessageType.NOTE, to="Codex", intent="FYI"),
                                   "status is unchanged")
        self.assertTrue(has_explicit_envelope(explicit))
        self.assertFalse(has_explicit_envelope("@example_codex_bot status is unchanged"))

    def test_header_is_a_single_line_so_it_never_wraps(self):
        text = format_message(Envelope(type=MessageType.HELLO, to="*", id="x-1"), "body")
        self.assertEqual(len(text.splitlines()[0].split("[A2A v1.1]")), 2)
        self.assertTrue(text.splitlines()[0].startswith("[A2A v1.1]"))

    def test_channel_budget_is_500_chars(self):
        self.assertEqual(CHANNEL_BUDGET_CHARS, 500)
        self.assertTrue(is_within_budget("x" * 500))
        self.assertFalse(is_within_budget("x" * 501))


class TestEnvelopeNearMiss(unittest.TestCase):
    """The header is only read on line 1 - a marker below it is silently ignored.

    Seen live 2026-09-20: a closure preceded by the sender's turn narration parsed as
    intent=None, the machine-class gate never fired, and the ticket stayed unresolved
    while the message opened a reply ticket of its own. ``ignored_envelope_marker``
    exists to make that failure visible in the receiver's log.
    """

    def test_marker_below_line_1_is_reported_with_its_line_number(self):
        text = ("The tail is fully reconciled. Final message (goes to the group as my reply):\n"
                "\n"
                "[A2A v1.1] NOTE to=Codex intent=CLOSED ref=0396f793\n"
                "@example_codex_bot - corrected closure.\n")
        reason = ignored_envelope_marker(text)
        self.assertIsNotNone(reason)
        self.assertIn("line 3", reason)

    def test_a_clean_formatted_message_reports_nothing(self):
        clean = format_message(Envelope(type=MessageType.NOTE, to="Codex",
                                        intent="CLOSED", ref="0396f793"), "done")
        self.assertIsNone(ignored_envelope_marker(clean))

    def test_banner_and_addressing_before_the_header_on_line_1_stay_valid(self):
        text = "[Codex] @example_codex_bot [A2A v1.1] NOTE to=Codex intent=FYI\nstatus is unchanged"
        self.assertIsNone(ignored_envelope_marker(text))

    def test_plain_prose_without_a_marker_reports_nothing(self):
        self.assertIsNone(ignored_envelope_marker("Codex, are you up?"))

    def test_malformed_header_on_line_1_is_reported(self):
        reason = ignored_envelope_marker("[A2A v1.1] BOGUS to=Codex")
        self.assertIsNotNone(reason)
        self.assertIn("malformed", reason)

    def test_prose_merely_quoting_the_marker_is_not_a_near_miss(self):
        text = "status update\nwe should use [A2A v1.1] markers for machine traffic\n"
        self.assertIsNone(ignored_envelope_marker(text))

    def test_a_valid_line_1_header_governs_even_when_the_body_quotes_one(self):
        text = format_message(Envelope(type=MessageType.NOTE, to="Codex", intent="FYI"),
                              "they wrote:\n[A2A v1.1] NOTE to=Hermes intent=CLOSED ref=x")
        self.assertIsNone(ignored_envelope_marker(text))


class TestAddressing(unittest.TestCase):
    def test_command_mention_uses_bot_command_entity_from_offset_zero(self):
        text = "/ping@example_codex_bot are you up?"
        ents = mention_entities(text, "example_codex_bot", command="ping")
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["type"], "bot_command")
        self.assertEqual(ents[0]["offset"], 0)
        # length must cover exactly "/ping@example_codex_bot" — one char more swallows the space
        self.assertEqual(text[ents[0]["offset"]:ents[0]["offset"] + ents[0]["length"]],
                         "/ping@example_codex_bot")

    def test_plain_username_gets_a_mention_entity(self):
        """The 2026-09-17 failure: plain-text usernames are NOT mentions, so nothing arrived."""
        text = "Codex, status please (@example_codex_bot)"
        ents = mention_entities(text, "example_codex_bot")
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["type"], "mention")
        covered = text[ents[0]["offset"]:ents[0]["offset"] + ents[0]["length"]]
        self.assertEqual(covered, "@example_codex_bot")

    def test_entity_offsets_are_utf16_units_not_code_points(self):
        """Telegram counts offsets in UTF-16 units; an emoji before the mention shifts it by 2."""
        text = "🟢 @example_codex_bot ping"
        ents = mention_entities(text, "example_codex_bot")
        covered = text.encode("utf-16-le")[ents[0]["offset"] * 2:
                                        (ents[0]["offset"] + ents[0]["length"]) * 2].decode("utf-16-le")
        self.assertEqual(covered, "@example_codex_bot")

    def test_no_entity_when_the_username_is_absent(self):
        self.assertEqual(mention_entities("Codex, status please", "example_codex_bot"), [])

    def test_command_and_mention_together_do_not_duplicate(self):
        text = "/ping@example_codex_bot Codex, up?"
        ents = mention_entities(text, "example_codex_bot", command="ping")
        self.assertEqual([e["type"] for e in ents], ["bot_command"])

    def test_username_match_is_case_insensitive_and_boundary_aware(self):
        ents = mention_entities("@Example_Codex_Bot hello", "example_codex_bot")
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["length"], len("@Example_Codex_Bot"))
        self.assertEqual(mention_entities("@example_codex_botsuffix", "example_codex_bot"), [])


class TestAcknowledgement(unittest.TestCase):
    def setUp(self):
        self.clock = {"t": 1000.0}
        self.tracker = AckTracker(clock=lambda: self.clock["t"], default_ttl_seconds=300)
        self.tracker.expect("h-1", to="Codex")

    def advance(self, seconds):
        self.clock["t"] += seconds

    def test_request_is_pending_before_the_deadline(self):
        self.advance(299)
        self.assertEqual(self.tracker.pending(), ["h-1"])
        self.assertEqual(self.tracker.escalations(), [])

    def test_ack_closes_the_expectation(self):
        self.tracker.ack("h-1")
        self.advance(10_000)
        self.assertEqual(self.tracker.pending(), [])
        self.assertEqual(self.tracker.escalations(), [])

    def test_expiry_produces_rung_1_then_2_then_3(self):
        self.advance(301)
        rungs = self.tracker.escalations()
        self.assertEqual([r.rung for r in rungs], [1])
        self.advance(300)
        self.assertEqual([r.rung for r in self.tracker.escalations()], [2])
        self.advance(300)
        self.assertEqual([r.rung for r in self.tracker.escalations()], [3])

    def test_escalation_carries_an_announcement_so_silence_is_never_ambiguous(self):
        self.advance(601)
        esc = self.tracker.escalations()[0]
        self.assertEqual(esc.to, "Codex")
        self.assertIn("no ACK", esc.announcement)
        self.assertIn("h-1", esc.announcement)

    def test_escalation_stops_at_the_human_rung(self):
        self.advance(100_000)
        self.assertEqual([e.rung for e in self.tracker.escalations()], [3])

    def test_an_ack_is_never_itself_tracked(self):
        """Never ACK an ACK: registering an ACK-shaped message must not create an expectation."""
        self.tracker.expect("h-2", to="Codex", type=MessageType.ACK)
        self.advance(10_000)
        self.assertEqual([e.msg_id for e in self.tracker.escalations()], ["h-1"])

    def test_ack_for_an_unknown_id_is_ignored_not_an_error(self):
        self.assertFalse(self.tracker.ack("does-not-exist"))
        self.assertEqual(self.tracker.pending(), ["h-1"])

    def test_custom_ttl_overrides_the_default(self):
        self.tracker.expect("h-3", to="Antigravity", ttl_seconds=60)
        self.advance(61)
        self.assertEqual([e.msg_id for e in self.tracker.escalations()], ["h-3"])

    def test_escalation_is_an_immutable_value(self):
        self.advance(301)
        esc = self.tracker.escalations()[0]
        self.assertIsInstance(esc, Escalation)
        with self.assertRaises(Exception):
            esc.rung = 9


class TestCommands(unittest.TestCase):
    def test_parse_command_extracts_the_target_username(self):
        self.assertEqual(parse_command("/ping@example_codex_bot"), ("ping", "example_codex_bot"))
        self.assertEqual(parse_command("/diag@example_hermes_bot please").__class__, tuple)

    def test_parse_command_ignores_ordinary_text(self):
        self.assertIsNone(parse_command("Hermes, ping the service"))

    def test_ping_reply_is_one_line_and_in_budget(self):
        reply = ping_reply("Codex", state="idle")
        self.assertEqual(len(reply.splitlines()), 1)
        self.assertTrue(reply.startswith("PONG Codex"))
        self.assertTrue(is_within_budget(reply))

    def test_status_reply_names_task_result_and_blocker(self):
        reply = status_reply("Hermes", task="TASK-074 spec", result="draft posted",
                             blocker="none")
        self.assertIn("TASK-074", reply)
        self.assertIn("none", reply)
        self.assertTrue(is_within_budget(reply))

    def test_diag_reply_reports_unknown_rather_than_guessing_b2b(self):
        reply = diag_reply(alias="Hermes", bot="@example_hermes_bot", node="node-a",
                           transport="gateway", session="20260917_123031_ea44eb01",
                           group="-1001234567890", admin=True, seen=13)
        self.assertIn("b2b=unknown", reply)          # no API exists; the honest answer
        self.assertIn("admin=true", reply)
        self.assertIn("transport=gateway", reply)
        self.assertTrue(is_within_budget(reply))

    def test_diag_reply_states_when_it_does_not_know_something(self):
        reply = diag_reply(alias="Codex", bot="@example_codex_bot", node="build-box",
                           transport="standalone", session=None,
                           group="-1001234567890", admin=False, seen=0)
        self.assertIn("session=unknown", reply)
        self.assertIn("admin=false", reply)


class TestChannelAddressing(unittest.TestCase):
    """Rule 4 in practice: alias in the body, mention entity over a real @username."""

    def _channel(self, peers=None):
        from swarmly.telegram.channel import AgentChannel
        from swarmly.telegram.config import AgentConfig
        config = AgentConfig.from_dict({
            "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "1:2",
                      "home_channel_id": -100, "admin_user_id": 1},
            "peers": peers if peers is not None else {"Codex": "example_codex_bot"},
            "adapter": {"type": "echo"},
            "limits": {"min_seconds_between_messages": 0},
        })

        async def sender(payload, reply_to_message_id=None):
            return payload

        return AgentChannel(config, sender=sender)

    def test_alias_addressed_message_gains_a_real_mention(self):
        rendered, entities = self._channel().addressed_payload("Codex, status?")
        self.assertTrue(rendered.startswith("[Hermes] "))
        self.assertTrue(rendered.endswith("@example_codex_bot"))
        self.assertEqual(entities[0], {"type": "bold", "offset": 0, "length": 8})   # [Hermes]
        entities = entities[1:]
        self.assertEqual(len(entities), 1)
        covered = rendered.encode("utf-16-le")[int(entities[0]["offset"]) * 2:
                                        (int(entities[0]["offset"]) + int(entities[0]["length"])) * 2]
        self.assertEqual(covered.decode("utf-16-le"), "@example_codex_bot")

    def test_a_bare_alias_in_prose_is_not_an_address(self):
        """Issue #28: naming a bot in passing must not append a handle that routes to it."""
        rendered, entities = self._channel().addressed_payload(
            "Codex said the diff is fine, so merge it")
        self.assertEqual(rendered, "[Hermes] Codex said the diff is fine, so merge it")
        self.assertEqual(entities, [])

    def test_an_alias_opening_a_line_or_a_list_still_addresses(self):
        channel = self._channel(peers={"Codex": "example_codex_bot", "Antigravity": "agy_bot"})
        for text in ("Codex, status?", "Codex: status?", "Codex and Antigravity, sync please"):
            rendered, entities = channel.addressed_payload(text)
            self.assertIn("@example_codex_bot", rendered, text)
            self.assertIn("mention", [e["type"] for e in entities], text)

    def test_unaddressed_message_is_left_alone(self):
        rendered, entities = self._channel().addressed_payload("morning, all clear")
        self.assertEqual(rendered, "[Hermes] morning, all clear")
        self.assertEqual(entities, [])

    def test_peer_username_already_present_is_not_duplicated(self):
        rendered, entities = self._channel().addressed_payload("Codex @example_codex_bot are you up?")
        self.assertEqual(rendered.count("@example_codex_bot"), 1)
        self.assertEqual([e["type"] for e in entities], ["bold", "mention"])

    def test_every_addressed_peer_gets_its_own_entity(self):
        channel = self._channel(peers={"Codex": "example_codex_bot", "Antigravity": "agy_bot"})
        rendered, entities = channel.addressed_payload("Codex and Antigravity, sync please")
        covered = {rendered.encode("utf-16-le")[int(e["offset"]) * 2:(int(e["offset"]) + int(e["length"])) * 2]
                   .decode("utf-16-le") for e in entities if e["type"] != "bold"}
        self.assertEqual(covered, {"@example_codex_bot", "@agy_bot"})

    def test_a_channel_without_peers_never_mentions_anyone(self):
        rendered, entities = self._channel(peers={}).addressed_payload("Codex, status?")
        self.assertEqual(entities, [])
        self.assertNotIn("@", rendered)


# --------------------------------------------------------------------------------------
# v1.1 §5–7 wired into the channel loop: commands, ACKs, escalation (no network, injected clock)
# --------------------------------------------------------------------------------------
GROUP_ID = -1001234567890


def _wiring_event(text, username="example_codex_bot", is_bot=True, author_id=202, message_id=1):
    from swarmly.telegram.channel import ChannelEvent
    return ChannelEvent(text=text, chat_id=GROUP_ID, author_id=author_id,
                        author_username=username, author_is_bot=is_bot, message_id=message_id)


def _wiring_run(coro):
    import asyncio
    return asyncio.run(coro)


def _wiring_channel(**limits):
    """A channel with a capturing sender, an injected clock and an adapter that only counts."""
    from unittest.mock import AsyncMock

    from swarmly.telegram.adapters import EchoAdapter
    from swarmly.telegram.adapters.base import AdapterReply
    from swarmly.telegram.channel import AgentChannel
    from swarmly.telegram.config import AgentConfig

    data = {
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "1:2",
                  "home_channel_id": GROUP_ID, "admin_user_id": 1},
        "peers": {"Codex": "example_codex_bot"},
        "adapter": {"type": "echo"},
        "limits": {"min_seconds_between_messages": 0,
                   "min_seconds_between_agent_turns": 0, **limits},
    }
    clock = {"t": 1000.0}
    slept = []

    async def sleeper(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    adapter = EchoAdapter("Hermes")
    adapter.respond = AsyncMock(return_value=AdapterReply(text="adapter was here"))
    sent = []

    async def sender(payload, reply_to_message_id=None):
        sent.append(payload)
        return payload

    channel = AgentChannel(AgentConfig.from_dict(data), adapter=adapter,
                           sender=sender, clock=lambda: clock["t"], sleeper=sleeper)
    channel._sent = sent
    channel._clock_state = clock
    return channel


class TestChannelCommandWiring(unittest.TestCase):
    """§6: /ping /status /diag are answered by the transport without waking the adapter."""

    def test_ping_is_answered_with_pong_and_never_touches_the_adapter(self):
        channel = _wiring_channel()
        reply = _wiring_run(channel.handle_event(_wiring_event("/ping@HermesBot")))
        self.assertTrue(reply.startswith("PONG Hermes"))
        channel.adapter.respond.assert_not_called()

    def test_status_answers_with_three_lines_and_where_and_diag_are_other_names(self):
        """One answer for /status, /where and /diag (the admin, 25 Oct): the STATUS line
        first, so a peer still reads the answer as machine traffic, then WHERE and DIAG."""
        channel = _wiring_channel()
        status = _wiring_run(channel.handle_event(_wiring_event("/status@HermesBot", message_id=2)))
        self.assertTrue(status.startswith("STATUS Hermes"))
        self.assertEqual([line.split()[0] for line in status.splitlines()],
                         ["STATUS", "WHERE", "DIAG"])
        self.assertIn("b2b=unknown", status)
        for name, message_id in (("diag", 3), ("where", 4)):
            same = _wiring_run(channel.handle_event(_wiring_event(f"/{name}@HermesBot",
                                                                  message_id=message_id)))
            self.assertEqual([line.split()[0] for line in same.splitlines()],
                             ["STATUS", "WHERE", "DIAG"], name)
            self.assertEqual(same.splitlines()[1], status.splitlines()[1], name)
        channel.adapter.respond.assert_not_called()

    def test_a_command_for_another_bot_is_never_answered(self):
        channel = _wiring_channel()
        self.assertIsNone(_wiring_run(channel.handle_event(_wiring_event("/ping@example_codex_bot"))))
        self.assertEqual(channel.stats.handled, 0)

    def test_a_bare_command_without_addressing_is_ignored(self):
        channel = _wiring_channel()
        self.assertIsNone(_wiring_run(channel.handle_event(_wiring_event("/ping"))))

    def test_disabling_commands_routes_them_to_the_adapter_instead(self):
        channel = _wiring_channel(commands_enabled=False)
        reply = _wiring_run(channel.handle_event(_wiring_event("/ping@HermesBot")))
        self.assertIsNotNone(reply)
        channel.adapter.respond.assert_awaited_once()


class TestChannelNearMissWiring(unittest.TestCase):
    """The receiver-side log line for a marker the parser could not see (see
    ``ignored_envelope_marker``): without it the failure is silent - the message routes
    as prose and its machine intent never fires."""

    def test_marker_below_line_1_logs_a_near_miss_warning(self):
        channel = _wiring_channel()
        text = ("The tail is fully reconciled. Final message (goes to the group as my reply):\n"
                "\n"
                "[A2A v1.1] NOTE to=Codex intent=CLOSED ref=0396f793\n"
                "@example_codex_bot - corrected closure.")
        with self.assertLogs("swarmly.telegram", level="WARNING") as captured:
            _wiring_run(channel.handle_event(_wiring_event(text)))
        self.assertTrue(any("near-miss" in line for line in captured.output), captured.output)

    def test_envelope_first_message_logs_no_warning(self):
        channel = _wiring_channel()
        text = format_message(Envelope(type=MessageType.REQUEST, to="Hermes",
                                       intent="REVIEW", ref="PR#999", id="h-1"), "please review")
        with self.assertLogs("swarmly.telegram", level="INFO") as captured:
            _wiring_run(channel.handle_event(_wiring_event(text)))
        self.assertFalse(any("near-miss" in line for line in captured.output), captured.output)


class TestChannelFreezeWiring(unittest.TestCase):
    """FREEZE pauses automation while STATUS controls and liveness remain available."""

    def test_broadcast_freeze_requires_the_configured_admin(self):
        channel = _wiring_channel()
        freeze = "@HermesBot [A2A v1.1] STATUS to=* intent=FREEZE ref=freeze-unauth"
        blocked = _wiring_run(channel.handle_event(_wiring_event(
            freeze, username="CodexBot", author_id=202, is_bot=True)))
        self.assertFalse(channel._frozen)
        self.assertIn("BLOCKED", blocked)
        self.assertIn("intent=UNAUTHORIZED", blocked)
        channel.adapter.respond.assert_not_called()

    def test_a_broadcast_freeze_written_as_at_star_also_requires_the_admin(self):
        channel = _wiring_channel()
        freeze = "@HermesBot [A2A v1.1] STATUS to=@* intent=FREEZE ref=freeze-at"
        blocked = _wiring_run(channel.handle_event(_wiring_event(
            freeze, username="CodexBot", author_id=202, is_bot=True)))
        self.assertFalse(channel._frozen)
        self.assertIn("intent=UNAUTHORIZED", blocked or "")

    def test_freeze_blocks_adapter_and_resume_releases_it(self):
        channel = _wiring_channel()
        freeze = "@HermesBot [A2A v1.1] STATUS to=* intent=FREEZE ref=freeze-1"
        frozen = _wiring_run(channel.handle_event(_wiring_event(
            freeze, message_id=10, username="admin", author_id=1, is_bot=False)))
        self.assertTrue(channel._frozen)
        self.assertIn("intent=FROZEN", frozen)
        self.assertIn("ref=freeze-1", frozen)
        self.assertIn("rev=", frozen)

        channel.adapter.respond.reset_mock()
        self.assertIsNone(_wiring_run(channel.handle_event(
            _wiring_event("@HermesBot please process this", message_id=11))))
        channel.adapter.respond.assert_not_called()
        self.assertEqual(_wiring_run(channel.check_acks()), [])

        resume = "@HermesBot [A2A v1.1] STATUS to=* intent=RESUME ref=resume-1"
        resumed = _wiring_run(channel.handle_event(_wiring_event(
            resume, message_id=12, username="admin", author_id=1, is_bot=False)))
        self.assertFalse(channel._frozen)
        self.assertIn("intent=RESUMED", resumed)
        self.assertIn("ref=resume-1", resumed)
        self.assertIsNotNone(_wiring_run(channel.handle_event(
            _wiring_event("@HermesBot process this", message_id=13))))
        channel.adapter.respond.assert_awaited_once()

    def test_freeze_during_pacing_suppresses_the_automated_send(self):
        channel = _wiring_channel(min_seconds_between_messages=1)
        channel._last_send = channel._clock_state["t"]

        async def freeze_during_pace(_seconds):
            channel._frozen = True

        channel._sleep = freeze_during_pace
        self.assertIsNone(_wiring_run(channel.send("automated alert")))
        self.assertEqual(channel._sent, [])

    def test_freeze_during_pacing_does_not_track_a_suppressed_request(self):
        channel = _wiring_channel(min_seconds_between_messages=1)
        channel._last_send = channel._clock_state["t"]

        async def freeze_during_pace(_seconds):
            channel._frozen = True

        channel._sleep = freeze_during_pace
        request = "[A2A v1.1] REQUEST to=Codex intent=REVIEW id=suppressed-1\n\nreview"
        self.assertIsNone(_wiring_run(channel.send(request)))
        self.assertEqual(channel.acks.pending(), [])


class TestChannelAckWiring(unittest.TestCase):
    """§5: REQUESTs are acknowledged, inbound ACKs close expectations, ttl escalates — all silent."""

    def test_an_addressed_request_is_acked_and_still_reaches_the_adapter(self):
        channel = _wiring_channel()
        text = ("@HermesBot [A2A v1.1] REQUEST to=Hermes intent=REVIEW ref=#6 id=x-9\n\n"
                "please review the wiring")
        reply = _wiring_run(channel.handle_event(_wiring_event(text)))
        acks = [p for p in channel._sent if "[A2A v1.1] ACK" in p]
        self.assertEqual(len(acks), 1)
        self.assertIn("to=Codex", acks[0])
        self.assertIn("ref=x-9", acks[0])
        channel.adapter.respond.assert_awaited_once()
        self.assertIsNotNone(reply)

    def test_the_ack_falls_back_to_the_telegram_message_id_when_the_request_has_no_id(self):
        channel = _wiring_channel()
        text = "@HermesBot [A2A v1.1] REQUEST to=Hermes intent=REVIEW\n\nno id in this one"
        _wiring_run(channel.handle_event(_wiring_event(text, message_id=4242)))
        acks = [p for p in channel._sent if "[A2A v1.1] ACK" in p]
        self.assertEqual(len(acks), 1)
        self.assertIn("ref=4242", acks[0])

    def test_an_inbound_ack_closes_the_expectation_and_stays_silent(self):
        channel = _wiring_channel()
        _wiring_run(channel.send("[A2A v1.1] REQUEST to=Codex intent=REVIEW id=h-9-1\n\nplease review"))
        self.assertEqual(channel.acks.pending(), ["h-9-1"])
        reply = _wiring_run(channel.handle_event(
            _wiring_event("@HermesBot [A2A v1.1] ACK to=Hermes ref=h-9-1")))
        self.assertIsNone(reply)
        self.assertEqual(channel.acks.pending(), [])
        channel.adapter.respond.assert_not_called()
        channel._clock_state["t"] += 10_000
        _wiring_run(channel.check_acks())
        self.assertEqual([p for p in channel._sent if "no ACK" in p], [])

    def test_outbound_request_is_tracked_with_its_ttl_override(self):
        channel = _wiring_channel(ack_ttl_seconds=9999)
        _wiring_run(channel.send("[A2A v1.1] REQUEST to=Codex intent=REVIEW id=h-5-1 ttl=90s\n\nx"))
        channel._clock_state["t"] += 89
        self.assertEqual(channel.acks.escalations(), [])
        channel._clock_state["t"] += 2
        self.assertEqual([e.msg_id for e in channel.acks.escalations()], ["h-5-1"])

    def test_requests_without_an_id_are_not_tracked(self):
        channel = _wiring_channel()
        _wiring_run(channel.send("[A2A v1.1] REQUEST to=Codex intent=REVIEW\n\nno id"))
        self.assertEqual(channel.acks.pending(), [])

    def test_escalation_rungs_are_announced_once_each(self):
        channel = _wiring_channel(ack_ttl_seconds=60)
        _wiring_run(channel.send("[A2A v1.1] REQUEST to=Codex intent=REVIEW id=h-77-1\n\nx"))
        channel._clock_state["t"] += 61
        _wiring_run(channel.check_acks())
        posts = [p for p in channel._sent if "no ACK" in p]
        self.assertEqual(len(posts), 1)
        self.assertIn("rung 1", posts[0])
        self.assertIn("h-77-1", posts[0])
        _wiring_run(channel.check_acks())                      # no repeat within the same rung
        self.assertEqual(len([p for p in channel._sent if "no ACK" in p]), 1)
        channel._clock_state["t"] += 60
        _wiring_run(channel.check_acks())                      # next ttl → rung 2
        posts = [p for p in channel._sent if "no ACK" in p]
        self.assertEqual(len(posts), 2)
        self.assertIn("rung 2", posts[1])

    def test_uncertain_ack_send_is_not_retried_as_a_duplicate(self):
        channel = _wiring_channel(ack_ttl_seconds=60)
        _wiring_run(channel.send("[A2A v1.1] REQUEST to=Codex intent=REVIEW id=h-88-1\n\nx"))
        channel._clock_state["t"] += 61
        attempts = []

        async def uncertain_sender(payload, reply_to_message_id=None):
            attempts.append(payload)
            raise RuntimeError("connection lost after Telegram accepted the message")

        channel._sender = uncertain_sender
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            _wiring_run(channel.check_acks())
        _wiring_run(channel.check_acks())
        self.assertEqual(len(attempts), 1)


class TestWirePrefixesAndTtl(unittest.TestCase):
    def test_envelope_after_a_mention_prefix_still_parses(self):
        env, body = parse_message("@HermesBot [A2A v1.1] REQUEST to=Hermes id=x-1\n\nplease review")
        self.assertEqual(env.type, MessageType.REQUEST)
        self.assertEqual(env.to, "Hermes")
        self.assertEqual(env.id, "x-1")
        self.assertEqual(body, "please review")

    def test_a_malformed_envelope_header_degrades_to_a_note(self):
        text = "[A2A v1.1] BANANA to=Hermes\n\nhello"
        env, body = parse_message(text)
        self.assertEqual(env.type, MessageType.NOTE)
        self.assertEqual(body, text.strip())

    def test_ttl_values_parse_to_seconds(self):
        self.assertEqual(parse_ttl("30m"), 1800.0)
        self.assertEqual(parse_ttl("90s"), 90.0)
        self.assertEqual(parse_ttl("2h"), 7200.0)
        self.assertEqual(parse_ttl("45"), 45.0)
        self.assertIsNone(parse_ttl(None))
        self.assertIsNone(parse_ttl("soon"))

    def test_diag_reports_admin_status_as_unknown_when_it_cannot_be_checked(self):
        reply = diag_reply(alias="Hermes", bot="@HermesBot", node="CT100", transport="standalone",
                           session=None, group="-100", admin=None, seen=0)
        self.assertIn("admin=unknown", reply)


# --------------------------------------------------------------------------------------
# Token economy: machine classes are recorded without waking the adapter (issue #43)
# --------------------------------------------------------------------------------------


class TestMachineEventClassifier(unittest.TestCase):
    """Machine-class traffic (receipts, alerts, liveness, FYI notes) must never wake a model."""

    def test_receipt_envelope_is_recorded_without_dispatch(self):
        channel = _wiring_channel()
        text = ("@HermesBot [A2A v1.1] RECEIPT to=Hermes ref=ab12cd34 state=queued\n"
                "✅ [Codex] queued in active task (ticket ab12cd34)")
        result = _wiring_run(channel.handle_event(_wiring_event(text, message_id=30)))
        self.assertIsNone(result)
        channel.adapter.respond.assert_not_called()
        self.assertEqual(channel.stats.machine_events, 1)

    def test_alert_envelope_is_recorded_without_dispatch(self):
        channel = _wiring_channel()
        text = ("@HermesBot [A2A v1.1] ALERT to=Hermes ref=ab12cd34 kind=overdue msg=955\n"
                "⚠️ [Codex] overdue for message 955 (ticket ab12cd34).")
        result = _wiring_run(channel.handle_event(_wiring_event(text, message_id=31)))
        self.assertIsNone(result)
        channel.adapter.respond.assert_not_called()
        self.assertEqual(channel.stats.machine_events, 1)

    def test_status_online_updates_peer_state_without_dispatch(self):
        channel = _wiring_channel()
        text = "[A2A v1.1] STATUS to=* intent=ONLINE rev=c0fb4ba"
        _wiring_run(channel.handle_event(_wiring_event(text, message_id=32)))
        channel.adapter.respond.assert_not_called()
        self.assertEqual(channel.stats.machine_events, 1)
        self.assertEqual(channel.peer_last_seen()["Codex"]["rev"], "c0fb4ba")

    def test_hello_and_fyi_note_are_machine_class(self):
        channel = _wiring_channel()
        _wiring_run(channel.handle_event(_wiring_event(
            "[A2A v1.1] HELLO to=* intent=ONLINE rev=x1", message_id=33)))
        _wiring_run(channel.handle_event(_wiring_event(
            "[A2A v1.1] NOTE to=* intent=FYI\nclosure note", message_id=34)))
        channel.adapter.respond.assert_not_called()
        self.assertEqual(channel.stats.machine_events, 2)

    def test_request_still_wakes_the_adapter(self):
        channel = _wiring_channel()
        text = ("@HermesBot [A2A v1.1] REQUEST to=Hermes intent=REVIEW ref=#99 id=h-1\n"
                "please review")
        _wiring_run(channel.handle_event(_wiring_event(text, message_id=35)))
        channel.adapter.respond.assert_awaited_once()

    def test_unknown_envelope_type_falls_open_to_dispatch(self):
        channel = _wiring_channel()
        text = "@HermesBot [A2A v1.1] WEIRD to=Hermes intent=??? ref=#1\nsurprise"
        _wiring_run(channel.handle_event(_wiring_event(text, message_id=36)))
        channel.adapter.respond.assert_awaited_once()

    def test_legacy_prose_receipt_is_filtered(self):
        channel = _wiring_channel()
        text = ("@HermesBot ✅ [Codex] queued in active task "
                "(receipt only; reply pending, ticket ab12cd34)")
        _wiring_run(channel.handle_event(_wiring_event(text, message_id=37)))
        channel.adapter.respond.assert_not_called()

    def test_queued_receipt_is_emitted_as_an_envelope(self):
        from unittest.mock import AsyncMock

        from swarmly.telegram.adapters.base import AdapterReply
        channel = _wiring_channel()
        channel.adapter.respond = AsyncMock(
            return_value=AdapterReply(text="queued", meta={"queued": True}))
        _wiring_run(channel.handle_event(
            _wiring_event("hey @HermesBot please look", message_id=38)))
        self.assertTrue(channel._sent, "receipt should be sent")
        self.assertTrue(channel._sent[-1].startswith("[A2A v1.1] RECEIPT to="),
                        channel._sent[-1])

    def test_human_author_is_never_machine_class(self):
        channel = _wiring_channel(strangers="answer")
        text = "@HermesBot [A2A v1.1] RECEIPT to=Hermes ref=ab12cd34 state=queued\nhuman note"
        _wiring_run(channel.handle_event(_wiring_event(
            text, username="SomeHuman", is_bot=False, author_id=555, message_id=41)))
        channel.adapter.respond.assert_awaited_once()
        self.assertEqual(channel.stats.machine_events, 0)

    def test_alert_targets_reverse_map_to_aliases(self):
        channel = _wiring_channel()
        self.assertEqual(channel._alias_for_username("example_codex_bot"), "Codex")
        self.assertEqual(channel._alias_for_username("@Example_Codex_Bot"), "Codex")
        self.assertEqual(channel._alias_for_username("stranger"), "stranger")
        self.assertEqual(channel._alias_for_username(None), "*")

    def test_status_command_reports_machine_events(self):
        channel = _wiring_channel()
        _wiring_run(channel.handle_event(_wiring_event(
            "[A2A v1.1] STATUS to=* intent=ONLINE rev=x", message_id=39)))
        reply = _wiring_run(channel.handle_event(
            _wiring_event("/status@HermesBot", message_id=40)))
        self.assertIn("machine=", reply)
class TestChannelPromptIsGeneric(unittest.TestCase):
    """The routing prompt carries no deployment-specific handles unless configured."""

    def test_roster_and_maintainer_come_from_config(self):
        from swarmly.telegram.adapters.base import format_channel_prompt
        prompt = format_channel_prompt(
            "Admin", "hello", mentions={"Codex": "codex_x", "Antigravity": "agy_x"},
            maintainer="my_handle")
        self.assertIn("* Codex: @codex_x", prompt)
        self.assertIn("* Antigravity: @agy_x", prompt)
        self.assertIn("* Human Maintainer: @my_handle", prompt)

    def test_no_hardcoded_handles_leak_without_config(self):
        from swarmly.telegram.adapters.base import format_channel_prompt
        import re
        from tests.test_publication_hygiene import find_identifiers
        bare = format_channel_prompt("SomeoneElse", "hi")
        # The only handles are the author and the instructions' literal placeholder ...
        self.assertLessEqual(set(re.findall(r"@\w+", bare)), {"@SomeoneElse", "@username"})
        # ... and no deployment handle or display name rides along without an @.
        self.assertEqual(find_identifiers(bare), [])
        self.assertNotIn("SWARM BOT MENTIONS", bare)
        self.assertIn("@SomeoneElse", bare)

    def test_numeric_peer_ids_are_not_advertised_as_mentions(self):
        from swarmly.telegram.adapters.base import format_channel_prompt
        prompt = format_channel_prompt("Admin", "hi", mentions={"Codex": "1234567890"})
        self.assertNotIn("1234567890", prompt)


class TestQuotaAutoFreeze(unittest.TestCase):
    """#49: consecutive quota-signature adapter failures freeze the channel by themselves."""

    def _quota_failure(self, channel, text="HTTP 429: Token Plan usage limit reached (2056)"):
        from unittest.mock import AsyncMock
        channel.adapter.respond = AsyncMock(side_effect=RuntimeError(text))

    def test_three_quota_failures_freeze_the_channel(self):
        channel = _wiring_channel(quota_freeze_threshold=3)
        self._quota_failure(channel)
        for mid in (1, 2, 3):
            _wiring_run(channel.handle_event(_wiring_event("@HermesBot please process", message_id=mid)))
        self.assertTrue(channel._frozen)
        self.assertTrue(any("intent=FROZEN" in s and "ref=quota" in s for s in channel._sent))
        self.assertFalse(_wiring_run(channel.handle_event(
            _wiring_event("@HermesBot more work", message_id=9))))
        self.assertEqual(channel.adapter.respond.await_count, 3)

    def test_non_quota_failures_never_freeze(self):
        channel = _wiring_channel(quota_freeze_threshold=3)
        self._quota_failure(channel, text="boom: traceback in adapter")
        for mid in range(1, 6):
            _wiring_run(channel.handle_event(_wiring_event("@HermesBot please process", message_id=mid)))
        self.assertFalse(channel._frozen)

    def test_auto_freeze_can_be_disabled(self):
        channel = _wiring_channel(quota_freeze_threshold=2, auto_freeze_on_quota=False)
        self._quota_failure(channel)
        for mid in range(1, 5):
            _wiring_run(channel.handle_event(_wiring_event("@HermesBot please process", message_id=mid)))
        self.assertFalse(channel._frozen)


if __name__ == "__main__":
    unittest.main()


# Issue #106: ``AgentConfig.from_dict`` merges the developer's real ``.env`` into the
# process-global ``os.environ``; the guard keeps this module's tests from leaking it.
from tests import envguard  # noqa: E402 - last on purpose: the wrapper stays two lines


def load_tests(loader, tests, pattern):
    return envguard.guard(loader, tests, pattern)
