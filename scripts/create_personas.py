#!/usr/bin/env python3
"""Create the three persona groups and users in the deployed Cognito pool and store their passwords in the users secret.

The learning path does this in module 2 (`workshop apply 2`). This script is the same step for the all-at-once deployment
(`scripts/deploy.sh`, the Workshop Studio deployer with DeployScope=all), so `scripts/doctor.py` and the `workshop` command find
the personas right after the deploy. Idempotent: existing groups and users are kept, existing passwords are reused.

Usage: `uv run python scripts/create_personas.py` (AWS credentials and region from the environment).
"""

from __future__ import annotations

import sys

from workshop_cli.apply_foundation import ensure_personas

from platform_sdk import config


def main() -> int:
    cfg = config.load()
    users = ensure_personas(cfg)
    print(f"personas ready: {', '.join(users)} (passwords: uv run python scripts/doctor.py --show-passwords)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
