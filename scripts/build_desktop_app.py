#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_pyinstaller_args(root: Path = ROOT, *, platform_name: str | None = None) -> list[str]:
    platform_name = platform_name or sys.platform
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
        f"{marker_path}{_pyinstaller_data_separator(platform_name)}.",
        "--hidden-import",
        "_cffi_backend",
    ]
    cffi_backend = _cffi_backend_binary(root, platform_name)
    if cffi_backend:
        args.extend(["--add-binary", f"{cffi_backend}:."])
    icon_path = _icon_path(root, platform_name)
    if icon_path.exists():
        args.extend(["--icon", str(icon_path)])
    args.append(str(root / "scripts" / "desktop_app.py"))
    return args


def build_output_path(root: Path = ROOT, *, platform_name: str | None = None) -> Path:
    platform_name = platform_name or sys.platform
    if platform_name.startswith("win"):
        return root / "dist" / "Bonehawk" / "Bonehawk.exe"
    if platform_name == "darwin":
        return root / "dist" / "Bonehawk.app"
    return root / "dist" / "Bonehawk"


def ensure_project_root_marker(root: Path = ROOT) -> Path:
    marker_path = root / "build" / "bonehawk_project_root.txt"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(root.resolve()), encoding="utf-8")
    return marker_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Bonehawk into a desktop app.")
    parser.parse_args(argv)
    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller is not installed. Run: python -m pip install pyinstaller pywebview")
        return 1
    result = subprocess.run(build_pyinstaller_args(ROOT), cwd=ROOT, check=False)
    if result.returncode == 0:
        print(f"Built desktop app: {build_output_path(ROOT)}")
    return result.returncode


def _pyinstaller_data_separator(platform_name: str) -> str:
    return ";" if platform_name.startswith("win") else ":"


def _cffi_backend_binary(root: Path, platform_name: str) -> Path | None:
    if platform_name.startswith("win"):
        return next((root / ".venv" / "Lib" / "site-packages").glob("_cffi_backend*.pyd"), None)
    return next((root / ".venv" / "lib").glob("python*/site-packages/_cffi_backend*.so"), None)


def _icon_path(root: Path, platform_name: str) -> Path:
    if platform_name.startswith("win"):
        return root / "assets" / "app_icon.ico"
    if platform_name == "darwin":
        return root / "assets" / "app_icon.icns"
    return root / "assets" / "app_icon.png"


if __name__ == "__main__":
    raise SystemExit(main())
