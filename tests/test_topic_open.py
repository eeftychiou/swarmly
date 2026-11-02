"""Agent-opened forum topics: NEW_TOPIC opens a topic, REPORT_BACK reports to the origin."""

from __future__ import annotations

import asyncio
import types
import unittest

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import AdapterReply
from swarmly.telegram.topic_open import (MAX_TOPICS_PER_HOUR, parse_new_topic,
                                         parse_post_to_topic, parse_report_back,
                                         topic_link, topic_name)
from tests.test_telegram_channel import ADMIN_ID, GROUP, event, make_channel, run


class Scripted(EchoAdapter):
    """Answers each turn with the next scripted reply; records the prompts it got."""

    def __init__(self, *replies):
        super().__init__("Hermes")
        self.replies = list(replies)
        self.seen = []

    async def respond(self, message):
        self.seen.append(message)
        return AdapterReply(text=self.replies.pop(0) if self.replies else "NO_REPLY")


class Session(Scripted):
    """A runtime that resumes one session per topic: its own recap is only what is new."""

    KEEPS_TOPIC_SESSION = True


class TopicBot:
    def __init__(self, fail=None):
        self.fail = fail
        self.created = []

    async def create_forum_topic(self, chat_id, name):
        if self.fail:
            raise RuntimeError(self.fail)
        self.created.append((chat_id, name))
        return types.SimpleNamespace(message_thread_id=40 + len(self.created))


def ask(text, **kw):
    return event(text, author_id=ADMIN_ID, username="alice", is_bot=False, **kw)


async def drain(channel):
    for _ in range(50):
        await asyncio.sleep(0)
    while channel._self_tasks:
        await asyncio.gather(*list(channel._self_tasks))
        channel._self_tasks = {t for t in channel._self_tasks if not t.done()}


class TestParsing(unittest.TestCase):

    def test_new_topic(self):
        self.assertEqual(parse_new_topic("NEW_TOPIC: EUR-Lex API\nFind the search endpoint."),
                         ("EUR-Lex API", "Find the search endpoint."))
        self.assertEqual(parse_new_topic("@alice new_topic: **x**\nbrief"), ("x", "brief"))
        self.assertIsNone(parse_new_topic("NEW_TOPIC: no brief"))
        self.assertIsNone(parse_new_topic("Opening a NEW_TOPIC: later\nbrief"))

    def test_report_back(self):
        self.assertEqual(parse_report_back("Done.\nREPORT_BACK: it works\nsee PR"),
                         "it works\nsee PR")
        self.assertIsNone(parse_report_back("plain reply"))
        self.assertIsNone(parse_report_back("REPORT_BACK:   "))

    def test_post_to_topic_directive(self):
        self.assertEqual(parse_post_to_topic(
            "POST_TO_TOPIC: 77\n@CodexBot please review #42"),
            (77, "@CodexBot please review #42"))
        self.assertIsNone(parse_post_to_topic("plain reply"))
        # A directive with no usable target is answered, not posted as prose (#498 review).
        self.assertEqual(parse_post_to_topic("POST_TO_TOPIC: nope\nassignment"),
                         (None, "assignment"))

    def test_names_and_links(self):
        self.assertEqual(topic_link(-1001234567890, 7), "https://t.me/c/1234567890/7")
        self.assertEqual(len(topic_name("Hermes", "x" * 300)), 128)


