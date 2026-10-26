"""/cleanup keep and unkeep: kept topics live in the registry, not in config.yaml (26 Oct)."""

import asyncio
import unittest

from swarmly.state.records import Topic
from swarmly.telegram.cleanup import Cleanup, GeneralLedger, TopicInfo
from tests.test_registry_links import LinkFlowTests


class KeepInTheRegistryTests(unittest.TestCase):
    def link(self, holder=True):
        return LinkFlowTests.link(self, holder=holder)

    def test_keep_and_unkeep_are_recorded_and_read_back(self):
        link = self.link()
        said = asyncio.run(link.set_topic_keep(-100, 55, True, "admin:1"))
        self.assertIn("kept", said)
        self.assertTrue(Topic.from_dict(link.store.read("topic/55")).keep)
        self.assertEqual(link.kept_topics(), {55})
        self.assertIn("already", asyncio.run(link.set_topic_keep(-100, 55, True, "admin:1")))
        asyncio.run(link.set_topic_keep(-100, 55, False, "admin:1"))
        self.assertEqual(link.kept_topics(), set())

    def test_only_the_holder_writes(self):
        link = self.link(holder=False)
        self.assertIn("coordinator", asyncio.run(link.set_topic_keep(-100, 55, True, "a")))


class Host:
    def __init__(self, topics, kept):
        self.topics, self.kept, self.calls = topics, kept, []

    def cleanup_home_chat(self):
        return -100

    def cleanup_topics(self):
        return list(self.topics)

    def cleanup_protected_topics(self):
        return set(self.kept)

    async def cleanup_set_keep(self, chat, thread, keep, by):
        self.calls.append((thread, keep, by))
        return "kept" if keep else "no longer kept"

    async def cleanup_delete_topic(self, chat, thread):
        return True

    async def cleanup_forget_topic(self, chat, thread):
        pass


class CommandTests(unittest.TestCase):
    def test_keep_is_a_command_in_the_topic(self):
        host = Host([], set())
        cleanup = Cleanup(host, GeneralLedger(None), handle="Claude")
        said = asyncio.run(cleanup.handle_args(["keep"], is_admin=True, chat_id=-100,
                                               thread_id=77, author_id=1))
        self.assertIn("kept", said)
        self.assertEqual(host.calls, [(77, True, "admin:1")])

    def test_a_kept_topic_is_never_deleted(self):
        old = 1.0
        host = Host([TopicInfo(thread_id=77, title="Swarm test", last_activity=old),
                     TopicInfo(thread_id=78, title="done", last_activity=old)], {77})
        cleanup = Cleanup(host, GeneralLedger(None), handle="Claude")
        said = asyncio.run(cleanup.handle_args([], is_admin=True))
        self.assertIn("Swarm test (77) (protected)", said)
        self.assertIn("deleted 1 topic(s): done (78)", said)


if __name__ == "__main__":
    unittest.main()
