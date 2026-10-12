#!/usr/bin/env python3
"""Create an agent's bus signing key; the same as the ``swarmly-keygen`` command.

    python scripts/generate_keys.py --agent alice --env-file .env --registry config/a2a_config.json
"""

import sys

from swarmly.keygen import main

if __name__ == "__main__":
    sys.exit(main())
