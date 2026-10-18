"""Issue #22: the maintainer's handle, chat ids and absolute paths stay off the bus.

The rescoped #1: bot @usernames may stay visible (public on Telegram already), but a chat id
must not leave the host, dedupe must keep working, and a peer that cannot resolve a token
runs the mirrored turn anyway -- processed twice, never lost.
"""

import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from swarmly.telegram.config import AgentConfig, ConfigError
from swarmly.telegram.redact import (CHAT, PATH, Redactor, SECRET_ENV, SECRET_FILE_ENV,
                                     hmac_token, local_secret, secret_file,
                                     resolve_topic_token, topic_token)

GROUP = -1001234567890
ADMIN_ID = 987654321
THREAD = 12


class SecretSandbox(unittest.TestCase):
    """Keep the generated local secret out of the runner's home directory."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.secret_path = Path(tmp.name) / "redaction.secret"
        patcher = mock.patch.dict(os.environ, {SECRET_FILE_ENV: str(self.secret_path)})
        patcher.start()
        self.addCleanup(patcher.stop)


def config(**overrides):
    data = {
        "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                  "home_channel_id": GROUP, "admin_user_id": ADMIN_ID,
                  "maintainer_username": "@example_admin"},
        "peers": {"Codex": "CodexBot"},
        "adapter": {"type": "echo"},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return AgentConfig.from_dict(data)


def project_config(tmp, **overrides):
    return config(projects={"webapp": {"repo": "example-org/webapp", "cwd": tmp,
                                      "topics": [THREAD]}}, **overrides)


class TestTokens(SecretSandbox):
    """The two token forms, and what a peer can do with each."""

    def test_a_bound_topic_is_an_alias_not_an_id(self):
        token = topic_token("webapp", THREAD)
        self.assertEqual(token, "topic:webapp:12")
        self.assertNotIn(str(GROUP), token)

    def test_the_hmac_fallback_is_stable_and_opaque(self):
        first = hmac_token(GROUP, "shared-secret")
        second = hmac_token(GROUP, "shared-secret")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("topic:hmac-"))
        self.assertNotIn(str(abs(GROUP)), first)
        self.assertNotEqual(first, hmac_token(GROUP, "another-secret"))
        # No configured secret is not "no token": a local secret stands in, so the token is
        # still keyed (review of PR #23 -- an unkeyed digest is guessable).
        local = hmac_token(GROUP, "")
        self.assertTrue(local.startswith("topic:hmac-"), local)
        self.assertNotEqual(local, first)

    def test_the_local_secret_is_generated_once_and_reused(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "redaction.secret"
            with mock.patch.dict(os.environ, {SECRET_ENV: ""}):
                first = local_secret(target)
                second = local_secret(target)
            self.assertEqual(first, second)
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8").strip(), first)
            self.assertGreaterEqual(len(first), 32)
            # A configured secret wins over the stored one.
            with mock.patch.dict(os.environ, {SECRET_ENV: "from-env"}):
                self.assertEqual(local_secret(target), "from-env")

    def test_a_bound_token_resolves_to_the_local_topic(self):
        from swarmly.telegram.projects import ProjectRouter
        cfg = project_config(".")
        router = ProjectRouter(cfg.projects, cfg.identity.home_channel_id)
        self.assertEqual(resolve_topic_token("topic:webapp:12", router), [(GROUP, THREAD)])
        self.assertEqual(resolve_topic_token("topic:webapp:general", router), [])

    def test_the_fallback_and_unknown_projects_resolve_to_nothing(self):
        from swarmly.telegram.projects import ProjectRouter
        cfg = project_config(".")
        router = ProjectRouter(cfg.projects, cfg.identity.home_channel_id)
        # Opaque by design: a peer cannot map it to a chat id, so it runs the mirror too.
        self.assertEqual(resolve_topic_token("topic:hmac-0123456789abcdef", router), [])
        self.assertEqual(resolve_topic_token("topic:someone-else:12", router), [])
        self.assertEqual(resolve_topic_token("", router), [])
        self.assertEqual(resolve_topic_token(None, router), [])


class TestRedactor(SecretSandbox):

    def test_a_hand_off_reference_is_a_token(self):
        with TemporaryDirectory() as tmp:
            cfg = project_config(tmp)
            channel = self._channel(cfg)
            ref = Redactor.from_config(cfg).topic_ref(GROUP, THREAD, channel.router)
        self.assertEqual(ref, {"topic": f"topic:webapp:{THREAD}"})
        self.assertNotIn(str(GROUP), repr(ref))

    def test_an_unbound_topic_falls_back_to_the_hmac_token(self):
        with TemporaryDirectory() as tmp:
            cfg = project_config(tmp)
            channel = self._channel(cfg)
            with mock.patch.dict(os.environ, {SECRET_ENV: "shared-secret"}):
                redactor = Redactor.from_config(cfg)
                ref = redactor.topic_ref(GROUP, 99, channel.router)     # thread 99: no project
                self.assertTrue(ref["topic"].startswith("topic:hmac-"), ref)
                self.assertEqual(ref["topic"], hmac_token(GROUP, "shared-secret"))
                # Without a configured secret the token is keyed by the local one, and it is
                # still opaque to every peer: they run the mirrored turn too, so a hand-off
                # is processed twice rather than lost.
                os.environ.pop(SECRET_ENV)
                fallback = Redactor.from_config(cfg).topic_ref(GROUP, 99, channel.router)
                self.assertTrue(fallback["topic"].startswith("topic:hmac-"), fallback)
                self.assertEqual(resolve_topic_token(fallback["topic"], channel.router), [])

    def test_chat_ids_user_ids_paths_and_the_handle_are_replaced(self):
        with TemporaryDirectory() as tmp:
            cfg = project_config(tmp, public_redactions={"@example_admin": "<maintainer>"})
            redactor = Redactor.from_config(cfg)
            text = (f"chat {GROUP}, also {abs(GROUP)}; admin {ADMIN_ID}; folder {tmp}; "
                    "ask @example_admin and another -1009876543210 on C:\\Users\\someone\\x")
            out = redactor.text(text)
        self.assertNotIn(str(abs(GROUP)), out)
        self.assertNotIn(str(ADMIN_ID), out)
        self.assertNotIn(str(tmp), out)
        self.assertNotIn("@example_admin", out)
        self.assertNotIn("someone", out)
        self.assertIn(CHAT, out)
        self.assertIn(PATH, out)
        self.assertIn("<maintainer>", out)

    def test_a_payload_stays_json_shaped(self):
        with TemporaryDirectory() as tmp:
            cfg = project_config(tmp)
            payload = Redactor.from_config(cfg).value(
                {"text": f"in {tmp}", "telegram": {"chat": GROUP, "message_id": 5},
                 "ids": [ADMIN_ID, "keep"]})
        self.assertIsInstance(payload, dict)
        self.assertNotIn(str(GROUP), repr(payload))
        self.assertEqual(payload["telegram"]["chat"], CHAT)      # replaced, not deleted
        self.assertEqual(payload["ids"][1], "keep")

    def test_an_unset_map_changes_nothing(self):
        data = {"agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                          "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
                "adapter": {"type": "echo"}}
        cfg = AgentConfig.from_dict(data)
        self.assertEqual(Redactor.from_config(cfg).literals, {})
        text = "a plain sentence with no secret in it"
        self.assertEqual(Redactor.from_config(cfg).text(text), text)

    def test_the_maintainer_handle_is_redacted_without_a_map(self):
        """The one literal the rescoped #1 names explicitly: it needs no configuration."""
        with TemporaryDirectory() as tmp:
            cfg = project_config(tmp)
            redactor = Redactor.from_config(cfg)
        self.assertEqual(redactor.text("ping @example_admin please"),
                         "ping <maintainer> please")
        self.assertNotIn("example_admin", redactor.text("example_admin asked"))

    @staticmethod
    def _channel(cfg):
        from swarmly.telegram.channel import AgentChannel
        from swarmly.telegram.adapters import EchoAdapter
        return AgentChannel(cfg, adapter=EchoAdapter("Hermes"), sender=mock.MagicMock(),
                            gitbus_pull=lambda _root: None, gitbus_read=lambda _root: [])


