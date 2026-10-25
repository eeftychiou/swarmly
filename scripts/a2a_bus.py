#!/usr/bin/env python3
"""
CLI Tool for the Swarmly Protocol.
Supports sending typed messages, reading inbox with crypto verification, and status inspection.
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from swarmly.models import A2AMessage, MessageIntent, MessagePriority
from swarmly.bus import DurableBus, BusPushError, MissingKeyError
from swarmly.transports import TransportError
from swarmly.client import A2AClient


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip("'").strip('"')


load_env()


def open_bus(args):
    """The bus over the transport chosen by flags or A2A_BUS_* env vars (gitbus by default)."""
    from swarmly.transports import build_transport
    transport = build_transport(
        args.transport, repo=ROOT, folder=args.folder,
        gdrive={"folder_id": args.gdrive_folder_id, "credentials": args.gdrive_credentials},
        state_dir=ROOT / ".agents" / "state")
    config = Path(args.config).expanduser() if args.config else None
    if config is not None and not config.is_file():
        print(f"agent registry {config} not found (--config / A2A_CONFIG)", file=sys.stderr)
        raise SystemExit(2)
    return DurableBus(root_dir=ROOT, config_path=config, transport=transport)


def cmd_status(args):
    bus = open_bus(args)
    print("=" * 60)
    print("SWARMLY CLUSTER STATUS")
    print("=" * 60)
    print(f"Protocol:         {bus.config.get('protocol_version')}")
    print(f"Primary Master:   {bus.config.get('coordination', {}).get('primary_master')}")
    print(f"Failover Master:  {bus.config.get('coordination', {}).get('failover_master')}")
    print(f"Fast Plane (WS):  {'Enabled' if bus.config.get('fast_plane', {}).get('enabled') else 'Disabled'}")
    
    print("\nAuthorized Swarm Agents:")
    agents = bus.config.get("authorized_agents", {})
    if not agents:
        print("  (none: onboard each agent with swarmly-keygen --agent <id> --registry <this file>)")
    for aid, info in agents.items():
        info = info if isinstance(info, dict) else {}
        try:
            key_type = bus.key_type_for(aid)
        except ValueError as exc:
            key_type = f"invalid ({exc})"
        family, scope = (str(info.get(k) or "-") for k in ("model_family", "role_scope"))
        print(f"  - @{aid:<16} | Family: {family:<10} | Scope: {scope:<10} | Key: {key_type}")

    revoked = bus.config.get("revocation_list", [])
    if revoked:
        print(f"\nRevocation Blacklist: {', '.join(revoked)}")

    inbox_msgs = bus.read_inbox(verify_signatures=False)
    print(f"\nPending Messages in Durable Bus: {len(inbox_msgs)}")
    for msg, _, _ in inbox_msgs[-5:]:
        print(f"  [{msg.intent.value:<18}] From: @{msg.sender.agent_id:<12} -> @{msg.recipient.agent_id}")


def cmd_send(args):
    bus = open_bus(args)
    agent_id = args.sender
    try:
        key, key_type = bus.signing_key(agent_id)
    except MissingKeyError as exc:
        print(f"Not sent: {exc.args[0]}", file=sys.stderr)
        raise SystemExit(2)

    try:
        payload = json.loads(args.payload) if args.payload else {"text": args.text or "Hello A2A swarm"}
    except Exception:
        payload = {"text": args.payload}

    msg = A2AMessage.create(
        sender_id=agent_id,
        intent=args.intent,
        payload=payload,
        recipient_id=args.recipient,
        priority=args.priority,
        correlation_id=args.correlation_id,
    )

    try:
        path = bus.post_message(msg, private_key=key, key_type=key_type, git_push=args.push)
    except BusPushError as exc:
        print(f"Message {msg.message_id} is written locally, but NOT published:",
              file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, TransportError) as exc:      # a bad id or a refused write
        print(f"Not sent: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    print(f"Message {msg.message_id} posted to: {path}")


def cmd_inbox(args):
    bus = open_bus(args)
    messages = bus.read_inbox(verify_signatures=not args.no_verify)
    print("=" * 60)
    print(f"A2A INBOX ({len(messages)} messages)")
    print("=" * 60)
    for idx, (msg, is_valid, status) in enumerate(messages, 1):
        status_icon = "✓" if is_valid else "✗"
        print(f"{idx}. [{status_icon}] {msg.message_id} | Intent: {msg.intent.value} | From: @{msg.sender.agent_id} -> @{msg.recipient.agent_id}")
        print(f"    Verification: {status}")
        print(f"    Payload: {json.dumps(msg.payload)}")


def _utf8_safe_console():
    """Legacy consoles (cp1252) cannot encode the inbox check icons; degrade, never crash."""
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass


def main():
    _utf8_safe_console()
    parser = argparse.ArgumentParser(description="Swarmly CLI")
    parser.add_argument("--transport", default=os.environ.get("A2A_BUS_TRANSPORT", "git"),
                        choices=["git", "folder", "gdrive"],
                        help="Durable-plane transport (env A2A_BUS_TRANSPORT; default git)")
    parser.add_argument("--folder", default=os.environ.get("A2A_BUS_FOLDER"),
                        help="Synced/shared bus folder for --transport folder (env A2A_BUS_FOLDER)")
    parser.add_argument("--gdrive-folder-id", default=os.environ.get("A2A_GDRIVE_FOLDER_ID"),
                        help="Drive folder id for --transport gdrive (env A2A_GDRIVE_FOLDER_ID)")
    parser.add_argument("--gdrive-credentials", default=os.environ.get("A2A_GDRIVE_CREDENTIALS"),
                        help="Service-account or authorized-user JSON (env A2A_GDRIVE_CREDENTIALS; "
                             "default: Application Default Credentials)")
    parser.add_argument("--config", default=os.environ.get("A2A_CONFIG"),
                        help="Agent registry JSON (env A2A_CONFIG; default config/a2a_config.json)")
    subparsers = parser.add_subparsers(dest="command")

    # status
    p_status = subparsers.add_parser("status", help="Show cluster status")
    p_status.set_defaults(func=cmd_status)

    # send
    p_send = subparsers.add_parser("send", help="Send an A2A message")
    p_send.add_argument("--sender", required=True, help="Sender agent ID")
    p_send.add_argument("--intent", default="QUERY_REQUEST", help="Message intent")
    p_send.add_argument("--recipient", default="*", help="Recipient agent ID")
    p_send.add_argument("--priority", default="ROUTINE", choices=["ROUTINE", "HIGH", "BLOCKING"])
    p_send.add_argument("--payload", help="JSON payload string")
    p_send.add_argument("--text", help="Simple text message")
    p_send.add_argument("--correlation-id", help="Correlation ID")
    p_send.add_argument("--push", action="store_true", help="Push to git remote")
    p_send.set_defaults(func=cmd_send)

    # inbox
    p_inbox = subparsers.add_parser("inbox", help="List and verify inbox messages")
    p_inbox.add_argument("--no-verify", action="store_true", help="Skip signature verification")
    p_inbox.set_defaults(func=cmd_inbox)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
