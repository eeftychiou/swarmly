import unittest
from pathlib import Path

from tests.swarm_helpers import SwarmCase, needs_swarm

ROOT = Path(__file__).resolve().parents[1]


@needs_swarm
class ProtocolFileTests(unittest.TestCase):
    def test_every_check_is_implemented_and_documented(self):
        from swarmly.swarm.doc import CHECK_DOCS
        from swarmly.swarm.projection import Projection
        from swarmly.swarm.protocol import load_protocol

        proto = load_protocol()
        used = {c for r in proto.intents.values() for c in r.checks}
        self.assertTrue(used)
        for check in used:
            self.assertTrue(hasattr(Projection, f"_check_{check}"), check)
            self.assertIn(check, CHECK_DOCS)

    def test_every_transition_names_a_known_state(self):
        from swarmly.swarm.protocol import load_protocol
        proto = load_protocol()
        for name, rule in proto.intents.items():
            if rule.to_state and rule.to_state != "@verdict":
                self.assertIn(rule.to_state, proto.states, name)

    def test_generated_doc_matches_protocol_yaml(self):
        from swarmly.swarm.doc import render_default
        committed = (ROOT / "docs" / "SWARM-PROTOCOL.md").read_text(encoding="utf-8")
        self.assertEqual(committed, render_default(),
                         "docs/SWARM-PROTOCOL.md drifted: run `swarmly protocol-doc "
                         "--out docs/SWARM-PROTOCOL.md`")

    def test_policy_override_merges(self):
        from swarmly.swarm.protocol import load_protocol
        proto = load_protocol({"stuck": {"max_dispute_rounds": 5}})
        self.assertEqual(proto.stuck("max_dispute_rounds"), 5)
        self.assertEqual(proto.stuck("max_task_failures"), 3)

    def test_safety_overrides_only_add_categories(self):
        from swarmly.swarm.protocol import SwarmConfigError, load_protocol
        defaults = {c.id for c in load_protocol().safety}
        self.assertIn("force_push", defaults)
        self.assertEqual({c.id for c in load_protocol({"safety": []}).safety}, defaults)
        extra = load_protocol({"safety": [{"id": "deploy", "summary": "Deploying",
                                           "commands": ["^make deploy"]}]})
        self.assertEqual({c.id for c in extra.safety}, defaults | {"deploy"})
        with self.assertRaises(SwarmConfigError):
            load_protocol({"safety": [{"id": "force_push", "summary": "x",
                                       "commands": ["^never$"]}]})

    def test_a_safety_category_needs_a_new_text_id(self):
        from swarmly.swarm.protocol import SwarmConfigError, load_protocol
        for safety in ([{"summary": "no id", "commands": ["^x"]}], ["deploy"],
                       [{"id": "a", "summary": "x", "commands": ["^x"]},
                        {"id": "a", "summary": "y", "commands": ["^y"]}]):
            with self.subTest(safety=safety):
                with self.assertRaises(SwarmConfigError):
                    load_protocol({"safety": safety})

    def test_bad_safety_regex_is_a_config_error(self):
        from swarmly.swarm.protocol import SwarmConfigError, load_protocol
        with self.assertRaises(SwarmConfigError):
            load_protocol({"safety": [{"id": "x", "commands": ["("]}]})


@needs_swarm
class SwarmFileTests(SwarmCase):
    def test_roles_come_from_owner_and_planner(self):
        self.assertEqual(self.swarm.role_of("alice"), "owner")
        self.assertEqual(self.swarm.role_of("claude"), "planner")
        self.assertEqual(self.swarm.role_of("codex"), "worker")

    def test_owner_cannot_be_planner(self):
        from swarmly.swarm.protocol import SwarmConfigError, swarm_from_dict
        cfg = dict(self.config, planner="alice")
        with self.assertRaises(SwarmConfigError):
            swarm_from_dict(cfg)

    def test_agent_needs_vendor(self):
        from swarmly.swarm.protocol import SwarmConfigError, swarm_from_dict
        agents = dict(self.config["agents"], extra={"key": "x"})
        with self.assertRaises(SwarmConfigError):
            swarm_from_dict(dict(self.config, agents=agents))

    def test_load_swarm_from_file(self):
        from swarmly.swarm.protocol import load_swarm
        swarm = load_swarm(str(self.write_swarm_yaml()))
        self.assertEqual(swarm.planner, "claude")
        self.assertEqual(swarm.standby, "hermes")


if __name__ == "__main__":
    unittest.main()