class TestConfig(unittest.TestCase):

    def test_the_map_is_read(self):
        cfg = config(public_redactions={"@someone": "<maintainer>", "C:\\work": "<path>"})
        self.assertEqual(cfg.public_redactions,
                         {"@someone": "<maintainer>", "C:\\work": "<path>"})

    def test_a_bad_map_is_refused(self):
        with self.assertRaises(ConfigError):
            AgentConfig.from_dict({"agent": {"alias": "Hermes", "bot_token": "1:x",
                                             "home_channel_id": GROUP},
                                   "public_redactions": ["not", "a", "mapping"]})
        with self.assertRaises(ConfigError):
            AgentConfig.from_dict({"agent": {"alias": "Hermes", "bot_token": "1:x",
                                             "home_channel_id": GROUP},
                                   "public_redactions": {"  ": "x"}})

    def test_the_key_is_not_reported_as_unknown(self):
        cfg = config(public_redactions={"@someone": "<maintainer>"})
        self.assertEqual([k for k in cfg.unknown_keys if "public_redactions" in k], [])


class TestMirrorAndDedupe(SecretSandbox):
    """The mirror names a token, and each peer decides from its own config."""

    def _channel(self, tmp):
        from swarmly.telegram.channel import AgentChannel
        from swarmly.telegram.adapters import EchoAdapter
        cfg = project_config(tmp, bus={"repo": tmp, "mirror_handoffs": True})
        channel = AgentChannel(cfg, adapter=EchoAdapter("Hermes"), sender=mock.MagicMock(),
                               gitbus_pull=lambda _root: None, gitbus_read=lambda _root: [])
        channel.bot = object()
        return channel

    def test_the_mirrored_envelope_carries_a_token_and_no_chat_id(self):
        with TemporaryDirectory() as tmp:
            channel = self._channel(tmp)
            posted = []

            async def fake_post(_channel, **kw):
                posted.append(kw)
            with mock.patch("swarmly.telegram.relay.post_and_notify", fake_post):
                async def scenario():
                    channel._mirror_handoffs("@CodexBot please review #22", GROUP, THREAD, 55)
                    await asyncio.gather(*list(channel._mirror_tasks))
                asyncio.run(scenario())
        self.assertEqual(len(posted), 1, posted)
        ref = posted[0]["extra"]["telegram"]
        self.assertEqual(ref["topic"], f"topic:webapp:{THREAD}")
        self.assertNotIn("chat", ref)
        self.assertNotIn(str(abs(GROUP)), repr(ref))
        self.assertEqual(ref["message_id"], 55)

    def test_a_resolvable_token_still_skips_the_copy_it_took(self):
        with TemporaryDirectory() as tmp:
            channel = self._channel(tmp)
            channel._tg_admitted.append((GROUP, 55))
            took = channel.took_telegram_copy({"topic": f"topic:webapp:{THREAD}",
                                              "message_id": 55})
        self.assertTrue(took)

    def test_an_hmac_token_runs_the_turn_again_rather_than_losing_it(self):
        with TemporaryDirectory() as tmp:
            channel = self._channel(tmp)
            channel._tg_admitted.append((GROUP, 55))
            took = channel.took_telegram_copy({"topic": "topic:hmac-0123456789abcdef",
                                              "message_id": 55})
        self.assertFalse(took)          # processed twice, never lost

    def test_a_numeric_id_in_a_structured_payload_is_replaced(self):
        """Finding 3: an id can travel as an int, not only inside a string."""
        with TemporaryDirectory() as tmp:
            cfg = project_config(tmp)
            payload = Redactor.from_config(cfg).value(
                {"admin": ADMIN_ID, "chat": GROUP, "abs_chat": abs(GROUP),
                 "peer": "CodexBot", "message_id": 55})
        self.assertEqual(payload["admin"], "<user>")
        self.assertEqual(payload["chat"], CHAT)
        self.assertEqual(payload["abs_chat"], CHAT)
        self.assertEqual(payload["message_id"], 55)      # not an id: stays a number
        self.assertEqual(payload["peer"], "CodexBot")

    def test_an_integer_dict_key_is_redacted_too(self):
        """Review finding: a chat id can travel as a dict *key*, not only as a value."""
        with TemporaryDirectory() as tmp:
            cfg = project_config(tmp)
            payload = Redactor.from_config(cfg).value(
                {GROUP: "chat", ADMIN_ID: "admin", 55: "message", "plain": "x"})
        self.assertNotIn(GROUP, payload)
        self.assertNotIn(ADMIN_ID, payload)
        self.assertEqual(payload["<chat>"], "chat")
        self.assertEqual(payload["<user>"], "admin")
        self.assertEqual(payload[55], "message")
        self.assertEqual(payload["plain"], "x")

    def test_the_old_chat_form_is_still_honoured(self):
        with TemporaryDirectory() as tmp:
            channel = self._channel(tmp)
            channel._tg_admitted.append((GROUP, 55))
            self.assertTrue(channel.took_telegram_copy({"chat": GROUP, "message_id": 55}))
            self.assertFalse(channel.took_telegram_copy({"chat": GROUP, "message_id": 56}))

    def test_a_malformed_reference_is_not_taken(self):
        with TemporaryDirectory() as tmp:
            channel = self._channel(tmp)
            self.assertFalse(channel.took_telegram_copy({"topic": "topic:webapp:12"}))
            self.assertFalse(channel.took_telegram_copy("nonsense"))