class TestOpenTopic(unittest.TestCase):

    def coordinator_channel(self, *replies, adapter=None):
        """A channel that acts as the coordinator, with the sends and links it makes recorded."""
        channel = make_channel(adapter=adapter or Scripted(*replies))
        channel.is_coordinator = lambda: True
        channel.topics.remember(GROUP, 77, "coordinated work")
        channel.sent = []
        channel.linked = []
        channel.state_link.note_handoff_peers = lambda *args: channel.linked.append(args)
        original_send = channel.send

        async def capture(text, **kwargs):
            channel.sent.append((text, kwargs))
            return await original_send(text, **kwargs)

        channel.send = capture
        return channel

    def test_handle_event_posts_assignment_into_a_known_topic(self):
        channel = self.coordinator_channel("POST_TO_TOPIC: 77\n"
                                           "@CodexBot please review #42")
        run(channel.handle_event(ask("@HermesBot dispatch the next round")))
        self.assertTrue(any("@CodexBot please review #42" in text and
                            kwargs.get("thread_id") == 77 for text, kwargs in channel.sent),
                        channel.sent)
        # The other half of the acceptance: the peer the assignment names is linked in that
        # topic, or it takes no turn there (review S2).
        self.assertEqual(channel.linked,
                         [(GROUP, 77, ["Codex"], channel.config.identity.alias)])
        self.assertTrue(any("posted in" in text for text, _ in channel.sent), channel.sent)

    def test_an_agent_that_is_not_the_coordinator_cannot_post_into_a_topic(self):
        channel = self.coordinator_channel("POST_TO_TOPIC: 77\n@CodexBot please review #42")
        channel.is_coordinator = lambda: False
        run(channel.handle_event(ask("@HermesBot dispatch the next round")))
        self.assertFalse(any("@CodexBot please review" in text for text, _ in channel.sent),
                         channel.sent)
        self.assertTrue(any("POST_TO_TOPIC refused" in text for text, _ in channel.sent),
                        channel.sent)

    def test_an_unknown_topic_is_refused_not_posted(self):
        channel = self.coordinator_channel("POST_TO_TOPIC: 999\n@CodexBot please review #42")
        run(channel.handle_event(ask("@HermesBot dispatch the next round")))
        self.assertFalse(any("@CodexBot please review" in text for text, kw in channel.sent
                             if kw.get("thread_id") == 999), channel.sent)
        self.assertTrue(any("POST_TO_TOPIC refused" in text for text, _ in channel.sent),
                        channel.sent)

    def test_a_gone_topic_is_refused(self):
        channel = self.coordinator_channel("POST_TO_TOPIC: 77\n@CodexBot please review #42")
        channel._gone_topics.add((GROUP, 77))
        run(channel.handle_event(ask("@HermesBot dispatch the next round")))
        self.assertFalse(any("@CodexBot please review" in text for text, kw in channel.sent
                             if kw.get("thread_id") == 77), channel.sent)
        self.assertTrue(any("POST_TO_TOPIC refused" in text for text, _ in channel.sent),
                        channel.sent)

    def test_a_directive_without_a_topic_id_is_answered(self):
        channel = self.coordinator_channel("POST_TO_TOPIC: nope\n@CodexBot please review #42")
        run(channel.handle_event(ask("@HermesBot dispatch the next round")))
        self.assertTrue(any("POST_TO_TOPIC refused" in text for text, _ in channel.sent),
                        channel.sent)

    def test_new_topic_copies_shared_plan_into_its_coordinator_turn(self):
        adapter = Session("NO_REPLY")          # the live run's coordinator resumes a session
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()
        # The plan lives in the source topic, and nothing here stubs the recap (review B2).
        channel._log_topic(GROUP, 12, "admin", "Opening context for the topic.", 12)
        channel._log_topic(GROUP, 12, "Hermes", "Plan: split work into rounds 1, 2 and 3.", 14)
        source = ask("open round two", thread_id=12, message_id=16)
        # The source topic has already had its own turn: a session runtime's recap is empty,
        # which is what lost the plan in the live run.
        self.assertEqual(channel._topic_recap(source), "")

        async def scenario():
            await channel._open_topic(source, "round two", "Handle round two.")
            await drain(channel)
        run(scenario())
        self.assertTrue(any("Plan: split work into rounds 1, 2 and 3." in msg.text
                            for msg in adapter.seen), adapter.seen)

    def test_plan_context_does_not_turn_peer_handoff_into_own_task(self):
        """B1: the copied plan must not sit in front of the peer's handle.

        An own line is in the log too, so the plan really is copied in from a session
        runtime's topic -- with nothing to copy, the test would pass on the old head.
        """
        own_tasks, handoffs = [], []
        adapter = Session("NO_REPLY")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()
        channel._log_topic(GROUP, 12, "admin", "Plan: split work into rounds 1, 2 and 3.", 12)
        channel._run_own_task = lambda brief, *a, **kw: own_tasks.append(brief)
        channel._mirror_handoffs = lambda body, *a, **kw: handoffs.append(body)
        source = ask("open round two", thread_id=12, message_id=13)

        async def scenario():
            await channel._open_topic(source, "round two", "@CodexBot please handle round two.")
            await drain(channel)
        run(scenario())
        # The plan really was copied in, so the hand-off below met a prepended block.
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertTrue(any("Plan: split work into rounds 1, 2 and 3." in text
                            for text in texts), texts)
        self.assertEqual(own_tasks, [])
        self.assertTrue(any("@CodexBot please handle round two." in body for body in handoffs),
                        handoffs)

    def test_opens_topic_runs_own_brief_and_reports_back(self):
        adapter = Scripted("NEW_TOPIC: research\nCompare the two APIs.",
                           "Compared them.\nREPORT_BACK: API B is better.")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()

        async def scenario():
            await channel.handle_event(ask("@HermesBot look into the APIs"))
            await drain(channel)
        run(scenario())
        self.assertEqual(channel.bot.created, [(GROUP, "Hermes: research")])
        self.assertEqual(len(adapter.seen), 2)
        self.assertIn("You opened this topic", adapter.seen[1].text)
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertTrue(any("opened topic «Hermes: research»" in t for t in texts), texts)
        self.assertTrue(any("API B is better." in t and "t.me/c/" in t for t in texts), texts)

    def test_brief_for_a_peer_is_not_run_by_this_agent(self):
        adapter = Scripted("NEW_TOPIC: review\n@CodexBot please review the change.")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()
        run(channel.handle_event(ask("@HermesBot get this reviewed")))
        self.assertEqual(len(adapter.seen), 1)
        self.assertEqual(channel._self_tasks, set())

    def test_failure_posts_the_brief_in_place(self):
        adapter = Scripted("NEW_TOPIC: research\nCompare the two APIs.")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot(fail="Bad Request: not enough rights")
        run(channel.handle_event(ask("@HermesBot look into it")))
        last = channel._test_sender.last
        self.assertIn("Compare the two APIs.", last)
        self.assertIn("not enough rights", last)

    def test_rate_limited(self):
        channel = make_channel(adapter=Scripted())
        channel.bot = TopicBot()
        channel._topics_opened = [channel._clock()] * MAX_TOPICS_PER_HOUR
        run(channel._open_topic(ask("x"), "one more", "@CodexBot brief"))
        self.assertEqual(channel.bot.created, [])
        self.assertIn("topics opened in the last hour", channel._test_sender.last)


