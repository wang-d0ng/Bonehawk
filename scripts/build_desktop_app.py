#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_pyinstaller_args(root: Path = ROOT) -> list[str]:
    marker_path = ensure_project_root_marker(root)
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--name",
        "Bonehawk",
        "--distpath",
        str(root / "dist"),
        "--workpath",
        str(root / "build" / "pyinstaller"),
        "--specpath",
        str(root / "build"),
        "--add-data",
        f"{marker_path}:.",
        "--hidden-import",
        "_cffi_backend",
    ]
    cffi_backend = next((root / ".venv" / "lib").glob("python*/site-packages/_cffi_backend*.so"), None)
    if cffi_backend:
        args.extend(["--add-binary", f"{cffi_backend}:."])
    icon_path = root / "assets" / "app_icon.icns"
    if icon_path.exists():
        args.extend(["--icon", str(icon_path)])
    args.append(str(root / "scripts" / "desktop_app.py"))
    return args


def ensure_project_root_marker(root: Path = ROOT) -> Path:
    marker_path = root / "build" / "bonehawk_project_root.txt"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(root.resolve()), encoding="utf-8")
    return marker_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Bonehawk into a desktop app bundle.")
    parser.parse_args(argv)
    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller is not installed. Run: python -m pip install pyinstaller pywebview")
        return 1
    result = subprocess.run(build_pyinstaller_args(ROOT), cwd=ROOT, check=False)
    if result.returncode == 0:
        print(f"Built desktop app: {ROOT / 'dist' / 'Bonehawk.app'}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
