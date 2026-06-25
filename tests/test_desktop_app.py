from __future__ import annotations

import sys
import types
from pathlib import Path

from scripts.desktop_app import DesktopAppConfig, build_desktop_url, resolve_project_root, run_desktop_app, start_dashboard_server


def test_build_desktop_url_uses_localhost_port() -> None:
    url = build_desktop_url(8765)

    assert url == "http://127.0.0.1:8765"


def test_start_dashboard_server_allocates_port(tmp_path: Path) -> None:
    config = DesktopAppConfig(root=tmp_path, host="127.0.0.1", port=0)

    server, thread = start_dashboard_server(config)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] > 0
        assert thread.is_alive()
    finally:
        server.shutdown()
        server.server_close()


def test_resolve_project_root_prefers_env(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "dashboard.py").write_text("")
    monkeypatch.setenv("BONEHAWK_PROJECT_ROOT", str(project))

    assert resolve_project_root() == project.resolve()


def test_run_desktop_app_uses_webview_window(tmp_path: Path, monkeypatch) -> None:
    calls = []

    fake_webview = types.SimpleNamespace(
        create_window=lambda *args, **kwargs: calls.append(("window", args, kwargs)),
        start=lambda **kwargs: calls.append(("start", (), kwargs)),
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    result = run_desktop_app(DesktopAppConfig(root=tmp_path, port=0))

    assert result == 0
    assert calls[0][0] == "window"
    assert calls[0][1][0] == "Bonehawk"
    assert calls[1][0] == "start"


def test_run_desktop_app_browser_fallback_when_webview_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "webview", raising=False)
    opened = []

    def fake_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    original_import = __import__
    monkeypatch.setattr("builtins.__import__", fake_import)
    monkeypatch.setattr("scripts.desktop_app.webbrowser.open", lambda url: opened.append(url))

    result = run_desktop_app(DesktopAppConfig(root=tmp_path, port=0, browser_fallback=True))

    assert result == 0
    assert opened[0].startswith("http://127.0.0.1:")
