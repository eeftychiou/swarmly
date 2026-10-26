"""Routing from the topic registry (phase 6, docs/design/topic-registry.md §2)."""

import asyncio
import unittest
from types import SimpleNamespace

from swarmly.state.records import Topic
from swarmly.telegram.routing import Facts, Message, decide
from tests.test_registry_links import LinkFlowTests
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, make_config, run
from tests.test_transport_enforcement import Recording

TOPIC = 9400


def msg(**kw):
    base = dict(author=None, author_is_bot=False, author_is_admin=True, names_me=False,
                replies_to_me=False, handed_to_me=False)
    base.update(kw)
    return Message(**base)


def peer(**kw):
    return msg(author="deepseek", author_is_bot=True, author_is_admin=False, **kw)


COORD = Facts(me="claudebot", coordinator="claudebot", linked=frozenset({"claudebot"}))
LINKED = Facts(me="codex", coordinator="claudebot", linked=frozenset({"claudebot", "codex"}))
UNLINKED = Facts(me="codex", coordinator="claudebot", linked=frozenset({"claudebot"}))


class DecideTests(unittest.TestCase):
    def test_the_coordinator_takes_a_persons_unaddressed_message(self):
        self.assertTrue(decide(COORD, msg()).respond)

    def test_but_not_a_peers_unaddressed_post(self):
        """Review: answering every progress post ping-pongs up to the peer-turn cap."""
        self.assertFalse(decide(COORD, peer()).respond)

    def test_the_coordinator_leaves_a_message_for_linked_agents(self):
        facts = Facts(me="claudebot", coordinator="claudebot",
                      linked=frozenset({"claudebot", "codexbot"}))
        self.assertFalse(decide(facts, peer(named=("codexbot",))).respond)
        self.assertTrue(decide(facts, peer(named=("codexbot",), names_me=True)).respond)

    def test_a_refused_hand_off_goes_to_the_coordinator(self):
        """Review: a peer naming an unlinked agent left nobody to act on it."""
        route = decide(COORD, peer(named=("hermesbot",)))
        self.assertTrue(route.respond)
        self.assertIn("coordinator decides", route.reason)
        # The admin's mention links the agent, so the coordinator stays out.
        self.assertFalse(decide(COORD, msg(named=("hermesbot",))).respond)

    def test_a_linked_agent_only_when_addressed(self):
        self.assertFalse(decide(LINKED, msg()).respond)
        for addressed in ("names_me", "replies_to_me", "handed_to_me"):
            self.assertTrue(decide(LINKED, msg(**{addressed: True})).respond, addressed)

    def test_an_unlinked_agent_named_by_the_coordinator_or_the_admin_answers(self):
        self.assertTrue(decide(UNLINKED, msg(names_me=True)).respond)          # the admin
        self.assertTrue(decide(UNLINKED, msg(names_me=True, author="claudebot",
                                             author_is_bot=True,
                                             author_is_admin=False)).respond)

    def test_the_coordinator_is_recognised_by_its_handle(self):
        """Review: a peer may call the coordinator by another alias than its own."""
        facts = Facts(me="codex", coordinator="claudebot", linked=frozenset({"claudebot"}),
                      coordinator_handle="claude_obot")
        self.assertTrue(decide(facts, msg(names_me=True, author="claude", author_is_bot=True,
                                          author_is_admin=False,
                                          author_handle="claude_obot")).respond)

    def test_an_unlinked_agent_named_by_a_peer_is_refused(self):
        route = decide(UNLINKED, peer(names_me=True))
        self.assertFalse(route.respond)
        self.assertTrue(route.refuse_unlinked)
        self.assertTrue(decide(UNLINKED, msg(names_me=True, author_is_admin=False))
                        .refuse_unlinked)

    def test_a_broadcast_reaches_only_linked_agents_and_draws_no_notice(self):
        route = decide(UNLINKED, peer(names_me=True, names_me_explicitly=False))
        self.assertEqual((route.respond, route.refuse_unlinked), (False, False))
        self.assertFalse(decide(UNLINKED, msg(names_me=True,
                                              names_me_explicitly=False)).respond)
        self.assertTrue(decide(LINKED, peer(names_me=True,
                                            names_me_explicitly=False)).respond)

    def test_identity_by_handle(self):
        facts = Facts(me="claude", coordinator="claudebot", linked=frozenset({"claude_obot"}),
                      coordinator_handle="claude_obot", me_handle="claude_obot")
        self.assertTrue(facts.i_coordinate)
        self.assertTrue(facts.i_am_linked)


