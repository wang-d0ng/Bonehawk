from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.dashboard import DashboardService, HTML


def run_packaged_smoke(root: Path, *, service: DashboardService | None = None) -> dict[str, Any]:
    service = service or DashboardService(root=root)
    checks = {
        "app_bundle": _check((root / "dist" / "Bonehawk.app").exists(), "Bonehawk.app bundle is present.", "Build the desktop app bundle first."),
        "html_shell": _html_shell_check(),
        "critical_routes": _critical_routes_check(service),
    }
    failed = sum(1 for check in checks.values() if check["status"] == "fail")
    payload = {
        "ok": failed == 0,
        "status": "pass" if failed == 0 else "fail",
        "checked_at": datetime.now(UTC).isoformat(),
        "summary": {
            "passed": sum(1 for check in checks.values() if check["status"] == "pass"),
            "failed": failed,
            "total": len(checks),
        },
        "checks": checks,
        "message": "Packaged smoke verifies the built app shell and critical local dashboard routes.",
    }
    path = root / "logs" / "packaged_smoke.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _html_shell_check() -> dict[str, Any]:
    required = ["id=\"setup-modal\"", "id=\"live-panel\"", "id=\"autopilot-panel\"", "/api/live-readiness", "/api/paper-evidence"]
    missing = [item for item in required if item not in HTML]
    return _check(not missing, "Dashboard shell contains critical packaged-app controls.", f"Dashboard shell is missing: {', '.join(missing)}")


def _critical_routes_check(service: DashboardService) -> dict[str, Any]:
    route_calls = [
        service.status,
        service.setup_status,
        service.paper_evidence,
        service.live_readiness,
        service.live_orders,
    ]
    failures: list[str] = []
    for call in route_calls:
        try:
            payload = call()
        except Exception as error:
            failures.append(f"{call.__name__}: {str(error)[:120]}")
            continue
        if not isinstance(payload, dict):
            failures.append(f"{call.__name__}: non-json payload")
    return _check(not failures, "Critical dashboard routes returned JSON payloads.", "; ".join(failures) or "Critical route smoke failed.")


def _check(ok: bool, message: str, recovery: str) -> dict[str, Any]:
    return {"status": "pass" if ok else "fail", "message": message if ok else recovery, "recovery": "" if ok else recovery}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Bonehawk packaged app smoke checks.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    payload = run_packaged_smoke(Path(args.root).resolve())
    print(json.dumps(payload, indent=2, sort_keys=True))
    raise SystemExit(0 if payload.get("ok") else 1)


if __name__ == "__main__":
    main()
