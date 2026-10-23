"""One message naming several bots is one hand-off per bot, never a refusal (#181).

The shapes here are the ones the swarm actually posts -- round 5 and 5.5, 44 of 62 messages
naming two or more handles -- plus the delegation the old refusal was written for.
"""

import unittest

from swarmly.telegram import handoff_split as hs

#: Both spellings are in play: an alias in the config, and the @username Telegram delivers.
BOTS = ["Claudebot", "Reasonix", "Antigravity", "Codex", "DeepSeek",
        "Claude_obot", "reasonix_dd_bot", "agy_dd_bot", "codex_dd_bot", "DeepSeek_dd_bot"]


class TestWhoIsAsked(unittest.TestCase):

    def test_two_asks_in_one_line_are_two_recipients(self):
        text = ("@reasonix_dd_bot: please re-review #173 at 840f8b0. "
                "@agy_dd_bot: please QA #173 at 840f8b0.")
        self.assertEqual(hs.requests(text, BOTS), ["reasonix_dd_bot", "agy_dd_bot"])
        self.assertTrue(hs.is_multi_recipient(text, BOTS))

    def test_bullets_are_one_recipient_each(self):
        text = ("Round 5: three items.\n"
                "@Codex: review the ladder change.\n"
                "- @Antigravity: QA the same head.\n"
                "- @DeepSeek: fix the flag wording.")
        self.assertEqual(hs.requests(text, BOTS),
                         ["codex", "antigravity", "deepseek"])

    def test_an_opening_run_shares_its_sentence(self):
        text = "@Claude_obot @Codex, please look at the exports before tomorrow."
        self.assertEqual(hs.requests(text, BOTS), ["claude_obot", "codex"])

    def test_a_delegation_asks_only_the_bot_it_addresses(self):
        """The case the refusal was written for: A is asked to hand the work to B."""
        text = "@Codex please hand @Reasonix the task, it is already in the topic."
        self.assertFalse(hs.is_multi_recipient(text, BOTS))
        self.assertEqual(hs.requests(text, BOTS), ["codex"])   # A is asked, B is not

    def test_a_question_after_a_passing_mention_is_not_an_ask(self):
        """#181 review: the "?" closed Codex's sentence, not the mention inside it."""
        text = ("@Codex: the review is in, and @reasonix_dd_bot agreed with it yesterday. "
                "Any news?")
        self.assertEqual(hs.requests(text, BOTS), ["codex"])
        self.assertFalse(hs.is_multi_recipient(text, BOTS))

    def test_a_name_in_passing_is_not_a_request(self):
        text = "@Codex: the review is in, and @Reasonix agreed with the verdict yesterday."
        self.assertEqual(hs.requests(text, BOTS), ["codex"])   # only the bot addressed
        self.assertFalse(hs.is_multi_recipient(text, BOTS))    # so nothing is split

    def test_a_label_and_a_bullet_do_not_confuse_it(self):
        text = "[Claudebot] - @agy_dd_bot: run QA on #195 at cc57d8a, then post the result."
        self.assertEqual(hs.requests(text, BOTS), ["agy_dd_bot"])

    def test_no_known_handle_asks_nobody(self):
        self.assertEqual(hs.requests("@someone_else please look", BOTS), [])
        self.assertEqual(hs.requests("", BOTS), [])


class TestTheSplit(unittest.TestCase):

    def test_each_bot_reads_the_context_and_its_own_line(self):
        text = ("The round is stuck on two PRs.\n"
                "@reasonix_dd_bot: re-review #195.\n"
                "@agy_dd_bot: QA #195 at cc57d8a.\n"
                "Both need the result in this topic.")
        reasonix = hs.own_brief(text, "reasonix_dd_bot", BOTS)
        agy = hs.own_brief(text, "agy_dd_bot", BOTS)
        self.assertIn("The round is stuck", reasonix)
        self.assertIn("re-review #195", reasonix)
        self.assertNotIn("QA #195", reasonix)              # that is the other bot's ask
        self.assertIn("Both need the result", reasonix)    # shared context
        self.assertIn("QA #195", agy)
        self.assertNotIn("re-review #195", agy)

    def test_two_asks_on_one_line_give_each_bot_its_own(self):
        """The blocker at f7a5793: a line-level split handed each bot *both* asks.

        This is the shape Claudebot posts most: one line, two handles, two asks.
        """
        text = "@reasonix_dd_bot: review it. @agy_dd_bot: QA it."
        reasonix = hs.own_brief(text, "reasonix_dd_bot", BOTS)
        agy = hs.own_brief(text, "agy_dd_bot", BOTS)
        self.assertIn("review it", reasonix)
        self.assertNotIn("QA it", reasonix)
        self.assertIn("QA it", agy)
        self.assertNotIn("review it", agy)

    def test_one_recipient_is_not_split(self):
        text = "@Codex: review #195 please."
        self.assertEqual(hs.own_brief(text, "Codex", BOTS), text)

    def test_a_brief_is_never_emptied(self):
        """A message that is only bullets for others still gives context, not nothing."""
        text = ("- @reasonix_dd_bot: re-review #195 today please\n"
                "- @agy_dd_bot: QA #195 at cc57d8a this morning")
        asked = hs.requests(text, BOTS)
        self.assertEqual(sorted(asked), ["agy_dd_bot", "reasonix_dd_bot"])
        self.assertTrue(all(hs.own_brief(text, bot, BOTS).strip() for bot in asked))

    def test_a_line_that_only_mentions_another_bot_is_context(self):
        """#181 review: a mention is not an ask, so the line is not another bot's brief."""
        text = ("@reasonix_dd_bot: review #195.\n"
                "@agy_dd_bot: QA #195 at cc57d8a.\n"
                "@Codex agreed with the spec yesterday.")
        self.assertTrue(hs.is_multi_recipient(text, BOTS))      # so a split happens at all
        own = hs.own_brief(text, "reasonix_dd_bot", BOTS)
        self.assertIn("review #195", own)
        self.assertNotIn("QA #195", own)                        # @agy's ask
        self.assertIn("agreed with the spec", own)               # context, not a task for @Codex

    def test_a_bots_own_brief_keeps_its_line_when_it_is_not_first(self):
        """The live shape: the bot that was not the first handle still gets its own ask."""
        text = ("@reasonix_dd_bot: review #191 at e326779.\n"
                "@DeepSeek: push the fix and ask for QA.")
        own = hs.own_brief(text, "DeepSeek", BOTS)
        self.assertIn("push the fix", own)
        self.assertNotIn("review #191", own)
