"""Agent-opened forum topics: NEW_TOPIC opens a topic, REPORT_BACK reports to the origin."""

from __future__ import annotations

import asyncio
import types
import unittest

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import AdapterReply
from swarmly.telegram.topic_open import (MAX_TOPICS_PER_HOUR, parse_new_topic,
                                         parse_report_back, topic_link, topic_name)
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

    def test_names_and_links(self):
        self.assertEqual(topic_link(-1001234567890, 7), "https://t.me/c/1234567890/7")
        self.assertEqual(len(topic_name("Hermes", "x" * 300)), 128)


class TestOpenTopic(unittest.TestCase):

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

    def test_a_peer_reply_about_a_known_pr_goes_to_its_topic(self):
        channel = make_channel(adapter=Scripted("@CodexBot QA notes on the change: " + "x " * 250))
        channel.bot = TopicBot()
        run(channel.handle_event(event("PR #6 is up for review", thread_id=77, message_id=3)))
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

    def test_a_team_list_is_not_addressing(self):
        channel = make_channel()
        listed = event("Team: @CodexBot (QA), @HermesBot (dev), @AntigravityBot (review).",
                       username="CodexBot")
        self.assertFalse(channel.should_respond(listed)[0])
        asked = event("@HermesBot please fix it; @CodexBot @AntigravityBot FYI", username="CodexBot")
        self.assertTrue(channel.should_respond(asked)[0])
        # The build 8 stall: a hand-off naming three bots, one task per bullet.
        handoff = event("Thanks. Merge conditions:\n- @AntigravityBot comments a merge verdict\n"
                        "- @HermesBot runs pytest and reports pass\n- @CodexBot merges",
                        username="CodexBot")
        self.assertTrue(channel.should_respond(handoff)[0])
        roster = event("Team:\n- @CodexBot (QA)\n- @HermesBot (dev)\n- @AntigravityBot (review)",
                       username="CodexBot")
        self.assertFalse(channel.should_respond(roster)[0])

    def test_report_back_copies(self):
        from swarmly.telegram.topic_open import defuse_mentions, strip_report_marker
        self.assertEqual(strip_report_marker("Done.\nREPORT_BACK: shipped"), "Done.\nshipped")
        self.assertFalse(defuse_mentions("@CodexBot done") .startswith("@C"))

    def test_titles_and_refs(self):
        from swarmly.telegram.topic_open import issue_refs, topic_name
        self.assertEqual(topic_name("Codex", "(https://github.com/x/y/pull/6) QA"), "Codex: QA")
        self.assertEqual(issue_refs("see #5 and https://github.com/a/b/pull/6, not a#7"),
                         ["#5", "#6"])
