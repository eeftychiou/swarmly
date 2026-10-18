"""Issue #22: the maintainer's handle, chat ids and absolute paths stay off the bus.

The rescoped #1: bot @usernames may stay visible (public on Telegram already), but a chat id
must not leave the host, dedupe must keep working, and a peer that cannot resolve a token
runs the mirrored turn anyway -- processed twice, never lost.
"""

import asyncio
import os
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from swarmly.telegram.config import AgentConfig, ConfigError
from swarmly.telegram.redact import (CHAT, PATH, Redactor, SECRET_ENV, hmac_token,
                                     resolve_topic_token, topic_token)

GROUP = -1001234567890
ADMIN_ID = 987654321
THREAD = 12


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


class TestTokens(unittest.TestCase):
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
        self.assertEqual(hmac_token(GROUP, ""), "")      # no secret, no token

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


class TestRedactor(unittest.TestCase):

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
                # ... and without a secret there is no token at all, which also means a peer
                # cannot dedupe: it runs the turn. Never a lost hand-off.
                os.environ.pop(SECRET_ENV)
                self.assertEqual(Redactor.from_config(cfg).topic_ref(GROUP, 99,
                                                                    channel.router), {})

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


class TestMirrorAndDedupe(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