class TestContextIds(SecretSandbox):
    """Finding 1: a context id reaches the bus and the prompt, so it carries no chat id."""

    def test_a_topic_in_another_group_is_tagged_not_named(self):
        from swarmly.telegram.topics import context_for
        with mock.patch.dict(os.environ, {SECRET_ENV: "shared-secret"}):
            ctx = context_for(-1009999999999, THREAD, home_chat_id=GROUP)
            self.assertNotIn("1009999999999", ctx)
            self.assertTrue(ctx.startswith("tg-") and ctx.endswith(f"-{THREAD}"), ctx)
            self.assertEqual(ctx, context_for(-1009999999999, THREAD, home_chat_id=GROUP))
            self.assertNotEqual(ctx, context_for(-1008888888888, THREAD, home_chat_id=GROUP))

    def test_a_non_home_topic_needs_a_shared_secret(self):
        """Review finding: a guessable key (the group id) allows offline confirmation."""
        from swarmly.telegram.topics import chat_tag, context_for
        with mock.patch.dict(os.environ, {SECRET_ENV: ""}):
            # Fail closed: no secret, no context id -- the topic keeps running turn by turn
            # rather than claiming an identity anyone could confirm a chat id against.
            self.assertIsNone(context_for(-1009999999999, THREAD, home_chat_id=GROUP))
            with self.assertRaises(ValueError):
                chat_tag(-1009999999999)
        # With the swarm's own secret every agent names the topic identically.
        with mock.patch.dict(os.environ, {SECRET_ENV: "shared-secret"}):
            ctx = context_for(-1009999999999, THREAD, home_chat_id=GROUP)
            self.assertEqual(ctx, context_for(-1009999999999, THREAD, home_chat_id=GROUP))
            self.assertEqual(ctx, f"tg-{chat_tag(-1009999999999, key='shared-secret')}-{THREAD}")
            self.assertEqual(len(chat_tag(-1009999999999, key="shared-secret")), 16)
            self.assertNotIn("1009999999999", ctx)
            self.assertNotEqual(ctx, context_for(-1008888888888, THREAD, home_chat_id=GROUP))
            import hashlib
            self.assertNotEqual(chat_tag(-1009999999999, key="shared-secret"),
                                hashlib.sha256(f"chat:{-1009999999999}".encode()).hexdigest()[:16])

    def test_the_generated_secret_stays_off_the_runner_home(self):
        # The unset-secret path must not write into ~: the sandbox redirects it.
        with mock.patch.dict(os.environ, {SECRET_ENV: ""}):
            local_secret()
        self.assertTrue(self.secret_path.exists())
        self.assertEqual(secret_file(), self.secret_path)

    def test_the_home_group_keeps_the_short_form(self):
        from swarmly.telegram.topics import context_for
        self.assertEqual(context_for(GROUP, THREAD, home_chat_id=GROUP), f"tg-{THREAD}")


