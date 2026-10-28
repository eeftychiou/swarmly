"""Work items and links (topic registry, phase 5)."""

import asyncio
import unittest
from types import SimpleNamespace

from swarmly.state import keys as K
from swarmly.state.links import AssignLine, LinkLine, PhaseLine, parse_lines, parse_ref
from swarmly.state.records import Topic
from swarmly.state.store import MemoryLog, MemoryStore
from swarmly.telegram.registry_ops import RegistryOps
from tests.test_registry_shadow import HOME, Clock, channel_for, seen


class ParseRefTests(unittest.TestCase):
    def test_each_provider(self):
        cases = {
            "#12": ("github", "issue", "https://github.com/o/r/issues/12", "#12"),
            "x/y#7": ("github", "issue", "https://github.com/x/y/issues/7", "#7"),
            "https://github.com/o/r/pull/256": ("github", "pr",
                                               "https://github.com/o/r/pull/256", "#256"),
            "https://github.com/o/r/issues/3.": ("github", "issue",
                                                "https://github.com/o/r/issues/3", "#3"),
            "https://gitlab.com/g/p/-/merge_requests/9": ("gitlab", "pr",
                                                         "https://gitlab.com/g/p/-/merge_requests/9",
                                                         "!9"),
            "https://drive.google.com/drive/folders/abc": ("gdrive", "folder",
                                                          "https://drive.google.com/drive/folders/abc",
                                                          ""),
            "https://docs.google.com/document/d/x": ("gdrive", "doc",
                                                    "https://docs.google.com/document/d/x", ""),
            "https://example.org/spec.pdf": ("cloud", "doc", "https://example.org/spec.pdf", ""),
            "T-10134-1": ("topic", "task", "T-10134-1", ""),
            "C:\\data\\run1\\": ("local", "folder", "C:\\data\\run1\\", ""),
            "./notes.md": ("local", "file", "./notes.md", ""),
        }
        for text, want in cases.items():
            ref = parse_ref(text, repo="o/r")
            self.assertEqual((ref.provider, ref.kind, ref.ref, ref.title), want, text)
        self.assertIsNone(parse_ref("#12"))                  # no repo to resolve it
        self.assertIsNone(parse_ref("hello"))

    def test_lines(self):
        text = ("Plan for the round.\nLINK: #256 as work — dashboard slice 2\n"
                "ASSIGN: @DeepSeek #256 as developer\nPHASE: L1 review\n"
                "LINK: https://docs.google.com/document/d/x as reference\n"
                "PHASE: L1 dreaming\nLINK: #9 as nonsense")
        self.assertEqual(parse_lines(text), [
            LinkLine("#256", "work", "dashboard slice 2"),
            AssignLine("DeepSeek", "#256", "developer"),
            PhaseLine("L1", "review"),
            LinkLine("https://docs.google.com/document/d/x", "reference", ""),
        ])


class ProseIsNotALineTests(unittest.TestCase):
    def test_prose_and_code_blocks(self):
        self.assertEqual(parse_lines("Link: https://docs.google.com/document/d/abc"), [])
        self.assertEqual(parse_lines("Phase: design review"), [])
        self.assertEqual(parse_lines("LINK: #12\n- fixed the login"), [LinkLine("#12")])
        self.assertEqual(parse_lines("Example:\n```\nLINK: #99\nPHASE: #99 qa\n```\nLINK: #5"),
                         [LinkLine("#5")])


class RegistryOpsTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.shared = MemoryLog(clock=self.clock)
        self.store = MemoryStore("Claudebot", self.shared, clock=self.clock)
        asyncio.run(self.store.start())
        asyncio.run(self.store.claim_lease(10 ** 6))
        asyncio.run(self.store.write(K.agent("Claudebot", "profile"), {"x": 1}))
        deepseek = MemoryStore("DeepSeek", self.shared, clock=self.clock)
        asyncio.run(deepseek.start())
        asyncio.run(deepseek.write(K.agent("DeepSeek", "profile"), {
            "alias": "DeepSeek", "handle": "@d", "runtime": "reasonix",
            "default_model": "deepseek-flash", "default_effort": "low"}))
        asyncio.run(self.store.refresh())
        self.ops = RegistryOps(self.store, home=HOME, repo="o/r", clock=self.clock)

    def test_a_round_of_lines(self):
        topic = self.ops.topic(10134, "Stability round")
        done = self.ops.apply(topic, parse_lines(
            "LINK: #237 as tracking\nLINK: https://github.com/o/r/pull/256\n"
            "ASSIGN: @DeepSeek #256 as developer\nPHASE: #256 review"), "claudebot")
        self.assertEqual(len(done), 4, done)
        tracking, work = topic.links
        self.assertEqual((tracking.role, tracking.status), ("tracking", None))
        self.assertEqual((work.id, work.kind, work.status, work.phase), ("L2", "pr", "open",
                                                                        "review"))
        self.assertEqual([(a.agent, a.role) for a in work.assignees], [("DeepSeek", "developer")])
        binding = topic.agents["DeepSeek"]
        self.assertEqual((binding.role, binding.runtime, binding.model, binding.effort),
                         ("developer", "reasonix", "deepseek-flash", "low"))
        self.assertEqual([c.field for c in topic.history], ["assignees", "role", "phase"])
        self.assertTrue(asyncio.run(self.ops.save(10134, topic)))
        again = Topic.from_dict(self.store.read("topic/10134"))
        self.assertEqual(again.link("L2").phase, "review")

    def test_done_closes_the_item_and_bad_lines_are_refused(self):
        topic = self.ops.topic(7)
        self.ops.apply(topic, parse_lines("LINK: #5"), "claudebot")
        self.ops.apply(topic, [PhaseLine("L1", "done")], "claudebot")
        self.assertEqual(topic.link("L1").status, "closed")
        done = self.ops.apply(topic, [PhaseLine("#99", "qa"),
                                      AssignLine("DeepSeek", "L9", "qa-tester")], "claudebot")
        self.assertTrue(all(d.startswith("refused") for d in done), done)

    def test_a_legacy_link_becomes_explicit(self):
        topic = self.ops.topic(7)
        topic.links.append(__import__("swarmly.state.records", fromlist=["Link"]).Link(
            id="L1", provider="github", kind="issue", ref="https://github.com/o/r/issues/5",
            role="reference", title="#5", added_by="legacy", added_at="t"))
        self.ops.apply(topic, parse_lines("LINK: #5 as work"), "claudebot")
        link = topic.link("L1")
        self.assertEqual((link.role, link.added_by, link.status), ("work", "claudebot", "open"))
        self.assertEqual(len(topic.links), 1)

    def test_the_coordinator_role_is_not_assigned(self):
        topic = self.ops.topic(7)
        self.ops.apply(topic, parse_lines("LINK: #5"), "claudebot")
        done = self.ops.apply(topic, [AssignLine("DeepSeek", "L1", "coordinator")], "claudebot")
        self.assertTrue(done[0].startswith("refused"))

    def test_unlink_keeps_the_coordinator(self):
        topic = self.ops.topic(7)
        self.ops.link_agent(topic, "DeepSeek", "admin")
        self.assertIn("DeepSeek", topic.agents)
        self.ops.unlink_agent(topic, "deepseek", "admin")
        self.assertNotIn("DeepSeek", topic.agents)
        self.ops.link_agent(topic, "Claudebot", "admin", role="coordinator")
        with self.assertRaises(ValueError):
            self.ops.unlink_agent(topic, "Claudebot", "admin")