PEERS = {"Codex": "CodexBot", "Antigravity": "AntigravityBot", "Claudebot": "Claude_obot",
         "Efty": "eftychios"}


def channel_for(facts):
    config = make_config(peers=PEERS)
    adapter = Recording("on it")
    channel = make_channel(config, adapter=adapter)
    channel.state_link.route_facts = lambda chat, thread: facts if thread == TOPIC else None
    admin_links = []
    channel.state_link.note_admin_mentions = \
        lambda chat, thread, named, author: admin_links.append((thread, list(named)))
    return channel, adapter, admin_links


def facts_for_hermes(coordinator, linked):
    return Facts(me="hermes", coordinator=coordinator, linked=frozenset(linked),
                 coordinator_handle="hermesbot" if coordinator == "hermes" else "claude_obot",
                 me_handle="hermesbot")


ME_COORD = facts_for_hermes("hermes", {"hermes", "hermesbot"})
ME_LINKED = facts_for_hermes("claudebot", {"claudebot", "claude_obot", "hermesbot"})
ME_UNLINKED = facts_for_hermes("claudebot", {"claudebot", "claude_obot"})


def admin(text, thread_id=TOPIC):
    return event(text, author_id=ADMIN_ID, username="admin", is_bot=False, thread_id=thread_id)