if __name__ == "__main__":
    unittest.main()


class TestBuild5Findings(unittest.TestCase):
    """What the build 5 live test found: plain-words topic requests, briefs that only consult
    a peer, and topics that never reported back."""

    def test_plain_words_ask_for_a_topic(self):
        from swarmly.telegram.topic_open import asks_for_topic
        self.assertTrue(asks_for_topic("open a new topic to research EUR-Lex, then report back"))
        self.assertTrue(asks_for_topic("do this in a topic please"))
        self.assertFalse(asks_for_topic("what is the topic of the meeting?"))
        self.assertFalse(asks_for_topic("reply with NEW_TOPIC: x"))

    def test_the_prompt_carries_the_topic_note(self):
        adapter = Scripted("fine")
        channel = make_channel(adapter=adapter)
        run(channel.handle_event(ask("@HermesBot open a new topic to research the API")))
        self.assertIn("NEW_TOPIC: <short title>", adapter.seen[0].text)

    def test_a_brief_that_only_consults_a_peer_stays_with_the_opener(self):
        adapter = Scripted("NEW_TOPIC: research\nSurvey the API.\n@CodexBot confirm the scope?",
                           "REPORT_BACK: done")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()

        async def scenario():
            await channel.handle_event(ask("@HermesBot research the API"))
            await drain(channel)
        run(scenario())
        self.assertEqual(len(adapter.seen), 2)              # the opener ran its own brief

    def test_a_quiet_topic_is_asked_for_its_report_once(self):
        adapter = Scripted("NEW_TOPIC: research\n@CodexBot please survey the API.",
                           "NO_REPLY", "REPORT_BACK: Codex found the endpoint.", "NO_REPLY")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()

        async def scenario():
            await channel.handle_event(ask("@HermesBot get the API surveyed"))
            # Codex works in the topic and reports; Hermes, named, chooses silence.
            await channel.handle_event(event("@HermesBot survey done: /search endpoint",
                                             thread_id=41, message_id=5))
            await drain(channel)
            await channel.handle_event(event("@HermesBot anything else?", thread_id=41,
                                             message_id=6))
            await drain(channel)
        run(scenario())
        self.assertIn("gone quiet", adapter.seen[2].text)
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertTrue(any("Codex found the endpoint." in t and "t.me/c/" in t for t in texts))
        self.assertEqual(sum("gone quiet" in m.text for m in adapter.seen), 1)


