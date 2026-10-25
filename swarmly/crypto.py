"""
Zero-Trust Cryptographic Identity and Authentication module for Swarmly.
Supports HMAC-SHA256 (standard library) and optional Ed25519 asymmetric signing.
"""

from __future__ import annotations
import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple

#: Review P1-3: the prefix of a signature over the whole envelope (a2a/v1 bus messages).
#: A signature without it is a v1 signature over the payload alone.
SIGNATURE_V2 = "v2:"


# Optional Ed25519 support via cryptography library
try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    HAS_ED25519 = True
except ImportError:
    HAS_ED25519 = False


@dataclass
class KeyPair:
    agent_id: str
    key_type: str  # "hmac" or "ed25519"
    private_key: str
    public_key: str

    @classmethod
    def generate_hmac(cls, agent_id: str) -> KeyPair:
        """Generate a secure 256-bit random symmetric secret."""
        secret = secrets.token_hex(32)
        return cls(agent_id=agent_id, key_type="hmac", private_key=secret, public_key=secret)

    @classmethod
    def generate_ed25519(cls, agent_id: str) -> KeyPair:
        """Generate an Ed25519 asymmetric keypair."""
        if not HAS_ED25519:
            # Never hand out an HMAC secret to someone who asked for a public key.
            raise RuntimeError("Ed25519 keys need the 'cryptography' package")
        
        priv = ed25519.Ed25519PrivateKey.generate()
        pub = priv.public_key()
        
        priv_bytes = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )
        pub_bytes = pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        
        priv_b64 = base64.b64encode(priv_bytes).decode("ascii")
        pub_b64 = base64.b64encode(pub_bytes).decode("ascii")
        return cls(agent_id=agent_id, key_type="ed25519", private_key=priv_b64, public_key=pub_b64)


