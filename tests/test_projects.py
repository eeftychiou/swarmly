"""
Tests for Phase 2: projects config, ProjectRouter, per-topic routing, and /where command.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from swarmly.telegram.adapters import EchoAdapter
from swarmly.telegram.adapters.base import (
    IncomingMessage,
    channel_envelope,
    format_channel_prompt,
    project_line,
)
from swarmly.telegram.channel import AgentChannel, ChannelEvent, _state_file
from swarmly.telegram.commands import where_reply
from swarmly.telegram.config import AgentConfig, ConfigError, ProjectSettings
from swarmly.telegram.projects import ProjectRouter
from swarmly.telegram.replies import reply_instructions
from swarmly.telegram.topics import TopicMap

GROUP = -1001234567890
ADMIN_ID = 1234567890


def make_config(**overrides) -> AgentConfig:
    data = {
        "agent": {
            "name": "HermesBot",
            "alias": "Hermes",
            "bot_token": "123456:TEST",
            "home_channel_id": GROUP,
            "admin_user_id": ADMIN_ID,
        },
        "peers": {"Codex": "CodexBot", "Antigravity": "AntigravityBot"},
        "adapter": {"type": "echo"},
        "limits": {
            "min_seconds_between_messages": 0,
            "min_seconds_between_agent_turns": 0,
            "selftest_seconds": 0,
        },
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return AgentConfig.from_dict(data)


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, payload: str, reply_to_message_id=None):
        self.sent.append({"text": payload, "reply_to": reply_to_message_id})
        return payload

    @property
    def last(self) -> str:
        return self.sent[-1]["text"] if self.sent else ""


def event(
    text: str,
    author_id=ADMIN_ID,
    username="alice_admin",
    is_bot=False,
    chat_id=GROUP,
    replies_to_bot=False,
    message_id=1,
    thread_id=None,
    attachments=None,
) -> ChannelEvent:
    return ChannelEvent(
        text=text,
        chat_id=chat_id,
        author_id=author_id,
        author_username=username,
        author_is_bot=is_bot,
        replies_to_bot=replies_to_bot,
        message_id=message_id,
        thread_id=thread_id,
        attachments=list(attachments or []),
    )


class TestProjectsConfig(unittest.TestCase):
    def test_config_no_projects_leaves_config_unchanged(self):
        cfg = make_config()
        self.assertEqual(cfg.projects, {})
        self.assertEqual(cfg.validate(), [])

    def test_config_valid_projects_parses(self):
        with TemporaryDirectory() as tmp1, TemporaryDirectory() as tmp2:
            data = {
                "agent": {
                    "name": "HermesBot",
                    "alias": "Hermes",
                    "bot_token": "123456:TEST",
                    "home_channel_id": GROUP,
                    "admin_user_id": ADMIN_ID,
                },
                "projects": {
                    "webapp": {
                        "repo": "example-org/webapp",
                        "cwd": tmp1,
                        "topics": [12, 15],
                        "worktrees": True,
                    },
                    "shop": {
                        "repo": "example-org/shop",
                        "cwd": tmp2,
                        "topics": ["-100999/21"],
                    },
                },
                "github": {
                    "login": "test-bot",
                },
            }
            cfg = AgentConfig.from_dict(data)
            self.assertEqual(len(cfg.projects), 2)
            self.assertIn("webapp", cfg.projects)
            self.assertIn("shop", cfg.projects)

            p1 = cfg.projects["webapp"]
            self.assertEqual(p1.key, "webapp")
            self.assertEqual(p1.repo, "example-org/webapp")
            self.assertEqual(p1.cwd, tmp1)
            self.assertEqual(p1.topics, [(None, 12), (None, 15)])
            self.assertTrue(p1.worktrees)

            p2 = cfg.projects["shop"]
            self.assertEqual(p2.key, "shop")
            self.assertEqual(p2.repo, "example-org/shop")
            self.assertEqual(p2.cwd, tmp2)
            self.assertEqual(p2.topics, [(-100999, 21)])
            self.assertFalse(p2.worktrees)

            problems = cfg.validate()
            self.assertEqual(problems, [])

    def test_config_unknown_keys_flags_typo_inside_project(self):
        with TemporaryDirectory() as tmp:
            data = {
                "agent": {
                    "name": "HermesBot",
                    "alias": "Hermes",
                    "bot_token": "123456:TEST",
                    "home_channel_id": GROUP,
                    "admin_user_id": ADMIN_ID,
                },
                "projects": {
                    "webapp": {
                        "repp": "example-org/webapp",
                        "cwd": tmp,
                        "topics": [12],
                    }
                },
            }
            cfg = AgentConfig.from_dict(data)
            self.assertTrue(any("projects.webapp.repp" in k and "repo" in k for k in cfg.unknown_keys))

    def test_config_validation_missing_repo_and_cwd(self):
        data = {
            "agent": {
                "name": "HermesBot",
                "alias": "Hermes",
                "bot_token": "123456:TEST",
                "home_channel_id": GROUP,
                "admin_user_id": ADMIN_ID,
            },
            "projects": {
                "webapp": {
                    "topics": [12],
                }
            },
        }
        cfg = AgentConfig.from_dict(data)
        problems = cfg.validate()
        self.assertTrue(any("projects.webapp.repo is required" in p for p in problems))
        self.assertTrue(any("projects.webapp.cwd is required" in p for p in problems))

    def test_config_validation_invalid_repo_format(self):
        with TemporaryDirectory() as tmp:
            data = {
                "agent": {
                    "name": "HermesBot",
                    "alias": "Hermes",
                    "bot_token": "123456:TEST",
                    "home_channel_id": GROUP,
                    "admin_user_id": ADMIN_ID,
                },
                "projects": {
                    "webapp": {
                        "repo": "invalid_repo_name_without_slash",
                        "cwd": tmp,
                        "topics": [12],
                    }
                },
            }
            cfg = AgentConfig.from_dict(data)
            problems = cfg.validate()
            self.assertTrue(any("projects.webapp.repo must match 'owner/name'" in p for p in problems))

    def test_config_validation_cwd_does_not_exist(self):
        data = {
            "agent": {
                "name": "HermesBot",
                "alias": "Hermes",
                "bot_token": "123456:TEST",
                "home_channel_id": GROUP,
                "admin_user_id": ADMIN_ID,
            },
            "projects": {
                "webapp": {
                    "repo": "example-org/webapp",
                    "cwd": "/nonexistent/path/for/test/cwd",
                    "topics": [12],
                }
            },
        }
        cfg = AgentConfig.from_dict(data)
        problems = cfg.validate()
        self.assertTrue(any("projects.webapp.cwd does not exist" in p for p in problems))

    def test_config_validation_topic_not_an_int(self):
        with TemporaryDirectory() as tmp:
            data = {
                "agent": {
                    "name": "HermesBot",
                    "alias": "Hermes",
                    "bot_token": "123456:TEST",
                    "home_channel_id": GROUP,
                    "admin_user_id": ADMIN_ID,
                },
                "projects": {
                    "webapp": {
                        "repo": "example-org/webapp",
                        "cwd": tmp,
                        "topics": ["not-a-number"],
                    }
                },
            }
            cfg = AgentConfig.from_dict(data)
            problems = cfg.validate()
            self.assertTrue(any("topic id must be an integer" in p for p in problems))

    def test_config_validation_topic_listed_under_two_projects(self):
        with TemporaryDirectory() as tmp1, TemporaryDirectory() as tmp2:
            data = {
                "agent": {
                    "name": "HermesBot",
                    "alias": "Hermes",
                    "bot_token": "123456:TEST",
                    "home_channel_id": GROUP,
                    "admin_user_id": ADMIN_ID,
                },
                "projects": {
                    "webapp": {
                        "repo": "example-org/webapp",
                        "cwd": tmp1,
                        "topics": [12, 15],
                    },
                    "shop": {
                        "repo": "example-org/shop",
                        "cwd": tmp2,
                        "topics": [15, 21],
                    },
                },
            }
            cfg = AgentConfig.from_dict(data)
            problems = cfg.validate()
            self.assertTrue(any("topic 15 is listed under multiple projects" in p for p in problems))

    def test_config_validation_shared_cwd_without_worktrees_parallel_turns(self):
        with TemporaryDirectory() as tmp:
            data = {
                "agent": {
                    "name": "HermesBot",
                    "alias": "Hermes",
                    "bot_token": "123456:TEST",
                    "home_channel_id": GROUP,
                    "admin_user_id": ADMIN_ID,
                },
                "limits": {
                    "max_parallel_turns": 2,
                    "allow_shared_cwd": False,
                },
                "projects": {
                    "p1": {
                        "repo": "owner/p1",
                        "cwd": tmp,
                        "topics": [1],
                        "worktrees": False,
                    },
                    "p2": {
                        "repo": "owner/p2",
                        "cwd": tmp,
                        "topics": [2],
                        "worktrees": False,
                    },
                },
            }
            cfg = AgentConfig.from_dict(data)
            problems = cfg.validate()
            self.assertTrue(any("share the same cwd" in p and "worktrees: false" in p for p in problems))

    def test_config_github_parses(self):
        data = {
            "agent": {
                "name": "HermesBot",
                "alias": "Hermes",
                "bot_token": "123456:TEST",
                "home_channel_id": GROUP,
                "admin_user_id": ADMIN_ID,
            },
            "github": {
                "login": "my-agent-bot",
                "token_env": "GH_TOKEN_TEST",
                "poll_minutes": 10,
                "mark_read": False,
            },
        }
        cfg = AgentConfig.from_dict(data)
        self.assertIsNotNone(cfg.github)
        assert cfg.github is not None
        self.assertEqual(cfg.github.login, "my-agent-bot")
        self.assertEqual(cfg.github.token_env, "GH_TOKEN_TEST")
        self.assertEqual(cfg.github.poll_minutes, 10)
        self.assertFalse(cfg.github.mark_read)
        self.assertEqual(cfg.validate(), [])

    def test_config_github_validation_missing_login_when_projects_configured(self):
        with TemporaryDirectory() as tmp:
            data = {
                "agent": {
                    "name": "HermesBot",
                    "alias": "Hermes",
                    "bot_token": "123456:TEST",
                    "home_channel_id": GROUP,
                    "admin_user_id": ADMIN_ID,
                },
                "projects": {
                    "webapp": {
                        "repo": "owner/webapp",
                        "cwd": tmp,
                        "topics": [12],
                    }
                },
            }
            cfg = AgentConfig.from_dict(data)
            problems = cfg.validate()
            # Routing alone needs no GitHub account: there is no backstop to poll with.
            self.assertFalse(any("github.login" in p for p in problems), problems)
            data["github"] = {"poll_minutes": 5}
            problems = AgentConfig.from_dict(data).validate()
            self.assertTrue(any("github.login is required" in p for p in problems), problems)
            data["github"] = {"poll_minutes": 0}
            problems = AgentConfig.from_dict(data).validate()
            self.assertFalse(any("github.login" in p for p in problems), problems)

    def test_config_rejects_malformed_projects(self):
        base = {"agent": {"name": "HermesBot", "alias": "Hermes", "bot_token": "123456:TEST",
                          "home_channel_id": GROUP, "admin_user_id": ADMIN_ID}}
        for projects in ([{"repo": "o/r"}], {"webapp": "o/r"}):
            with self.subTest(projects=projects), self.assertRaises(ConfigError):
                AgentConfig.from_dict({**base, "projects": projects})
        with TemporaryDirectory() as tmp:
            cfg = AgentConfig.from_dict({**base, "projects": {
                "Web App!": {"repo": "o/r", "cwd": tmp, "topics": [0, -5]}}})
            problems = cfg.validate()
        self.assertTrue(any("project key" in p for p in problems), problems)
        self.assertEqual(sum("must be positive" in p for p in problems), 2, problems)

    def test_config_github_validation_poll_minutes_negative(self):
        data = {
            "agent": {
                "name": "HermesBot",
                "alias": "Hermes",
                "bot_token": "123456:TEST",
                "home_channel_id": GROUP,
                "admin_user_id": ADMIN_ID,
            },
            "github": {
                "login": "bot",
                "poll_minutes": -1,
            },
        }
        cfg = AgentConfig.from_dict(data)
        problems = cfg.validate()
        self.assertTrue(any("github.poll_minutes must be an integer >= 0" in p for p in problems))

    def test_config_github_unknown_keys_typo(self):
        data = {
            "agent": {
                "name": "HermesBot",
                "alias": "Hermes",
                "bot_token": "123456:TEST",
                "home_channel_id": GROUP,
                "admin_user_id": ADMIN_ID,
            },
            "github": {
                "loginn": "bot",
            },
        }
        cfg = AgentConfig.from_dict(data)
        self.assertTrue(any("github.loginn" in k and "login" in k for k in cfg.unknown_keys))


class TestProjectRouter(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.cwd = self.tmp.name
        self.projects = [
            ProjectSettings(
                key="webapp",
                repo="example-org/webapp",
                cwd=self.cwd,
                topics=[(None, 12), (None, 15)],
                worktrees=False,
            ),
            ProjectSettings(
                key="shop",
                repo="example-org/shop",
                cwd=self.cwd,
                topics=[(-100999, 21)],
                worktrees=True,
            ),
        ]
        self.router = ProjectRouter(self.projects, home_chat_id=GROUP)

    def tearDown(self):
        self.tmp.cleanup()

    def test_router_home_group_int_ids(self):
        p = self.router.project_for(GROUP, 12)
        self.assertIsNotNone(p)
        self.assertEqual(p.key, "webapp")

        p15 = self.router.project_for(GROUP, 15)
        self.assertIsNotNone(p15)
        self.assertEqual(p15.key, "webapp")

    def test_router_chat_slash_thread_ids(self):
        p = self.router.project_for(-100999, 21)
        self.assertIsNotNone(p)
        self.assertEqual(p.key, "shop")

        # Wrong chat id for that thread id returns None
        self.assertIsNone(self.router.project_for(GROUP, 21))

    def test_router_general_returns_none(self):
        self.assertIsNone(self.router.project_for(GROUP, None))
        self.assertIsNone(self.router.project_for(-100999, None))

    def test_router_unbound_topic_returns_none(self):
        self.assertIsNone(self.router.project_for(GROUP, 999))

    def test_router_topic_rename_does_not_change_route(self):
        with TemporaryDirectory() as tmp_topics:
            tmap = TopicMap(Path(tmp_topics) / "topics.json", home_chat_id=GROUP)
            tmap.remember(GROUP, 12, "Old Name")
            p1 = self.router.project_for(GROUP, 12)
            self.assertIsNotNone(p1)
            self.assertEqual(p1.key, "webapp")

            # Rename topic in TopicMap
            tmap.remember(GROUP, 12, "Brand New Name")
            p2 = self.router.project_for(GROUP, 12)
            self.assertIsNotNone(p2)
            self.assertEqual(p2.key, "webapp")

    def test_router_topics_of(self):
        topics = self.router.topics_of("webapp")
        self.assertEqual(topics, [(GROUP, 12), (GROUP, 15)])

        topics_tw = self.router.topics_of("shop")
        self.assertEqual(topics_tw, [(-100999, 21)])

        self.assertEqual(self.router.topics_of("unknown"), [])

    def test_router_worktrees_false_returns_cwd(self):
        proj = self.projects[0]
        workdir = self.router.workdir_for(proj, 12)
        self.assertEqual(workdir, Path(self.cwd).resolve())

    def test_router_worktrees_true_creates_and_returns_worktree(self):
        with TemporaryDirectory() as repo_dir:
            # Initialize a real git repo
            subprocess.run(["git", "init", "-b", "main", repo_dir], capture_output=True, check=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.email", "test@example.com"], check=True)
            test_file = Path(repo_dir) / "README.md"
            test_file.write_text("initial commit\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo_dir, "add", "README.md"], check=True)
            subprocess.run(["git", "-C", repo_dir, "commit", "-m", "initial"], check=True)

            proj = ProjectSettings(
                key="testproj",
                repo="owner/testproj",
                cwd=repo_dir,
                topics=[(None, 42)],
                worktrees=True,
            )
            router = ProjectRouter([proj], home_chat_id=GROUP)
            workdir = router.workdir_for(proj, 42)

            expected = Path(repo_dir).resolve() / ".swarmly-worktrees" / "topic-42"
            self.assertEqual(workdir, expected)
            self.assertTrue(workdir.exists())

            # Verify .swarmly-worktrees/ added to .git/info/exclude
            exclude_file = Path(repo_dir) / ".git" / "info" / "exclude"
            self.assertTrue(exclude_file.exists())
            exclude_text = exclude_file.read_text(encoding="utf-8")
            self.assertIn(".swarmly-worktrees/", exclude_text)

            # Verify tracked .gitignore does NOT exist or does not contain it
            gitignore = Path(repo_dir) / ".gitignore"
            self.assertFalse(gitignore.exists())

    def test_router_worktrees_fallback_on_failure(self):
        with TemporaryDirectory() as non_git_dir:
            proj = ProjectSettings(
                key="failproj",
                repo="owner/failproj",
                cwd=non_git_dir,
                topics=[(None, 42)],
                worktrees=True,
            )
            router = ProjectRouter([proj], home_chat_id=GROUP)
            # Not a git repo, so worktree add fails; should fall back to cwd
            workdir = router.workdir_for(proj, 42)
            self.assertEqual(workdir, Path(non_git_dir).resolve())


class TestProjectChannel(unittest.TestCase):
    def test_channel_routing_echo_adapter_workdirs(self):
        with TemporaryDirectory() as tmp_main, TemporaryDirectory() as tmp_press, TemporaryDirectory() as tmp_tw:
            cfg = make_config(
                adapter={"type": "echo", "cwd": tmp_main},
                projects={
                    "webapp": {
                        "repo": "example-org/webapp",
                        "cwd": tmp_press,
                        "topics": [12],
                        "worktrees": False,
                    },
                    "shop": {
                        "repo": "example-org/shop",
                        "cwd": tmp_tw,
                        "topics": [21],
                        "worktrees": False,
                    },
                },
            )
            sender = FakeSender()
            channel = AgentChannel(cfg, sender=sender)

            # Topic 12 should route to webapp's cwd
            a12 = channel._adapter_for(GROUP, 12)
            self.assertEqual(Path(a12.settings.cwd).resolve(), Path(tmp_press).resolve())

            # Topic 21 should route to shop's cwd
            a21 = channel._adapter_for(GROUP, 21)
            self.assertEqual(Path(a21.settings.cwd).resolve(), Path(tmp_tw).resolve())

            # General (None) should route to default adapter.cwd
            a_gen = channel._adapter_for(GROUP, None)
            self.assertEqual(Path(a_gen.settings.cwd).resolve(), Path(tmp_main).resolve())
            self.assertIs(a_gen, channel.adapter)

    def test_channel_two_topics_share_adapter_when_worktrees_false(self):
        with TemporaryDirectory() as tmp_press:
            cfg = make_config(
                projects={
                    "webapp": {
                        "repo": "example-org/webapp",
                        "cwd": tmp_press,
                        "topics": [12, 15],
                        "worktrees": False,
                    }
                }
            )
            channel = AgentChannel(cfg, sender=FakeSender())
            a12 = channel._adapter_for(GROUP, 12)
            a15 = channel._adapter_for(GROUP, 15)
            self.assertIs(a12, a15)
            self.assertEqual(len(channel._project_adapters), 1)

    def test_channel_two_topics_separate_adapters_when_worktrees_true(self):
        with TemporaryDirectory() as repo_dir:
            subprocess.run(["git", "init", "-b", "main", repo_dir], capture_output=True, check=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", repo_dir, "config", "user.email", "test@example.com"], check=True)
            Path(repo_dir, "README.md").write_text("init\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo_dir, "add", "README.md"], check=True)
            subprocess.run(["git", "-C", repo_dir, "commit", "-m", "init"], check=True)

            cfg = make_config(
                projects={
                    "webapp": {
                        "repo": "example-org/webapp",
                        "cwd": repo_dir,
                        "topics": [12, 15],
                        "worktrees": True,
                    }
                }
            )
            channel = AgentChannel(cfg, sender=FakeSender())
            a12 = channel._adapter_for(GROUP, 12)
            a15 = channel._adapter_for(GROUP, 15)
            self.assertIsNot(a12, a15)
            self.assertNotEqual(a12.settings.cwd, a15.settings.cwd)
            self.assertEqual(len(channel._project_adapters), 2)

    def test_channel_state_files_per_project(self):
        with TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.yaml"
            config_file.write_text("agent:\n  name: TestBot\n  alias: test\n", encoding="utf-8")
            cfg = make_config(projects={})
            cfg.source_path = config_file
            cfg.identity.alias = "test"

            # Default unbound state file
            sf_default = _state_file(cfg, "codex-sessions")
            self.assertIsNotNone(sf_default)
            self.assertTrue(str(sf_default).endswith("codex-sessions-test.json"))

            # Project with worktrees: false
            sf_proj = _state_file(cfg, "codex-sessions", suffix="webapp")
            self.assertIsNotNone(sf_proj)
            self.assertTrue(str(sf_proj).endswith("codex-sessions-test-webapp.json"))

            # Project with worktrees: true (includes topic thread)
            sf_thread = _state_file(cfg, "codex-sessions", suffix="webapp-t12")
            self.assertIsNotNone(sf_thread)
            self.assertTrue(str(sf_thread).endswith("codex-sessions-test-webapp-t12.json"))

    def test_where_command_bound_topic(self):
        with TemporaryDirectory() as tmp:
            cfg = make_config(
                projects={
                    "webapp": {
                        "repo": "example-org/webapp",
                        "cwd": tmp,
                        "topics": [12],
                    }
                }
            )
            sender = FakeSender()
            channel = AgentChannel(cfg, sender=sender)
            reply = channel._command_reply("where", event("/where@HermesBot", thread_id=12))
            self.assertIn("WHERE Hermes", reply)
            self.assertIn(f"chat={GROUP}", reply)
            self.assertIn("topic=12", reply)
            self.assertIn("project=webapp", reply)
            self.assertIn("repo=example-org/webapp", reply)
            self.assertIn(f"workdir={Path(tmp).resolve()}", reply)

    def test_where_command_unbound_topic(self):
        cfg = make_config()
        channel = AgentChannel(cfg, sender=FakeSender())
        reply = channel._command_reply("where", event("/where@HermesBot", thread_id=99))
        self.assertIn("WHERE Hermes", reply)
        self.assertIn(f"chat={GROUP}", reply)
        self.assertIn("topic=99", reply)
        self.assertIn("not bound: Telegram only", reply)

    def test_status_command_includes_bound_project(self):
        with TemporaryDirectory() as tmp:
            cfg = make_config(
                projects={
                    "webapp": {
                        "repo": "example-org/webapp",
                        "cwd": tmp,
                        "topics": [12],
                    }
                }
            )
            channel = AgentChannel(cfg, sender=FakeSender())
            # In bound topic 12
            reply_bound = channel._command_reply("status", event("/status@HermesBot", thread_id=12))
            self.assertIn("project=webapp", reply_bound)

            # In unbound topic 99
            reply_unbound = channel._command_reply("status", event("/status@HermesBot", thread_id=99))
            self.assertNotIn("project=", reply_unbound)

    def test_incoming_message_project_and_prompt_framing(self):
        msg = IncomingMessage(
            text="Please test",
            author_alias="Alice",
            project="webapp",
            project_repo="example-org/webapp",
        )
        pline = project_line(msg)
        self.assertEqual(pline, "Project: webapp (github.com/example-org/webapp)")

        envelope = channel_envelope(msg)
        self.assertIn("project webapp (github.com/example-org/webapp)", envelope)

        prompt = format_channel_prompt(
            author_alias="Alice",
            text="Please test",
            project=pline,
        )
        self.assertIn("- Project: webapp (github.com/example-org/webapp)", prompt)

    def test_reply_instructions_uses_routed_workdir(self):
        with TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.yaml"
            config_file.write_text("agent:\n  name: TestBot\n  alias: test\n", encoding="utf-8")
            cfg = make_config()
            cfg.source_path = config_file

            custom_workdir = Path(tmp) / "custom_workdir"
            instructions = reply_instructions(cfg, {"id": "req-123"}, workdir=custom_workdir)
            self.assertIn(f"Working directory: {custom_workdir}", instructions)


class TestProjectsEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_realistic_two_projects_with_worktrees_and_github_e2e(self):
        """End-to-end self-test verifying that a realistic config with two projects
        (one with worktrees enabled, one without) and GitHub notifications enabled:
        - loads from dict and validates without errors
        - resolves project routing and distinct working directories
        - simulates two incoming Telegram messages routing to distinct working directories
        - respects /where and /status diagnostic queries in both topics
        """
        with TemporaryDirectory() as tmp_root:
            webapp_dir = Path(tmp_root) / "webapp"
            shop_dir = Path(tmp_root) / "shop"
            default_dir = Path(tmp_root) / "default"

            webapp_dir.mkdir()
            shop_dir.mkdir()
            default_dir.mkdir()

            # Initialize a real git repo for webapp to exercise worktree creation
            subprocess.run(["git", "init", "-b", "main", str(webapp_dir)], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(webapp_dir), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(webapp_dir), "config", "user.email", "test@example.com"], check=True)
            (webapp_dir / "README.md").write_text("# Webapp\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(webapp_dir), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(webapp_dir), "commit", "-m", "init"], check=True)

            raw_config = {
                "agent": {
                    "name": "HermesBot",
                    "alias": "Hermes",
                    "bot_token": "123456:TEST",
                    "home_channel_id": GROUP,
                    "admin_user_id": ADMIN_ID,
                },
                "peers": {
                    "Codex": "CodexBot",
                    "Antigravity": "AntigravityBot",
                },
                "adapter": {
                    "type": "echo",
                    "cwd": str(default_dir),
                },
                "projects": {
                    "webapp": {
                        "repo": "example-org/webapp",
                        "cwd": str(webapp_dir),
                        "topics": [12],
                        "worktrees": True,
                    },
                    "shop": {
                        "repo": "example-org/shop",
                        "cwd": str(shop_dir),
                        "topics": [21],
                        "worktrees": False,
                    },
                },
                "github": {
                    "login": "swarmly-hermes-bot",
                    "token_env": "GH_TOKEN_HERMES",
                    "poll_minutes": 5,
                    "mark_read": True,
                },
                "limits": {
                    "min_seconds_between_messages": 0,
                    "min_seconds_between_agent_turns": 0,
                    "selftest_seconds": 0,
                },
            }

            # 1. Load and validate
            cfg = AgentConfig.from_dict(raw_config)
            errors = cfg.validate()
            self.assertEqual(errors, [])
            self.assertIsNotNone(cfg.github)
            self.assertEqual(cfg.github.login, "swarmly-hermes-bot")
            self.assertEqual(cfg.github.poll_minutes, 5)
            self.assertEqual(len(cfg.projects), 2)

            # 2. Set up AgentChannel
            sender = FakeSender()
            channel = AgentChannel(cfg, sender=sender)

            # 3. Simulate first message in topic 12 (webapp, worktrees=True)
            ev1 = event("@HermesBot implement webapp feature", thread_id=12, message_id=101)
            await channel.handle_event(ev1)

            # 4. Simulate second message in topic 21 (shop, worktrees=False)
            ev2 = event("@HermesBot adjust shop styles", thread_id=21, message_id=102)
            await channel.handle_event(ev2)

            # Verify responses were generated
            self.assertEqual(len(sender.sent), 2)
            self.assertIn("implement webapp feature", sender.sent[0]["text"])
            self.assertIn("adjust shop styles", sender.sent[1]["text"])

            # 5. Verify adapters and distinct working directories
            adapter12 = channel._adapter_for(GROUP, 12)
            adapter21 = channel._adapter_for(GROUP, 21)
            workdir12 = Path(adapter12.settings.cwd).resolve()
            workdir21 = Path(adapter21.settings.cwd).resolve()

            self.assertNotEqual(workdir12, workdir21)
            self.assertTrue(workdir12.is_relative_to(webapp_dir.resolve()))
            self.assertEqual(workdir21, shop_dir.resolve())

            # 6. Verify diagnostic commands /where and /status in both topics
            where12 = channel._command_reply("where", event("/where@HermesBot", thread_id=12))
            self.assertIn("project=webapp", where12)
            self.assertIn("example-org/webapp", where12)

            where21 = channel._command_reply("where", event("/where@HermesBot", thread_id=21))
            self.assertIn("project=shop", where21)
            self.assertIn("example-org/shop", where21)

            status12 = channel._command_reply("status", event("/status@HermesBot", thread_id=12))
            self.assertIn("project=webapp", status12)
            self.assertIn("github: @swarmly-hermes-bot", status12)


if __name__ == "__main__":
    unittest.main()