class ChannelRoutingTests(unittest.TestCase):
    def test_the_coordinator_answers_a_person_who_names_nobody(self):
        channel, adapter, _ = channel_for(ME_COORD)
        run(channel.handle_event(admin("what is left on the round?")))
        self.assertEqual(len(adapter.prompts), 1)

    def test_a_linked_agent_stays_out_of_an_unaddressed_message(self):
        channel, adapter, _ = channel_for(ME_LINKED)
        run(channel.handle_event(admin("what is left on the round?")))
        self.assertEqual(adapter.prompts, [])

    def test_the_coordinator_stays_out_of_a_reply_to_another_bot(self):
        """Review: the admin replying to Codex was answered by Codex and the coordinator."""
        channel, adapter, _ = channel_for(ME_COORD)
        ev = admin("yes, go ahead")
        ev.replies_to_peer = "codexbot"
        run(channel.handle_event(ev))
        self.assertEqual(adapter.prompts, [])

    def test_a_peer_naming_an_unlinked_bot_gets_a_notice_and_no_turn(self):
        channel, adapter, _ = channel_for(ME_UNLINKED)
        run(channel.handle_event(event("@HermesBot please review #12", thread_id=TOPIC)))
        self.assertEqual(adapter.prompts, [])
        self.assertIn("isn't linked to this topic", channel._test_sender.last)

    def test_the_coordinator_naming_an_unlinked_bot_gets_its_turn(self):
        channel, adapter, _ = channel_for(ME_UNLINKED)
        run(channel.handle_event(event("@HermesBot please review #12", author_id=303,
                                       username="Claude_obot", thread_id=TOPIC)))
        self.assertEqual(len(adapter.prompts), 1)

    def test_an_assign_line_hands_the_work(self):
        channel, adapter, _ = channel_for(ME_LINKED)
        run(channel.handle_event(event("Round plan.\nASSIGN: @Hermes #12 as reviewer",
                                       author_id=303, username="Claude_obot",
                                       thread_id=TOPIC)))
        self.assertEqual(len(adapter.prompts), 1)

    def test_the_admins_mentions_are_handed_to_the_registry_as_bot_handles(self):
        channel, _, admin_links = channel_for(ME_COORD)
        run(channel.handle_event(admin("@CodexBot and @Antigravity: take #12, @eftychios FYI")))
        self.assertEqual(admin_links, [(TOPIC, ["codexbot", "antigravitybot"])])

    def test_an_admin_command_links_nobody(self):
        channel, _, admin_links = channel_for(ME_COORD)
        run(channel.handle_event(admin("/model@CodexBot")))
        self.assertEqual(admin_links, [])

    def test_a_peer_reporting_it_asked_a_linked_bot_is_not_a_refused_delegation(self):
        """Live, 25 Oct: "@Reasonix: thanks. I've asked @agy for QA" drew "a peer asks another
        bot to hand this bot the work" from the linked QA bot: "asked" read as a request."""
        text = "@AntigravityBot: thanks, verdict noted. I've asked @HermesBot for QA."
        channel, adapter, _ = channel_for(ME_LINKED)
        run(channel.handle_event(event(text, thread_id=TOPIC)))       # from Codex, a peer
        self.assertEqual(len(adapter.prompts), 1)

    def test_a_delegation_still_goes_only_to_the_bot_it_addresses_under_the_registry(self):
        """Review on #357: the registry has no notion of relaying, so the guard stays."""
        for facts in (ME_LINKED, ME_COORD):
            channel, adapter, _ = channel_for(facts)
            run(channel.handle_event(event("@AntigravityBot please hand @HermesBot the task",
                                           thread_id=TOPIC)))
            self.assertEqual(adapter.prompts, [], facts)

    def test_without_registry_facts_the_mention_rules_decide(self):
        channel, adapter, _ = channel_for(None)
        run(channel.handle_event(event("@HermesBot please review #12", thread_id=TOPIC)))
        self.assertEqual(len(adapter.prompts), 1)

    def test_commands_keep_their_own_rules(self):
        channel, adapter, _ = channel_for(ME_UNLINKED)
        run(channel.handle_event(event("/ping@HermesBot", thread_id=TOPIC)))
        self.assertNotIn("isn't linked", channel._test_sender.last)

    def test_a_reply_to_a_peer_bot_is_read_from_telegram(self):
        channel, _, _ = channel_for(ME_COORD)
        channel.bot = SimpleNamespace(id=1)
        bot = SimpleNamespace(id=7, username="CodexBot", is_bot=True)
        reply_to = SimpleNamespace(from_user=bot, message_id=55, forum_topic_created=None)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=ADMIN_ID, username="admin", is_bot=False),
            reply_to_message=reply_to, text="go", chat=SimpleNamespace(id=-100),
            message_id=60, message_thread_id=TOPIC, is_topic_message=True)
        ev = channel.event_from_aiogram(message)
        self.assertEqual((ev.replies_to_bot, ev.replies_to_peer), (False, "codexbot"))


HANDLES = {"deepseekbot": "DeepSeek", "codexbot": "Codex"}


