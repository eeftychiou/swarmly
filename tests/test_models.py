"""
Unit tests for A2A Message models, serialization, and YAML frontmatter.
"""

import json
import unittest
from swarmly.models import (
    A2AMessage,
    MessageIntent,
    MessagePriority,
    TaskClaimPayload,
    TaskHandoffPayload,
    PRReviewRequestPayload,
    PRReviewVerdictPayload,
    FalsificationPayload,
)


class TestA2AModels(unittest.TestCase):

    def test_message_creation_and_json_roundtrip(self):
        payload = TaskClaimPayload(
            task_id="TASK-017",
            topic="Same-stand arrival turnaround features",
            branch="agents/hermes/turnaround",
            claimed_files=["src/features/builder.py"],
            rationale="Captures 24h stand-idle gap"
        )
        msg = A2AMessage.create(
            sender_id="hermes",
            intent=MessageIntent.TASK_CLAIM,
            payload=payload,
            recipient_id="*",
            priority=MessagePriority.HIGH,
            correlation_id="issue-25"
        )

        self.assertEqual(msg.protocol, "a2a/v1.0")
        self.assertEqual(msg.sender.agent_id, "hermes")
        self.assertEqual(msg.intent, MessageIntent.TASK_CLAIM)
        self.assertEqual(msg.priority, MessagePriority.HIGH)
        self.assertEqual(msg.payload["task_id"], "TASK-017")

        # JSON Roundtrip
        json_str = msg.to_json()
        deserialized = A2AMessage.from_json(json_str)

        self.assertEqual(deserialized.message_id, msg.message_id)
        self.assertEqual(deserialized.sender.agent_id, "hermes")
        self.assertEqual(deserialized.intent, MessageIntent.TASK_CLAIM)
        self.assertEqual(deserialized.payload["task_id"], "TASK-017")

    def test_yaml_frontmatter_roundtrip(self):
        payload = {"falsified": True, "delta_lb": "+1.72s", "finding": "0.554 transfer factor rejected"}
        msg = A2AMessage.create(
            sender_id="deepseek",
            intent=MessageIntent.FALSIFICATION_REPORT,
            payload=payload,
            recipient_id="antigravity",
            priority=MessagePriority.BLOCKING
        )

        frontmatter = msg.to_yaml_frontmatter(body_markdown="## Analysis Report\nEmpirical Bayes delta did not transport.")
        self.assertTrue(frontmatter.startswith("---"))
        self.assertIn("intent: FALSIFICATION_REPORT", frontmatter)
        self.assertIn("sender:", frontmatter)
        self.assertIn("agent_id: deepseek", frontmatter)

        # Parse back
        parsed = A2AMessage.from_yaml_frontmatter(frontmatter)
        self.assertEqual(parsed.message_id, msg.message_id)
        self.assertEqual(parsed.sender.agent_id, "deepseek")
        self.assertEqual(parsed.recipient.agent_id, "antigravity")
        self.assertEqual(parsed.intent, MessageIntent.FALSIFICATION_REPORT)
        self.assertEqual(parsed.priority, MessagePriority.BLOCKING)
        self.assertEqual(parsed.payload["delta_lb"], "+1.72s")

    def test_all_typed_payloads(self):
        handoff = TaskHandoffPayload(
            task_id="TASK-015",
            git_commit_sha="178a641",
            description="Run calibration upload",
            pre_registered_hypothesis="Within bounds",
            expected_bounds=[421.7, 422.69]
        )
        self.assertEqual(handoff.to_dict()["expected_bounds"], [421.7, 422.69])

        review_req = PRReviewRequestPayload(
            pr_number=46,
            branch="agents/hermes/archive",
            files_modified=["scripts/analysis/README.md"]
        )
        self.assertEqual(review_req.to_dict()["pr_number"], 46)

        review_res = PRReviewVerdictPayload(
            pr_number=46,
            verdict="APPROVED",
            summary="All 23 scripts present and verified."
        )
        self.assertEqual(review_res.to_dict()["verdict"], "APPROVED")


if __name__ == "__main__":
    unittest.main()