class TestWorkInTopics(unittest.TestCase):
    """General is for setup: bots coordinating there, and long answers, move to a topic."""

    def test_title_from_the_request(self):
        from swarmly.telegram.topic_open import title_from
        self.assertEqual(title_from("@HermesBot **Implement** the EUR-Lex search client now"),
                         "Implement the EUR-Lex search client now")
        self.assertEqual(title_from("@HermesBot"), "work")

    def test_the_prompt_says_general_is_for_setup(self):
        adapter = Scripted("ok")
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()
        run(channel.handle_event(ask("@HermesBot what is your role?")))
        self.assertTrue(adapter.seen[0].general_is_for_setup)
        from swarmly.telegram.adapters.base import discussion_line
        self.assertIn("NEW_TOPIC", discussion_line(adapter.seen[0]))

    def test_a_long_answer_to_a_person_moves_to_a_new_topic(self):
        adapter = Scripted("Here is the plan. " * 60)
        channel = make_channel(adapter=adapter)
        channel.bot = TopicBot()
        run(channel.handle_event(ask("@HermesBot @AntigravityBot plan the search feature")))
        self.assertEqual(channel.bot.created, [(GROUP, "Hermes: plan the search feature")])
        texts = [s["text"] for s in channel._test_sender.sent]
        self.assertTrue(any(t.startswith("↪ moved this to topic") for t in texts), texts)
        quote = next(t for t in texts if t.startswith("Moved here from General"))
        self.assertNotIn("@AntigravityBot", quote)              # no second turn for the peer
        self.assertEqual(len(adapter.seen), 1)                  # nothing more is run

    def test_a_peer_reply_about_a_linked_pr_goes_to_its_topic(self):
        """#472: the topic is the one the item is **linked** in, not the one that named it."""
        channel = make_channel(adapter=Scripted("@CodexBot QA notes on the change: " + "x " * 250))
        channel.bot = TopicBot()
        channel.state_link.topic_for_refs = lambda refs: (GROUP, 77) if "#6" in refs else None
        run(channel.handle_event(event("@HermesBot please QA #6", message_id=4)))
        self.assertEqual(channel.bot.created, [])
        sent = channel._test_sender.sent
        self.assertTrue(any(s["text"].startswith("↪ moved this to") for s in sent))

    def test_a_peer_reply_with_no_topic_stays_in_general(self):
        channel = make_channel(adapter=Scripted("@CodexBot " + "a long status " * 60))
        channel.bot = TopicBot()
        run(channel.handle_event(event("@HermesBot status?")))
        self.assertEqual(channel.bot.created, [])

    def test_a_short_answer_to_a_person_stays_in_general(self):
        channel = make_channel(adapter=Scripted("Reviewer."))
        channel.bot = TopicBot()
        run(channel.handle_event(ask("@HermesBot your role?")))
        self.assertEqual(channel.bot.created, [])
        self.assertEqual(channel._test_sender.last.strip().split()[-1], "Reviewer.")

    def test_a_long_answer_to_a_person_moves(self):
        channel = make_channel(adapter=Scripted("x " * 600))
        channel.bot = TopicBot()
        run(channel.handle_event(ask("@HermesBot design the cache layer")))
        self.assertEqual(len(channel.bot.created), 1)

    def test_off_keeps_everything_in_general(self):
        from tests.test_telegram_channel import make_config
        channel = make_channel(make_config(limits={"work_in_topics": False}),
                               adapter=Scripted("the plan " * 200))
        channel.bot = TopicBot()
        run(channel.handle_event(ask("@HermesBot plan it")))
        self.assertEqual(channel.bot.created, [])


