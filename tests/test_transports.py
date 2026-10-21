"""
Tests for the durable-plane transports (swarmly.transports): git, folder and gdrive.

The Google Drive transport is exercised against an in-memory fake of the Drive v3
service, so these tests need neither the Google client libraries nor network access.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional

from tests.gitenv import git_env
from swarmly.bus import DurableBus
from swarmly.crypto import CryptoAuth, KeyPair
from swarmly.models import A2AMessage, MessageIntent, MessagePriority, RecipientInfo, SenderInfo
from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.channel import AgentChannel
from swarmly.telegram.config import AgentConfig, ConfigError
from swarmly.telegram.poll import bus_poll_once
from swarmly.transports import (
    FolderTransport,
    GitTransport,
    TransportError,
    build_transport,
    is_envelope_name,
)
from swarmly.transports.gdrive import GoogleDriveTransport

GROUP = -1001234567890
ADMIN_ID = 1234567890


def make_message(message_id: str, sender: str = "codex", recipient: str = "hermes",
                 text: str = "hello") -> A2AMessage:
    return A2AMessage(
        protocol="a2a/v1.0", message_id=message_id, timestamp_utc="2026-10-07T00:00:00+00:00",
        sender=SenderInfo(agent_id=sender, model_family="custom", framework="custom"),
        recipient=RecipientInfo(agent_id=recipient),
        intent=MessageIntent.QUERY_REQUEST, priority=MessagePriority.ROUTINE, signature="",
        payload={"text": text})


def make_config(bus: Dict[str, Any]) -> AgentConfig:
    return AgentConfig.from_dict({
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                  "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
        "peers": {"Codex": "CodexBot"},
        "adapter": {"type": "echo"},
        "limits": {"min_seconds_between_messages": 0, "min_seconds_between_agent_turns": 0},
        "bus": bus,
    })


# --------------------------------------------------------------------------------------
# A fake Drive v3 service: files().list / get_media / create, each returning .execute()
# --------------------------------------------------------------------------------------
class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeDrive:
    def __init__(self, page_size: int = 1000) -> None:
        self.files_by_id: Dict[str, Dict[str, Any]] = {}
        self.page_size = page_size
        self.list_calls: List[Dict[str, Any]] = []
        self.downloads: List[str] = []
        self.fail_list = False
        self._next = 0

    def add(self, name: str, content: str, parent: str = "FOLDER1", mime: str = "text/markdown"):
        self._next += 1
        file_id = f"id{self._next:04d}"
        self.files_by_id[file_id] = {"id": file_id, "name": name, "parent": parent,
                                     "content": content, "mimeType": mime}
        return file_id

    def files(self):
        return self

    def list(self, q: str, pageSize: int, pageToken: Optional[str], orderBy: str, fields: str,
             supportsAllDrives: bool, includeItemsFromAllDrives: bool):
        self.list_calls.append({"q": q, "pageToken": pageToken})

        def run():
            if self.fail_list:
                raise OSError("network down")
            parent = q.split("'")[1]
            matching = [f for f in sorted(self.files_by_id.values(), key=lambda f: f["name"])
                        if f["parent"] == parent and f["mimeType"] != "application/vnd.google-apps.folder"]
            start = int(pageToken or 0)
            page = matching[start:start + self.page_size]
            response: Dict[str, Any] = {"files": [{"id": f["id"], "name": f["name"]} for f in page]}
            if start + self.page_size < len(matching):
                response["nextPageToken"] = str(start + self.page_size)
            return response
        return _Call(run)

    def get_media(self, fileId: str, supportsAllDrives: bool):
        def run():
            self.downloads.append(fileId)
            return self.files_by_id[fileId]["content"].encode("utf-8")
        return _Call(run)

    def create(self, body: Dict[str, Any], media_body: Any, fields: str, supportsAllDrives: bool):
        def run():
            file_id = self.add(body["name"], media_body.decode("utf-8"), parent=body["parents"][0])
            return {"id": file_id}
        return _Call(run)


def fake_gdrive(drive: FakeDrive, cache_dir: Optional[Path] = None) -> GoogleDriveTransport:
    return GoogleDriveTransport("FOLDER1", service=drive, cache_dir=cache_dir,
                                media_factory=lambda raw: raw)


# --------------------------------------------------------------------------------------
class TestEnvelopeNames(unittest.TestCase):
    def test_only_markdown_non_hidden_files_are_envelopes(self):
        self.assertTrue(is_envelope_name("msg-1_codex_status_update.md"))
        self.assertTrue(is_envelope_name(".agents/inbox/msg-1_codex_status_update.md"))
        self.assertFalse(is_envelope_name(".tmp-abc.part"))
        self.assertFalse(is_envelope_name(".hidden.md"))
        self.assertFalse(is_envelope_name("~lock.md"))
        self.assertFalse(is_envelope_name("desktop.ini"))


class TestFolderTransport(unittest.TestCase):
    def test_post_then_list_round_trip(self):
        with TemporaryDirectory() as tmp:
            transport = FolderTransport(Path(tmp))
            location = transport.post("m1_codex_query_request.md", "body one")
            self.assertEqual(Path(location), Path(tmp) / "inbox" / "m1_codex_query_request.md")
            items = transport.list_messages()
            self.assertEqual([(Path(n).name, t) for n, t in items],
                             [("m1_codex_query_request.md", "body one")])

    def test_post_leaves_no_temp_file_behind(self):
        with TemporaryDirectory() as tmp:
            transport = FolderTransport(Path(tmp))
            transport.post("m1_codex_query_request.md", "x")
            self.assertEqual(sorted(p.name for p in (Path(tmp) / "inbox").iterdir()),
                             ["m1_codex_query_request.md"])

    def test_post_never_replaces_an_existing_envelope(self):
        # P0-7 review m-2: a second message under the same name used to erase the first.
        with TemporaryDirectory() as tmp:
            transport = FolderTransport(Path(tmp))
            path = Path(transport.post("m1_codex_query_request.md", "first"))
            transport.post("m1_codex_query_request.md", "first")      # same content: no-op
            with self.assertRaises(TransportError):
                transport.post("m1_codex_query_request.md", "second")
            self.assertEqual(path.read_text(encoding="utf-8"), "first")
            self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_agents_a_b_and_a_underscore_b_get_different_files(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "config").mkdir()
            (Path(tmp) / "config" / "a2a_config.json").write_text("{}", encoding="utf-8")
            bus = DurableBus(root_dir=Path(tmp),
                             transport=FolderTransport(Path(tmp) / "bus", create=True))
            first = A2AMessage.create(sender_id="a-b", intent="STATUS_UPDATE",
                                      payload={"text": "one"}, recipient_id="*")
            second = A2AMessage.create(sender_id="a_b", intent="STATUS_UPDATE",
                                       payload={"text": "two"}, recipient_id="*")
            second.message_id = first.message_id
            paths = {bus.post_message(m, private_key="k" * 64) for m in (first, second)}
            self.assertEqual(len(paths), 2)

    def test_list_skips_temp_and_hidden_files(self):
        with TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "inbox"
            inbox.mkdir()
            (inbox / ".tmp-123.part").write_text("partial", encoding="utf-8")
            (inbox / ".sync-state.md").write_text("client file", encoding="utf-8")
            (inbox / "desktop.ini").write_text("[x]", encoding="utf-8")
            (inbox / "m2_codex_query_request.md").write_text("real", encoding="utf-8")
            names = [Path(n).name for n, _ in FolderTransport(Path(tmp)).list_messages()]
            self.assertEqual(names, ["m2_codex_query_request.md"])

    def test_missing_folder_lists_nothing(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(FolderTransport(Path(tmp) / "not-mounted").list_messages(), [])

    def test_post_refuses_an_unmounted_folder(self):
        """A write to a folder that is not there must fail, not silently create a
        local directory that no sync client will ever upload."""
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "GoogleDrive" / "a2a-bus"
            with self.assertRaises(TransportError):
                FolderTransport(missing).post("m1_codex_query_request.md", "x")
            self.assertFalse(missing.exists())

    def test_post_rejects_non_envelope_names(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(TransportError):
                FolderTransport(Path(tmp)).post("../escape.md.txt", "x")


class TestGitTransport(unittest.TestCase):
    def test_bus_ref_publish_does_not_commit_to_feature_branch(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            remote = Path(tmp) / "origin.git"
            root.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True,
                                      capture_output=True, text=True, env=git_env())
            subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                           capture_output=True)
            git("init", "-b", "main")
            git("config", "user.name", "Codex")
            git("config", "user.email", "codex@a2a.local")
            (root / "README").write_text("base")
            git("add", "README")
            git("commit", "-m", "base")
            git("remote", "add", "origin", str(remote))
            git("push", "-u", "origin", "main")
            git("switch", "-c", "feature")
            before = git("rev-parse", "HEAD").stdout.strip()
            state = ".agents/wakes/codex/timer-state.json"
            (root / state).parent.mkdir(parents=True)
            (root / state).write_text('{"state":"armed"}')

            GitTransport(root, ref="origin/main").commit_and_push("wake state", paths=[state])

            self.assertEqual(git("rev-parse", "HEAD").stdout.strip(), before)
            self.assertEqual(subprocess.run(["git", "--git-dir", str(remote), "show",
                                             f"main:{state}"], check=True, capture_output=True,
                                            text=True).stdout, '{"state":"armed"}')

    def test_local_listing_without_ref(self):
        with TemporaryDirectory() as tmp:
            transport = GitTransport(Path(tmp))
            transport.post("m1_codex_query_request.md", "body", publish=False)
            items = transport.list_messages()
            self.assertEqual([(Path(n).name, t) for n, t in items],
                             [("m1_codex_query_request.md", "body")])

    def test_unavailable_ref_falls_back_to_the_working_tree(self):
        with TemporaryDirectory() as tmp:  # not a git repository at all
            transport = GitTransport(Path(tmp), ref="origin/main")
            transport.post("m1_codex_query_request.md", "body", publish=False)
            self.assertEqual(len(transport.list_messages()), 1)


class TestGoogleDriveTransport(unittest.TestCase):
    def test_rejects_a_folder_id_that_would_break_the_query(self):
        for bad in ("", "abc' or trashed=true or '", "https://drive.google.com/x"):
            with self.assertRaises(TransportError):
                GoogleDriveTransport(bad, service=FakeDrive())

    def test_lists_only_envelopes_in_the_folder(self):
        drive = FakeDrive()
        drive.add("m2_codex_query_request.md", "two")
        drive.add("m1_codex_query_request.md", "one")
        drive.add("notes.txt", "not an envelope")
        drive.add("sub", "", mime="application/vnd.google-apps.folder")
        drive.add("m3_codex_query_request.md", "elsewhere", parent="OTHER")
        items = fake_gdrive(drive).list_messages()
        self.assertEqual([t for _, t in items], ["one", "two"])
        self.assertTrue(all(n.startswith("gdrive:FOLDER1/") for n, _ in items))

    def test_follows_pagination(self):
        drive = FakeDrive(page_size=2)
        for i in range(5):
            drive.add(f"m{i}_codex_query_request.md", f"body {i}")
        items = fake_gdrive(drive).list_messages()
        self.assertEqual(len(items), 5)
        self.assertEqual(len(drive.list_calls), 3)

    def test_downloads_each_file_once(self):
        drive = FakeDrive()
        drive.add("m1_codex_query_request.md", "one")
        transport = fake_gdrive(drive)
        transport.list_messages()
        drive.add("m2_codex_query_request.md", "two")
        transport.list_messages()
        self.assertEqual(sorted(drive.downloads), ["id0001", "id0002"])

    def test_disk_cache_survives_a_new_process(self):
        """``bus-poll --once`` is a new process each cycle: the cache must be on disk."""
        drive = FakeDrive()
        drive.add("m1_codex_query_request.md", "one")
        with TemporaryDirectory() as tmp:
            fake_gdrive(drive, cache_dir=Path(tmp)).list_messages()
            fake_gdrive(drive, cache_dir=Path(tmp)).list_messages()
        self.assertEqual(drive.downloads, ["id0001"])

    def test_listing_failure_is_an_empty_cycle(self):
        drive = FakeDrive()
        drive.add("m1_codex_query_request.md", "one")
        drive.fail_list = True
        self.assertEqual(fake_gdrive(drive).list_messages(), [])

    def test_post_uploads_into_the_folder(self):
        drive = FakeDrive()
        transport = fake_gdrive(drive)
        location = transport.post("m9_codex_query_request.md", "posted")
        self.assertEqual(location, "gdrive:FOLDER1/m9_codex_query_request.md")
        stored = list(drive.files_by_id.values())
        self.assertEqual(stored[0]["parent"], "FOLDER1")
        self.assertEqual(stored[0]["content"], "posted")
        # A node reading its own post does not download it again.
        transport.list_messages()
        self.assertEqual(drive.downloads, [])

    def test_post_failure_raises_transport_error(self):
        class Broken(FakeDrive):
            def create(self, **kwargs):
                return _Call(lambda: (_ for _ in ()).throw(OSError("quota")))
        with self.assertRaises(TransportError):
            fake_gdrive(Broken()).post("m1_codex_query_request.md", "x")


class TestBuildTransport(unittest.TestCase):
    def test_kinds(self):
        with TemporaryDirectory() as tmp:
            self.assertIsInstance(build_transport(None, repo=Path(tmp)), GitTransport)
            self.assertIsInstance(build_transport("git", repo=Path(tmp), ref="origin/main"),
                                  GitTransport)
            self.assertIsInstance(build_transport("folder", repo=Path(tmp), folder=tmp),
                                  FolderTransport)
            gdrive = build_transport("gdrive", repo=Path(tmp), gdrive={"folder_id": "F1"},
                                     state_dir=Path(tmp))
            self.assertIsInstance(gdrive, GoogleDriveTransport)
            self.assertEqual(gdrive.cache_dir, Path(tmp) / "gdrive-cache-F1")

    def test_errors(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(TransportError):
                build_transport("folder", repo=Path(tmp))
            with self.assertRaises(TransportError):
                build_transport("gdrive", repo=Path(tmp))
            with self.assertRaises(TransportError):
                build_transport("s3", repo=Path(tmp))


class TestDurableBusOverFolder(unittest.TestCase):
    def test_signed_round_trip_verifies(self):
        with TemporaryDirectory() as bus_dir, TemporaryDirectory() as root:
            keys = KeyPair.generate_hmac("codex")
            os.environ["A2A_KEY_CODEX"] = keys.private_key
            (Path(root) / "config").mkdir()
            (Path(root) / "config" / "a2a_config.json").write_text(json.dumps(
                {"authorized_agents": {"codex": {"public_key_or_secret_name": "A2A_KEY_CODEX"}}}),
                encoding="utf-8")
            try:
                bus = DurableBus(root_dir=Path(root), transport=FolderTransport(Path(bus_dir)))
                message = make_message("msg-20261007T000000Z-m1m1")
                message.timestamp_utc = datetime.now(timezone.utc).isoformat()  # signed window
                bus.post_message(message, private_key=keys.private_key)
                results = bus.read_inbox()
            finally:
                del os.environ["A2A_KEY_CODEX"]
            self.assertEqual(len(results), 1)
            message, valid, status = results[0]
            self.assertEqual(message.message_id, "msg-20261007T000000Z-m1m1")
            self.assertTrue(valid, status)
            # Nothing was written into the local checkout.
            self.assertFalse((Path(root) / ".agents").exists())


class TestBusTransportConfig(unittest.TestCase):
    def test_default_is_git(self):
        cfg = make_config({"poll_minutes": 10})
        self.assertEqual(cfg.bus.transport, "git")
        self.assertEqual(cfg.validate(), [])

    def test_folder_transport_is_parsed_and_validated(self):
        with TemporaryDirectory() as tmp:
            cfg = make_config({"poll_minutes": 10, "transport": "folder", "folder": tmp})
            self.assertEqual(cfg.bus.transport, "folder")
            self.assertEqual(cfg.bus.folder, tmp)
            self.assertEqual(cfg.validate(), [])
            missing = make_config({"transport": "folder", "folder": str(Path(tmp) / "nope")})
            self.assertTrue(any("does not exist" in p for p in missing.validate()))
        self.assertTrue(any("bus.folder is required" in p
                            for p in make_config({"transport": "folder"}).validate()))

    def test_gdrive_needs_a_folder_id(self):
        problems = make_config({"transport": "gdrive"}).validate()
        self.assertTrue(any("folder_id" in p for p in problems))
        ok = make_config({"transport": "gdrive", "gdrive": {"folder_id": "F1"}})
        self.assertEqual(ok.bus.gdrive["folder_id"], "F1")
        self.assertEqual(ok.validate(), [])

    def test_unknown_transport_and_bad_gdrive_shape(self):
        self.assertTrue(any("bus.transport" in p
                            for p in make_config({"transport": "s3"}).validate()))
        with self.assertRaises(ConfigError):
            make_config({"transport": "gdrive", "gdrive": "F1"})


@patch("swarmly.telegram.poll._needs_baseline", new=lambda *_args: False)   # later cycles
class TestPollerOverFolderBus(unittest.TestCase):
    """End to end: a message dropped into a synced folder reaches the adapter once."""

    @patch.dict(os.environ, {"A2A_KEY_CODEX": "k-codex"})
    def test_folder_bus_delivers_once(self):
        with TemporaryDirectory() as bus_dir, TemporaryDirectory() as state:
            # Only a signature that verifies against the registered key is delivered (P0-1).
            (Path(state) / "config").mkdir()
            (Path(state) / "config" / "a2a_config.json").write_text(json.dumps(
                {"authorized_agents": {"codex": {"public_key_or_secret_name": "A2A_KEY_CODEX"}}}),
                encoding="utf-8")
            message = make_message("m1", text="from the cloud")
            message.timestamp_utc = datetime.now(timezone.utc).isoformat()   # signed window
            message.signature = CryptoAuth.sign_envelope(message.signed_fields(), "k-codex", "hmac")
            FolderTransport(Path(bus_dir)).post("m1_codex_query_request.md",
                                                message.to_yaml_frontmatter())
            cfg = make_config({"poll_minutes": 10, "transport": "folder", "folder": bus_dir,
                               "repo": state})
            sent: List[str] = []

            async def sender(payload, reply_to_message_id=None):
                sent.append(payload)

            channel = AgentChannel(cfg, adapter=EchoAdapter("Hermes"), sender=sender)
            first = asyncio.run(bus_poll_once(channel))
            second = asyncio.run(bus_poll_once(channel))
            self.assertEqual(first.delivered, ["m1"])
            self.assertEqual(second.delivered, [])
            # Local state stays in the local checkout, never in the synced folder.
            self.assertTrue((Path(state) / ".agents" / "state").is_dir())
            self.assertEqual(sorted(p.name for p in Path(bus_dir).iterdir()), ["inbox"])


class TestDurableThenInstant(unittest.TestCase):
    """``swarmly-telegram post``: durable write first, then a Telegram pointer to it."""

    def setUp(self):
        # Envelopes are signed with A2A_KEY_<ALIAS>; nothing is posted without one.
        previous = os.environ.get("A2A_KEY_HERMES")
        os.environ["A2A_KEY_HERMES"] = "test-signing-key"
        self.addCleanup(lambda: os.environ.__setitem__("A2A_KEY_HERMES", previous)
                        if previous is not None else os.environ.pop("A2A_KEY_HERMES", None))

    def test_refuses_to_post_unsigned(self):
        from swarmly.bus import MissingKeyError
        from swarmly.telegram.relay import post_and_notify
        os.environ.pop("A2A_KEY_HERMES", None)

        async def sender(payload, reply_to_message_id=None):
            raise AssertionError("nothing may reach Telegram")

        with TemporaryDirectory() as bus_dir, TemporaryDirectory() as state:
            channel = self._channel(bus_dir, state, sender)
            with self.assertRaises(MissingKeyError):
                asyncio.run(post_and_notify(channel, to="Codex", text="hi"))
            self.assertFalse((Path(bus_dir) / "inbox").exists()
                             and any((Path(bus_dir) / "inbox").iterdir()))

    def _channel(self, bus_dir: str, state: str, sender):
        cfg = make_config({"poll_minutes": 10, "transport": "folder", "folder": bus_dir,
                           "repo": state})
        return AgentChannel(cfg, adapter=EchoAdapter("Hermes"), sender=sender)

    def test_writes_durably_then_points_telegram_at_it(self):
        from swarmly.telegram.relay import post_and_notify
        from swarmly.telegram.wire import parse_message
        sent: List[str] = []

        async def sender(payload, reply_to_message_id=None):
            sent.append(payload)

        with TemporaryDirectory() as bus_dir, TemporaryDirectory() as state:
            channel = self._channel(bus_dir, state, sender)
            result = asyncio.run(post_and_notify(channel, to="Codex", text="please review PR #7",
                                                 intent="PR_REVIEW_REQUEST"))
            files = list((Path(bus_dir) / "inbox").iterdir())
            self.assertEqual(len(files), 1)
            self.assertIn(result.message_id, files[0].name)
            self.assertEqual(result.bus_pointer, f"folder:{files[0].name}")
            self.assertEqual(result.telegram, "sent")
            self.assertEqual(len(sent), 1)
            envelope, body = parse_message(sent[0])
            self.assertEqual((envelope.type, envelope.to, envelope.intent),
                             ("REQUEST", "Codex", "PR_REVIEW_REQUEST"))
            self.assertEqual(envelope.bus, result.bus_pointer)
            self.assertIn("@CodexBot", body)
            # The peer's poller reads the same envelope from the durable copy.
            bus_items = FolderTransport(Path(bus_dir)).list_messages()
            parsed = A2AMessage.from_yaml_frontmatter(bus_items[0][1])
            self.assertEqual(parsed.recipient.agent_id, "codex")
            self.assertEqual(parsed.payload["text"], "please review PR #7")

    def test_a_mirror_carries_its_telegram_reference_signed(self):
        from swarmly.telegram.relay import post_and_notify
        with TemporaryDirectory() as bus_dir, TemporaryDirectory() as state:
            channel = self._channel(bus_dir, state, None)
            ref = {"chat": -100, "thread": 7, "message_id": 55}
            asyncio.run(post_and_notify(channel, to="Codex", text="@CodexBot QA #8",
                                        intent="TASK_HANDOFF", notify=False,
                                        extra={"telegram": ref}))
            raw = FolderTransport(Path(bus_dir)).list_messages()[0][1]
            parsed = A2AMessage.from_yaml_frontmatter(raw)
            self.assertEqual(parsed.payload["telegram"], ref)
            self.assertTrue(parsed.signature)                 # signed like any envelope

    def test_telegram_failure_keeps_the_durable_copy(self):
        from swarmly.telegram.relay import post_and_notify

        async def sender(payload, reply_to_message_id=None):
            raise RuntimeError("telegram unavailable")

        with TemporaryDirectory() as bus_dir, TemporaryDirectory() as state:
            channel = self._channel(bus_dir, state, sender)
            result = asyncio.run(post_and_notify(channel, to="Codex", text="x"))
            self.assertEqual(result.telegram, "failed")
            self.assertEqual(len(list((Path(bus_dir) / "inbox").iterdir())), 1)

    def test_broadcast_without_telegram(self):
        from swarmly.telegram.relay import post_and_notify
        sent: List[str] = []

        async def sender(payload, reply_to_message_id=None):
            sent.append(payload)

        with TemporaryDirectory() as bus_dir, TemporaryDirectory() as state:
            channel = self._channel(bus_dir, state, sender)
            result = asyncio.run(post_and_notify(channel, to="*", text="upgrade available",
                                                 notify=False))
            self.assertEqual(result.telegram, "skipped")
            self.assertEqual(sent, [])

    def test_gitbus_pointer_names_path_and_commit(self):
        import subprocess
        from swarmly.telegram.relay import bus_pointer
        with TemporaryDirectory() as tmp:
            for cmd in (["git", "init", "-q", "."], ["git", "config", "user.email", "t@x"],
                        ["git", "config", "user.name", "t"],
                        ["git", "commit", "-q", "--allow-empty", "-m", "init"]):
                subprocess.run(cmd, cwd=tmp, check=True, capture_output=True, env=git_env())
            sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=tmp, check=True,
                                 capture_output=True, text=True,
                                 env=git_env()).stdout.strip()
            pointer = bus_pointer(GitTransport(Path(tmp)), "ignored", "m1_codex_x.md")
            self.assertEqual(pointer, f".agents/inbox/m1_codex_x.md@{sha}")


if __name__ == "__main__":
    unittest.main()
