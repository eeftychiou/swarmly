"""Per-host Ed25519 keys. There is no default identity: every caller names its agent.

The private key for agent ``x`` lives in ``$A2A_KEY_DIR/x.key`` (default
``~/.a2a/keys``), base64 raw bytes, mode 0600. The matching public key goes in the
project's ``swarm.yaml`` roster.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Optional, Tuple

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError:  # pragma: no cover - exercised only without the swarm extra
    ed25519 = None  # type: ignore[assignment]

from .protocol import AGENT_RE, SwarmConfigError


def _need_crypto() -> None:
    if ed25519 is None:
        raise SwarmConfigError(
            'the swarm kernel signs with Ed25519: pip install "swarmly[swarm]"')


def key_dir() -> Path:
    return Path(os.environ.get("A2A_KEY_DIR") or "~/.a2a/keys").expanduser()


def key_path(agent: str) -> Path:
    if not AGENT_RE.match(agent):
        raise SwarmConfigError(f"bad agent name {agent!r}")
    return key_dir() / f"{agent}.key"


def generate(agent: str, *, overwrite: bool = False) -> Tuple[Path, str]:
    """Create ``agent``'s key file and return (path, base64 public key)."""
    _need_crypto()
    path = key_path(agent)
    if path.exists() and not overwrite:
        raise SwarmConfigError(f"{path} already exists")
    priv = ed25519.Ed25519PrivateKey.generate()
    raw = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(base64.b64encode(raw).decode("ascii") + "\n")
    return path, public_key_b64(priv)


def public_key_b64(priv: "ed25519.Ed25519PrivateKey") -> str:
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def load_private(agent: str) -> "ed25519.Ed25519PrivateKey":
    _need_crypto()
    path = key_path(agent)
    try:
        raw = base64.b64decode(path.read_text(encoding="ascii").strip(), validate=True)
    except FileNotFoundError:
        raise SwarmConfigError(
            f"no key for {agent!r} at {path}: run `swarmly keygen --as {agent}`") from None
    except ValueError:
        raise SwarmConfigError(f"{path}: not a base64 Ed25519 key") from None
    if len(raw) != 32:
        raise SwarmConfigError(f"{path}: expected 32 key bytes, got {len(raw)}")
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw)


def sign(priv: "ed25519.Ed25519PrivateKey", data: bytes) -> str:
    return base64.b64encode(priv.sign(data)).decode("ascii")


def verify(public_b64: Optional[str], data: bytes, sig_b64: str) -> bool:
    _need_crypto()
    if not public_b64:
        return False
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        pub.verify(base64.b64decode(sig_b64), data)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
