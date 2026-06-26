from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.readiness import risk_acknowledgement_status


def build_private_beta_report(root: Path, *, version: str = "") -> dict[str, Any]:
    env = _read_env(root / ".env")
    checks = {
        "setup_complete": _check(bool(env.get("BONEHAWK_SETUP_COMPLETE") == "true"), "First-run setup is marked complete.", "Run the setup wizard again."),
        "alpaca_keys": _check(bool(env.get("ALPACA_API_KEY") and env.get("ALPACA_SECRET_KEY")), "Alpaca keys are present.", "Add Alpaca paper keys in setup."),
        "autopilot_config": _check((root / "config" / "autopilot.json").exists(), "Autopilot config exists.", "Create config/autopilot.json from setup or the example file."),
        "live_mode": _live_mode_check(env),
        "risk_disclosure": _risk_disclosure_check(root / "README.md"),
        "risk_acknowledgement": _risk_acknowledgement_check(root),
        "release_dmg": _release_dmg_check(root, version),
        "tests": _check((root / "tests").exists(), "Test suite is present.", "Restore the tests directory before beta distribution."),
    }
    summary = _summary(checks)
    return {
        "ok": summary["failed"] == 0,
        "status": "ready_for_private_beta" if summary["failed"] == 0 else "not_ready",
        "summary": summary,
        "checks": checks,
        "message": "Private beta readiness checks are local only; paper-test across real market sessions before public release.",
    }


def _live_mode_check(env: dict[str, str]) -> dict[str, Any]:
    paper = str(env.get("ALPACA_PAPER", "true")).strip().lower()
    allow_live = str(env.get("ALPACA_ALLOW_LIVE", "false")).strip().lower()
    ok = paper != "false" and allow_live not in {"1", "true", "yes", "on"}
    return _check(ok, "Live trading is disabled for beta testing.", "Set ALPACA_PAPER=true and ALPACA_ALLOW_LIVE=false before giving the app to testers.")


def _risk_disclosure_check(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").lower() if path.exists() else ""
    ok = "not financial advice" in text and "can lose money" in text
    return _check(ok, "Risk disclosure is present.", "Add clear financial-risk and not-financial-advice language to README.md.")


def _risk_acknowledgement_check(root: Path) -> dict[str, Any]:
    status = risk_acknowledgement_status(root)
    return _check(bool(status.get("accepted")), "Risk acknowledgement is recorded.", "Complete first-run risk acknowledgement before private beta.")


def _release_dmg_check(root: Path, version: str) -> dict[str, Any]:
    dist = root / "dist"
    pattern = f"Bonehawk-{version}-macOS-arm64.dmg" if version else "Bonehawk-*-macOS-arm64.dmg"
    matches = list(dist.glob(pattern)) if dist.exists() else []
    dmg = matches[0] if matches else None
    checksum = Path(str(dmg) + ".sha256") if dmg else None
    ok = bool(dmg and checksum and checksum.exists())
    return _check(ok, f"Release DMG is present: {dmg.name if dmg else 'missing'}.", "Build the desktop app and attach a DMG plus .sha256 file to the release.")


def _summary(checks: dict[str, dict[str, Any]]) -> dict[str, int]:
    passed = sum(1 for check in checks.values() if check["status"] == "pass")
    failed = sum(1 for check in checks.values() if check["status"] == "fail")
    warn = sum(1 for check in checks.values() if check["status"] == "warn")
    return {"passed": passed, "failed": failed, "warn": warn, "total": len(checks)}


def _check(ok: bool, message: str, recovery: str, *, warn: bool = False) -> dict[str, Any]:
    return {
        "status": "pass" if ok else "warn" if warn else "fail",
        "message": message if ok else recovery,
        "recovery": "" if ok else recovery,
    }


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    print(json.dumps(build_private_beta_report(root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
