"""The live swarm dashboard as a local web page (issue #240, slice 1).

What these pin: the JSON the page polls is rendered from fixed records with nothing invented
(a bot that stopped beating is ``down``, an unreported number is ``null`` and not a zero), no
identifier beyond aliases reaches the wire, the page is self-contained and follows the
system's light or dark preference, and the server is read-only and bound to the loopback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import unittest
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

from swarmly.state import keys as K
from swarmly.state.records import (AgentInTopic, Assignment, Binding, Link, Profile, Topic)
from swarmly.telegram import webui
from swarmly.telegram.config import ChannelLimits
from swarmly.telegram.rounds import RoundState
from swarmly.telegram.snapshot import BUSY, DOWN, FROZEN, IDLE, PAUSED, STUCK, Snapshot

NOW = 1_700_000_000.0

#: The fixed registry records the panel is built from (two topics, one stalled, one due).
CHAT = -1001234567890123
THREAD = 19286
OTHER = 4242
SECRET_PATH = r"C:\Users\eftyc\swarm\notes.md"
TASK_REF = "T-19286-7"


def iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


class FakeStore:
    """A stand-in for the shared state store: just ``ready``, ``keys`` and ``read``."""

    def __init__(self, data: Dict[str, Any], *, ready: bool = True) -> None:
        self.data = dict(data)
        self.ready = ready

    def keys(self, prefix: str = "") -> list:
        return sorted(key for key in self.data if key.startswith(prefix))

    def read(self, key: str) -> Optional[Dict[str, Any]]:
        return self.data.get(key)


def records(*, phase_at: float = NOW - 3600, last_activity: Optional[str] = None,
            max_turns: int = 50, max_age_hours: Optional[float] = None) -> Dict[str, Any]:
    """Two open topics: a stalled item, closed work, a local path, a task ref, a due binding."""
    def work(link_id: str, ref: str, kind: str, phase: str, at: float,
             status: str = "open") -> Link:
        return Link(id=link_id, provider="github", kind=kind, ref=ref, role="work", title=ref,
                    added_by="Claudebot", added_at=iso(NOW - 7200), status=status, phase=phase,
                    phase_at=iso(at), assignees=[
                        Assignment(agent="DeepSeek", role="developer", assigned_by="Claudebot",
                                   assigned_at=iso(NOW - 7200))])

    dashboard = Topic(
        topic_id=THREAD, chat_id=CHAT, title="Dashboard", status="open", opened_by="Claudebot",
        opened_at=iso(NOW - 86400), last_activity=(iso(NOW - 60) if last_activity is None
                                                   else last_activity),
        links=[work("L1", "#332", "issue", "review", phase_at),
               Link(id="L2", provider="github", kind="issue",
                    ref="https://github.com/eeftychiou/swarmly/issues/240", role="reference",
                    title="#240", added_by="Claudebot", added_at=iso(NOW - 7200)),
               Link(id="L3", provider="local", kind="file", ref=SECRET_PATH, role="reference",
                    title="UX notes", added_by="admin", added_at=iso(NOW - 7200)),
               work("L4", "#300", "issue", "done", NOW - 7200, status="closed"),
               Link(id="L5", provider="topic", kind="task", ref=TASK_REF, role="reference",
                    title="A task", added_by="admin", added_at=iso(NOW - 7200))],
        agents={"Claudebot": Binding(role="coordinator", runtime="claude", model="opus",
                                     effort="high", linked_by="transport",
                                     linked_at=iso(NOW - 7200)),
                "DeepSeek": Binding(role="developer", runtime="reasonix", model="deepseek",
                                    effort="low", linked_by="Claudebot",
                                    linked_at=iso(NOW - 7200))})
    registry = Topic(
        topic_id=OTHER, chat_id=CHAT, title="Registry", status="open", opened_by="Claudebot",
        opened_at=iso(NOW - 86400), last_activity=iso(NOW - 7200),
        links=[work("L1", "#335", "pr", "development", NOW - 60)])
    closed = Topic(topic_id=999, chat_id=CHAT, title="Closed work", status="closed",
                   opened_by="Claudebot", opened_at=iso(NOW - 86400),
                   last_activity=iso(NOW - 60))
    session = AgentInTopic(session_id="20261025-deepseek", started_at=iso(NOW - 3600),
                           turns=7, rotations=1)
    profile = Profile(alias="DeepSeek", handle="@DeepSeek_dd_bot", runtime="reasonix",
                      default_model="deepseek", max_turns=max_turns,
                      max_age_hours=max_age_hours)
    return {
        K.topic(THREAD): dashboard.to_dict(),
        K.topic(OTHER): registry.to_dict(),
        K.topic(999): closed.to_dict(),
        K.topic_agent(THREAD, "DeepSeek"): session.to_dict(),
        K.agent("DeepSeek", "profile"): profile.to_dict(),
    }


def snap(alias: str, state: str = IDLE, **fields: Any) -> Snapshot:
    base: Dict[str, Any] = dict(alias=alias, state=state, since=NOW - 300, at=NOW - 5,
                                every=60.0, topic="tg-307", model="deepseek", effort="high",
                                rev="8ffe1d6")
    base.update(fields)
    return Snapshot(**base)


def usage(**fields: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = dict(turns=4, tokens_in=1200, tokens_out=340, tokens_in_known=4,
                                tokens_out_known=4, cost_usd=0.42, cost_known=4,
                                quota_left=90000, quota_resets_at=NOW + 3600)
    base.update(fields)
    return base


def state(**items: Any) -> RoundState:
    round_state = RoundState(round="237", status="ok", term=4)
    round_state.items = dict(items)
    round_state.order = list(items)
    return round_state


class Row:
    """A stand-in for one owed row (#162): only the fields the page reads."""

    def __init__(self, **fields: Any) -> None:
        self.kind = "review"
        self.item = "#241"
        self.debtor = "Reasonix"
        self.owes = "a verdict on 8ffe1d6"
        self.waiting_on = "DeepSeek"
        self.head = "8ffe1d6aaaa"
        self.topic = "tg-10134"
        self.flag = ""
        self.stalls = 1
        self.created_at = NOW - 1800
        self.deadline = NOW + 600
        for name, value in fields.items():
            setattr(self, name, value)


class TestTheState(unittest.TestCase):
    def rendered(self, **fields: Any) -> Dict[str, Any]:
        base: Dict[str, Any] = dict(
            now=NOW,
            snapshots=[snap("Claude", BUSY, since=NOW - 180, term=4, lease_ends=NOW + 600),
                       snap("DeepSeek", IDLE, since=NOW - 720),
                       snap("Codex", PAUSED, since=NOW - 3600),
                       snap("Reasonix", STUCK, since=NOW - 700, turn_elapsed=700,
                            turn_timeout=600),
                       snap("Hermes", DOWN)],
            usage={"Claude": usage(turns=9, tokens_in=15_000_000, tokens_out=4_000_000,
                                   tokens_in_known=9, tokens_out_known=9,
                                   cost_usd=22.33, cost_known=9),
                   "DeepSeek": usage(turns=4, tokens_in=900, tokens_out=None,
                                     tokens_in_known=4, tokens_out_known=0,
                                     cost_usd=0.42, cost_known=4)},
            roles={"Claude": "coordinator", "Reasonix": "reviewer", "DeepSeek": "developer"},
        )
        base.update(fields)
        return webui.watch_state(**base)

    def test_one_card_per_bot_in_alias_order_with_every_state(self):
        rendered = self.rendered()
        self.assertEqual([bot["alias"] for bot in rendered["bots"]],
                         ["Claude", "Codex", "DeepSeek", "Hermes", "Reasonix"])
        badges = {bot["alias"]: bot["badge"] for bot in rendered["bots"]}
        self.assertEqual(badges, {"Claude": "working", "Codex": "paused", "DeepSeek": "idle",
                                  "Hermes": "down", "Reasonix": "stuck"})

    def test_every_state_has_its_own_badge_and_a_frozen_bot_reads_as_frozen(self):
        self.assertEqual(webui.BADGES, {BUSY: "working", IDLE: "idle", PAUSED: "paused",
                                        FROZEN: "frozen", STUCK: "stuck", DOWN: "down"})
        rendered = self.rendered(snapshots=[snap("Antigravity", FROZEN, since=NOW - 5400)])
        self.assertEqual(rendered["bots"][0]["badge"], "frozen")
        self.assertEqual(rendered["bots"][0]["since"], 5400.0)

    def test_a_working_bot_says_the_topic_and_how_long_the_turn_has_run(self):
        claude = self.rendered()["bots"][0]
        self.assertEqual(claude["topic"], "tg-307")
        self.assertEqual(claude["turn_seconds"], 0.0)          # no turn in flight in the record
        reasonix = [bot for bot in self.rendered()["bots"] if bot["alias"] == "Reasonix"][0]
        self.assertEqual(reasonix["turn_seconds"], 700.0)
        self.assertEqual(reasonix["turn_timeout"], 600.0)

    def test_a_bot_that_stopped_beating_is_down_whatever_it_last_said(self):
        rendered = self.rendered(snapshots=[snap("Hermes", BUSY, at=NOW - 600)])
        self.assertEqual(rendered["bots"][0]["badge"], "down")
        self.assertEqual(rendered["bots"][0]["since"], 600.0)   # how long it has been quiet

    def test_a_number_nobody_reported_is_null_and_never_a_zero(self):
        hermes = [bot for bot in self.rendered()["bots"] if bot["alias"] == "Hermes"][0]
        for field in ("turns", "tokens_in", "tokens_out", "cost"):
            self.assertIsNone(hermes[field], field)
        deepseek = [bot for bot in self.rendered()["bots"] if bot["alias"] == "DeepSeek"][0]
        self.assertEqual(deepseek["tokens_in"], 900)
        self.assertIsNone(deepseek["tokens_out"])              # never a made-up zero
        self.assertEqual(deepseek["cost"], 0.42)

    def test_the_swarm_total_says_how_many_bots_reported(self):
        total = self.rendered()["total"]
        self.assertEqual(total["bots"], 5)
        self.assertEqual(total["turns"], 13)                    # 9 + 4, the rest unknown
        self.assertEqual(total["tokens_in"], 15_000_900)
        self.assertEqual(total["reported_turns"], 2)
        self.assertEqual(total["tokens_out"], 4_000_000)        # one side, as reported

    def test_the_strip_carries_the_lease_and_the_round(self):
        rendered = self.rendered(coordinator="Claude", round_state=state(**{
            "241": {"state": "merged", "owner": "DeepSeek", "pr": 254},
            "240": {"state": "open", "owner": "DeepSeek", "pr": ""},
        }))
        self.assertEqual(rendered["coordinator"], "Claude")
        self.assertEqual(rendered["lease"]["term"], None)       # no lease record in this call
        self.assertEqual(rendered["round"]["name"], "237")
        self.assertEqual((rendered["round"]["done"], rendered["round"]["total"]), (1, 2))

    def test_the_live_lease_wins_over_a_name_the_caller_remembers(self):
        class Lease:
            holder, term, expires_at, status = "Claude", 4, NOW + 600, "ok"
        rendered = self.rendered(coordinator="Reasonix", lease=Lease())
        self.assertEqual(rendered["coordinator"], "Claude")      # the folded lease, not the name
        self.assertEqual(rendered["lease"]["holder"], "Claude")
        self.assertEqual(rendered["lease"]["expires_in"], 600.0)

    def test_the_open_debts_say_who_owes_what_and_for_how_long(self):
        owed = self.rendered(owed=[Row(), Row(item="", owes="an answer")])["owed"]
        self.assertEqual(owed[0]["debtor"], "Reasonix")
        self.assertEqual(owed[0]["item"], "#241")
        self.assertEqual(owed[0]["head"], "8ffe1d6")
        self.assertEqual(owed[0]["open_seconds"], 1800.0)
        self.assertEqual(owed[0]["due_in"], 600.0)
        self.assertIsNone(owed[1]["item"])


class TestNoLeaks(unittest.TestCase):
    def payload(self) -> str:
        rendered = webui.watch_state(
            now=NOW,
            snapshots=[snap("DeepSeek")],
            usage={"DeepSeek": usage(chat_id=-1001234567890, thread_id=1336,
                                     token="123456:AAH-secret",
                                     path=r"C:\Users\eftyc\secret")},
            roles={"DeepSeek": "developer"},
            coordinator="DeepSeek",
            round_state=state(**{"241": {"state": "open", "owner": "DeepSeek",
                                         "chat_id": -1001234567890123, "pr": 254,
                                         "token": "123456:AAH-secret"}}),
            owed=[Row(chat_id=-1001234567890123, thread_id=1336)])
        return json.dumps(rendered, sort_keys=True)

    def test_the_json_carries_no_identifier_beyond_aliases(self):
        payload = self.payload()
        for secret in ("1001234567890123", "1001234567890", "1336", "123456:AAH",
                       "C:\\\\Users", "eftyc"):
            self.assertNotIn(secret, payload)

    def test_the_page_carries_no_identifier_either(self):
        page = webui.page_html(title="Swarm")
        for secret in ("1001234567890", "123456:AAH", "eftyc"):
            self.assertNotIn(secret, page)

    def test_a_record_cannot_carry_a_chat_id_in_the_first_place(self):
        with self.assertRaises(TypeError):
            Snapshot(alias="DeepSeek", state=IDLE, chat_id=-1001234567890)


class TestThePage(unittest.TestCase):
    def page(self) -> str:
        return webui.page_html(title="Swarm <test>")

    def test_it_fetches_nothing_from_anywhere_else(self):
        page = self.page()
        for marker in ("http://", "https://", "//cdn", "<script src", "<link rel=\"stylesheet\""):
            self.assertNotIn(marker, page)

    def test_it_follows_the_system_light_and_dark_and_fits_a_phone(self):
        page = self.page()
        self.assertIn("prefers-color-scheme: dark", page)
        self.assertIn('name="viewport"', page)
        self.assertIn("width=device-width", page)

    def test_it_polls_the_state_endpoint_on_its_own(self):
        page = self.page()
        self.assertIn(webui.STATE_PATH, page)
        self.assertIn("setInterval(tick,", page)
        self.assertIn('cache: "no-store"', page)

    def test_the_title_is_escaped(self):
        self.assertIn("Swarm &lt;test&gt;", self.page())


class TestTheServer(unittest.TestCase):
    def server(self, provider: Any = None) -> webui.DashboardServer:
        payload = {"bots": [{"alias": "Claude", "badge": "working"}], "at": NOW}
        return webui.DashboardServer(provider or (lambda: payload), port=0)

    def get(self, url: str) -> Any:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()

    def test_the_page_and_the_state_are_served_on_the_loopback(self):
        server = self.server()
        try:
            url = server.start()
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)
            status, body = self.get(url)
            self.assertEqual(status, 200)
            self.assertIn(b"<html", body)
            self.assertIn(b"Swarm", body)
            status, body = self.get(url.rstrip("/") + webui.STATE_PATH)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["bots"][0]["alias"], "Claude")
        finally:
            server.stop()
        self.assertFalse(server.running)

    def test_anything_else_is_a_404(self):
        server = self.server()
        try:
            url = server.start()
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(url.rstrip("/") + "/etc/passwd")
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.stop()

    def test_a_write_is_refused_because_the_page_is_read_only(self):
        server = self.server()
        try:
            url = server.start()
            request = urllib.request.Request(url.rstrip("/") + webui.STATE_PATH,
                                             data=b'{"pause":"all"}', method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(request)
            self.assertEqual(caught.exception.code, 405)
        finally:
            server.stop()

    def test_a_provider_that_fails_is_a_503_not_a_traceback(self):
        def broken() -> Any:
            raise RuntimeError("state is unreadable")
        server = self.server(broken)
        try:
            url = server.start()
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(url.rstrip("/") + webui.STATE_PATH)
            self.assertEqual(caught.exception.code, 503)
            self.assertNotIn(b"Traceback", caught.exception.read())
        finally:
            server.stop()

    def test_starting_twice_keeps_one_server_and_one_port(self):
        server = self.server()
        try:
            first = server.start()
            self.assertEqual(server.start(), first)
            self.assertTrue(server.running)
        finally:
            server.stop()


class TestTheTopicsPanel(unittest.TestCase):
    """#332: one card per open topic, read from the store's registry and nothing else."""

    def store(self, *, ready: bool = True) -> Any:
        return FakeStore(records(), ready=ready)

    def panel(self, **fields: Any) -> Dict[str, Any]:
        base: Dict[str, Any] = dict(now=NOW, store=self.store(),
                                    drift={(THREAD, "deepseek"): {"score": 6, "hard": 1}})
        base.update(fields)
        return webui.registry_panel(**base)

    def test_one_card_per_open_topic_with_its_links_status_phase_and_assignees(self):
        cards = self.panel()["topics"]
        self.assertEqual([card["topic"] for card in cards], ["Dashboard", "Registry"])
        dashboard = cards[0]
        self.assertEqual([link["id"] for link in dashboard["links"]],
                         ["L1", "L2", "L3", "L4", "L5"])
        work = dashboard["links"][0]
        self.assertEqual((work["ref"], work["status"], work["phase"]),
                         ("#332", "open", "review"))
        self.assertEqual(work["assignees"], [{"agent": "DeepSeek", "as": "developer"}])
        self.assertEqual(cards[1]["links"][0]["phase"], "development")

    def test_most_recently_active_topic_comes_first(self):
        topics = self.panel()["topics"]
        self.assertLess(topics[0]["updated"], topics[1]["updated"])
        self.assertEqual(topics[0]["updated"], 60.0)
        # A topic that never said when it was active goes last, and is not made a zero.
        records_quiet = records(last_activity="")
        cards = webui.registry_panel(now=NOW, store=FakeStore(records_quiet))["topics"]
        self.assertEqual(cards[-1]["topic"], "Dashboard")
        self.assertIsNone(cards[-1]["updated"])

    def test_an_item_with_no_phase_change_past_the_nudge_threshold_is_stalled(self):
        self.assertEqual(webui.STALL_SECONDS, ChannelLimits().stall_minutes * 60.0)
        cards = self.panel()["topics"]
        self.assertTrue(cards[0]["links"][0]["stalled"])       # review, an hour ago
        self.assertFalse(cards[1]["links"][0]["stalled"])      # development, a minute ago
        self.assertTrue(cards[0]["stalled"])
        self.assertFalse(cards[1]["stalled"])
        self.assertFalse(cards[0]["links"][1].get("stalled"))   # a reference has no phase

    def test_closed_work_reads_closed_and_is_never_stalled(self):
        closed = self.panel()["topics"][0]["links"][3]
        self.assertEqual((closed["status"], closed["phase"]), ("closed", "done"))
        self.assertFalse(closed["stalled"])

    def test_a_task_ref_withholds_its_thread_id_and_a_local_ref_its_path(self):
        links = self.panel()["topics"][0]["links"]
        task = links[4]
        self.assertIsNone(task["ref"])                     # T-<thread>-<n> carries a thread id
        self.assertEqual((task["role"], task["name"]), ("reference", "A task"))
        self.assertIsNone(links[2]["ref"])                 # a local ref is a path
        self.assertEqual(links[2]["name"], "UX notes")

    def test_a_phase_change_clears_the_stall(self):
        fresh = records(phase_at=NOW - 60)
        cards = webui.registry_panel(now=NOW, store=FakeStore(fresh))["topics"]
        self.assertFalse(cards[0]["links"][0]["stalled"])

    def test_the_ladder_turned_off_flags_nothing_from_age(self):
        cards = webui.registry_panel(now=NOW, store=FakeStore(records()),
                                     stall_seconds=float("inf"))["topics"]
        self.assertFalse(cards[0]["links"][0]["stalled"])

    def test_a_binding_due_for_rotation_says_why(self):
        bindings = {b["agent"]: b for b in self.panel()["topics"][0]["bindings"]}
        self.assertEqual(bindings["DeepSeek"]["turns"], 7)
        self.assertEqual(bindings["DeepSeek"]["rotations"], 1)
        self.assertEqual(bindings["DeepSeek"]["drift"], 6)
        self.assertEqual(bindings["DeepSeek"]["rotation_due"], "drift")
        # No session record yet: unknown, never a zero, and never a made-up rotation.
        self.assertIsNone(bindings["Claudebot"]["turns"])
        self.assertIsNone(bindings["Claudebot"]["drift"])
        self.assertIsNone(bindings["Claudebot"]["rotation_due"])

    def test_a_profiles_turn_limit_also_makes_a_rotation_due(self):
        asked = records(max_turns=5)
        cards = webui.registry_panel(now=NOW, store=FakeStore(asked))["topics"]
        binding = [b for b in cards[0]["bindings"] if b["agent"] == "DeepSeek"][0]
        self.assertEqual(binding["rotation_due"], "turns")

    def test_a_profiles_age_limit_also_makes_a_rotation_due(self):
        # The session started an hour ago; half an hour of age is the limit (QA, #337).
        old = records(max_age_hours=0.5)
        cards = webui.registry_panel(now=NOW, store=FakeStore(old))["topics"]
        binding = [b for b in cards[0]["bindings"] if b["agent"] == "DeepSeek"][0]
        self.assertEqual(binding["session_age"], 3600.0)
        self.assertEqual(binding["rotation_due"], "age")
        # A limit the session has not reached trips nothing, and drift outranks both.
        young = records(max_age_hours=4)
        cards = webui.registry_panel(now=NOW, store=FakeStore(young))["topics"]
        self.assertIsNone(cards[0]["bindings"][1]["rotation_due"])
        cards = webui.registry_panel(now=NOW, store=FakeStore(old),
                                     drift={(THREAD, "deepseek"): {"score": 6, "hard": 0}}
                                     )["topics"]
        self.assertEqual(cards[0]["bindings"][1]["rotation_due"], "drift")

    def test_a_store_that_is_not_ready_says_not_ready_and_not_an_empty_panel(self):
        self.assertEqual(self.panel(store=self.store(ready=False)),
                         {"ready": False, "topics": []})
        self.assertEqual(self.panel(store=None), {"ready": False, "topics": []})

    def test_only_aliases_names_and_refs_reach_the_wire(self):
        payload = json.dumps(self.panel(), sort_keys=True)
        for secret in (str(CHAT), str(THREAD), str(OTHER), "Users", "notes.md", "C:\\\\",
                       TASK_REF):
            self.assertNotIn(secret, payload)
        for kept in ("Dashboard", "Registry", "#332", "#335", "DeepSeek", "UX notes"):
            self.assertIn(kept, payload)

    def test_the_state_carries_the_panel(self):
        rendered = webui.watch_state(now=NOW, registry=self.panel())
        self.assertTrue(rendered["registry"]["ready"])
        self.assertEqual(len(rendered["registry"]["topics"]), 2)
        self.assertIsNone(webui.watch_state(now=NOW)["registry"])

    def test_the_page_shows_the_panel_and_says_so_when_it_is_not_ready(self):
        page = webui.page_html(title="Swarm")
        for marker in ('id="topics"', "topicCard(", "registry not ready", "no open topics yet",
                       "rotation due"):
            self.assertIn(marker, page)
        # The card paints a work item's status and kind beside its phase: closed work must
        # not read as open (review, #337).
        self.assertIn('if (link.status) line.append(el("span", "badge "', page)
        self.assertIn('if (link.kind) line.append(el("span", "sub", link.kind))', page)
        rendered = webui.watch_state(now=NOW, registry={"ready": False, "topics": []})
        self.assertFalse(rendered["registry"]["ready"])


class TestTheLeaseHoldersTransportServesIt(unittest.TestCase):
    """#240: the page belongs to the lease holder, like the pin -- and closes with the lease."""

    def channel(self, **web: Any) -> Any:
        from tests.test_telegram_channel import make_channel, make_config
        return make_channel(make_config(web=dict({"enabled": True, "port": 0}, **web)))

    def test_no_web_section_or_a_disabled_one_opens_no_port(self):
        from tests.test_telegram_channel import make_channel, make_config
        for config in (make_config(), make_config(web={"enabled": False})):
            channel = make_channel(config)
            self.assertEqual(channel._sync_web_dashboard(), "off")
            self.assertIsNone(getattr(channel, "_web_server", None))

    def test_a_bot_that_does_not_hold_the_lease_serves_nothing(self):
        channel = self.channel()
        channel.is_coordinator = lambda now=None: False
        self.assertIn("not the lease holder", channel._sync_web_dashboard())
        self.assertIsNone(getattr(channel, "_web_server", None))

    def test_the_lease_holder_serves_the_page_and_losing_the_lease_closes_it(self):
        channel = self.channel()
        channel.is_coordinator = lambda now=None: True
        said = channel._sync_web_dashboard()
        try:
            self.assertTrue(said.startswith("serving http://127.0.0.1:"), said)
            self.assertTrue(channel._web_server.running)
            self.assertTrue(channel._sync_web_dashboard().startswith("serving"))
        finally:
            channel.is_coordinator = lambda now=None: False
            self.assertIn("stopped", channel._sync_web_dashboard())
        self.assertIsNone(channel._web_server)

    def test_the_page_reads_this_bots_own_records_and_no_identifier(self):
        channel = self.channel()
        rendered = json.dumps(channel.web_state(), sort_keys=True)
        self.assertIn("Hermes", rendered)                  # its own alias
        from tests.test_telegram_channel import GROUP
        self.assertNotIn(str(GROUP), rendered)
        self.assertNotIn("TEST", rendered)                 # the bot token never reaches a page

    def test_the_topics_panel_says_not_ready_without_a_store_then_follows_a_phase_change(self):
        from tests.test_telegram_channel import GROUP
        channel = self.channel()
        self.assertFalse(channel.web_state()["registry"]["ready"])
        channel.state_link.store = FakeStore(records())
        panel = channel.web_state()["registry"]
        self.assertTrue(panel["ready"])
        self.assertEqual(panel["topics"][0]["links"][0]["phase"], "review")
        moved = Topic.from_dict(records()[K.topic(THREAD)])
        moved.links[0].phase = "qa"
        channel.state_link.store.data[K.topic(THREAD)] = moved.to_dict()
        # One refresh later the page has it: the panel is read per request, never cached.
        panel = channel.web_state()["registry"]
        self.assertEqual(panel["topics"][0]["links"][0]["phase"], "qa")
        self.assertNotIn(str(GROUP), json.dumps(panel))
        # The threshold is the stall ladder's own, and 0 (the ladder off) flags nothing.
        self.assertEqual(channel.registry_stall_seconds(), 30 * 60.0)
        from tests.test_telegram_channel import make_channel, make_config
        off = make_channel(make_config(limits={"stall_minutes": 0}))
        self.assertEqual(off.registry_stall_seconds(), float("inf"))
        off.state_link.store = FakeStore(records())
        self.assertFalse(off.web_state()["registry"]["topics"][0]["links"][0]["stalled"])



ADMIN_ID = 424242
TOKEN = "123456:TEST-TOKEN"

def init_data(*, token: str = TOKEN, admin: int = ADMIN_ID, auth_date: float = NOW,
              hash_override: str = "", **extra: Any) -> str:
    """A Telegram Mini App ``initData`` string, signed the way Telegram signs one.

    Written from Telegram's Web App docs, not from ``webui.verify_init_data``: ``key`` is
    ``HMAC-SHA256("WebAppData", bot_token)`` and the hash is ``HMAC-SHA256(key, sorted
    ``k=v`` lines joined by newlines)``. The signer here and the checker there agreeing is
    therefore two readings of the spec, not one function checking itself (review, #258).
    """
    fields = {"auth_date": str(int(auth_date)), "user": json.dumps({"id": admin}),
              "query_id": "AAH-test"}
    fields.update({key: str(value) for key, value in extra.items()})
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    fields["hash"] = hash_override or hmac.new(secret, check.encode("utf-8"),
                                               hashlib.sha256).hexdigest()
    return urlencode(fields)


class TestWhoMayRead(unittest.TestCase):
    """#240 slice 2: the Mini App's ``Host`` and ``initData`` rules (review, #256)."""

    def server(self, **fields: Any) -> Any:
        base: Dict[str, Any] = dict(provider=lambda: {"ok": True}, port=0)
        base.update(fields)
        return webui.DashboardServer(**base)

    def get(self, url: str, *, host: str = "", headers: Optional[Dict[str, str]] = None) -> Any:
        wanted = dict(headers or {})
        if host:
            wanted["Host"] = host
        request = urllib.request.Request(url, headers=wanted)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_a_host_the_operator_did_not_name_is_refused(self):
        """DNS rebinding: a page on a name pointing at 127.0.0.1 is same-origin with this one."""
        server = self.server()
        try:
            url = server.start() + webui.STATE_PATH.lstrip("/")
            self.assertEqual(self.get(url)[0], 200)                    # the loopback itself
            self.assertEqual(self.get(url, host="evil.example.com")[0], 404)
            self.assertEqual(self.get(url.replace(webui.STATE_PATH, "/"),
                                      host="evil.example.com")[0], 404)
        finally:
            server.stop()

    def test_the_configured_public_host_is_served_and_only_with_init_data(self):
        server = self.server(public_host="swarm.example.ts.net", bot_token=TOKEN,
                             admin_id=ADMIN_ID, init_max_age=300.0)
        try:
            url = server.start() + webui.STATE_PATH.lstrip("/")
            host = "swarm.example.ts.net"
            # The operator on the host itself keeps the plain local page (review, #258).
            self.assertEqual(self.get(url)[0], 200)
            self.assertEqual(self.get(url, host="localhost")[0], 200)
            self.assertEqual(self.get(url, host=host)[0], 403)         # no payload
            fresh = init_data(auth_date=time.time())
            self.assertEqual(self.get(url, host=host,
                                      headers={webui.INIT_HEADER: fresh})[0], 200)
            self.assertEqual(self.get(url, host=host,
                                      headers={webui.INIT_HEADER:
                                               init_data(hash_override="0" * 64)})[0], 403)
            stale = init_data(auth_date=time.time() - 3600)
            self.assertEqual(self.get(url, host=host,
                                      headers={webui.INIT_HEADER: stale})[0], 403)
            self.assertEqual(self.get(url, host=host,
                                      headers={webui.INIT_HEADER:
                                               init_data(admin=99)})[0], 403)
            # The page itself is harmless without state, and is what a Mini App opens first.
            self.assertEqual(self.get(server.url, host=host)[0], 200)
        finally:
            server.stop()

    def test_init_data_checked_on_its_own(self):
        now = NOW
        ok, why = webui.verify_init_data(init_data(auth_date=now), bot_token=TOKEN,
                                         admin_id=ADMIN_ID, now=now)
        self.assertTrue(ok, why)
        for raw, reason in (
                ("", "nothing"),
                (init_data(token="999:OTHER", auth_date=now), "signed with another token"),
                (init_data(auth_date=now - 10_000), "replayed"),
                (init_data(auth_date=now + 3600), "dated in the future"),
                (init_data(admin=1, auth_date=now), "not the admin"),
                ("auth_date=%d&user=%%7B%%7D" % int(now), "no hash"),
        ):
            ok, _ = webui.verify_init_data(raw, bot_token=TOKEN, admin_id=ADMIN_ID, now=now)
            self.assertFalse(ok, reason)

    def test_no_token_or_no_admin_means_no_access(self):
        raw = init_data(auth_date=NOW)
        self.assertFalse(webui.verify_init_data(raw, bot_token="", admin_id=ADMIN_ID,
                                                now=NOW)[0])
        self.assertFalse(webui.verify_init_data(raw, bot_token=TOKEN, admin_id=0,
                                                now=NOW)[0])

    def test_a_dead_serve_thread_is_not_reported_as_running(self):
        server = self.server()
        try:
            server.start()
            self.assertTrue(server.running)
            server._thread.join(0)                      # pretend it died
            server._thread._target = lambda: None        # (the thread object itself is alive)
        finally:
            server.stop()
        self.assertFalse(server.running)


class TestThePinnedCardsButton(unittest.TestCase):
    """#240 slice 2: a ``web_app`` button exists only when there is somewhere to open."""

    def channel(self, **web: Any) -> Any:
        from tests.test_telegram_channel import make_channel, make_config
        return make_channel(make_config(web=dict({"enabled": True, "port": 8787}, **web)))

    def test_no_public_host_means_no_button_and_no_url(self):
        channel = self.channel()
        self.assertEqual(channel.mini_app_url(), "")
        for row in channel.control_markup():
            for button in row:
                self.assertNotIn("web_app", button)

    def test_a_disabled_section_draws_no_button_even_with_a_public_host(self):
        """A button that opens nothing is worse than no button (review, #258)."""
        from tests.test_telegram_channel import make_channel, make_config
        channel = make_channel(make_config(web={"enabled": False, "port": 8787,
                                                "public_host": "swarm.example.ts.net"}))
        self.assertEqual(channel.mini_app_url(), "")
        self.assertFalse(any("web_app" in button
                             for row in channel.control_markup() for button in row))

    def test_a_non_443_public_port_goes_in_the_host(self):
        channel = self.channel(public_host="swarm.example.ts.net:9443")
        self.assertEqual(channel.mini_app_url(), "https://swarm.example.ts.net:9443/")

    def test_a_public_host_puts_the_button_first(self):
        channel = self.channel(public_host="swarm.example.ts.net")
        # The public name is served on 443 by the tunnel: web.port is the loopback *bind* port
        # and must not appear, or the button opens a dead address (review, #258).
        self.assertEqual(channel.mini_app_url(), "https://swarm.example.ts.net/")
        first = channel.control_markup()[0][0]
        self.assertEqual(first["text"], "Open dashboard")
        self.assertEqual(first["web_app"], {"url": "https://swarm.example.ts.net/"})
        # The controls are still there, below it.
        self.assertTrue(any("callback_data" in button
                            for row in channel.control_markup() for button in row))

    def test_a_public_host_makes_the_server_ask_for_init_data_and_allow_that_host(self):
        channel = self.channel(public_host="swarm.example.ts.net")
        channel.is_coordinator = lambda now=None: True
        said = channel._sync_web_dashboard()
        try:
            self.assertTrue(said.startswith("serving"), said)
            server = channel._web_server
            self.assertTrue(server.requires_init)
            self.assertIn("swarm.example.ts.net", server.hosts)
            self.assertIn("127.0.0.1", server.hosts)
        finally:
            channel._sync_web_dashboard()

    def test_a_taken_port_is_retried_on_an_interval_not_every_beat(self):
        """A bind that fails is one warning and a quiet minute, not one per heartbeat."""
        channel = self.channel(port=8787)
        channel.is_coordinator = lambda now=None: True
        tries: list = []

        def refuse(self: Any) -> str:
            tries.append(1)
            raise OSError("[WinError 10048] only one usage of each socket address")

        original = webui.DashboardServer.start
        webui.DashboardServer.start = refuse
        try:
            said = channel._sync_web_dashboard()
            self.assertTrue(said.startswith("failed"), said)
            self.assertIn("waiting to retry", channel._sync_web_dashboard())
            self.assertEqual(len(tries), 1)              # the beat after it did not try again
            self.assertIsNone(getattr(channel, "_web_server", None))
        finally:
            webui.DashboardServer.start = original

class TestTheLookAndLayout(unittest.TestCase):
    """#343: one shell (header, navigation, sections) and a design system behind it."""

    SECTIONS = ("swarm", "round", "topics", "usage", "controls")

    def page(self) -> str:
        return webui.page_html(title="Swarm")

    def blocks(self) -> Tuple[str, str]:
        """The base token block and the ``prefers-color-scheme`` dark one."""
        palettes = self.palettes()
        return palettes[":root"], palettes["@media dark"]

    def palettes(self) -> Dict[str, str]:
        """Every block that defines tokens, by the selector (or media query) that holds it.

        Four in all: the base ``:root`` palette, the same palette under
        ``prefers-color-scheme: dark``, and the two the Mini App's scheme attribute selects
        (review, #347).
        """
        page = self.page()
        wanted = {
            ":root": r"(?<!\])\:root\s*\{(.*?)\}",
            "@media dark": r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{(.*?)\}",
            'attribute dark': r':root\[data-theme="dark"\]\s*\{(.*?)\}',
            'attribute light': r':root\[data-theme="light"\]\s*\{(.*?)\}',
        }
        found: Dict[str, str] = {}
        for name, pattern in wanted.items():
            match = re.search(pattern, page, re.S)
            self.assertIsNotNone(match, "the %s token block is missing" % name)
            found[name] = match.group(1) if match else ""
        return found

    @staticmethod
    def colours(block: str) -> Dict[str, str]:
        """The colour tokens of a block: anything whose value holds a colour literal."""
        return {name: value.strip() for name, value in
                dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block)).items()
                if "#" in value or "rgb" in value}

    @staticmethod
    def tokens(block: str) -> Dict[str, str]:
        return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))

    def test_every_navigation_anchor_lands_on_a_section(self):
        page = self.page()
        anchors = set(re.findall(r'href="#([\w-]+)"', page))
        self.assertEqual(anchors, set(self.SECTIONS))
        for name in self.SECTIONS:
            self.assertIn('id="%s"' % name, page, name)

    def test_the_header_shows_the_swarms_health_at_a_glance(self):
        page = self.page()
        self.assertIn('id="health"', page)
        for marker in ("round", "coordinator", "lease", "bots", "today"):
            self.assertIn('pill("%s"' % marker, page, marker)
        # The counts come from the same cards the swarm section draws: one source, two views.
        self.assertIn("counts[bot.badge]", page)

    def test_both_colour_schemes_define_every_colour_token(self):
        light, dark = self.blocks()
        colours = {name: value for name, value in self.tokens(light).items()
                   if "#" in value or "rgb" in value}
        self.assertTrue(colours, "no colour tokens at all")
        missing = sorted(set(colours) - set(self.tokens(dark)))
        self.assertEqual(missing, [], "the dark scheme leaves these undefined")

    def test_every_token_the_stylesheet_uses_is_defined(self):
        page = self.page()
        light, _dark = self.blocks()
        defined = set(self.tokens(light))
        used = set(re.findall(r"var\((--[\w-]+)\)", page))
        self.assertEqual(sorted(used - defined), [], "a var() names a token nothing defines")

    def test_every_scheme_block_is_a_whole_palette(self):
        """Review #347: a scheme must never inherit a token from the other one."""
        palettes = self.palettes()
        base = self.colours(palettes[":root"])
        dark = self.colours(palettes["@media dark"])
        self.assertTrue(base and dark, "no colour tokens found")
        for name in ("attribute dark", "attribute light"):
            self.assertTrue(self.colours(palettes[name]), name)
        # The two dark blocks and the two light blocks are the same palettes.
        self.assertEqual(self.colours(palettes['attribute dark']), dark,
                         "the data-theme=dark palette drifted from prefers-color-scheme")
        self.assertEqual(self.colours(palettes['attribute light']), base,
                         "the data-theme=light palette drifted from the base palette")
        # Every colour token of one palette exists in the other: no leftovers, no gaps.
        self.assertEqual(sorted(base), sorted(dark))

    def test_it_stays_phone_width_first_and_two_columns_when_there_is_room(self):
        page = self.page()
        self.assertIn('name="viewport"', page)
        self.assertIn("width=device-width", page)
        self.assertIn("@media (min-width: 760px)", page)
        self.assertIn("grid-template-columns", page)
        # Nothing is pinned to a fixed pixel width, so nothing can overflow a 360px phone.
        # (min-width is the breakpoint itself; the only fixed sizes are inline-block badges.)
        self.assertEqual(re.findall(r"(?<!min-)(?<!max-)width:\s*[\d.]+px", page), [])

    def test_the_telegram_theme_params_are_read_without_loading_anything(self):
        page = self.page()
        self.assertIn("tgWebAppThemeParams", page)
        self.assertIn("tgWebAppColorScheme", page)
        # The scheme a Mini App reports is *applied*, not only mentioned: it lands on the
        # attribute the stylesheet gives a whole palette to (review, #347).
        self.assertIn("document.documentElement.dataset.theme = SCHEME", page)
        for name in ('[data-theme="dark"]', '[data-theme="light"]'):
            self.assertIn(name, page)
        # ... and a chat that sends colours but no scheme still gets the right palette.
        self.assertIn("function luma(", page)
        self.assertIn("light < 0.45 ? \"dark\" : \"light\"", page)
        # The chat's own colours go on top of that palette, as inline overrides.
        self.assertIn("--bg", page)
        self.assertIn("style.setProperty(token, THEME_PARAMS[key])", page)
        for marker in ("http://", "https://", "<script src", "<link rel=\"stylesheet\""):
            self.assertNotIn(marker, page)

    def test_unknown_values_still_read_as_a_question_mark(self):
        page = self.page()
        self.assertIn('? "?" : String(value)', page)          # num()
        self.assertIn('? "?" : "$" + Number(value).toFixed(2)', page)   # money()