class RecordedLinkTests(unittest.TestCase):
    """The holder links the agents the coordinator or the admin names."""

    def link(self, holder=True):
        link = LinkFlowTests.link(self, holder=holder)
        link.routing_mode = "registry"
        link.channel.config.peer_alias_for = lambda handle, uid=None: HANDLES.get(handle)
        return link

    def opened(self, link, thread=55):
        """A topic with a record (the holder's beat writes one for every active topic)."""
        async def go():
            link.note_reply(-100, thread, "LINK: #12")
            await asyncio.gather(*list(link._tasks))
        asyncio.run(go())

    def test_the_coordinators_reply_links_the_peers_it_names(self):
        link = self.link()
        self.opened(link)

        async def go():
            link.note_reply(-100, 55, "@DeepSeek please take #12", ["deepseekbot"])
            await asyncio.gather(*list(link._tasks))
        asyncio.run(go())
        topic = Topic.from_dict(link.store.read("topic/55"))
        self.assertIn("DeepSeek", topic.agents)
        facts = link.route_facts(-100, 55)
        self.assertIn("deepseek", facts.linked)
        self.assertEqual(facts.coordinator, "claudebot")

    def test_a_link_is_keyed_by_the_agents_own_alias(self):
        """Review: a peer's spelling ("Claude") made a second entry for "Claudebot"."""
        link = self.link()
        self.opened(link)

        async def go():
            await link.store.write("agent/claudebot/profile", {"alias": "Claudebot",
                                                               "handle": "@Claude_obot"})
        asyncio.run(go())
        self.assertEqual(link.agent_for_handle("claude_obot"), "Claudebot")
        self.assertIn("claude_obot", link.route_facts(-100, 55).coordinator_handle)

    def test_the_admins_mention_links_and_a_second_one_writes_nothing(self):
        link = self.link()
        self.opened(link)

        async def go():
            link.note_admin_mentions(-100, 55, ["codexbot"], 1)
            await asyncio.gather(*list(link._tasks))
            before = len(link._tasks)
            link.note_admin_mentions(-100, 55, ["codexbot"], 1)
            return before, len(link._tasks)
        before, after = asyncio.run(go())
        self.assertEqual(before, after)                     # already linked: no new write
        topic = Topic.from_dict(link.store.read("topic/55"))
        self.assertEqual(topic.agents["Codex"].linked_by, "admin:1")

    def test_a_new_topic_routes_to_the_coordinator(self):
        """Live, 25 Oct: the admin's first message in a fresh topic reached nobody."""
        link = self.link()
        facts = link.route_facts(-100, 77)
        self.assertEqual(facts.coordinator, "claudebot")
        self.assertIn("claudebot", facts.linked)
        self.assertNotIn("codex", facts.linked)

    def test_the_admins_mention_in_a_new_topic_links_and_opens_its_record(self):
        link = self.link()

        async def go():
            link.note_admin_mentions(-100, 77, ["codexbot"], 1)
            await asyncio.gather(*list(link._tasks))
        asyncio.run(go())
        self.assertIn("codex", link.route_facts(-100, 77).linked)

    def test_with_routing_legacy_nothing_is_linked_or_routed(self):
        link = LinkFlowTests.link(self)
        link.note_admin_mentions(-100, 55, ["codexbot"], 1)
        self.assertFalse(getattr(link, "_tasks", None))
        self.assertIsNone(link.route_facts(-100, 55))

    def test_a_closed_topic_and_swarm_pulse_are_not_routed(self):
        link = self.link()
        self.opened(link)
        self.opened(link, thread=3418)
        link.channel._pulse_topic = lambda: 3418
        self.assertIsNone(link.route_facts(-100, 3418))
        record = dict(link.store.read("topic/55"), status="closed")

        async def go():
            await link.store.write("topic/55", record)
        asyncio.run(go())
        self.assertIsNone(link.route_facts(-100, 55))

    def _worked(self, link, alias):
        """An agent's own topic key (it alone writes it), as the store would hold it."""
        keys, read = link.store.keys, link.store.read
        held = {f"topic/55/agent/{alias}": {"session_id": "s", "turns": 3}}
        link.store.keys = lambda prefix="": keys(prefix) + [k for k in held
                                                            if k.startswith(prefix)]
        link.store.read = lambda key: held.get(key) or read(key)

    def test_an_agent_that_already_worked_here_counts_as_linked(self):
        link = self.link()
        self.opened(link)
        self._worked(link, "deepseek")
        self.assertIn("deepseek", link.route_facts(-100, 55).linked)

    def test_unlink_outranks_having_worked_here(self):
        """Review: /unlink could not take off an agent that had turns in the topic."""
        link = self.link()
        self.opened(link)

        def command(text):
            return asyncio.run(link.registry_command(SimpleNamespace(
                text=text, author_id=1, author_is_bot=False, chat_id=-100, thread_id=55)))
        self.assertIn("Recorded", command("/link @DeepSeek"))
        self.assertIn("Recorded", command("/unlink @DeepSeek"))
        self._worked(link, "deepseek")
        self.assertNotIn("deepseek", link.route_facts(-100, 55).linked)


if __name__ == "__main__":
    unittest.main()
