#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.market_universe import DEFAULT_UNIVERSE, build_market_universe_payload, fetch_nasdaqtrader_universe


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a broad stock universe for the market scanner.")
    parser.add_argument("--output", default=str(ROOT / "config" / "market_universe.json"))
    parser.add_argument("--max-scan-symbols", type=int, default=250)
    args = parser.parse_args()

    symbols = fetch_nasdaqtrader_universe()
    payload = build_market_universe_payload([*DEFAULT_UNIVERSE, *symbols], max_scan_symbols=args.max_scan_symbols)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {len(payload['symbols'])} symbols to {output}")


if __name__ == "__main__":
    main()
