"""The swarm kernel: one protocol file, signed envelopes, per-agent lanes, one write path.

Agents propose; code decides. Every write goes through :class:`Governor`, which checks
the whole envelope against ``protocol.yaml``, signs it with the agent's Ed25519 key and
appends it to that agent's own lane. Reads replay every lane through
:class:`Projection`, which enforces the same rules again and computes task state,
obligations, legal next moves and escalations for the owner.

Needs the ``swarm`` extra: ``pip install "swarmly[swarm]"``.
"""

from __future__ import annotations

from .envelope import Envelope, EnvelopeError
from .governor import Governor, Rejected
from .lanes import DirLaneStore, GitLaneStore, LaneConflict, LaneStore
from .projection import Projection
from .protocol import Protocol, Swarm, SwarmConfigError, load_swarm

__all__ = [
    "DirLaneStore", "Envelope", "EnvelopeError", "GitLaneStore", "Governor", "LaneConflict",
    "LaneStore", "Projection", "Protocol", "Rejected", "Swarm", "SwarmConfigError", "load_swarm",
]
