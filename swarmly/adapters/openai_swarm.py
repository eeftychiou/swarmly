"""
OpenAI Swarm and Function Calling Adapter for Swarmly.
Exports tools conforming to standard OpenAI Chat Completion JSON schemas.
"""

from __future__ import annotations
from typing import Any, Dict, List
from swarmly.adapters.base import BaseAgentAdapter


class OpenAISwarmAdapter(BaseAgentAdapter):
    """Adapter for ChatGPT, OpenAI Swarm, AutoGen, and CrewAI."""

    def export_tools(self) -> List[Dict[str, Any]]:
        """Export tool specifications in standard OpenAI function format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "a2a_send_message",
                    "description": "Send a verified A2A message to the swarm (Hermes, DeepSeek, Antigravity, MiniMax) via WebSocket or Git.",
                    "parameters": {
                        "type": "object",
                        "required": ["intent", "payload"],
                        "properties": {
                            "intent": {
                                "type": "string",
                                "enum": [
                                    "TASK_PROPOSE", "TASK_CLAIM", "TASK_APPROVE", "TASK_HANDOFF",
                                    "PR_REVIEW_REQUEST", "PR_REVIEW_VERDICT", "FALSIFICATION_REPORT",
                                    "EXPERIMENT_REGISTER", "QUERY_REQUEST", "QUERY_RESPONSE", "ESCALATE_BLOCKER"
                                ],
                                "description": "The operational intent of this message"
                            },
                            "recipient_id": {
                                "type": "string",
                                "description": "Target agent ID (e.g. 'antigravity', 'hermes', 'deepseek') or '*' for broadcast",
                                "default": "*"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["ROUTINE", "HIGH", "BLOCKING"],
                                "default": "ROUTINE"
                            },
                            "correlation_id": {
                                "type": "string",
                                "description": "Optional correlation ID linking to a task or PR"
                            },
                            "payload": {
                                "type": "object",
                                "description": "Structured message payload"
                            }
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "a2a_read_inbox",
                    "description": "Read and cryptographically verify all pending incoming messages from the swarm inbox.",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    }
                }
            }
        ]
