from __future__ import annotations

import sys
from pathlib import Path

from scripts.build_desktop_app import build_pyinstaller_args, ensure_project_root_marker, main


def test_build_pyinstaller_args_targets_mac_app(tmp_path: Path) -> None:
    icon_path = tmp_path / "assets" / "app_icon.icns"
    icon_path.parent.mkdir()
    icon_path.write_bytes(b"icns")

    args = build_pyinstaller_args(tmp_path)

    assert args[:5] == [sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed"]
    assert "--name" in args
    assert "Bonehawk" in args
    assert "--add-data" in args
    assert any(value.startswith(str(tmp_path / "build" / "bonehawk_project_root.txt")) for value in args)
    assert "--hidden-import" in args
    assert "_cffi_backend" in args
    assert "--icon" in args
    assert str(icon_path) in args
    assert str(tmp_path / "scripts" / "desktop_app.py") in args


def test_ensure_project_root_marker_writes_root(tmp_path: Path) -> None:
    marker = ensure_project_root_marker(tmp_path)

    assert marker.read_text() == str(tmp_path.resolve())


def test_build_main_reports_missing_pyinstaller(monkeypatch) -> None:
    monkeypatch.setattr("scripts.build_desktop_app.importlib.util.find_spec", lambda name: None)

    assert main([]) == 1


def test_build_main_runs_pyinstaller(monkeypatch) -> None:
    calls = []

    def fake_run(args, cwd, check):
        calls.append((args, cwd, check))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("scripts.build_desktop_app.importlib.util.find_spec", lambda name: object())
    monkeypatch.setattr("scripts.build_desktop_app.subprocess.run", fake_run)

    assert main([]) == 0
    assert calls[0][0][:3] == [sys.executable, "-m", "PyInstaller"]