class TestGitHubWrites(SecretSandbox):
    """Finding 2: the GitHub write helpers apply the same redactor."""

    def _client(self, seen):
        from swarmly.github.client import GitHubClient
        with TemporaryDirectory() as tmp:
            cfg = project_config(tmp, public_redactions={"@example_admin": "<maintainer>"})
            redactor = Redactor.from_config(cfg)
            client = GitHubClient(alias="DeepSeek", redact=redactor.text)
            client._run = lambda args, timeout=30.0: (seen.append(args), (0, "", ""))[1]
            return client, str(cfg.identity.home_channel_id)

    def test_a_comment_is_redacted_before_it_is_sent(self):
        seen = []
        client, chat = self._client(seen)
        leaky = "C:" + chr(92) + "Users" + chr(92) + "someone" + chr(92) + "x"
        client.comment("example-org/repo", 22, f"chat {chat}, see {leaky}")
        body = seen[0][seen[0].index("--body") + 1]
        self.assertNotIn(chat, body)
        self.assertNotIn("someone", body)
        self.assertIn("<chat>", body)
        self.assertIn("<path>", body)

    def test_an_issue_title_and_body_are_redacted(self):
        seen = []
        client, chat = self._client(seen)
        client.create_issue("example-org/repo", f"leak {chat}", "@example_admin please look")
        title = seen[0][seen[0].index("--title") + 1]
        body = seen[0][seen[0].index("--body") + 1]
        self.assertNotIn(chat, title)
        self.assertNotIn("@example_admin", body)
        self.assertIn("<maintainer>", body)

    def test_the_watcher_builds_a_redacting_client(self):
        from swarmly.github.watch import GitHubWatcher
        with TemporaryDirectory() as tmp:
            cfg = project_config(tmp)
            watcher = GitHubWatcher(cfg, client=None, state_path=Path(tmp) / "s.json")
        self.assertEqual(watcher.client.redact(f"chat {GROUP}"), "chat <chat>")
        self.assertEqual(watcher.client.redact("plain text"), "plain text")


