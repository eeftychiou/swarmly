"""``swarmly-keygen``: create an agent's bus signing key and register it.

Onboarding an agent onto the durable bus takes three things: its private key on its own
host (``A2A_KEY_<AGENT>``), its entry in the registry (``config/a2a_config.json``) that
everyone else verifies it against, and, for HMAC only, the same secret on every host that
reads its messages. With ``--env-file`` and ``--registry`` this command does the first two
in place and never prints the private key::

    swarmly-keygen --agent alice --env-file ~/.a2a-telegram.env --registry config/a2a_config.json

Ed25519 is the default: only the public key goes in the registry, so nothing secret is
shared or committed. When the .env already holds the agent's key, that key is registered
rather than replaced (an existing peer onboards without rotating); ``--force`` makes a new
one. Without ``--env-file`` the key is printed, as the old ``scripts/generate_keys.py`` did.

Both files are checked before either is written, and the .env is written before the
registry, so a refusal never leaves a registered key whose private half was thrown away.
On Windows the owner-only mode is not enforced by chmod; keep the .env in the user profile.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from swarmly import crypto
from swarmly.bus import key_env_name
from swarmly.models import AGENT_ID


class KeygenError(Exception):
    """A refusal with a message for the person running the command."""


def _target(path: Path) -> Path:
    """Where a write to *path* lands: through a symlink to the file it points at."""
    return path.resolve() if path.is_symlink() else path


def _replace_file(path: Path, text: str, mode: int) -> None:
    """Write *text* to *path* in one step (temp file + rename) with permission *mode*."""
    path = _target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _env_lines(path: Path) -> List[str]:
    # utf-8-sig: a byte-order mark (Notepad) must not hide the first line's name.
    return path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []


def _env_pattern(name: str) -> "re.Pattern[str]":
    return re.compile(rf"^\s*(export\s+)?{re.escape(name)}\s*=(.*)$")


def read_env_key(path: Path, name: str) -> Optional[str]:
    """The value *path* sets for *name*, or None. More than one line setting it is refused:
    a shell takes the last and the transports' .env loader the first, so which key signs
    would depend on who reads the file."""
    values = [match.group(2).strip().strip("'\"")
              for match in map(_env_pattern(name).match, _env_lines(path)) if match]
    if len(values) > 1:
        raise KeygenError(f"{path} sets {name} on {len(values)} lines; keep the one this "
                          "agent signs with and delete the others (or pass --force to "
                          "replace them all with a new key)")
    return (values[0] or None) if values else None


def write_env_key(path: Path, name: str, value: str) -> None:
    """Set ``name=value`` in the .env at *path* (owner-only), keeping every other line."""
    pattern = _env_pattern(name)
    kept = [line for line in _env_lines(path) if not pattern.match(line)]
    kept.append(f"{name}={value}")
    _replace_file(path, "\n".join(kept) + "\n", 0o600)


def key_from_private(agent: str, private: str,
                     key_type: Optional[str] = None) -> "crypto.KeyPair":
    """The key pair an existing private key belongs to. *key_type* says what it is; when
    None it is told from its form (64 hex: HMAC; 32 bytes of base64: Ed25519)."""
    if key_type == "hmac" or (key_type is None and re.fullmatch(r"[0-9a-fA-F]{64}", private)):
        return crypto.KeyPair(agent_id=agent, key_type="hmac", private_key=private,
                              public_key=private)
    try:
        raw = base64.b64decode(private.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        raw = b""
    if len(raw) != 32:
        what = "an Ed25519 key" if key_type else "an HMAC secret nor an Ed25519 key"
        raise KeygenError(f"the existing key is not {what}; pass --type hmac if it is an "
                          "HMAC secret, or --force to replace it")
    if not crypto.HAS_ED25519:
        raise KeygenError("the existing key is Ed25519, which needs the 'cryptography' "
                          "package: pip install cryptography")
    public = crypto.ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key()
    raw_public = public.public_bytes(encoding=crypto.serialization.Encoding.Raw,
                                     format=crypto.serialization.PublicFormat.Raw)
    return crypto.KeyPair(agent_id=agent, key_type="ed25519", private_key=private,
                          public_key=base64.b64encode(raw_public).decode("ascii"))


def plan_registry(path: Path, agent: str, verifier: str, key_type: str, force: bool = False,
                  model_family: str = "custom", framework: str = "custom") -> Tuple[str, bool]:
    """The registry's new text with ``authorized_agents.<agent>`` set, and whether it
    replaces a different registered key. Nothing is written here.

    *verifier* is the Ed25519 public key, or for HMAC the name of the variable that holds
    the shared secret (the secret itself never goes in the registry)."""
    if not path.is_file():
        # A mistyped path must not quietly start a second registry nobody reads.
        raise KeygenError(f"registry {path} not found (it is config/a2a_config.json in the "
                          "bus checkout)")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise KeygenError(f"{path} is not a JSON object")
    agents = data.setdefault("authorized_agents", {})
    if not isinstance(agents, dict):
        raise KeygenError(f"{path}: authorized_agents is not an object")
    env_name = key_env_name(agent)
    for other in agents:
        if other != agent and key_env_name(str(other)) == env_name:
            # Both would sign with the one A2A_KEY_ variable, so onboarding this agent
            # would hand it the other's key or rotate the other's out from under it.
            raise KeygenError(f"{agent!r} and the registered {other!r} both keep their key "
                              f"in {env_name}; pick an id that differs in more than - or _")
    entry = agents.get(agent)
    if not isinstance(entry, dict):
        entry = {"model_family": model_family, "framework": framework,
                 "role_scope": "worker"}
    old = (entry.get("public_key_or_secret_name"), entry.get("key_type"))
    replaces = bool(old[0]) and old != (verifier, key_type)
    if replaces and not force:
        raise KeygenError(f"{path} already registers a different key for {agent!r}; pass "
                          "--force to replace it (messages signed with the old key stop "
                          "verifying)")
    entry["public_key_or_secret_name"] = verifier
    entry["key_type"] = key_type
    agents[agent] = entry
    return json.dumps(data, indent=2) + "\n", replaces


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarmly-keygen", description="Create an agent's bus signing key and register it.")
    parser.add_argument("--agent", required=True,
                        help="agent id, as in the registry (lowercase, e.g. alice)")
    parser.add_argument("--type", choices=["ed25519", "hmac"],
                        help="key type (default ed25519 for a new key: only the public key "
                             "is shared; a key already in the .env is told from its form)")
    parser.add_argument("--env-file", type=lambda p: Path(p).expanduser(),
                        help="write A2A_KEY_<AGENT> into this .env (owner-only) instead of "
                             "printing it; a key already there is registered, not replaced")
    parser.add_argument("--registry", type=lambda p: Path(p).expanduser(),
                        help="add the agent's verifying entry to this a2a_config.json")
    parser.add_argument("--model-family", default="custom",
                        help="model_family for a new registry entry")
    parser.add_argument("--framework", default="custom",
                        help="framework for a new registry entry")
    parser.add_argument("--force", action="store_true",
                        help="make a new key even if the .env or the registry has one")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    agent = args.agent
    if not AGENT_ID.fullmatch(agent):
        print(f"agent id {agent!r} is not [a-z0-9_-]{{2,32}}", file=sys.stderr)
        return 2
    env_name = key_env_name(agent)
    try:
        existing = (read_env_key(args.env_file, env_name)
                    if args.env_file and not args.force else None)
        if existing:
            pair = key_from_private(agent, existing, args.type)
        elif args.type != "hmac" and not crypto.HAS_ED25519:
            raise KeygenError("Ed25519 needs the 'cryptography' package: pip install "
                              "cryptography (or pass --type hmac)")
        else:
            pair = (crypto.KeyPair.generate_hmac(agent) if args.type == "hmac"
                    else crypto.KeyPair.generate_ed25519(agent))
        # HMAC is symmetric: the verifying value IS the secret, so the registry names the
        # variable that holds it, never the secret.
        verifier = pair.public_key if pair.key_type == "ed25519" else env_name
        planned = None
        if args.registry:
            planned = plan_registry(args.registry, agent, verifier, pair.key_type,
                                    force=args.force, model_family=args.model_family,
                                    framework=args.framework)
        # The private key first: a registry entry whose private half was never stored
        # would make every message of this agent fail to verify.
        if args.env_file and not existing:
            write_env_key(args.env_file, env_name, pair.private_key)
        if planned:
            text, replaces = planned
            if replaces:
                print(f"swarmly-keygen: replacing the key registered for {agent!r}; its older "
                      "messages stop verifying", file=sys.stderr)
            mode = stat.S_IMODE(_target(args.registry).stat().st_mode)
            _replace_file(args.registry, text, mode)
    except (KeygenError, OSError, ValueError) as exc:
        print(f"swarmly-keygen: {exc}", file=sys.stderr)
        return 2

    print(f"{pair.key_type} key for agent {agent!r}:")
    if existing:
        print(f"  private key: the {env_name} already in {args.env_file} (kept)")
    elif args.env_file:
        print(f"  private key: {env_name} written to {args.env_file} (owner-only)")
    else:
        print(f"  add to the private .env of {agent!r}'s host (never git):")
        print(f"{env_name}={pair.private_key}")
    if args.registry:
        print(f"  registered in {args.registry}; commit it so every reader verifies "
              f"{agent!r}")
    else:
        print(f"  add to config/a2a_config.json under authorized_agents.{agent}:")
        print(f'"public_key_or_secret_name": "{verifier}",')
        print(f'"key_type": "{pair.key_type}"')
    if pair.key_type == "hmac":
        print(f"  HMAC: set {env_name} to the same secret on every host that reads "
              f"{agent!r}'s messages (environment or .env, never git)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
