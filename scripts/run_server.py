#!/usr/bin/env python3
"""
Entry point to launch the Swarmly Fast-Plane Gateway Server.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add repo root to python path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from swarmly.server import A2AGatewayServer


def main():
    parser = argparse.ArgumentParser(description="Swarmly Fast-Plane Gateway Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host address to bind")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    parser.add_argument("--config", help="Path to a2a_config.json")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else (ROOT / "config" / "a2a_config.json")
    server = A2AGatewayServer(host=args.host, port=args.port, config_path=config_path)

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\nShutting down A2A Gateway...")


if __name__ == "__main__":
    main()
