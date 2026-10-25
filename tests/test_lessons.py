"""``memory.md``: each bot's lessons, kept by the transport (#226).

Round 5.5 decided roles are dynamic, so a bot learns from experience: a small file of its own,
read into every turn, written when the transport asks, capped in code and never truncated.
These tests are the issue's acceptance list, one each.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from swarmly.telegram.config import AgentConfig
from swarmly.telegram.lessons import (END_MARKER, MAX_BYTES, Lessons, describe, extract_block,
                                      memory_path, prompt_block, size_of, turns_path)
from tests.test_telegram_channel import ADMIN_ID, event, make_channel, run

LESSON = "- the lease read must be bounded; a slow bus cost a whole round (#208)"


def config_in(tmp: str, **limits) -> AgentConfig:
    """A config with a state folder: `_state_file` needs a config path and a bus root."""
    data = {
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                  "home_channel_id": -1001234567890, "admin_user_id": ADMIN_ID},
        "peers": {"Codex": "CodexBot"},
        "adapter": {"type": "echo"},
        "limits": {"min_seconds_between_messages": 0,
                   "min_seconds_between_agent_turns": 0, **limits},
        "fallback": {"gitbus_root": tmp},
    }
    return AgentConfig.from_dict(data, source_path=Path(tmp) / "config.yaml")


def _reply_with(text: str):
    """An echo adapter that answers with exactly ``text`` (the framing is not echoed back)."""
    from swarmly.telegram.adapters import EchoAdapter
    from swarmly.telegram.adapters.base import AdapterReply

    class _Replies(EchoAdapter):
        def __init__(self):
            super().__init__("Hermes")

        async def respond(self, message):
            return AdapterReply(text=text)
    return _Replies()


class TestTheFile(unittest.TestCase):

    def test_it_lives_beside_the_topic_state_not_in_the_repo(self):
        with TemporaryDirectory() as tmp:
            cfg = config_in(tmp)
            path = memory_path(cfg)
            self.assertIsNotNone(path)
            self.assertEqual(path.name, "memory-hermes.md")
            self.assertEqual(path.parent.name, "state")
            self.assertEqual(path.parent, turns_path(cfg).parent)
            self.assertNotIn("swarmly", str(path).lower())

    def test_it_survives_a_restart(self):
        with TemporaryDirectory() as tmp:
            cfg = config_in(tmp)
            saved, said = Lessons(cfg).save(LESSON)
            self.assertTrue(saved, said)
            restarted = Lessons(config_in(tmp))          # a new process, same folder
            self.assertEqual(restarted.text(), LESSON)

    def test_an_oversize_block_is_refused_and_the_old_memory_kept(self):
        with TemporaryDirectory() as tmp:
            cfg = config_in(tmp, memory_max_bytes=120)
            lessons = Lessons(cfg)
            self.assertTrue(lessons.save(LESSON)[0])
            too_big = "\n".join([LESSON] * 5)
            saved, said = lessons.save(too_big)
            self.assertFalse(saved)
            self.assertIn(str(size_of(too_big)), said)   # the size it wrote...
            self.assertIn("120", said)                   # ...and the cap it must fit
            self.assertIn("Compress", said)
            self.assertEqual(lessons.text(), LESSON)     # the old one stands
            self.assertEqual(Lessons(config_in(tmp, memory_max_bytes=120)).text(), LESSON)

    def test_the_cap_is_counted_in_utf8_bytes(self):
        with TemporaryDirectory() as tmp:
            cfg = config_in(tmp, memory_max_bytes=64)
            # 30 three-byte characters: 90 bytes, 30 characters.
            lessons = Lessons(cfg)
            saved, said = lessons.save("✓" * 30)
            self.assertFalse(saved, said)
            self.assertIn("90", said)
            self.assertTrue(lessons.save("✓" * 20)[0])    # 60 bytes fits

    def test_clear_empties_the_file(self):
        with TemporaryDirectory() as tmp:
            cfg = config_in(tmp)
            Lessons(cfg).save(LESSON)
            self.assertIn("cleared", Lessons(cfg).clear())
            self.assertEqual(Lessons(config_in(tmp)).text(), "")


class TestTheBlockInTheReply(unittest.TestCase):

    def test_a_block_is_taken_out_and_never_posted(self):
        text = f"Done, PR #229 is up.\n\nMEMORY:\n{LESSON}\n{END_MARKER}\n"
        body, block, reason = extract_block(text)
        self.assertEqual(body, "Done, PR #229 is up.")
        self.assertEqual(block, LESSON)
        self.assertIsNone(reason)

    def test_a_block_without_the_end_marker_runs_to_the_end(self):
        body, block, _reason = extract_block(f"PR is up.\n\nMEMORY:\n{LESSON}\n")
        self.assertEqual(body, "PR is up.")
        self.assertEqual(block, LESSON)

    def test_the_transports_own_note_is_not_read_as_a_block(self):
        """#226 review nit: the note must not open a block, or a notes-only reply re-opens one."""
        for note in ("Memory updated: memory updated (68 bytes)",
                     "Memory refused: your memory is unchanged: that block is 283 bytes "
                     "and the cap is 100."):
            with self.subTest(note=note[:20]):
                whole = chr(10).join(["PR is up.", "", note])
                body, block, reason = extract_block(whole)
                self.assertEqual((body, block, reason), (whole, None, None))

    def test_a_second_block_is_not_posted_either(self):
        text = (f"PR is up.\n\nMEMORY:\n{LESSON}\n{END_MARKER}\n\nMEMORY:\n- another\n"
                f"{END_MARKER}\nMEMORY:\n- a third, unclosed")
        body, block, reason = extract_block(text)
        self.assertEqual(body, "PR is up.")
        self.assertEqual(block, LESSON)
        self.assertIn("more than one", reason or "")

    def test_an_empty_first_block_does_not_hide_the_lessons_after_it(self):
        body, block, reason = extract_block(
            f"PR is up.\n\nMEMORY:\n\n{END_MARKER}\nMEMORY:\n{LESSON}\n{END_MARKER}")
        self.assertEqual((body, block), ("PR is up.", LESSON))
        self.assertIn("more than one", reason or "")

    def test_a_reply_without_a_block_is_untouched(self):
        text = "PR is up. Nothing learned this time."
        self.assertEqual(extract_block(text), (text, None, None))

    def test_an_empty_block_is_refused_and_still_stripped(self):
        body, block, reason = extract_block("PR is up.\n\nMEMORY:\n\n")
        self.assertIsNone(block)
        self.assertIn("empty", reason or "")
        self.assertNotIn("MEMORY:", body)

    def test_the_prompt_block_says_whose_notes_these_are(self):
        block = prompt_block(LESSON)
        self.assertIn("Your lessons so far", block)
        self.assertIn("memory.md", block)
        self.assertIn(LESSON, block)
        self.assertEqual(prompt_block(""), "")           # nothing learned: no block at all

    def test_a_memory_past_the_cap_is_shown_cut_and_says_so(self):
        block = prompt_block(LESSON * 40, max_bytes=200)
        self.assertLessEqual(size_of(block), 400)        # the header, the cut text, the note
        self.assertIn("cut at 200 bytes", block)


