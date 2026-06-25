#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.robinhood import RobinhoodConfig, RobinhoodCryptoClient
from scripts.robinhood_integration import robinhood_snapshot


def main() -> None:
    config = RobinhoodConfig.from_env(ROOT / ".env")
    client = None
    try:
        client = RobinhoodCryptoClient(config)
    except Exception:
        client = None
    print(json.dumps(robinhood_snapshot(config, client), indent=2))


if __name__ == "__main__":
    main()