class LinkFlowTests(unittest.TestCase):
    """Through the state link: the coordinator's reply, and the admin's commands."""

    def link(self, holder=True):
        from tests.test_state_link import FakeGroup, link_for
        from tests.test_state_store import Clock as StoreClock
        clock = StoreClock()
        group = FakeGroup(clock)
        link = link_for(group, "Claudebot", clock, coordinator=True)
        link.channel.config.projects = {"p": SimpleNamespace(repo="o/r")}
        link.channel.config.is_admin = lambda author: author == 1
        link.channel.config.identity.name = "Claude_obot"

        async def start():
            await link.store.start()
            if holder:
                await link.store.claim_lease(10 ** 6)
        asyncio.run(start())
        return link

    def test_the_coordinators_lines_are_recorded(self):
        link = self.link()

        async def go():
            link.note_reply(-100, 55, "Opening the round.\nLINK: #12\nPHASE: #12 research")
            await asyncio.gather(*list(link._tasks))
        asyncio.run(go())
        topic = Topic.from_dict(link.store.read("topic/55"))
        self.assertEqual((topic.links[0].title, topic.links[0].phase), ("#12", "research"))

    def test_a_bot_without_the_lease_records_nothing(self):
        link = self.link(holder=False)
        link.note_reply(-100, 55, "LINK: #12")
        self.assertFalse(getattr(link, "_tasks", None))

    def test_a_beat_and_a_recorded_line_do_not_lose_each_others_change(self):
        """Review: both rebuilt topic/<id> from one read; the later write dropped the link."""
        link = self.link()
        link.channel.topics.remember(-100, 55, "round")
        link.channel.topics.touch(-100, 55, when=link.clock())

        async def go():
            link.note_reply(-100, 55, "LINK: #12")
            await asyncio.gather(self._sync(link), *list(link._tasks))
            await self._sync(link)
        asyncio.run(go())
        topic = Topic.from_dict(link.store.read("topic/55"))
        self.assertIn("#12", [l.title for l in topic.links])

    @staticmethod
    async def _sync(link):
        async with link.topic_lock:
            await link.registry.sync(link.store)

    def test_a_bare_command_reaching_a_bot_without_the_lease_is_silent(self):
        link = self.link(holder=False)
        reply = asyncio.run(link.registry_command(SimpleNamespace(
            text="/link #12", author_id=1, author_is_bot=False, chat_id=-100, thread_id=55)))
        self.assertEqual(reply, "")

    def test_admin_commands(self):
        link = self.link()

        def command(text, author=1, thread=55):
            return asyncio.run(link.registry_command(SimpleNamespace(
                text=text, author_id=author, author_is_bot=False, chat_id=-100,
                thread_id=thread)))
        self.assertIn("Recorded", command("/link #12 as work"))
        self.assertIn("Recorded", command("/phase L1 qa"))
        self.assertIn("Recorded", command("/link @DeepSeek"))
        self.assertIn("Only the admin", command("/phase L1 done", author=2))
        self.assertIn("inside a topic", command("/phase L1 done", thread=None))
        self.assertEqual(command("/phase@OtherBot L1 done"), "")
        self.assertIsNone(command("/ping"))
        topic = Topic.from_dict(link.store.read("topic/55"))
        self.assertEqual(topic.link("L1").phase, "qa")
        self.assertIn("DeepSeek", topic.agents)