class TestRelayEndToEnd(SecretSandbox):
    """Finding 4: prove the redaction on the envelope that actually lands on the bus."""

    def setUp(self):
        super().setUp()
        previous = os.environ.get("A2A_KEY_HERMES")
        os.environ["A2A_KEY_HERMES"] = "test-signing-key"
        self.addCleanup(lambda: os.environ.__setitem__("A2A_KEY_HERMES", previous)
                        if previous is not None else os.environ.pop("A2A_KEY_HERMES", None))

    def test_the_stored_envelope_carries_no_handle_id_or_path(self):
        from swarmly.bus import A2AMessage
        from swarmly.telegram.adapters import EchoAdapter
        from swarmly.telegram.channel import AgentChannel
        from swarmly.telegram.relay import post_and_notify
        from swarmly.transports.folder import FolderTransport

        async def sender(payload, reply_to_message_id=None):
            return None

        with TemporaryDirectory() as bus_dir, TemporaryDirectory() as state,                 TemporaryDirectory() as work:
            cfg = config(bus={"transport": "folder", "folder": bus_dir, "repo": state,
                              "mirror_handoffs": True},
                         projects={"webapp": {"repo": "example-org/webapp", "cwd": work,
                                              "topics": [THREAD]}},
                         public_redactions={"@example_admin": "<maintainer>"})
            channel = AgentChannel(cfg, adapter=EchoAdapter("Hermes"), sender=sender)
            asyncio.run(post_and_notify(
                channel, to="Codex", intent="TASK_HANDOFF", notify=False,
                text=f"@example_admin see {work} on chat {GROUP}, admin {ADMIN_ID}",
                extra={"telegram": {"chat": GROUP, "message_id": 7}}))
            raw = FolderTransport(Path(bus_dir)).list_messages()[0][1]
        message = A2AMessage.from_yaml_frontmatter(raw)
        payload = message.payload
        self.assertIn("<maintainer>", payload["text"])
        for secret in (str(GROUP), str(abs(GROUP)), str(ADMIN_ID), work):
            self.assertNotIn(secret, raw)
        self.assertEqual(payload["telegram"]["chat"], "<chat>")   # replaced, not deleted
        self.assertEqual(payload["telegram"]["message_id"], 7)
        self.assertTrue(message.signature)                        # still a signed envelope


class TestSecretIsolation(SecretSandbox):
    """A runner-exported secret file must never be trusted (review of PR #23)."""

    def test_the_suite_overrides_a_runner_secret_file(self):
        import subprocess
        import sys
        repo = Path(__file__).resolve().parents[1]
        host_secret = Path(self.secret_path).parent / "host.redaction.secret"
        env = dict(os.environ)
        env["SWARMLY_REDACTION_SECRET_FILE"] = str(host_secret)
        env["PYTHONPATH"] = str(repo)
        env["A2A_LOG_DIR"] = "off"
        out = subprocess.run(
            [sys.executable, "-c",
             "import tests, swarmly.telegram.redact as r; print(r.secret_file())"],
            capture_output=True, text=True, env=env, cwd=str(repo))
        self.assertEqual(out.returncode, 0, out.stderr)
        chosen = out.stdout.strip()
        self.assertNotEqual(chosen, str(host_secret))
        self.assertIn("swarmly-redaction-", chosen)
        self.assertFalse(host_secret.exists())


