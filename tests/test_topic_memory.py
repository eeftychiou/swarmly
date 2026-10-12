"""Each discussion keeps its task and progress; session runtimes only hear what is new."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swarmly.telegram.adapters.antigravity import AntigravityAdapter
from swarmly.telegram.adapters.base import AdapterReply, topic_mode
from swarmly.telegram.config import AdapterSettings
from swarmly.telegram.topic_memory import TopicMemory


def filled(path=None):
    memory = TopicMemory(path, peer_handles=("@CodexBot",))
    memory.record(1, 7, "admin", "Build the word counter with JSON output", 10)
    for n in range(11, 40):
        memory.record(1, 7, "Hermes", f"chatter {n}", n)
    memory.record(1, 7, "Hermes", "opened PR #42 for the counter", 40)
    memory.record(1, 7, "Hermes", "@CodexBot please test it", 41)
    for n in range(42, 50):
        memory.record(1, 7, "Codex", f"more chatter {n}", n)
    return memory


class TestTopicMemory(unittest.TestCase):

    def test_a_fresh_turn_keeps_the_task_and_progress_after_many_messages(self):
        recap = filled().recap(1, 7, current_id=99, fresh=True, own_alias="Hermes")
        self.assertIn("The task, as first posted by admin:\n> Build the word counter", recap)
        self.assertIn("- Hermes: opened PR #42 for the counter", recap)
        self.assertIn("- Hermes: @CodexBot please test it", recap)
        self.assertIn("more chatter 49", recap)
        self.assertNotIn("chatter 20", recap)
        self.assertTrue(recap.endswith("New message:\n"))
        self.assertLess(len(recap), 2600)          # no history pump on uncached runs

    def test_a_session_turn_gets_only_what_is_new_since_its_reply(self):
        memory = filled()
        recap = memory.recap(1, 7, current_id=99, fresh=False, own_alias="Hermes")
        self.assertNotIn("The task", recap)
        self.assertNotIn("PR #42", recap)
        self.assertIn("- Codex: more chatter 42", recap)
        self.assertIn("- Codex: more chatter 49", recap)
        memory.record(1, 7, "Hermes", "noted", 50)
        self.assertEqual(memory.recap(1, 7, current_id=99, fresh=False, own_alias="Hermes"), "")

    def test_a_new_session_hears_the_task_once(self):
        recap = filled().recap(1, 7, current_id=99, fresh=False, own_alias="Antigravity")
        self.assertIn("The task, as first posted by admin", recap)

    def test_the_current_message_is_never_repeated(self):
        memory = TopicMemory()
        memory.record(1, 7, "admin", "the task", 10)
        self.assertEqual(memory.recap(1, 7, current_id=10, fresh=True, own_alias="x"), "")

    def test_it_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "topic-memory.json"
            filled(path)
            again = TopicMemory(path)
            self.assertEqual(again.opening(1, 7)[2], "Build the word counter with JSON output")
            self.assertIn("PR #42", again.recap(1, 7, current_id=None, fresh=True,
                                                own_alias="x"))
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(TopicMemory(path).opening(1, 7))


class TestAntigravityPerTopic(unittest.TestCase):

    def make(self, folder, **settings):
        adapter = AntigravityAdapter("Antigravity", AdapterSettings(
            type="antigravity", cwd=folder, **settings))
        adapter.use_state_file(Path(folder) / "antigravity-sessions.json")
        return adapter

    def test_the_first_turn_starts_a_conversation_and_later_turns_resume_it(self):
        from swarmly.telegram.adapters.base import IncomingMessage, SubprocessAdapter
        with tempfile.TemporaryDirectory() as folder:
            adapter = self.make(folder)
            self.assertEqual(topic_mode(adapter, "tg-7"), "fresh")
            argvs = []
            store = {"old"}

            async def fake_respond(self, message):
                argvs.append(self.build_argv("prompt"))
                store.add("conv-1234abcd")          # agy writes the conversation's file
                return AdapterReply(text="ok")

            message = IncomingMessage(text="hi", author_alias="admin", context_id="tg-7")
            with mock.patch("swarmly.telegram.adapters.antigravity._cli_conversations",
                            side_effect=lambda: set(store)), \
                    mock.patch.object(SubprocessAdapter, "respond", fake_respond):
                import asyncio
                asyncio.run(adapter.respond(message))
                self.assertNotIn("--conversation", argvs[0])
                self.assertEqual(topic_mode(adapter, "tg-7"), "session")
                self.assertEqual(topic_mode(adapter, "tg-8"), "fresh")
                asyncio.run(adapter.respond(message))
            self.assertEqual(argvs[1][argvs[1].index("--conversation") + 1], "conv-1234abcd")
            saved = json.loads((Path(folder) / "antigravity-sessions.json").read_text())
            self.assertEqual(saved["tg-7"]["conversation"], "conv-1234abcd")
            with mock.patch("swarmly.telegram.adapters.antigravity._cli_conversations",
                            return_value={"conv-1234abcd"}):
                self.assertEqual(topic_mode(self.make(folder), "tg-7"), "session")
            with mock.patch("swarmly.telegram.adapters.antigravity._cli_conversations",
                            return_value=set()):         # the conversation was deleted
                self.assertEqual(topic_mode(self.make(folder), "tg-7"), "fresh")

    def test_a_configured_conversation_keeps_the_old_behaviour(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter = self.make(folder, conversation="abc12345")
            self.assertEqual(topic_mode(adapter, "tg-7"), "fresh")
            self.assertFalse(adapter._per_topic())


if __name__ == "__main__":
    unittest.main()


class TestReasonixPerTopic(unittest.TestCase):

    def make(self, folder):
        from swarmly.telegram.adapters.reasonix import ReasonixAdapter
        adapter = ReasonixAdapter("DeepSeek", AdapterSettings(type="reasonix", cwd=folder))
        adapter.use_state_file(Path(folder) / "reasonix-sessions.json")
        return adapter

    def run_turns(self, adapter, outputs):
        import asyncio
        from swarmly.telegram.adapters.base import IncomingMessage
        argvs = []
        outs = iter(outputs)

        async def fake_run(argv):
            argvs.append(list(argv))
            rc, out, err = next(outs)
            return mock.Mock(returncode=rc), out.encode(), err.encode()

        adapter._run = fake_run
        message = IncomingMessage(text="hi", author_alias="admin", context_id="tg-9")
        turns = getattr(self, "turns", len(outputs))
        replies = [asyncio.run(adapter.respond(message)) for _ in range(turns)]
        return argvs, replies

    def test_the_topic_session_is_stored_and_resumed(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter = self.make(folder)
            first = 'loading…\n{"result": "ok", "session_id": "20261012-0646-flash", "is_error": false}'
            second = '{"result": "PELICAN", "session_id": "20261012-0646-flash"}'
            argvs, replies = self.run_turns(adapter, [(0, first, ""), (0, second, "")])
            self.assertEqual([r.text for r in replies], ["ok", "PELICAN"])
            self.assertEqual(argvs[0][argvs[0].index("--output-format") + 1], "json")
            self.assertNotIn("--resume", argvs[0])
            self.assertLess(argvs[1].index("--resume"), argvs[1].index("--"))
            self.assertEqual(argvs[1][argvs[1].index("--resume") + 1], "20261012-0646-flash")
            self.assertEqual(topic_mode(self.make(folder), "tg-9"), "session")

    def test_a_lost_session_starts_a_new_one(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "reasonix-sessions.json").write_text('{"tg-9": {"session": "old-session-1"}}')
            adapter = self.make(folder)
            self.turns = 1
            argvs, replies = self.run_turns(adapter, [
                (1, "", 'error: no session matches "old-session-1"'),
                (0, '{"result": "fresh", "session_id": "new-session-2"}', "")])
            self.assertEqual(replies[0].text, "fresh")
            self.assertIn("--resume", argvs[0])
            self.assertNotIn("--resume", argvs[1])
            self.assertEqual(adapter.session_for("tg-9"), "new-session-2")

    def test_a_long_session_is_retired_after_the_turn_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter = self.make(folder)
            adapter.settings.session_turn_limit = 3
            out = '{"result": "ok", "session_id": "sess-123456"}'
            handover = ('{"result": "1. Goal and scope: issue #16 docs. 2. Decisions: never '
                        'commit flight data. 7. Next: open the PR.", "session_id": "sess-123456"}')
            new = '{"result": "ok", "session_id": "sess-654321"}'
            self.turns = 4
            argvs, replies = self.run_turns(adapter, [(0, out, "")] * 3 + [(0, handover, ""),
                                                                           (0, new, "")])
            self.assertTrue(replies[2].meta.get("rotated"))
            self.assertEqual(argvs[3][argvs[3].index("--resume") + 1], "sess-123456")
            self.assertIn("Session handover", argvs[3][-1])
            self.assertNotIn("--resume", argvs[4])           # a new session after 3 turns
            self.assertIn("taking over this topic", argvs[4][-1])
            self.assertIn("never commit flight data", argvs[4][-1])
            self.assertEqual(adapter.session_for("tg-9"), "sess-654321")
            self.assertEqual(self.make(folder)._session_turns.get("tg-9"), 1)
            self.assertFalse(self.make(folder)._handovers.pending("tg-9"))
            self.assertEqual(len(list((Path(folder) / "handover").glob("*.md"))), 1)

    def test_the_turn_count_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter = self.make(folder)
            out = '{"result": "ok", "session_id": "sess-123456"}'
            self.run_turns(adapter, [(0, out, "")] * 2)
            self.assertEqual(self.make(folder)._session_turns.get("tg-9"), 2)

    def test_a_custom_command_stays_one_shot(self):
        from swarmly.telegram.adapters.reasonix import ReasonixAdapter
        adapter = ReasonixAdapter("DeepSeek", AdapterSettings(
            type="reasonix", command="my-wrapper {prompt}"))
        self.assertFalse(adapter._sessions_supported())
        self.assertEqual(topic_mode(adapter, "tg-9"), "fresh")


class TestResumedTurnsSkipTheRules(unittest.TestCase):

    def test_a_resumed_reasonix_turn_carries_the_envelope_only(self):
        with tempfile.TemporaryDirectory() as folder:
            helper = TestReasonixPerTopic()
            adapter = helper.make(folder)
            out = '{"result": "ok", "session_id": "sess-123456"}'
            argvs, _ = helper.run_turns(adapter, [(0, out, ""), (0, out, "")])
            self.assertIn("OPERATIONAL CHANNEL INSTRUCTIONS", argvs[0][-1])
            self.assertNotIn("OPERATIONAL CHANNEL INSTRUCTIONS", argvs[1][-1])
            self.assertTrue(argvs[1][-1].startswith("[Telegram message from admin"))
            self.assertIn("turn rules", argvs[1][-1])


class TestReviewFindings(unittest.TestCase):

    def test_a_message_posted_during_a_turn_reaches_the_next_one(self):
        memory = TopicMemory()
        memory.record(1, 7, "admin", "@A do the task", 8)
        memory.recap(1, 7, current_id=8, fresh=False, own_alias="A")     # A's turn starts
        memory.record(1, 7, "admin", "@B note: use the v2 API", 9)        # while A works
        memory.record(1, 7, "A", "done", 10)                              # A's reply lands
        recap = memory.recap(1, 7, current_id=11, fresh=False, own_alias="A")
        self.assertIn("use the v2 API", recap)
        self.assertNotIn("- A: done", recap)

    def test_only_the_first_message_is_called_the_task(self):
        memory = TopicMemory()
        memory.record(1, 500, "B", "on it", 540)
        recap = memory.recap(1, 500, current_id=None, fresh=True, own_alias="A")
        self.assertIn("The earliest message seen here, from B", recap)
        self.assertNotIn("The task", recap)

    def test_a_lost_reasonix_session_gets_the_fresh_recap(self):
        import dataclasses
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "reasonix-sessions.json").write_text(
                '{"tg-9": {"session": "old-session-1"}}')
            helper = TestReasonixPerTopic()
            adapter = helper.make(folder)
            argvs = []
            outs = iter([(1, "", 'error: no session matches "old-session-1"\nhint: list'),
                         (0, '{"result": "ok", "session_id": "new-session-2"}', "")])

            async def fake_run(argv):
                argvs.append(list(argv))
                rc, out, err = next(outs)
                return mock.Mock(returncode=rc), out.encode(), err.encode()

            adapter._run = fake_run
            from swarmly.telegram.adapters.base import IncomingMessage
            message = IncomingMessage(text="since: x\nhi", author_alias="admin",
                                      context_id="tg-9", fresh_text="TASK: build it\nhi")
            import asyncio
            reply = asyncio.run(adapter.respond(dataclasses.replace(message)))
            self.assertEqual(reply.text, "ok")
            self.assertIn("TASK: build it", argvs[1][-1])
            self.assertEqual(adapter.session_for("tg-9"), "new-session-2")