class RoutingAndGitHubTests(unittest.TestCase):
    """Phase 5, part 2: notices follow the links, GitHub moves the items."""

    def link_with_topics(self):
        flow = LinkFlowTests()
        link = flow.link()

        def command(text, thread):
            return asyncio.run(link.registry_command(SimpleNamespace(
                text=text, author_id=1, author_is_bot=False, chat_id=-100, thread_id=thread)))
        command("/link #12 as work", 55)
        command("/link #12 as reference", 66)
        command("/link https://github.com/o/r/pull/40 as work", 66)
        command("/link https://github.com/x/other/pull/41 as work", 66)
        command("/link #13 as work — the login fix", 88)
        command("/link #7 as work", 77)
        command("/phase #7 done", 77)
        return link

    def test_a_ref_finds_the_topic_whose_work_item_it_is(self):
        link = self.link_with_topics()
        self.assertEqual(link.topic_for_refs(["#12"]), (-100, 55))
        self.assertEqual(link.topic_for_refs(["#40"]), (-100, 66))
        self.assertIsNone(link.topic_for_refs(["#7"]), "a closed item routes nothing")
        self.assertIsNone(link.topic_for_refs(["#999"]))
        self.assertEqual(link.topic_for_refs(["#13"]), (-100, 88), "a titled link routes")
        self.assertIsNone(link.topic_for_refs(["#41"]), "another repo's #41 is not ours")

    def test_the_coordinator_gets_the_registry_note_and_no_one_else(self):
        link = self.link_with_topics()
        note = link.registry_note(-100, 66)
        self.assertIn("LINK:", note)
        self.assertIn("L2 #40 development", note)
        self.assertEqual(link.registry_note(-100, None), "")
        flow = LinkFlowTests()
        other = flow.link(holder=False)
        self.assertEqual(other.registry_note(-100, 66), "")

    def test_github_moves_the_items(self):
        from unittest import mock
        from swarmly.github.client import GitHubClient
        link = self.link_with_topics()
        states = {12: ("closed", False), 40: ("open", True), 13: ("closed", True),
                  41: None}
        with mock.patch.object(GitHubClient, "ref_kind_state",
                               lambda self, repo, number: states.get(number)):
            moved = asyncio.run(link.reconcile_github(link.store))
        self.assertEqual(moved, 3)
        t88 = Topic.from_dict(link.store.read("topic/88"))
        self.assertEqual((t88.link("L1").kind, t88.link("L1").phase, t88.link("L1").status),
                         ("pr", "development", "closed"), "a closed, unmerged PR is not done")
        t55 = Topic.from_dict(link.store.read("topic/55"))
        t66 = Topic.from_dict(link.store.read("topic/66"))
        self.assertEqual((t55.link("L1").phase, t55.link("L1").status), ("done", "closed"))
        self.assertEqual(t66.link("L2").phase, "review")
        self.assertEqual(t55.history[-1].by, "github")
        with mock.patch.object(GitHubClient, "ref_kind_state",
                               lambda self, repo, number: states.get(number)):
            self.assertEqual(asyncio.run(link.reconcile_github(link.store)), 0, "settled")

    def test_the_channel_asks_the_registry_and_nothing_else(self):
        """#472: there is no second answer, so a config still naming ``links: legacy`` reads
        the registry like every other one."""
        from tests.test_telegram_channel import make_channel, make_config
        for named in ("registry", "legacy"):
            channel = make_channel(make_config(state={"backend": "telegram",
                                                      "links": named}))
            channel.state_link.topic_for_refs =                 lambda refs: (-100, 55) if "#12" in refs else None
            self.assertEqual(channel.topic_for_text("CI went red on #12"), (-100, 55), named)
            self.assertIsNone(channel.topic_for_text("CI went red on #99"), named)

    def test_registry_links_need_the_store(self):
        from swarmly.telegram.config import ConfigError
        from tests.test_telegram_channel import make_config
        with self.assertRaises(ConfigError):
            make_config(state={"links": "registry"})


