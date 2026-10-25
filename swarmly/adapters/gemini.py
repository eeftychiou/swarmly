"""
Google Gemini and Antigravity Adapter for Swarmly.
"""

from __future__ import annotations
from typing import Any, Dict, List
from swarmly.adapters.base import BaseAgentAdapter
from swarmly.models import MessageIntent, MessagePriority


class GeminiAdapter(BaseAgentAdapter):
    """Adapter for Google Gemini and Antigravity agents."""

    def export_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "a2a_handoff_task",
                "description": "Formally hand off an experimental task to a peer agent with pre-registered bounds.",
                "parameters": {
                    "type": "object",
                    "required": ["task_id", "git_commit_sha", "description", "pre_registered_hypothesis", "expected_bounds"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "git_commit_sha": {"type": "string"},
                        "description": {"type": "string"},
                        "pre_registered_hypothesis": {"type": "string"},
                        "expected_bounds": {"type": "array", "items": {"type": "number"}},
                        "verification_command": {"type": "string"},
                        "target_output": {"type": "string"}
                    }
                }
            }
        ]

    async def handoff_task(
        self,
        task_id: str,
        git_commit_sha: str,
        description: str,
        hypothesis: str,
        bounds: List[float],
        cmd: str = "",
        target: str = "",
        recipient_id: str = "hermes"
    ):
        payload = {
            "task_id": task_id,
            "git_commit_sha": git_commit_sha,
            "description": description,
            "pre_registered_hypothesis": hypothesis,
            "expected_bounds": bounds,
            "verification_command": cmd,
            "target_output": target
        }
        return await self.client.send_message(
            intent=MessageIntent.TASK_HANDOFF,
            payload=payload,
            recipient_id=recipient_id,
            priority=MessagePriority.HIGH
        )
