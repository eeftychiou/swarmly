"""Tests for limits.max_parallel_turns live per-bot setting (#491, AGENTS.md §6).

Wanted:
1. Dashboard: max_parallel_turns field on settings form offering adapter choices.
2. Command: /parallel@bot <n> (admin only); bare /parallel@bot says value in force and source;
   /parallel@bot default clears the pick.
3. Same checks as config parser on refusal with parser's own reason; leaves value as it was.
4. Pick survives restart; default returns to config value.
5. Applied without restart: limiter picks up at next idle moment without cutting running turns;
   lowering to 1 while two turns run lets both finish and holds the third.
6. Visible in /status and dashboard card.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from typing import Any, List

from swarmly.telegram import config as config_mod
from swarmly.telegram import control as ct
from swarmly.telegram import control_press as cp
from swarmly.telegram import snapshot as sn
from swarmly.telegram.channel import AgentChannel, ChannelEvent
from tests.aio import run
from tests.test_topics import Gate, settle
from tests.test_telegram_channel import ADMIN_ID, GROUP, event, make_channel, make_config


def human(text: str, thread_id: int | None = None, message_id: int = 1) -> ChannelEvent:
    return event(
        text=text,
        chat_id=GROUP,
        thread_id=thread_id,
        message_id=message_id,
        author_id=ADMIN_ID,
        username="admin",
        is_bot=False,
    )


class TestLiveParallel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import subprocess
        from tests.gitenv import git_env
        cls._repo = TemporaryDirectory()
        repo = cls._repo.name
        subprocess.run(["git", "init", "-b", "main", repo], capture_output=True, check=True,
                       env=git_env())
        subprocess.run(["git", "-C", repo, "config", "user.name", "Test"], check=True,
                       env=git_env())
        subprocess.run(["git", "-C", repo, "config", "user.email", "test@example.com"],
                       check=True, env=git_env())
        Path(repo, "README.md").write_text("init\n", encoding="utf-8")
        subprocess.run(["git", "-C", repo, "add", "README.md"], check=True, env=git_env())
        subprocess.run(["git", "-C", repo, "commit", "-m", "init"], check=True, env=git_env())

    @classmethod
    def tearDownClass(cls):
        cls._repo.cleanup()

    def channel(self, state_dir: str | None = None, parallel: int = 1,
                adapter_type: str = "echo", cwd: str | None = None,
                allow_shared_cwd: bool = False, conversation: str | None = None,
                **extra):
        cfg_dict: dict[str, Any] = {
            "agent": {"alias": "Hermes", "name": "HermesBot", "admin_user_id": ADMIN_ID},
            "limits": {"max_parallel_turns": parallel, "allow_shared_cwd": allow_shared_cwd},
            "adapter": {"type": adapter_type, "cwd": cwd or self._repo.name},
        }
        if conversation:
            cfg_dict["adapter"]["conversation"] = conversation
        if state_dir:
            cfg_dict["state"] = {"dir": state_dir}
        cfg_dict.update(extra)
        cfg = make_config(**cfg_dict)
        if state_dir:
            cfg.source_path = Path(state_dir) / "config.yaml"
        channel = make_channel(cfg)
        gate = Gate()
        channel.adapter = gate
        channel._topic_adapter = lambda workdir, suffix: gate

        async def resolved_in_place(chat_id, thread_id):
            if thread_id is not None:
                channel._resolve_folder(int(chat_id), int(thread_id))

        channel.resolve_topic_folder = resolved_in_place
        return channel, gate

    def test_bare_command_and_status_show_value_and_source(self):
        """Bare /parallel shows value and source (config vs runtime pick). /status shows it too."""
        channel, gate = self.channel(parallel=1)
        gate.release.set()

        async def scenario():
            await channel.handle_event(human("/parallel@HermesBot"))
            ans1 = channel._test_sender.sent[-1]["text"]
            self.assertIn("1 parallel turn", ans1)
            self.assertIn("config.yaml", ans1)

            # /status before override
            await channel.handle_event(human("/status@HermesBot"))
            st1 = channel._test_sender.sent[-1]["text"]
            self.assertIn("parallel=1", st1)
            self.assertNotIn("(override)", st1)

            # Set 2 via command
            await channel.handle_event(human("/parallel@HermesBot 2"))
            ans2 = channel._test_sender.sent[-1]["text"]
            self.assertIn("runs up to 2 parallel turns", ans2)

            # Bare command now shows runtime pick
            await channel.handle_event(human("/parallel@HermesBot"))
            ans3 = channel._test_sender.sent[-1]["text"]
            self.assertIn("2 parallel turns", ans3)
            self.assertIn("runtime pick", ans3)

            # /status shows override
            await channel.handle_event(human("/status@HermesBot"))
            st2 = channel._test_sender.sent[-1]["text"]
            self.assertIn("parallel=2(override)", st2)

        run(scenario())

    def test_admin_sets_2_and_second_topic_runs_beside_first(self):
        """Through handle_event: the admin sets 2, and a second topic then runs beside the first."""
        channel, gate = self.channel(parallel=1)

        async def scenario():
            # Initially 1 turn runs at a time
            t1 = asyncio.ensure_future(channel.handle_event(human("@HermesBot t1", thread_id=4, message_id=1)))
            await settle()
            self.assertEqual(gate.running, 1)

            # Admin sets 2 via handle_event while t1 is running
            await channel.handle_event(human("/parallel@HermesBot 2", thread_id=4, message_id=2))
            await settle()

            # Now topic 2 arrives: since parallel is 2, it runs beside topic 1!
            t2 = asyncio.ensure_future(channel.handle_event(human("@HermesBot t2", thread_id=5, message_id=3)))
            await settle()
            self.assertEqual(gate.running, 2)
            self.assertEqual(gate.peak, 2)

            # Release both turns
            gate.release.set()
            await asyncio.gather(t1, t2)

        run(scenario())

    def test_non_admin_is_refused(self):
        """A non-admin user is refused and value is unchanged."""
        channel, gate = self.channel(parallel=1)

        async def scenario():
            peer_event = event("/parallel@HermesBot 2", author_id=202, username="CodexBot", is_bot=True)
            await channel.handle_event(peer_event)
            ans = channel._test_sender.sent[-1]["text"]
            self.assertIn("only the admin can change parallel turns", ans)
            self.assertEqual(channel.max_parallel_turns(), 1)

        run(scenario())

    def test_refusals_give_parser_reason_and_leave_value_as_it_was(self):
        """Each refusal in point 3 says the parser's reason and leaves value as it was."""
        with TemporaryDirectory() as tmp:
            not_git = Path(tmp) / "not_git"
            not_git.mkdir()

            # 1. Above MAX_PARALLEL_TURNS (8)
            ch1, _ = self.channel(parallel=1)
            async def test_cap():
                await ch1.handle_event(human("/parallel@HermesBot 9"))
                ans = ch1._test_sender.sent[-1]["text"]
                self.assertIn("limits.max_parallel_turns must be at most 8 (got 9)", ans)
                self.assertEqual(ch1.max_parallel_turns(), 1)
            run(test_cap())

            # 2. Above 1 on single-session adapter (codex_thread)
            ch2, _ = self.channel(parallel=1, adapter_type="codex_thread")
            async def test_single():
                await ch2.handle_event(human("/parallel@HermesBot 2"))
                ans = ch2._test_sender.sent[-1]["text"]
                self.assertIn("needs an adapter that keeps one session per discussion", ans)
                self.assertEqual(ch2.max_parallel_turns(), 1)
            run(test_single())

            # 3. Above 1 where cwd is not a git checkout and allow_shared_cwd is false
            ch3, _ = self.channel(parallel=1, cwd=str(not_git), allow_shared_cwd=False,
                                 adapter_type="claude")
            async def test_not_git():
                await ch3.handle_event(human("/parallel@HermesBot 2"))
                ans = ch3._test_sender.sent[-1]["text"]
                self.assertIn("is not a git checkout", ans)
                self.assertEqual(ch3.max_parallel_turns(), 1)
            run(test_not_git())

    def test_pick_survives_restart_and_default_returns_to_config(self):
        """The pick survives a restart, and default returns to config's value."""
        with TemporaryDirectory() as state_tmp:
            ch1, _ = self.channel(state_dir=state_tmp, parallel=1)
            async def set_override():
                await ch1.handle_event(human("/parallel@HermesBot 3"))
                self.assertEqual(ch1.max_parallel_turns(), 3)
            run(set_override())

            # Simulate restart with fresh channel instance pointing at same state
            ch2, _ = self.channel(state_dir=state_tmp, parallel=1)
            self.assertEqual(ch2.max_parallel_turns(), 3)
            self.assertEqual(ch2._parallel_override, 3)

            # Admin calls default
            async def set_default():
                await ch2.handle_event(human("/parallel@HermesBot default"))
                ans = ch2._test_sender.sent[-1]["text"]
                self.assertIn("goes back to 1 parallel turn", ans)
                self.assertIn("config.yaml", ans)
                self.assertEqual(ch2.max_parallel_turns(), 1)
                self.assertIsNone(ch2._parallel_override)
            run(set_default())

            # Simulate another restart: back to config default (1)
            ch3, _ = self.channel(state_dir=state_tmp, parallel=1)
            self.assertEqual(ch3.max_parallel_turns(), 1)
            self.assertIsNone(ch3._parallel_override)

    def test_lowering_to_1_while_two_turns_run_lets_both_finish_and_holds_third(self):
        """Lowering to 1 while two turns run lets both finish and holds the third."""
        channel, gate = self.channel(parallel=2)

        async def scenario():
            # Topic 1 starts
            t1 = asyncio.ensure_future(channel.handle_event(human("@HermesBot t1", thread_id=4, message_id=1)))
            await settle()
            # Topic 2 starts
            t2 = asyncio.ensure_future(channel.handle_event(human("@HermesBot t2", thread_id=5, message_id=2)))
            await settle()
            self.assertEqual(gate.running, 2)

            # Admin lowers to 1 while 2 turns run
            await channel.handle_event(human("/parallel@HermesBot 1", thread_id=4, message_id=3))
            ans = channel._test_sender.sent[-1]["text"]
            self.assertIn("from the next idle moment", ans)
            self.assertIn("2 turns running now", ans)

            # Topic 3 arrives in thread 6: it MUST be held!
            t3 = asyncio.ensure_future(channel.handle_event(human("@HermesBot t3", thread_id=6, message_id=4)))
            await settle()
            self.assertEqual(gate.running, 2)
            self.assertFalse(t3.done())

            # Release t1 and t2 together: the gate's one event frees both.
            gate.release.set()
            await asyncio.gather(t1, t2)
            await settle()

            # Now that both finished (idle moment!), t3 ran and finished
            await t3
            self.assertEqual(channel.max_parallel_turns(), 1)
            # A notice was posted stating that max_parallel_turns is now 1
            sent_texts = [s["text"] for s in channel._test_sender.sent]
            self.assertTrue(any("max_parallel_turns is now 1" in t for t in sent_texts))

        run(scenario())

    def test_lowering_to_1_with_one_turn_running_holds_the_next(self):
        """A cap of 1 set while one turn runs still lets only that turn run (#491 review B1).

        Applying the lower value at once dropped `_turn_slots` while the running turn held
        only its own per-topic lock, so the next topic started beside it: two turns at a cap
        of 1.
        """
        channel, gate = self.channel(parallel=2)

        async def scenario():
            t1 = asyncio.ensure_future(
                channel.handle_event(human("@HermesBot t1", thread_id=4, message_id=1)))
            await settle()
            self.assertEqual(gate.running, 1)

            await channel.handle_event(human("/parallel@HermesBot 1", thread_id=4, message_id=3))
            ans = channel._test_sender.sent[-1]["text"]
            self.assertIn("from the next idle moment", ans)

            t2 = asyncio.ensure_future(
                channel.handle_event(human("@HermesBot t2", thread_id=5, message_id=2)))
            await settle()
            self.assertEqual(gate.running, 1, "a second turn ran at max_parallel_turns=1")
            self.assertFalse(t2.done())

            gate.release.set()
            await asyncio.gather(t1, t2)
            await settle()
            self.assertEqual(channel.max_parallel_turns(), 1)

        run(scenario())

    def test_a_pinned_conversation_is_refused(self):
        """Above 1 is refused while the adapter pins one conversation (#491 point 3)."""
        channel, _ = self.channel(parallel=1, adapter_type="antigravity",
                                 conversation="conv-42")

        async def scenario():
            await channel.handle_event(human("/parallel@HermesBot 2"))
            ans = channel._test_sender.sent[-1]["text"]
            self.assertIn("pins a single one", ans)
            self.assertEqual(channel.max_parallel_turns(), 1)
            self.assertIsNone(channel._parallel_override)

        run(scenario())

    def test_a_stale_pick_is_dropped_on_load(self):
        """A stored pick the adapter can no longer carry falls back to config (#491 point 4)."""
        with TemporaryDirectory() as state_tmp:
            ch1, _ = self.channel(state_dir=state_tmp, parallel=1)

            async def set_override():
                await ch1.handle_event(human("/parallel@HermesBot 3"))
                self.assertEqual(ch1.max_parallel_turns(), 3)

            run(set_override())

            # The config changes under the pick: codex_thread keeps one conversation, so 4 is
            # not a value this bot may run any more.
            ch2, _ = self.channel(state_dir=state_tmp, parallel=1, adapter_type="codex_thread")
            self.assertEqual(ch2.max_parallel_turns(), 1)
            self.assertIsNone(ch2._parallel_override)
            self.assertIsNone(ch2._turn_slots)

    def test_a_stored_pick_reaches_the_adapter_on_load(self):
        """A restart hands the stored pick to the adapter too, not only to the limiter (B2).

        The #78 pinned-conversation guard reads the adapter's own idea of the limit: an
        adapter left at the config's number while the limiter ran the pick would accept a
        runtime conversation pin with parallel turns above 1.
        """
        with TemporaryDirectory() as state_tmp:
            ch1, _ = self.channel(state_dir=state_tmp, parallel=1)

            async def set_override():
                await ch1.handle_event(human("/parallel@HermesBot 3"))
                self.assertEqual(ch1.max_parallel_turns(), 3)

            run(set_override())

            cfg = make_config(
                agent={"alias": "Hermes", "name": "HermesBot", "admin_user_id": ADMIN_ID},
                limits={"max_parallel_turns": 1},
                adapter={"type": "antigravity", "cwd": self._repo.name},
                state={"dir": state_tmp},
            )
            cfg.source_path = Path(state_tmp) / "config.yaml"
            ch2 = AgentChannel(cfg)          # its own real adapter, built from the config
            self.assertEqual(ch2.max_parallel_turns(), 3)
            self.assertEqual(ch2.adapter.max_parallel_turns, 3)

    def test_a_stored_pick_reaches_a_topics_own_adapter(self):
        """pick 2 with `config.yaml` at 1 must reach the adapter a topic's turn runs in (B2).

        `_topic_adapter` builds one adapter per working copy, and it used config.yaml's
        number: that adapter would accept a pinned conversation while two topics run, which
        is the #78 guard turned off by a runtime pick (QA on #509).
        """
        with TemporaryDirectory() as state_tmp:
            ch1, _ = self.channel(state_dir=state_tmp, parallel=1)

            async def set_override():
                await ch1.handle_event(human("/parallel@HermesBot 2"))
                self.assertEqual(ch1.max_parallel_turns(), 2)

            run(set_override())

            cfg = make_config(
                agent={"alias": "Hermes", "name": "HermesBot", "admin_user_id": ADMIN_ID},
                limits={"max_parallel_turns": 1},
                adapter={"type": "antigravity", "cwd": self._repo.name},
                state={"dir": state_tmp},
            )
            cfg.source_path = Path(state_tmp) / "config.yaml"
            ch2 = AgentChannel(cfg)
            self.assertEqual(ch2.max_parallel_turns(), 2)
            topic_adapter = ch2._topic_adapter(Path(self._repo.name), "topic-4")
            self.assertEqual(topic_adapter.max_parallel_turns, 2)
            ok, said = topic_adapter.set_active_conversation(
                "0190f000-0000-7000-8000-000000000001")
            self.assertFalse(ok, said)
            self.assertIn("parallel turns", said)

    def test_one_control_line_per_press(self):
        """A press logs its own line, so the command must not log a second (#491 review S1)."""
        channel, _ = self.channel(parallel=1)

        async def scenario():
            def controls():
                return [s["text"] for s in channel._test_sender.sent
                        if "CONTROL" in s["text"] and "bot.parallel" in s["text"]]
            # The typed command owns its line ...
            await channel.handle_event(human("/parallel@HermesBot 2"))
            await settle()
            self.assertEqual(len(controls()), 1, controls())
            # ... and a press, which writes its own, does not get a second.
            await channel._apply_pick("bot.parallel", "3")
            await settle()
            self.assertEqual(channel.max_parallel_turns(), 3)
            self.assertEqual(len(controls()), 1, controls())

        run(scenario())

    def test_the_beat_carries_the_parallel_fields_to_a_peer(self):
        """A peer's card offers its own parallel values, read from its last beat (#491, #373).

        The fields have to survive the beat both ways: `status_snapshot` puts them on the
        record, `Snapshot.to_dict` writes them onto the wire, and `from_dict` reads them back
        for `peer_choices`. This pins the one thing #509 claimed and #508 was asked to check
        it had not lost: a peer's card offering that bot's own parallel values.
        """
        channel, _ = self.channel(parallel=3)

        async def scenario():
            snap = channel.status_snapshot(1000.0)
            self.assertEqual(snap.max_parallel_turns, 3)
            self.assertEqual(snap.parallels, tuple(str(k) for k in range(1, 9)))
            row = sn.Snapshot.from_dict(snap.to_dict())
            self.assertEqual(row.max_parallel_turns, 3)
            self.assertEqual(row.parallels, snap.parallels)

            peer = SimpleNamespace(snapshot=snap.to_dict())
            channel.presence = lambda: SimpleNamespace(peers={"Codex": peer})
            self.assertEqual(channel.peer_choices("bot.parallel", "Codex"), snap.parallels)
            # The card's prefilled value reads the same beat through `control_snapshots`.
            channel.control_snapshots = lambda: [sn.Snapshot.from_dict(
                dict(snap.to_dict(), alias="Codex"))]
            self.assertEqual(channel.peer_pick_value("Codex", "bot.parallel"), "3")

        run(scenario())

    def test_dashboard_settings_press_and_choices(self):
        """Settings form carries max_parallel_turns and one Apply sets it with CONTROL line."""
        channel, _ = self.channel(parallel=1)
        fields = {f["key"]: f for f in channel.settings_fields()}
        self.assertIn("max_parallel_turns", fields)
        self.assertEqual(fields["max_parallel_turns"]["value"], "1")
        # Echo in git checkout offers all 8 choices
        self.assertEqual(len(fields["max_parallel_turns"]["choices"]), 8)

        # Apply max_parallel_turns=2 via dashboard settings press
        answer = run(channel.dashboard_action({"action": "bot.settings", "value": "max_parallel_turns=2"}))
        self.assertTrue(answer["ok"])
        self.assertIn("max_parallel_turns → 2", answer["text"])
        self.assertEqual(channel.max_parallel_turns(), 2)

        # A CONTROL bot.settings line was posted on the durable plane
        notes = [s["text"] for s in channel._test_sender.sent if "CONTROL bot.settings" in s["text"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("value=max_parallel_turns=2", notes[0])


    def test_the_keyboard_pick_and_the_page_carry_the_control(self):
        """The card's Parallel picker sets it live, and the build claims it (#491)."""
        channel, _ = self.channel(parallel=1)
        self.assertIn("bot.parallel", channel.control_actions())
        self.assertTrue(ct.is_picker("bot.parallel"))
        said = run(channel._apply_pick("bot.parallel", "3"))
        self.assertEqual(channel.max_parallel_turns(), 3)
        self.assertIn("runs up to 3 parallel turns", said)


if __name__ == "__main__":
    unittest.main()
