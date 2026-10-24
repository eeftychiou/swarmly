"""The live swarm dashboard as a local web page (issue #240, slice 1).

What these pin: the JSON the page polls is rendered from fixed records with nothing invented
(a bot that stopped beating is ``down``, an unreported number is ``null`` and not a zero), no
identifier beyond aliases reaches the wire, the page is self-contained and follows the
system's light or dark preference, and the server is read-only and bound to the loopback.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request
from typing import Any, Dict

from swarmly.telegram import webui
from swarmly.telegram.rounds import RoundState
from swarmly.telegram.snapshot import BUSY, DOWN, FROZEN, IDLE, PAUSED, STUCK, Snapshot

NOW = 1_700_000_000.0


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


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
