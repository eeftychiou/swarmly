"""
Base abstract adapter defining interface for heterogeneous agent frameworks.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from swarmly.models import A2AMessage, MessageIntent, MessagePriority
from swarmly.client import A2AClient


class BaseAgentAdapter(ABC):
    """Abstract adapter connecting a specific agent framework to Swarmly."""

    def __init__(self, client: A2AClient):
        self.client = client
        self.agent_id = client.agent_id

    @abstractmethod
    def export_tools(self) -> List[Dict[str, Any]]:
        """Export tool definitions suitable for the framework's tool-calling API."""
        pass

    async def execute_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a tool call triggered by the underlying LLM."""
        if tool_name == "a2a_send_message":
            msg = await self.client.send_message(
                intent=arguments.get("intent", MessageIntent.QUERY_REQUEST),
                payload=arguments.get("payload", {}),
                recipient_id=arguments.get("recipient_id", "*"),
                priority=arguments.get("priority", MessagePriority.ROUTINE),
                correlation_id=arguments.get("correlation_id"),
            )
            return {"status": "sent", "message_id": msg.message_id}

        elif tool_name == "a2a_read_inbox":
            messages = self.client.bus.read_inbox(verify_signatures=True)
            return {
                "count": len(messages),
                "messages": [
                    {
                        "message_id": m.message_id,
                        "sender": m.sender.agent_id,
                        "intent": m.intent.value,
                        "priority": m.priority.value,
                        "payload": m.payload,
                        "is_valid": valid,
                        "status": status,
                    }
                    for m, valid, status in messages
                ]
            }

        return {"error": f"Unknown tool: {tool_name}"}
