from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from swarmly.telegram.adapters.antigravity import (
    AntigravityAdapter,
    AntigravityThreadAdapter,
    resolve_agentapi,
    ACTIVE_CONVERSATION_FILENAME,
)
from swarmly.telegram.adapters.base import IncomingMessage
from swarmly.telegram.config import AdapterSettings


class TestAntigravityAdapter(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmpdir.name).resolve()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _create_mock_summaries_db(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                preview TEXT,
                step_count INTEGER,
                last_modified_time datetime,
                workspace_uris TEXT,
                status TEXT,
                source TEXT,
                project_id TEXT,
                agent_name TEXT,
                parent_conversation_id TEXT,
                nesting_depth INTEGER,
                battle_id TEXT,
                winning_conversation_id TEXT,
                not_fully_idle numeric,
                killed numeric,
                last_user_input_time datetime,
                last_user_input_step_index INTEGER,
                app_data_dir TEXT,
                raw_summary BLOB,
                group_id TEXT
            )
        """)

        norm_path = str(self.cwd).replace("\\", "/")
        cwd_uri = "file:///" + norm_path
        other_uri = "file:///c:/Users/User/OtherProject"

        # Convo 1: matching cwd, older
        conn.execute("""
            INSERT INTO conversation_summaries (
                conversation_id, title, last_modified_time, workspace_uris, parent_conversation_id, nesting_depth, killed
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("conv-old", "Old Workspace Convo", "2026-09-19 09:00:00.000", json.dumps([cwd_uri]), "", 0, 0))

        # Convo 2: other workspace, newer
        conn.execute("""
            INSERT INTO conversation_summaries (
                conversation_id, title, last_modified_time, workspace_uris, parent_conversation_id, nesting_depth, killed
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("conv-other", "Other Project", "2026-09-19 10:30:00.000", json.dumps([other_uri]), "", 0, 0))

        # Convo 3: matching cwd, subagent (nesting_depth=1) - should be ignored
        conn.execute("""
            INSERT INTO conversation_summaries (
                conversation_id, title, last_modified_time, workspace_uris, parent_conversation_id, nesting_depth, killed
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("conv-subagent", "Subagent Convo", "2026-09-19 10:25:00.000", json.dumps([cwd_uri]), "conv-target", 1, 0))

        # Convo 4: matching cwd, newest root convo - target!
        conn.execute("""
            INSERT INTO conversation_summaries (
                conversation_id, title, last_modified_time, workspace_uris, parent_conversation_id, nesting_depth, killed
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("conv-target", "Target Workspace Convo", "2026-09-19 10:20:00.000", json.dumps([cwd_uri]), "", 0, 0))

        # Convo 5: matching cwd, killed - should be ignored
        conn.execute("""
            INSERT INTO conversation_summaries (
                conversation_id, title, last_modified_time, workspace_uris, parent_conversation_id, nesting_depth, killed
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("conv-killed", "Killed Convo", "2026-09-19 10:40:00.000", json.dumps([cwd_uri]), "", 0, 1))

        conn.commit()
        conn.close()

    def test_resolve_conversation_auto_from_db(self):
        gemini_dir = self.cwd / ".mock_gemini"
        mock_db = gemini_dir / "antigravity" / "conversation_summaries.db"
        self._create_mock_summaries_db(mock_db)

        settings = AntigravityAdapter.default_settings(cwd=str(self.cwd), conversation="auto")
        adapter = AntigravityAdapter("Antigravity", settings)

        with patch("pathlib.Path.home", return_value=self.cwd / "nonexistent_home"), \
             patch("swarmly.telegram.adapters.antigravity.Path.home", return_value=self.cwd / "fake_home"):
            # Patch the sum_dbs in resolve_conversation
            with patch.object(adapter, "_get_summary_dbs", return_value=[mock_db]):
                res = adapter.resolve_conversation()
                self.assertIsNotNone(res)
                conv_id, title = res
                self.assertEqual(conv_id, "conv-target")
                self.assertEqual(title, "Target Workspace Convo")

    def test_resolve_conversation_from_active_file(self):
        active_file = self.cwd / ACTIVE_CONVERSATION_FILENAME
        active_file.write_text("conv-pinned-file", encoding="utf-8")

        settings = AntigravityAdapter.default_settings(cwd=str(self.cwd), conversation="auto")
        adapter = AntigravityAdapter("Antigravity", settings)
        with patch.object(adapter, "_lookup_conversation", return_value=("conv-pinned-file", "Pinned")):
            res = adapter.resolve_conversation()
            self.assertIsNotNone(res)
            self.assertEqual(res[0], "conv-pinned-file")

    def test_lookup_conversation_unknown_non_uuid_returns_none(self):
        settings = AntigravityAdapter.default_settings(cwd=str(self.cwd))
        adapter = AntigravityAdapter("Antigravity", settings)
        with patch.object(adapter, "_get_summary_dbs", return_value=[]):
            self.assertIsNone(adapter._lookup_conversation("nonexistent-title"))
            test_uuid = "12345678-1234-5678-1234-567812345678"
            self.assertEqual(adapter._lookup_conversation(test_uuid), (test_uuid, test_uuid))

    def test_antigravity_thread_queues_via_agentapi(self):
        test_uuid = "12345678-1234-5678-1234-567812345678"
        settings = AntigravityThreadAdapter.default_settings(cwd=str(self.cwd), conversation=test_uuid)
        adapter = AntigravityThreadAdapter("Antigravity", settings)

        completed = subprocess.CompletedProcess(
            args=["agentapi", "send-message"], returncode=0, stdout='{"ok": true}', stderr=""
        )

        # No endpoint discovery: on Windows it runs tasklist and PowerShell through the same
        # subprocess.run this test counts.
        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value="/bin/agentapi"), \
             patch("swarmly.telegram.endpoint.resolve_endpoint", return_value=None), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run", return_value=completed) as mock_run:
            msg = IncomingMessage(text="please review PR #17", author_alias="Hermes", author_username="example_hermes_bot")
            reply = asyncio.run(adapter.respond(msg))

            self.assertIsNone(reply)
            mock_run.assert_called_once()
            args = mock_run.call_args.args[0]
            self.assertEqual(args[0], "/bin/agentapi")
            self.assertEqual(args[1], "send-message")
            self.assertIn("--title=Telegram: @example_hermes_bot", args[2])
            self.assertEqual(args[3], test_uuid)
            self.assertIn("please review PR #17", args[4])
            self.assertEqual(mock_run.call_args.kwargs.get("encoding"), "utf-8")
            self.assertEqual(mock_run.call_args.kwargs.get("errors"), "replace")

    def test_endpoint_recovery_does_not_block_the_event_loop(self):
        """DISCRIMINATING: recovery must run OFF the channel's single event loop.

        ``resolve_endpoint`` shells out -- a ~1.3s PowerShell/CIM query for the
        live processes, then agentapi probes of up to 5s per candidate. Called
        synchronously from ``async def respond`` it holds the loop for that entire
        duration, so heartbeats stop and concurrent messages queue behind it.
        Measured on this host: 2.4s for a cold ``resolve_endpoint``.

        The observable is loop liveness, not thread identity: a ticker coroutine
        counts how often it is scheduled while recovery is stalling for 0.5s. On
        the loop it gets ~0 ticks; off-loop it gets dozens. The 10-tick threshold
        is far from both, so the test cannot flap on a slow machine.
        """
        import contextlib
        import time as _time
        from swarmly.telegram import endpoint as endpoint_module

        test_uuid = "12345678-1234-5678-1234-567812345678"
        settings = AntigravityThreadAdapter.default_settings(
            cwd=str(self.cwd), conversation=test_uuid)
        adapter = AntigravityThreadAdapter("Antigravity", settings)

        STALL = 0.5
        ticks = 0

        def slow_resolve(*_args, **_kwargs):
            _time.sleep(STALL)              # stands in for the host query + probes
            return endpoint_module.EndpointProbe(
                address="127.0.0.1:1", port="1", source="environment",
                latency_ms=0, csrf_token="stand-in")

        completed = subprocess.CompletedProcess(
            args=["agentapi", "send-message"], returncode=0,
            stdout='{"ok": true}', stderr="")

        async def scenario() -> None:
            nonlocal ticks

            async def ticker() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.005)

            beat = asyncio.create_task(ticker())
            try:
                with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                           return_value="/bin/agentapi"), \
                     patch("swarmly.telegram.endpoint.resolve_endpoint",
                           side_effect=slow_resolve), \
                     patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                           return_value=completed):
                    msg = IncomingMessage(text="are you there?", author_alias="Admin")
                    await adapter.respond(msg)
            finally:
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat

        asyncio.run(scenario())

        self.assertGreater(
            ticks, 10,
            "the event loop stopped ticking during endpoint recovery: it is being "
            "called synchronously instead of via asyncio.to_thread")

    def test_antigravity_thread_falls_back_to_agy_when_agentapi_missing(self):
        test_uuid = "12345678-1234-5678-1234-567812345678"
        settings = AntigravityThreadAdapter.default_settings(cwd=str(self.cwd), conversation=test_uuid)
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        adapter.resolve_conversation_model = lambda cid: "gemini-3.8-flash-high"

        completed = subprocess.CompletedProcess(
            args=["agy", "-p"], returncode=0, stdout='done', stderr=""
        )

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value=None), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run", return_value=completed) as mock_run:
            msg = IncomingMessage(text="status check", author_alias="Hermes")
            reply = asyncio.run(adapter.respond(msg))

            self.assertIsNone(reply)
            mock_run.assert_called_once()
            args = mock_run.call_args.args[0]
            self.assertEqual(args[0:2], ["agy", "-p"])
            self.assertIn("--conversation", args)
            self.assertIn(test_uuid, args)
            self.assertIn("--model", args)
            self.assertIn("gemini-3.8-flash-high", args)
            self.assertEqual(mock_run.call_args.kwargs.get("encoding"), "utf-8")
            self.assertEqual(mock_run.call_args.kwargs.get("errors"), "replace")

    def test_headless_host_does_not_pay_a_discovery_per_message(self):
        """DISCRIMINATING: the negative cache must survive the retry loop.

        On a headless host discovery finds nothing, so `resolve_endpoint` takes
        its negative path. The adapter's retry loop used to call
        `invalidate_cache()` unconditionally on an endpoint failure, which clears
        `_negative_until` -- so attempt 2 always re-read the host. Measured: one
        full `discover()` per message, forever, defeating the constant whose
        docstring promises "one probe per minute".

        The observable is the number of `discover()` calls over three messages: a
        healthy negative cache means one, a defeated one means one per message.
        Counts, not timings, so it cannot flap.
        """
        import os
        from swarmly.telegram import endpoint as endpoint_module

        test_uuid = "12345678-1234-5678-1234-567812345678"
        settings = AntigravityThreadAdapter.default_settings(
            cwd=str(self.cwd), conversation=test_uuid)
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        adapter.allow_blocking_fallback = False        # the transport path

        discoveries = 0

        def counting_discover(*_args, **_kwargs):
            nonlocal discoveries
            discoveries += 1
            return None                                # headless: nothing to find

        # Exactly what agentapi prints when no endpoint was injected.
        failed = subprocess.CompletedProcess(
            args=["agentapi"], returncode=1, stdout="",
            stderr='{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}')

        # The negative cache is module-level, so a preceding test could otherwise
        # leave it warm and the count would be 0 -- passing even with the bug.
        endpoint_module.invalidate_cache()
        # ...and scrub the ambient environment: an inherited ANTIGRAVITY_LS_ADDRESS
        # would take tier 3 and never call discover() at all.
        scrubbed = {k: v for k, v in os.environ.items()
                    if k.startswith("ANTIGRAVITY_")}

        async def scenario():
            for i in range(3):
                msg = IncomingMessage(text=f"ping {i}", author_alias="Admin")
                await adapter.respond(msg)

        try:
            for k in scrubbed:
                os.environ.pop(k, None)
            with patch.object(endpoint_module, "discover",
                              side_effect=counting_discover), \
                 patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                       return_value="/bin/agentapi"), \
                 patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                       return_value=failed):
                asyncio.run(scenario())
        finally:
            os.environ.update(scrubbed)

        self.assertEqual(
            discoveries, 1,
            f"discovery ran {discoveries}x for 3 messages on a headless host: the "
            "retry loop is clearing the negative cache, so every message pays a "
            "full host read instead of reusing the negative result for "
            "_NEGATIVE_TTL_SECONDS")

    def test_headless_host_does_not_spawn_a_second_identical_agentapi(self):
        """DISCRIMINATING: one dispatch per message, not two.

        The negative-cache fix stopped the retry re-READING the host, but the
        retry itself still ran: attempt 2 resolved the same `None` straight off
        the negative cache and handed the child byte-identical argv, so the
        second agentapi process could only repeat the first one's exit code and
        reason. Measured before the loop learned to stop: 2.0 agentapi processes
        per message on a headless host.

        The observable is the number of `agentapi` processes over three messages.
        Counts only (and only argv[0] containing "agentapi", so an unrelated
        subprocess call cannot inflate it), so it cannot flap on timing.
        """
        import os
        from swarmly.telegram import endpoint as endpoint_module

        test_uuid = "12345678-1234-5678-1234-567812345678"
        settings = AntigravityThreadAdapter.default_settings(
            cwd=str(self.cwd), conversation=test_uuid)
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        adapter.allow_blocking_fallback = False        # the transport path

        runs = 0
        failed = subprocess.CompletedProcess(
            args=["agentapi"], returncode=1, stdout="",
            stderr='{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}')

        def counting_run(*args, **kwargs):
            nonlocal runs
            argv = args[0] if args else kwargs.get("args", [])
            if argv and "agentapi" in str(argv[0]):
                runs += 1
            return failed

        endpoint_module.invalidate_cache()
        scrubbed = {k: v for k, v in os.environ.items()
                    if k.startswith("ANTIGRAVITY_")}

        async def scenario():
            for i in range(3):
                msg = IncomingMessage(text=f"ping {i}", author_alias="Admin")
                await adapter.respond(msg)

        try:
            for k in scrubbed:
                os.environ.pop(k, None)
            with patch.object(endpoint_module, "discover", return_value=None), \
                 patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                       return_value="/bin/agentapi"), \
                 patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                       side_effect=counting_run):
                asyncio.run(scenario())
        finally:
            os.environ.update(scrubbed)

        self.assertEqual(
            runs, 3,
            f"agentapi ran {runs}x for 3 messages ({runs / 3:.1f} per message): "
            "with nothing resolved and recovery not erroring, the retry cannot "
            "differ from attempt 1, so a second process per message is pure cost")

    def test_a_recovery_error_still_retries_the_dispatch(self):
        """INVARIANT: the retry must survive when recovery RAISED.

        This is the guard on the fix above. `resolve_endpoint` returning `None`
        means discovery reached its negative-cache arm, so attempt 2 provably
        cannot differ. An EXCEPTION never reaches that arm, leaving the negative
        cache unarmed -- so attempt 2 does a real discovery and can genuinely
        recover. Skipping the retry in that case would trade a per-message cost
        for a lost recovery path, which is the wrong trade.

        Two messages: the dispatch must still be attempted on both attempts of
        the first, so the count exceeds one per message.
        """
        import os
        from swarmly.telegram import endpoint as endpoint_module

        test_uuid = "12345678-1234-5678-1234-567812345678"
        settings = AntigravityThreadAdapter.default_settings(
            cwd=str(self.cwd), conversation=test_uuid)
        adapter = AntigravityThreadAdapter("Antigravity", settings)
        adapter.allow_blocking_fallback = False

        runs = 0
        failed = subprocess.CompletedProcess(
            args=["agentapi"], returncode=1, stdout="",
            stderr='{"error": "ANTIGRAVITY_LS_ADDRESS is not set"}')

        def counting_run(*args, **kwargs):
            nonlocal runs
            argv = args[0] if args else kwargs.get("args", [])
            if argv and "agentapi" in str(argv[0]):
                runs += 1
            return failed

        def exploding_resolve(*_args, **_kwargs):
            raise RuntimeError("host query blew up")

        endpoint_module.invalidate_cache()
        scrubbed = {k: v for k, v in os.environ.items()
                    if k.startswith("ANTIGRAVITY_")}

        async def scenario():
            msg = IncomingMessage(text="ping", author_alias="Admin")
            await adapter.respond(msg)

        try:
            for k in scrubbed:
                os.environ.pop(k, None)
            with patch.object(endpoint_module, "resolve_endpoint",
                              side_effect=exploding_resolve), \
                 patch("swarmly.telegram.adapters.antigravity.resolve_agentapi",
                       return_value="/bin/agentapi"), \
                 patch("swarmly.telegram.adapters.antigravity.subprocess.run",
                       side_effect=counting_run):
                asyncio.run(scenario())
        finally:
            os.environ.update(scrubbed)

        self.assertEqual(
            runs, 2,
            "a recovery EXCEPTION left the negative cache unarmed, so attempt 2 "
            "can genuinely resolve a working endpoint -- the retry must not be "
            "skipped in that case")

    def test_antigravity_thread_admin_switch_context(self):
        settings = AntigravityThreadAdapter.default_settings(cwd=str(self.cwd), conversation="auto")
        adapter = AntigravityThreadAdapter("Antigravity", settings)

        with patch.object(adapter, "_lookup_conversation", return_value=("new-session-uuid", "New Session")):
            msg = IncomingMessage(
                text="switch context to new-session-uuid",
                author_alias="Admin",
                is_admin=True
            )
            reply = asyncio.run(adapter.respond(msg))
            self.assertIsNotNone(reply)
            self.assertTrue(reply.ok)
            self.assertIn("new-session-uuid", reply.text)
            self.assertEqual(
                (self.cwd / ACTIVE_CONVERSATION_FILENAME).read_text(encoding="utf-8"),
                "new-session-uuid"
            )

    def test_antigravity_adapter_is_stateless_by_default(self):
        gemini_dir = self.cwd / ".mock_gemini"
        mock_db = gemini_dir / "antigravity" / "conversation_summaries.db"
        self._create_mock_summaries_db(mock_db)

        # conversation=None (default for AntigravityAdapter), no .active_conversation file
        settings = AntigravityAdapter.default_settings(cwd=str(self.cwd), conversation=None)
        adapter = AntigravityAdapter("Antigravity", settings)

        with patch.object(adapter, "_get_summary_dbs", return_value=[mock_db]):
            res = adapter.resolve_conversation()
            self.assertIsNone(res)

    def test_antigravity_thread_fallback_syncs_sqlite(self):
        test_uuid = "12345678-1234-5678-1234-567812345678"
        settings = AntigravityThreadAdapter.default_settings(cwd=str(self.cwd), conversation=test_uuid)
        adapter = AntigravityThreadAdapter("Antigravity", settings)

        completed = subprocess.CompletedProcess(
            args=["agy", "-p"], returncode=0, stdout='done', stderr=""
        )

        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value=None), \
             patch.object(adapter, "sync_conversation_to_cli") as mock_sync_cli, \
             patch.object(adapter, "sync_conversation_to_ide") as mock_sync_ide, \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run", return_value=completed):
            msg = IncomingMessage(text="status check", author_alias="Hermes")
            reply = asyncio.run(adapter.respond(msg))

            self.assertIsNone(reply)
            mock_sync_cli.assert_called_once_with(test_uuid)
            mock_sync_ide.assert_called_once_with(test_uuid)

    def test_set_active_conversation_updates_in_memory_settings_and_clean_reply(self):
        settings = AntigravityThreadAdapter.default_settings(cwd=str(self.cwd), conversation="initial-uuid")
        adapter = AntigravityThreadAdapter("Antigravity", settings)

        # Test switching to auto
        ok, text = adapter.set_active_conversation("auto")
        self.assertTrue(ok)
        self.assertEqual(adapter.settings.conversation, "auto")
        self.assertNotIn("<code>", text)

        # Test switching to a specific conversation
        with patch.object(adapter, "_lookup_conversation", return_value=("target-uuid", "My Target Convo")), \
             patch.object(adapter, "sync_conversation_to_cli"):
            ok, text = adapter.set_active_conversation("target-uuid")
            self.assertTrue(ok)
            self.assertEqual(adapter.settings.conversation, "target-uuid")
            self.assertNotIn("<code>", text)
            self.assertIn("target-uuid", text)

    def test_lookup_conversation_finds_uuid_in_secondary_db(self):
        db1 = self.cwd / "db1.db"
        db2 = self.cwd / "db2.db"
        for p in (db1, db2):
            conn = sqlite3.connect(p)
            conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT)")
            conn.commit()
            conn.close()

        target_uuid = "12345678-1234-5678-1234-567812345678"
        conn2 = sqlite3.connect(db2)
        conn2.execute("INSERT INTO conversation_summaries VALUES (?, ?)", (target_uuid, "Secondary DB Title"))
        conn2.commit()
        conn2.close()

        settings = AntigravityAdapter.default_settings(cwd=str(self.cwd))
        adapter = AntigravityAdapter("Antigravity", settings)
        with patch.object(adapter, "_get_summary_dbs", return_value=[db1, db2]):
            res = adapter._lookup_conversation(target_uuid)
            self.assertEqual(res, (target_uuid, "Secondary DB Title"))

    def test_agentapi_title_sanitizes_author(self):
        test_uuid = "12345678-1234-5678-1234-567812345678"
        settings = AntigravityThreadAdapter.default_settings(cwd=str(self.cwd), conversation=test_uuid)
        adapter = AntigravityThreadAdapter("Antigravity", settings)

        completed = subprocess.CompletedProcess(
            args=["agentapi", "send-message"], returncode=0, stdout='{"ok": true}', stderr=""
        )

        # No endpoint discovery: on Windows it runs tasklist and PowerShell through the same
        # subprocess.run this test counts.
        with patch("swarmly.telegram.adapters.antigravity.resolve_agentapi", return_value="/bin/agentapi"), \
             patch("swarmly.telegram.endpoint.resolve_endpoint", return_value=None), \
             patch("swarmly.telegram.adapters.antigravity.subprocess.run", return_value=completed) as mock_run:
            msg = IncomingMessage(text="hello", author_alias="Hermes\nEvil", author_username="@example_hermes_bot")
            asyncio.run(adapter.respond(msg))

            mock_run.assert_called_once()
            args = mock_run.call_args.args[0]
            # Should not have double @ and no newlines
            self.assertEqual(args[2], "--title=Telegram: @example_hermes_bot")

    def test_resolve_conversation_custom_session_passthrough(self):
        settings = AntigravityAdapter.default_settings(cwd=str(self.cwd), conversation="custom-session-slug")
        adapter = AntigravityAdapter("Antigravity", settings)
        with patch.object(adapter, "_get_summary_dbs", return_value=[]):
            res = adapter.resolve_conversation()
            self.assertEqual(res, ("custom-session-slug", "custom-session-slug"))

    def test_non_admin_switch_context_returns_unauthorized(self):
        settings = AntigravityThreadAdapter.default_settings(cwd=str(self.cwd), conversation="auto")
        adapter = AntigravityThreadAdapter("Antigravity", settings)

        msg = IncomingMessage(
            text="switch context to other-session-uuid",
            author_alias="RegularUser",
            is_admin=False
        )
        reply = asyncio.run(adapter.respond(msg))
        self.assertIsNotNone(reply)
        self.assertFalse(reply.ok)
        self.assertIn("Unauthorized", reply.text)
        self.assertIn("Unauthorized", reply.error)

    def test_antigravity_adapter_non_admin_switch_context_returns_unauthorized(self):
        settings = AntigravityAdapter.default_settings(cwd=str(self.cwd))
        adapter = AntigravityAdapter("Antigravity", settings)

        msg = IncomingMessage(
            text="switch context to other-session-uuid",
            author_alias="RegularUser",
            is_admin=False
        )
        reply = asyncio.run(adapter.respond(msg))
        self.assertIsNotNone(reply)
        self.assertFalse(reply.ok)
        self.assertIn("Unauthorized", reply.text)
        self.assertIn("Unauthorized", reply.error)

    def test_switch_context_not_found_populates_error(self):
        settings = AntigravityThreadAdapter.default_settings(cwd=str(self.cwd), conversation="auto")
        adapter = AntigravityThreadAdapter("Antigravity", settings)

        with patch.object(adapter, "_lookup_conversation", return_value=None):
            msg = IncomingMessage(
                text="switch context to nonexistent-uuid",
                author_alias="Admin",
                is_admin=True
            )
            reply = asyncio.run(adapter.respond(msg))
            self.assertIsNotNone(reply)
            self.assertFalse(reply.ok)
            self.assertIn("not found", reply.error)

    def test_build_fallback_argv_omits_conversation_when_auto(self):
        settings = AntigravityThreadAdapter.default_settings(cwd=str(self.cwd), conversation="auto")
        adapter = AntigravityThreadAdapter("Antigravity", settings)

        argv = adapter.build_fallback_argv("my prompt", "auto")
        self.assertNotIn("--conversation", argv)
        self.assertNotIn("auto", argv)

    def test_cli_check_displays_title_for_antigravity_thread(self):
        import io
        from contextlib import redirect_stdout
        from swarmly.telegram.cli import _check
        from swarmly.telegram.config import AgentConfig, AdapterSettings, AgentIdentity

        config = AgentConfig(
            identity=AgentIdentity(alias="Antigravity", name="example_agy_bot", bot_token="fake:token", home_channel_id=-100),
            adapter=AdapterSettings(type="antigravity_thread", conversation="auto")
        )
        f = io.StringIO()
        with redirect_stdout(f):
            asyncio.run(_check(config))
        out = f.getvalue()
        self.assertIn("agentapi send-message --title='Telegram: @author' auto {prompt}", out)


if __name__ == "__main__":
    unittest.main()


# Issue #106: ``AgentConfig.from_dict`` merges the developer's real ``.env`` into the
# process-global ``os.environ``; the guard keeps this module's tests from leaking it.
from tests import envguard  # noqa: E402 - last on purpose: the wrapper stays two lines


def load_tests(loader, tests, pattern):
    return envguard.guard(loader, tests, pattern)