class CryptoAuth:
    """Zero-Trust authentication engine with replay protection and nonce challenges."""

    def __init__(self, nonce_ttl_seconds: int = 15):
        self.nonce_ttl = nonce_ttl_seconds
        self._seen_nonces: Dict[str, float] = {}
        self._seen_msg_ids: Set[str] = set()

    def clean_expired_nonces(self) -> None:
        """Prune nonces that have exceeded their TTL."""
        now = time.time()
        expired = [n for n, exp in self._seen_nonces.items() if exp < now]
        for n in expired:
            del self._seen_nonces[n]

    def create_challenge_nonce(self) -> Tuple[str, str]:
        """Generate an ephemeral challenge nonce with expiration timestamp."""
        self.clean_expired_nonces()
        token = secrets.token_urlsafe(24)
        expires_at = time.time() + self.nonce_ttl
        nonce_str = f"{token}:{int(expires_at)}"
        self._seen_nonces[nonce_str] = expires_at
        return token, nonce_str

    @staticmethod
    def canonical_bytes(payload: Dict) -> bytes:
        """Deterministically serialize payload dictionary to UTF-8 bytes."""
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def sign_payload(cls, payload: Dict, private_key: str, key_type: str = "hmac") -> str:
        """Sign a payload dictionary using HMAC-SHA256 or Ed25519.

        Raises ``ValueError`` without a key or with a key that does not fit ``key_type``:
        a message is never signed with some other algorithm than the one asked for.
        """
        if not private_key:
            raise ValueError("no signing key")
        data_bytes = cls.canonical_bytes(payload)

        if key_type == "ed25519":
            if not HAS_ED25519:
                raise RuntimeError("Ed25519 signing needs the 'cryptography' package")
            try:
                priv_bytes = base64.b64decode(private_key.encode("ascii"), validate=True)
                priv = ed25519.Ed25519PrivateKey.from_private_bytes(priv_bytes)
            except Exception as exc:
                raise ValueError(f"not an Ed25519 private key: {type(exc).__name__}") from None
            sig_bytes = priv.sign(data_bytes)
            return "ed25519:" + base64.b64encode(sig_bytes).decode("ascii")
        if key_type != "hmac":
            raise ValueError(f"unknown key type {key_type!r}")

        mac = hmac.new(private_key.encode("utf-8"), data_bytes, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{mac}"

    @classmethod
    def verify_payload(cls, payload: Dict, signature: str, public_key: str,
                       key_type: str) -> bool:
        """Verify a payload signature with the sender's registered key, in constant time.

        The algorithm comes from the registered ``key_type``, never from the signature: an
        Ed25519 agent's public key is public, so accepting an ``hmac-sha256:`` signature
        for it would let anyone sign as that agent. *key_type* therefore has no default.
        """
        if not signature or not public_key:
            return False

        data_bytes = cls.canonical_bytes(payload)

        if key_type == "ed25519":
            if not signature.startswith("ed25519:"):
                return False
            if not HAS_ED25519:
                raise RuntimeError("Ed25519 verification needs the 'cryptography' package")
            try:
                sig_raw = base64.b64decode(signature[len("ed25519:"):].encode("ascii"),
                                           validate=True)
                pub_raw = base64.b64decode(public_key.encode("ascii"), validate=True)
                pub = ed25519.Ed25519PublicKey.from_public_bytes(pub_raw)
                pub.verify(sig_raw, data_bytes)
                return True
            except Exception:
                return False

        if key_type == "hmac" and signature.startswith("hmac-sha256:"):
            mac_expected = signature[len("hmac-sha256:"):]
            mac_computed = hmac.new(public_key.encode("utf-8"), data_bytes,
                                    hashlib.sha256).hexdigest()
            return hmac.compare_digest(mac_expected, mac_computed)
        return False                       # no prefix (legacy raw hex) or a mismatch

    @classmethod
    def sign_envelope(cls, fields: Dict, private_key: str, key_type: str) -> str:
        """Sign a whole envelope (review P1-3): *fields* is
        :meth:`~swarmly.models.A2AMessage.signed_fields`, so the header (sender, recipient,
        id, time, intent) is covered as well as the payload. The result carries a
        ``v2:`` prefix that tells it apart from a v1 payload-only signature."""
        return SIGNATURE_V2 + cls.sign_payload(fields, private_key, key_type)

    @classmethod
    def verify_envelope(cls, fields: Dict, signature: Any, public_key: str,
                        key_type: str) -> bool:
        """Verify a ``v2:`` signature over *fields*; anything else is False."""
        if not isinstance(signature, str) or not signature.startswith(SIGNATURE_V2):
            return False
        return cls.verify_payload(fields, signature[len(SIGNATURE_V2):], public_key, key_type)

    def verify_challenge_response(
        self,
        nonce_str: str,
        signature: str,
        public_key: str,
        key_type: str,
    ) -> bool:
        """Verify that an agent correctly solved an ephemeral challenge nonce.

        *key_type* is the registered one and has no default: a caller that forgot it
        must not silently verify an Ed25519 agent's public key as an HMAC secret."""
        if not nonce_str or not signature or not public_key:
            return False

        # 1. Check expiration
        try:
            _, exp_str = nonce_str.split(":", 1)
            expires_at = float(exp_str)
            if time.time() > expires_at:
                return False  # Expired
        except Exception:
            return False

        # 2. Check nonce validity and consume it (replay protection)
        if nonce_str not in self._seen_nonces:
            return False
        del self._seen_nonces[nonce_str]

        # 3. Verify signature of the nonce string
        payload = {"nonce": nonce_str}
        return self.verify_payload(payload, signature, public_key, key_type)

    def is_message_replay(self, message_id: str) -> bool:
        """Detect and register message IDs to prevent duplicate replay."""
        if message_id in self._seen_msg_ids:
            return True
        self._seen_msg_ids.add(message_id)
        # Prune if too large
        if len(self._seen_msg_ids) > 10000:
            self._seen_msg_ids.clear()
        return False

    @staticmethod
    def is_revoked(agent_id: str, revocation_list: list) -> bool:
        """Check if an agent ID or key is on the cluster revocation blacklist."""
        return agent_id in revocation_list
