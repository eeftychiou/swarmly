"""
Hermes and Local vLLM Agent Adapter for Swarmly.
"""

from __future__ import annotations
from typing import Any, Dict, List
from swarmly.adapters.base import BaseAgentAdapter
from swarmly.models import MessageIntent, MessagePriority


class HermesAdapter(BaseAgentAdapter):
    """Adapter for Hermes agents running on Linux/vLLM compute nodes."""

    def export_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "a2a_claim_task",
                "description": "Claim a research or feature engineering task atomically.",
                "parameters": {
                    "type": "object",
                    "required": ["task_id", "topic"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "topic": {"type": "string"},
                        "branch": {"type": "string"},
                        "claimed_files": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"}
                    }
                }
            }
        ]

    async def claim_task(self, task_id: str, topic: str, branch: str, files: List[str], rationale: str):
        payload = {
            "task_id": task_id,
            "topic": topic,
            "branch": branch,
            "claimed_files": files,
            "rationale": rationale
        }
        return await self.client.send_message(
            intent=MessageIntent.TASK_CLAIM,
            payload=payload,
            recipient_id="*",
            priority=MessagePriority.HIGH
        )