class ReviewFixesTests(unittest.TestCase):
    """#279 review: gone topics, the admin's /topic, and the GitHub check's cap."""

    def link(self):
        return RoutingAndGitHubTests().link_with_topics()

    def run_spawned(self, link, call):
        async def go():
            call()
            await asyncio.gather(*getattr(link, "_tasks", set()))
        asyncio.run(go())

    def test_a_deleted_topic_routes_nothing_and_its_record_closes(self):
        link = self.link()
        self.run_spawned(link, lambda: link.topic_gone(55, deleted=True))
        self.assertEqual(link.topic_for_refs(["#12"]), (-100, 66), "the next topic's link")
        record = link.store.read("topic/55")
        self.assertEqual((record["status"], bool(record["closed_at"])), ("closed", True))
        self.assertEqual(record["history"][-1]["to"], "closed")

    def test_a_closed_topic_is_skipped_here_but_its_record_stays(self):
        link = self.link()
        self.run_spawned(link, lambda: link.topic_gone(55, deleted=False))
        self.assertEqual(link.topic_for_refs(["#12"]), (-100, 66))
        self.assertEqual(link.store.read("topic/55")["status"], "open")

    def test_a_closed_topic_is_routed_again_once_a_message_is_seen_there(self):
        link = self.link()
        link.topic_gone(55, deleted=False)
        link.topic_seen(55)
        self.assertEqual(link.topic_for_refs(["#12"]), (-100, 55))

    def test_a_closed_topics_items_are_still_checked_on_github(self):
        from unittest import mock
        from swarmly.github.client import GitHubClient
        link = self.link()
        link.topic_gone(55, deleted=False)
        with mock.patch.object(GitHubClient, "ref_kind_state",
                               lambda self, repo, number: ("closed", False) if number == 12
                               else None):
            self.assertEqual(asyncio.run(link.reconcile_github(link.store)), 1)
        self.assertEqual(Topic.from_dict(link.store.read("topic/55")).link("L1").status,
                         "closed")

    def test_only_a_refusal_that_says_deleted_means_deleted(self):
        from swarmly.telegram.channel import _is_topic_deleted
        self.assertFalse(_is_topic_deleted(Exception("Bad Request: TOPIC_CLOSED")))
        self.assertTrue(_is_topic_deleted(Exception("Bad Request: message thread not found")))
        self.assertTrue(_is_topic_deleted(Exception("Bad Request: TOPIC_DELETED")))
        self.assertFalse(_is_topic_deleted(None))

    def test_the_channel_tells_the_registry_a_topic_is_gone(self):
        from tests.test_telegram_channel import GROUP, make_channel, make_config
        channel = make_channel(make_config(state={"backend": "telegram"}))
        told = []
        channel.state_link.topic_gone = lambda thread, deleted: told.append((thread, deleted))
        channel.forget_topic_state(GROUP, 55)
        self.assertEqual(told, [(55, True)])

    def test_the_admins_topic_pin_outranks_an_older_work_link(self):
        link = self.link()
        link.clock.t += 60                              # the pin comes after the /link above
        self.run_spawned(link, lambda: link.note_pin(-100, 44, ["#12"], 1))
        self.assertEqual(link.topic_for_refs(["#12"]), (-100, 44))
        pinned = Topic.from_dict(link.store.read("topic/44")).link("L1")
        self.assertEqual((pinned.role, pinned.added_by), ("work", "admin:1"))

    def test_the_newest_work_link_wins_and_a_pin_re_adds_an_existing_one(self):
        link = self.link()

        async def coordinator_links():
            link.note_reply(-100, 33, "LINK: #12 as work")
            await asyncio.gather(*link._tasks)
        link.clock.t += 60
        asyncio.run(coordinator_links())
        self.assertEqual(Topic.from_dict(link.store.read("topic/33")).link("L1").added_by,
                         "claudebot")
        self.assertEqual(link.topic_for_refs(["#12"]), (-100, 33), "work moved to topic 33")
        link.clock.t += 60
        self.run_spawned(link, lambda: link.note_pin(-100, 55, ["#12"], 1))
        self.assertEqual(link.topic_for_refs(["#12"]), (-100, 55), "the admin moved it back")
        link.clock.t += 60
        self.run_spawned(link, lambda: link.note_pin(-100, 33, ["#12"], 1))
        pinned = Topic.from_dict(link.store.read("topic/33")).link("L1")
        self.assertEqual((pinned.added_by, len(Topic.from_dict(
            link.store.read("topic/33")).links)), ("admin:1", 1), "re-added, not duplicated")
        self.assertEqual(link.topic_for_refs(["#12"]), (-100, 33))

    def test_the_channel_records_the_pin(self):
        from tests.test_telegram_channel import ADMIN_ID, GROUP, make_channel, make_config
        channel = make_channel(make_config(state={"backend": "telegram"}))
        pins = []
        channel.state_link.note_pin = lambda *args: pins.append(args)
        channel._handle_topic_pin(SimpleNamespace(
            text="/topic #164", author_is_bot=False, author_id=ADMIN_ID, chat_id=GROUP,
            thread_id=55))
        self.assertEqual(pins, [(GROUP, 55, ["#164"], ADMIN_ID)])

    def test_the_cap_goes_to_the_links_asked_about_longest_ago(self):
        from unittest import mock
        from swarmly.github.client import GitHubClient
        link = self.link()
        link.GITHUB_MAX_ITEMS = 2
        asked = []

        def state(_self, repo, number):
            asked.append((repo, number))
            return None
        with mock.patch.object(GitHubClient, "ref_kind_state", state):
            asyncio.run(link.reconcile_github(link.store))
            first = list(asked)
            asyncio.run(link.reconcile_github(link.store))
        self.assertEqual(len(first), 2)
        self.assertEqual(sorted(asked), [("o/r", 12), ("o/r", 13), ("o/r", 40),
                                         ("x/other", 41)], "each link once, in its own repo")

    def test_an_item_linked_in_two_topics_is_asked_about_once_for_both(self):
        from unittest import mock
        from swarmly.github.client import GitHubClient
        link = self.link()
        asyncio.run(link.registry_command(SimpleNamespace(
            text="/link #12 as work", author_id=1, author_is_bot=False, chat_id=-100,
            thread_id=99)))
        link.GITHUB_MAX_ITEMS = 1
        asked = []

        def state(_self, repo, number):
            asked.append(number)
            return ("closed", False) if number == 12 else None
        with mock.patch.object(GitHubClient, "ref_kind_state", state):
            asyncio.run(link.reconcile_github(link.store))
        self.assertEqual(asked, [12])
        for topic in ("topic/55", "topic/99"):
            work = [item for item in Topic.from_dict(link.store.read(topic)).links
                    if item.ref.endswith("/12")]
            self.assertEqual(work[0].status, "closed", topic)

    def test_a_pr_found_by_its_short_ref_is_relabelled_even_when_nothing_moves(self):
        from unittest import mock
        from swarmly.github.client import GitHubClient
        link = self.link()
        asyncio.run(link.registry_command(SimpleNamespace(
            text="/phase #13 review", author_id=1, author_is_bot=False, chat_id=-100,
            thread_id=88)))
        with mock.patch.object(GitHubClient, "ref_kind_state",
                               lambda self, repo, number: ("open", True) if number == 13
                               else None):
            self.assertEqual(asyncio.run(link.reconcile_github(link.store)), 0, "nothing moved")
        self.assertEqual(Topic.from_dict(link.store.read("topic/88")).link("L1").kind, "pr")

    def test_an_unreadable_topic_record_does_not_stop_the_check(self):
        from unittest import mock
        from swarmly.github.client import GitHubClient
        link = self.link()
        bad = dict(link.store.read("topic/55"), topic_id=11, status="bogus")
        asyncio.run(link.store.write("topic/11", bad))
        with mock.patch.object(GitHubClient, "ref_kind_state",
                               lambda self, repo, number: ("closed", False) if number == 12
                               else None):
            moved = asyncio.run(link.reconcile_github(link.store))
        self.assertEqual(moved, 1)
        t55 = Topic.from_dict(link.store.read("topic/55"))
        self.assertEqual(t55.link("L1").status, "closed")


class TopicsShowWorkItemsTests(unittest.TestCase):
    def test_describe(self):
        from swarmly.telegram.registry_shadow import RegistryShadow
        clock = Clock()
        claude = channel_for("Claudebot", clock)
        seen(claude, 7, "t")
        text = RegistryShadow._link_text({"id": "L2", "title": "#256", "role": "work",
                                          "phase": "review",
                                          "assignees": [{"agent": "DeepSeek",
                                                         "as": "developer"}]})
        self.assertEqual(text, "L2 #256 review [DeepSeek developer]")


if __name__ == "__main__":
    unittest.main()
