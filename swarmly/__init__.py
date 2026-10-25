"""
Swarmly: coordinate coding-agent CLIs from different vendors, on different machines.

The import package was ``a2a`` before the rename; the wire identifiers (``a2a/v1``,
``a2a/2``) and the ``A2A_*`` environment variables keep their names.
"""

__version__ = "0.11.0"
__protocol__ = "a2a/v1.0"

from swarmly.models import (
    A2AMessage,
    SenderInfo,
    RecipientInfo,
    MessageIntent,
    MessagePriority,
    TaskClaimPayload,
    TaskHandoffPayload,
    PRReviewRequestPayload,
    PRReviewVerdictPayload,
    FalsificationPayload,
)
from swarmly.crypto import CryptoAuth, KeyPair
from swarmly.bus import DurableBus
from swarmly.client import A2AClient

__all__ = [
    "A2AMessage",
    "SenderInfo",
    "RecipientInfo",
    "MessageIntent",
    "MessagePriority",
    "TaskClaimPayload",
    "TaskHandoffPayload",
    "PRReviewRequestPayload",
    "PRReviewVerdictPayload",
    "FalsificationPayload",
    "CryptoAuth",
    "KeyPair",
    "DurableBus",
    "A2AClient",
]
