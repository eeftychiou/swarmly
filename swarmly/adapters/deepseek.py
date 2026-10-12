"""
DeepSeek Adapter for Swarmly.
Supports both programmatic integration and CLI Aider/runner workflows.
"""

from __future__ import annotations
from typing import Any, Dict, List
from swarmly.adapters.base import BaseAgentAdapter
from swarmly.models import MessageIntent, MessagePriority


class DeepSeekAdapter(BaseAgentAdapter):
    """Adapter for DeepSeek-R1 and DeepSeek-V3 reasoning agents."""

    def export_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "a2a_send_falsification_report",
                "description": "Publish a formal falsification or statistical audit to the swarm with citations.",
                "parameters": {
                    "type": "object",
                    "required": ["hypothesis", "predicted_metric", "measured_metric", "verdict"],
                    "properties": {
                        "hypothesis": {"type": "string"},
                        "predicted_metric": {"type": "string"},
                        "measured_metric": {"type": "string"},
                        "bounds_violated": {"type": "boolean"},
                        "verdict": {"type": "string"},
                        "citation_scripts": {"type": "array", "items": {"type": "string"}}
                    }
                }
            }
        ]

    async def publish_falsification(
        self,
        hypothesis: str,
        predicted_metric: str,
        measured_metric: str,
        bounds_violated: bool,
        verdict: str,
        citation_scripts: List[str]
    ):
        """Helper to post a typed falsification report to all agents."""
        payload = {
            "hypothesis": hypothesis,
            "predicted_metric": predicted_metric,
            "measured_metric": measured_metric,
            "bounds_violated": bounds_violated,
            "verdict": verdict,
            "citation_scripts": citation_scripts
        }
        return await self.client.send_message(
            intent=MessageIntent.FALSIFICATION_REPORT,
            payload=payload,
            recipient_id="*",
            priority=MessagePriority.HIGH
        )
