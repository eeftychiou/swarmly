"""
Multi-Agent Framework Adapters for Swarmly.
"""

from swarmly.adapters.base import BaseAgentAdapter
from swarmly.adapters.openai_swarm import OpenAISwarmAdapter
from swarmly.adapters.deepseek import DeepSeekAdapter
from swarmly.adapters.gemini import GeminiAdapter
from swarmly.adapters.hermes import HermesAdapter

__all__ = [
    "BaseAgentAdapter",
    "OpenAISwarmAdapter",
    "DeepSeekAdapter",
    "GeminiAdapter",
    "HermesAdapter",
]