class TestTheTurnPrompt(unittest.TestCase):
    """The acceptance list that matters in a turn: read every time, asked for every N."""

    def channel(self, tmp, adapter=None, **limits):
        return make_channel(config_in(tmp, **limits), adapter=adapter)

    def test_the_memory_is_in_every_turn(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_every_turns=99)
            channel.lessons.save(LESSON)
            self.assertIn(LESSON, channel._memory_note())
            self.assertIn(LESSON, channel._memory_note())     # and the turn after it
            self.assertIn("Your lessons so far", channel._memory_note())

    def test_the_update_is_asked_every_n_turns_and_not_more_often(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_every_turns=3)
            notes = [channel._memory_note() for _ in range(9)]
            self.assertEqual(["MEMORY:" in note for note in notes], [False, False, True] * 3)
            self.assertIn("Update your memory now", notes[2])
            self.assertIn("lessons, not a log", notes[2])

    def test_the_count_survives_a_restart(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_every_turns=3)
            channel._memory_note()                            # 1
            channel._memory_note()                            # 2
            restarted = self.channel(tmp, memory_every_turns=3)
            self.assertIn("Update your memory now", restarted._memory_note())   # 3rd

    def test_the_lessons_ride_in_the_turn_itself(self):
        """Not only in the helper: the framed message a real turn receives carries them."""
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            channel.lessons.save(LESSON)
            reply = run(channel.handle_event(event("@HermesBot: what next?")))
            self.assertIn("Your lessons so far", reply)
            self.assertIn(LESSON, reply)

    def oversize(self, channel, head="PR #229 is up.", ev=None):
        return channel.apply_memory_block(
            f"{head}\n\nMEMORY:\n" + "\n".join([LESSON] * 4) + f"\n{END_MARKER}",
            ev if ev is not None else event("@HermesBot: ship it"))

    def test_an_oversize_block_is_refused_and_nothing_of_it_is_posted(self):
        """Admin, 25 Oct: "Memory refused" lines in a work topic were the bot's feedback in
        the people's chat. The reply keeps only its point; the refusal waits for the bot."""
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_max_bytes=100)
            channel.lessons.save(LESSON)
            body = self.oversize(channel)
            self.assertEqual(body, "PR #229 is up.")
            self.assertEqual(channel.lessons.text(), LESSON)   # the old memory stands

    def test_the_refusal_is_told_on_the_next_turn_with_the_update_asked_again(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_max_bytes=100, memory_every_turns=99)
            channel.lessons.save(LESSON)
            self.oversize(channel)
            note = channel._memory_note()
            self.assertIn("After your last reply: Memory refused", note)
            self.assertIn("the cap is 100", note)
            self.assertIn("Update your memory now", note)     # compress it on this turn
            channel.lessons.turn_ran()
            later = channel._memory_note()
            self.assertNotIn("Memory refused", later)          # told once
            self.assertNotIn("Update your memory now", later)  # and asked once

    def test_a_second_refusal_in_a_row_waits_for_the_count(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_max_bytes=100, memory_every_turns=99)
            channel.lessons.save(LESSON)
            self.oversize(channel)
            asked = event("@HermesBot: next")
            self.assertIn("Update your memory now", channel._memory_note(asked))
            channel.lessons.turn_ran(asked.__dict__["memory_seq"])
            self.oversize(channel, ev=asked)                   # the re-asked block, too big
            note = channel._memory_note()
            self.assertIn("Memory refused", note)
            self.assertNotIn("Update your memory now", note)

    def test_a_refused_re_ask_is_not_re_asked_when_two_prompts_carried_it(self):
        """Review of #307: queued turns, or two topics, both carry the re-ask. The one that
        runs first writes no block; the second one's refused block must not be re-asked."""
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_max_bytes=100, memory_every_turns=99)
            self.oversize(channel)
            first, second = event("@HermesBot: one"), event("@HermesBot: two")
            self.assertIn("Update your memory now", channel._memory_note(first))
            self.assertIn("Update your memory now", channel._memory_note(second))
            channel.lessons.turn_ran(first.__dict__["memory_seq"])     # no block in it
            channel.lessons.turn_ran(second.__dict__["memory_seq"])
            self.oversize(channel, ev=second)
            self.assertNotIn("Update your memory now", channel._memory_note())

    def test_a_parallel_save_does_not_make_a_refused_re_ask_re_asked(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_max_bytes=100, memory_every_turns=99)
            self.oversize(channel)
            one, two = event("@HermesBot: a", thread_id=7), event("@HermesBot: b", thread_id=8)
            channel._memory_note(one)
            channel._memory_note(two)
            channel.lessons.turn_ran(one.__dict__["memory_seq"])
            channel.apply_memory_block(f"ok\n\nMEMORY:\n{LESSON}\n{END_MARKER}", one)  # saved
            channel.lessons.turn_ran(two.__dict__["memory_seq"])       # stale: a no-op
            self.oversize(channel, ev=two)
            note = channel._memory_note()
            self.assertIn("Memory refused", note)
            self.assertNotIn("Update your memory now", note)

    def test_a_saved_block_after_a_refusal_lets_the_next_refusal_be_re_asked(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_max_bytes=100, memory_every_turns=99)
            self.oversize(channel)
            channel._memory_note()
            channel.lessons.turn_ran()                         # the re-ask went out
            channel.apply_memory_block(f"ok\n\nMEMORY:\n{LESSON}\n{END_MARKER}", event("hi"))
            channel._memory_note()
            channel.lessons.turn_ran()
            self.oversize(channel)                             # a fresh refusal
            self.assertIn("Update your memory now", channel._memory_note())

    def test_a_turn_that_never_ran_does_not_spend_the_note(self):
        """Review of #302: the note was spent when the prompt was built, so a turn stopped,
        paused or failed before the model answered lost the refusal and its re-ask."""
        from swarmly.telegram.adapters import EchoAdapter
        from swarmly.telegram.adapters.base import AdapterReply

        class _FailsOnce(EchoAdapter):
            calls = 0

            def __init__(self):
                super().__init__("Hermes")

            async def respond(self, message):
                _FailsOnce.calls += 1
                if _FailsOnce.calls == 1:
                    return AdapterReply(ok=False, error="quota exceeded")
                return AdapterReply(text=message.text)        # the framed prompt, echoed

        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, adapter=_FailsOnce(), memory_max_bytes=100,
                                   memory_every_turns=99)
            self.oversize(channel)
            run(channel.handle_event(event("@HermesBot: first", message_id=1)))   # fails
            second = run(channel.handle_event(event("@HermesBot: second", message_id=2)))
            self.assertIn("Memory refused", second)
            self.assertIn("Update your memory now", second)
            third = run(channel.handle_event(event("@HermesBot: third", message_id=3)))
            self.assertNotIn("Memory refused", third)
            self.assertNotIn("Update your memory now", third)

    def test_a_reply_that_is_only_a_memory_block_posts_nothing(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, adapter=_reply_with(
                f"MEMORY:\n{LESSON}\n{END_MARKER}"))
            run(channel.handle_event(event("@HermesBot: anything learned?")))
            self.assertEqual(channel.lessons.text(), LESSON)
            self.assertFalse([sent for sent in channel._test_sender.sent
                              if "Memory" in sent["text"] or LESSON in sent["text"]])

    def test_the_refusal_survives_a_restart(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_max_bytes=100, memory_every_turns=99)
            channel.lessons.save(LESSON)
            self.oversize(channel)
            restarted = self.channel(tmp, memory_max_bytes=100, memory_every_turns=99)
            note = restarted._memory_note()
            self.assertIn("Memory refused", note)
            self.assertIn("Update your memory now", note)

    def test_a_reply_that_is_only_a_refused_block_posts_nothing(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_max_bytes=100)
            self.assertEqual(self.oversize(channel, head=""), "")

    def test_a_memory_block_does_not_eat_the_lines_that_follow_it(self):
        """#226 review: with no END MEMORY the block runs to the end of the reply.

        So it is read **last**: a `ROLE:` line written under it is an assignment the
        coordinator made, not a lesson, and a transport that read the block first dropped the
        assignment silently and refused the memory for being over the cap besides.
        """
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, adapter=_reply_with(chr(10).join([
                "PR #231 is up.", "", "MEMORY:", LESSON,
                "ROLE: @CodexBot reviewer because Reasonix is frozen",
            ])))
            assigned = []

            async def assign(handle, role, reason=""):
                assigned.append((handle, role))
                return True
            channel.is_coordinator = lambda *args, **kwargs: True
            channel.membership.assign = assign
            reply = run(channel.handle_event(event("@HermesBot: ship it")))
        self.assertEqual(assigned, [("CodexBot", "reviewer")])    # the line reached review
        self.assertEqual(channel.lessons.text(), LESSON)          # and was not a lesson
        self.assertNotIn("ROLE:", reply)
        self.assertNotIn("ROLE:", channel.lessons.text())

    def test_a_good_block_is_saved_and_the_reply_keeps_its_point(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            body = channel.apply_memory_block(
                f"Review is in.\n\nMEMORY:\n{LESSON}\n{END_MARKER}", event("hi"))
            self.assertEqual(body, "Review is in.")
            self.assertEqual(channel.lessons.text(), LESSON)
            note = channel._memory_note()
            self.assertIn("After your last reply: Memory updated", note)
            self.assertNotIn("Update your memory now", note)  # a saved block is not re-asked


class TestTheMemoryCommand(unittest.TestCase):
    """/memory shows the file and /memory clear empties it — admin only."""

    def channel(self, tmp):
        return make_channel(config_in(tmp))

    def admin_event(self, text):
        return event(text, author_id=ADMIN_ID, username="eeftychiou", is_bot=False,
                     replies_to_bot=True)

    def test_the_admin_reads_it(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            channel.lessons.save(LESSON)
            reply = run(channel.handle_event(self.admin_event("/memory")))
            self.assertIn("memory (", reply)
            self.assertIn(LESSON, reply)

    def test_anyone_else_is_refused(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            channel.lessons.save(LESSON)
            reply = run(channel.handle_event(          # a peer bot: admitted, not an admin
                event("/memory", author_id=202, username="CodexBot", is_bot=True,
                      replies_to_bot=True)))
            self.assertIn("only the admin", reply)
            self.assertNotIn(LESSON, reply)

    def test_clear_empties_it(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp)
            channel.lessons.save(LESSON)
            reply = run(channel.handle_event(self.admin_event("/memory clear")))
            self.assertIn("cleared", reply)
            self.assertEqual(channel.lessons.text(), "")
            self.assertEqual(Lessons(config_in(tmp)).text(), "")

    def test_an_empty_memory_says_so(self):
        with TemporaryDirectory() as tmp:
            reply = describe(self.channel(tmp).lessons.text(), max_bytes=MAX_BYTES)
            self.assertIn("empty", reply)


if __name__ == "__main__":
    unittest.main()
