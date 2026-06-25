#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dashboard import DashboardService, make_handler


@dataclass(frozen=True)
class DesktopAppConfig:
    root: Path = ROOT
    host: str = "127.0.0.1"
    port: int = 8765
    title: str = "Bonehawk"
    width: int = 1440
    height: int = 960
    browser_fallback: bool = False


def build_desktop_url(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}"


def resolve_project_root() -> Path:
    env_root = os.getenv("BONEHAWK_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    marker = _bundle_root_marker()
    if marker and marker.exists():
        try:
            marked_root = Path(marker.read_text(encoding="utf-8").strip()).expanduser().resolve()
        except OSError:
            marked_root = ROOT
        if (marked_root / "scripts" / "dashboard.py").exists():
            return marked_root
    cwd = Path.cwd().resolve()
    if (cwd / "scripts" / "dashboard.py").exists():
        return cwd
    return ROOT


def start_dashboard_server(config: DesktopAppConfig) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer((config.host, config.port), make_handler(DashboardService(root=config.root)))
    thread = threading.Thread(target=server.serve_forever, name="bonehawk-dashboard", daemon=True)
    thread.start()
    return server, thread


def run_desktop_app(config: DesktopAppConfig) -> int:
    server, _thread = _start_with_available_port(config)
    host, port = server.server_address[:2]
    url = build_desktop_url(port, host)
    try:
        try:
            import webview
        except ImportError:
            if not config.browser_fallback:
                print("Desktop window support is not installed. Run: python -m pip install pywebview")
                return 1
            webbrowser.open(url)
            print(f"Bonehawk opened in your browser: {url}")
            return 0

        webview.create_window(config.title, url, width=config.width, height=config.height, min_size=(1100, 720))
        webview.start(debug=False)
        return 0
    finally:
        server.shutdown()
        server.server_close()


def _start_with_available_port(config: DesktopAppConfig) -> tuple[ThreadingHTTPServer, threading.Thread]:
    try:
        return start_dashboard_server(config)
    except OSError:
        fallback = DesktopAppConfig(
            root=config.root,
            host=config.host,
            port=0,
            title=config.title,
            width=config.width,
            height=config.height,
            browser_fallback=config.browser_fallback,
        )
        return start_dashboard_server(fallback)


def _bundle_root_marker() -> Path | None:
    bundle_dir = getattr(sys, "_MEIPASS", "")
    if not bundle_dir:
        return None
    return Path(bundle_dir) / "bonehawk_project_root.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Bonehawk as a desktop app.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--browser-fallback", action="store_true", help="Open the app in a browser if the desktop window package is missing.")
    args = parser.parse_args()
    return run_desktop_app(DesktopAppConfig(root=resolve_project_root(), port=args.port, browser_fallback=args.browser_fallback))


if __name__ == "__main__":
    raise SystemExit(main())
