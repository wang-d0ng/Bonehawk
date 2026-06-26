from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.command_center import command_catalog, redact_output, run_command


def test_command_catalog_hides_argv_and_groups_commands() -> None:
    payload = command_catalog()

    assert any(command["group"] == "Setup" for command in payload["commands"])
    assert all("argv" not in command for command in payload["commands"])


def test_command_catalog_contains_current_cycle_commands() -> None:
    ids = {command["id"] for command in command_catalog()["commands"]}

    assert {"paper-cycle", "paper-cycle-notify", "daily-loop", "telegram-autopilot-once", "telegram-autopilot-loop"}.issubset(ids)


def test_run_command_copy_creates_missing_target(tmp_path: Path) -> None:
    (tmp_path / "env.template").write_text("TRADING_MODE=paper\n")

    payload = run_command(tmp_path, "copy-env")

    assert payload["ok"] is True
    assert payload["status"] == "created"
    assert (tmp_path / ".env").read_text() == "TRADING_MODE=paper\n"


def test_run_command_copy_reports_missing_source(tmp_path: Path) -> None:
    payload = run_command(tmp_path, "copy-env")

    assert payload["ok"] is False
    assert payload["status"] == "missing_source"


def test_run_command_reports_timeout(tmp_path: Path, monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr("scripts.command_center.subprocess.run", fake_run)

    payload = run_command(tmp_path, "pytest")

    assert payload["ok"] is False
    assert payload["status"] == "timeout"


def test_run_command_background_starts_process(tmp_path: Path, monkeypatch) -> None:
    class FakeProcess:
        pid = 4242

    calls = []

    def fake_popen(args, cwd, stdout, stderr):
        calls.append((args, cwd, stdout, stderr))
        return FakeProcess()

    monkeypatch.setattr("scripts.command_center.subprocess.Popen", fake_popen)

    payload = run_command(tmp_path, "daily-loop", confirm="START_LOOP")

    assert payload["ok"] is True
    assert payload["status"] == "started"
    assert payload["pid"] == 4242
    assert calls[0][0][-1] == "--loop"


def test_run_command_status_action_does_not_spawn_process(tmp_path: Path) -> None:
    payload = run_command(tmp_path, "dashboard-health")

    assert payload["ok"] is True
    assert "Dashboard is already running" in payload["stdout"]


def test_command_catalog_includes_desktop_actions() -> None:
    ids = {command["id"] for command in command_catalog()["commands"]}

    assert {"desktop-run", "desktop-build", "packaged-smoke"}.issubset(ids)


def test_redact_output_masks_sensitive_values() -> None:
    text = '{"id":"abcdef1234567890","account_number":"123456789","token":"secret-token"}'

    redacted = redact_output(text)

    assert "123456789" not in redacted
    assert "secret-token" not in redacted
    assert "account_number" in redacted