class TestTheWritePath(unittest.TestCase):
    """#342: ``POST /api/action`` -- one write, behind the admin's Mini App login."""

    def server(self, action: Any = None, **fields: Any) -> Any:
        self.asked: list = []

        def handler(payload: Any) -> Any:
            self.asked.append(dict(payload))
            return {"ok": True, "verdict": "run", "text": "Pause done.", "action": "bot.pause"}

        base: Dict[str, Any] = dict(provider=lambda: {"bots": []}, port=0, bot_token=TOKEN,
                                    admin_id=ADMIN_ID, action=action or handler)
        base.update(fields)
        return webui.DashboardServer(**base)

    def post(self, url: str, body: Any = None, *, init: str = "", origin: str = "",
             method: str = "POST", path: str = "") -> Any:
        data = body if isinstance(body, bytes) else json.dumps(body or {"action": "bot.pause"})
        if not isinstance(data, bytes):
            data = data.encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if init:
            headers[webui.INIT_HEADER] = init
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(url.rstrip("/") + (path or webui.ACTION_PATH),
                                         data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_a_write_without_init_data_is_refused_even_on_the_loopback(self):
        server = self.server()
        try:
            url = server.start()
            status, answer = self.post(url)
            self.assertEqual(status, 403)
            self.assertEqual(answer["refused"], "auth")
            self.assertFalse(answer["ok"])
            self.assertEqual(self.asked, [])                  # nothing ran
            # ... and the page itself still reads as before, with no initData (#258).
            with urllib.request.urlopen(url.rstrip("/") + webui.STATE_PATH, timeout=5) as got:
                self.assertEqual(got.status, 200)
        finally:
            server.stop()

    def test_a_forged_or_expired_login_is_refused(self):
        server = self.server()
        try:
            url = server.start()
            forged = init_data(hash_override="0" * 64)
            self.assertEqual(self.post(url, init=forged)[0], 403)
            stale = init_data(auth_date=NOW - webui.INIT_MAX_AGE - 60)
            self.assertEqual(self.post(url, init=stale)[0], 403)
            self.assertEqual(self.asked, [])
        finally:
            server.stop()

    def test_a_login_that_is_not_the_admins_is_refused(self):
        server = self.server()
        try:
            url = server.start()
            status, answer = self.post(url, init=init_data(admin=ADMIN_ID + 1))
            self.assertEqual(status, 403)
            self.assertEqual(answer["refused"], "auth")
            self.assertEqual(self.asked, [])
        finally:
            server.stop()

    def test_a_verified_admin_write_runs_once_with_the_policys_own_words(self):
        server = self.server()
        try:
            url = server.start()
            status, answer = self.post(url, {"action": "bot.pause"},
                                       init=init_data(auth_date=time.time()))
            self.assertEqual(status, 200)
            self.assertEqual(answer["ok"], True)
            self.assertEqual(answer["text"], "Pause done.")
            self.assertEqual(answer["verdict"], "run")
            self.assertEqual(len(self.asked), 1)              # one POST, one run
        finally:
            server.stop()

    def test_a_confirm_answer_carries_its_code_and_runs_on_the_second_post(self):
        ran: list = []

        def handler(payload: Any) -> Any:
            if not payload.get("code"):
                return {"ok": False, "verdict": "confirm", "confirm": "a1b2",
                        "action": "bot.restart", "text": "Tap again to confirm Restart."}
            ran.append(payload["code"])
            return {"ok": True, "verdict": "run", "text": "Restart done."}

        server = self.server(action=handler)
        try:
            url = server.start()
            login = init_data(auth_date=time.time())
            status, asked = self.post(url, {"action": "bot.restart"}, init=login)
            self.assertEqual((status, asked["ok"]), (200, False))
            self.assertEqual(asked["confirm"], "a1b2")
            self.assertEqual(ran, [])                         # the first tap changes nothing
            status, done = self.post(url, {"action": "bot.restart", "code": asked["confirm"]},
                                     init=login)
            self.assertEqual(status, 200)
            self.assertEqual(done["ok"], True)
            self.assertEqual(ran, ["a1b2"])
        finally:
            server.stop()

    def test_only_the_named_fields_of_an_answer_reach_the_wire(self):
        def handler(payload: Any) -> Any:
            return {"ok": True, "verdict": "run", "text": "Pause done.",
                    "chat_id": -1001234567890123, "thread_id": 19286,
                    "token": TOKEN, "path": r"C:\Users\eftyc\secret", "extra": "dropped"}

        server = self.server(action=handler)
        try:
            url = server.start()
            status, answer = self.post(url, init=init_data(auth_date=time.time()))
            self.assertEqual(status, 200)
            self.assertEqual(sorted(answer), ["ok", "text", "verdict"])
            payload = json.dumps(answer)
            for secret in ("1001234567890123", "19286", TOKEN, "Users", "eftyc", "dropped"):
                self.assertNotIn(secret, payload)
        finally:
            server.stop()

    def test_a_cross_site_origin_is_refused_before_anything_runs(self):
        server = self.server()
        try:
            url = server.start()
            status, answer = self.post(url, init=init_data(auth_date=time.time()),
                                       origin="https://evil.example.com")
            self.assertEqual(status, 403)
            self.assertEqual(answer["refused"], "origin")
            self.assertEqual(self.asked, [])
        finally:
            server.stop()

    def test_another_path_is_still_read_only(self):
        server = self.server()
        try:
            url = server.start()
            status, _answer = self.post(url, b"{}", path=webui.STATE_PATH,
                                        init=init_data(auth_date=time.time()))
            self.assertEqual(status, 405)
            self.assertEqual(self.asked, [])
        finally:
            server.stop()

    def test_a_server_with_no_action_wired_refuses_the_write_path(self):
        server = webui.DashboardServer(lambda: {"bots": []}, port=0, bot_token=TOKEN,
                                       admin_id=ADMIN_ID)          # no action: read-only
        self.assertIsNone(server.action)
        try:
            url = server.start()
            status, answer = self.post(url, init=init_data(auth_date=time.time()))
            self.assertEqual(status, 405)
            self.assertIn("error", answer)
        finally:
            server.stop()

    def test_a_page_with_no_init_data_window_refuses_every_write(self):
        server = self.server(init_max_age=0)                        # fails closed (#347)
        try:
            url = server.start()
            status, answer = self.post(url, init=init_data(auth_date=time.time()))
            self.assertEqual(status, 403)
            self.assertEqual(answer["refused"], "auth")
            self.assertEqual(self.asked, [])
        finally:
            server.stop()

    def test_a_body_that_is_not_json_or_is_too_big_is_a_400(self):
        server = self.server()
        try:
            url = server.start()
            login = init_data(auth_date=time.time())
            self.assertEqual(self.post(url, b"not json", init=login)[0], 400)
            self.assertEqual(self.post(url, b'["no"]', init=login)[0], 400)
            big = b'{"action": "' + b"a" * (webui.MAX_BODY_BYTES + 1) + b'"}'
            self.assertEqual(self.post(url, big, init=login)[0], 400)
            self.assertEqual(self.asked, [])
        finally:
            server.stop()

    def test_a_handler_that_raises_is_a_503_not_a_traceback(self):
        def broken(payload: Any) -> Any:
            raise RuntimeError("the control blew up")

        server = self.server(action=broken)
        try:
            url = server.start()
            status, answer = self.post(url, init=init_data(auth_date=time.time()))
            self.assertEqual(status, 503)
            self.assertFalse(answer["ok"])
            self.assertNotIn("blew up", json.dumps(answer))
        finally:
            server.stop()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
