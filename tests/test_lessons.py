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

    def test_an_oversize_block_is_refused_in_the_reply_and_nothing_is_posted(self):
        with TemporaryDirectory() as tmp:
            channel = self.channel(tmp, memory_max_bytes=100)
            channel.lessons.save(LESSON)
            body = channel.apply_memory_block(
                f"PR #229 is up.\n\nMEMORY:\n" + "\n".join([LESSON] * 4) + f"\n{END_MARKER}",
                event("@HermesBot: ship it"))
            self.assertIn("PR #229 is up.", body)
            self.assertIn("Memory refused", body)
            self.assertNotIn(LESSON, body)
            self.assertEqual(channel.lessons.text(), LESSON)   # the old memory stands

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
            self.assertEqual(body.splitlines()[0], "Review is in.")
            self.assertNotIn(LESSON, body)
            self.assertIn("Memory updated", body)
            self.assertEqual(channel.lessons.text(), LESSON)


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