class TestFourAgentFindings(unittest.TestCase):
    """The four-agent run: notice cascades, duplicate pause notices, team lists, reports."""

    def test_a_peer_notice_with_its_banner_starts_no_turn(self):
        channel = make_channel()
        for text in ("[Claude] ⚠️ Claude skipped this message from example_agy_bot: cooldown",
                     "[Codex] ↪ moved this to topic «x»", "[Codex] @HermesBot ⚠️ paused"):
            decision, reason = channel.should_respond(event(text, replies_to_bot=True))
            self.assertFalse(decision, text)
            self.assertIn("notice", reason)

    def test_one_pause_notice_per_topic(self):
        channel = make_channel()
        run(channel.handle_event(event("[Codex] ⚠️ paused after 3 bot turns in a row in this "
                                       "topic. Post here to continue.", thread_id=9)))
        self.assertIn((GROUP, 9), channel._paused_topics)

    def test_every_handle_in_a_bots_message_addresses_it(self):
        """Issue #27 reversed the old "named in a list, not addressed" rule: a list a peer bot
        writes is a hand-off, so every @handle in it counts — a roster included, because a
        hand-off the recipient second-guesses is a hand-off that is lost."""
        channel = make_channel()
        listed = event("Team: @CodexBot (QA), @HermesBot (dev), @AntigravityBot (review).",
                       username="CodexBot")
        self.assertTrue(channel.should_respond(listed)[0])
        asked = event("@HermesBot please fix it; @CodexBot @AntigravityBot FYI", username="CodexBot")
        self.assertTrue(channel.should_respond(asked)[0])
        # The build 8 stall: a hand-off naming three bots, one task per bullet.
        handoff = event("Thanks. Merge conditions:\n- @AntigravityBot comments a merge verdict\n"
                        "- @HermesBot runs pytest and reports pass\n- @CodexBot merges",
                        username="CodexBot")
        self.assertTrue(channel.should_respond(handoff)[0])
        roster = event("Team:\n- @CodexBot (QA)\n- @HermesBot (dev)\n- @AntigravityBot (review)",
                       username="CodexBot")
        self.assertTrue(channel.should_respond(roster)[0])
        elsewhere = event("@CodexBot @AntigravityBot: plan round 3", username="CodexBot")
        self.assertFalse(channel.should_respond(elsewhere)[0])   # a bot naming only others

    def test_report_back_copies(self):
        from swarmly.telegram.topic_open import defuse_mentions, strip_report_marker
        self.assertEqual(strip_report_marker("Done.\nREPORT_BACK: shipped"), "Done.\nshipped")
        self.assertFalse(defuse_mentions("@CodexBot done") .startswith("@C"))

    def test_titles_and_refs(self):
        from swarmly.telegram.topic_open import issue_refs, topic_name
        self.assertEqual(topic_name("Codex", "(https://github.com/x/y/pull/6) QA"), "Codex: QA")
        self.assertEqual(issue_refs("see #5 and https://github.com/a/b/pull/6, not a#7"),
                         ["#5", "#6"])