class TestRefusedTopicTurns(SecretSandbox):
    """A turn in a non-home topic is refused without the secret, not run as General."""

    def _config(self, tmp, chat):
        # ``topics:`` takes "<chat>/<thread>" for another group, a bare thread for home.
        topic = [str(THREAD)] if chat == GROUP else [f"{chat}/{THREAD}"]
        return AgentConfig.from_dict({
            "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                      "home_channel_id": GROUP, "admin_user_id": ADMIN_ID},
            "adapter": {"type": "echo", "cwd": tmp},
            "projects": {"webapp": {"repo": "example-org/webapp", "cwd": tmp,
                                    "topics": topic}},
            "github": {"login": "hermes-bot", "poll_minutes": 5},
        })

    def _poll(self, tmp, chat):
        from swarmly.github.client import GitHubClient, Notification
        from swarmly.github.watch import GitHubWatcher
        client = mock.MagicMock(spec=GitHubClient)
        client.notifications.return_value = [Notification(
            id="n1", reason="mention", repo="example-org/webapp", subject_title="t",
            subject_type="Issue",
            subject_url="https://api.github.com/repos/example-org/webapp/issues/1",
            number=1, updated_at="2026-10-10T12:00:00Z")]
        client.issue_view.return_value = None
        watcher = GitHubWatcher(self._config(tmp, chat), client=client,
                                state_path=Path(tmp) / "s.json")
        return watcher, asyncio.run(watcher.poll_once()), client

    def test_a_topic_in_another_group_is_refused_without_the_secret(self):
        with TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {SECRET_ENV: ""}):
            watcher, outcome, client = self._poll(tmp, -1009999999999)
        self.assertEqual(outcome.turns_queued, 0)
        self.assertTrue(any("refused" in error for error in outcome.errors), outcome.errors)
        self.assertTrue(watcher.state.is_processed("n1"))     # not retried forever
        client.mark_notification_read.assert_called_once_with("n1")

    def test_the_same_topic_runs_once_the_secret_is_set(self):
        env = mock.patch.dict(os.environ, {SECRET_ENV: "shared-secret"})
        with TemporaryDirectory() as tmp, env:
            watcher, outcome, client = self._poll(tmp, -1009999999999)
        self.assertEqual(outcome.turns_queued, 1, outcome.errors)

    def test_the_home_group_needs_no_secret(self):
        with TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {SECRET_ENV: ""}):
            watcher, outcome, client = self._poll(tmp, GROUP)
        self.assertEqual(outcome.turns_queued, 1, outcome.errors)


class TestChannelRefuses(SecretSandbox):
    """The Telegram side is gated too, not only the GitHub backstop (review of PR #23)."""

    def _channel(self, home):
        from swarmly.telegram.adapters import EchoAdapter
        from swarmly.telegram.channel import AgentChannel
        cfg = AgentConfig.from_dict({
            "agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                      "home_channel_id": home or GROUP, "admin_user_id": ADMIN_ID},
            "peers": {"Codex": "CodexBot"},
            "adapter": {"type": "echo"},
        })
        if home is None:
            cfg.identity.home_channel_id = None      # a host that never named its group
        channel = AgentChannel(cfg, adapter=EchoAdapter("Hermes"), sender=mock.MagicMock(),
                               gitbus_pull=lambda _root: None, gitbus_read=lambda _root: [])
        channel.send = mock.AsyncMock()
        return channel

    def test_a_threaded_topic_without_a_secret_is_refused(self):
        from tests.test_telegram_channel import event
        with mock.patch.dict(os.environ, {SECRET_ENV: ""}):
            channel = self._channel(home=None)
            asyncio.run(channel.handle_event(event("@HermesBot hello", thread_id=THREAD,
                                                   chat_id=-1009999999999)))
        self.assertEqual(channel.stats.ignored, 1)
        channel.send.assert_not_awaited()               # no turn, no reply, no General session

    def test_the_same_topic_runs_once_the_secret_is_set(self):
        from tests.test_telegram_channel import event
        with mock.patch.dict(os.environ, {SECRET_ENV: "shared-secret"}):
            channel = self._channel(home=None)
            asyncio.run(channel.handle_event(event("@HermesBot hello", thread_id=THREAD,
                                                   chat_id=-1009999999999)))
        channel.send.assert_awaited()

    def test_the_home_group_topic_needs_no_secret(self):
        from tests.test_telegram_channel import event
        with mock.patch.dict(os.environ, {SECRET_ENV: ""}):
            channel = self._channel(home=GROUP)
            asyncio.run(channel.handle_event(event("@HermesBot hello", thread_id=THREAD,
                                                   chat_id=GROUP)))
        channel.send.assert_awaited()


if __name__ == "__main__":
    unittest.main()
