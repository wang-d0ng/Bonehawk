from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommandInput:
    name: str
    label: str
    default: str = ""
    pattern: str = r".+"
    required: bool = True

    def public(self) -> dict[str, str | bool]:
        return {"name": self.name, "label": self.label, "default": self.default, "required": self.required}


@dataclass(frozen=True)
class CommandSpec:
    id: str
    group: str
    label: str
    description: str
    display: str
    argv: tuple[str, ...] = ()
    inputs: tuple[CommandInput, ...] = ()
    confirm_phrase: str = ""
    timeout: int = 120
    action: str = "subprocess"
    source: str = "README"
    source_path: str = ""
    target_path: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group,
            "label": self.label,
            "description": self.description,
            "command": self.display,
            "inputs": [item.public() for item in self.inputs],
            "requires_confirmation": bool(self.confirm_phrase),
            "confirm_phrase": self.confirm_phrase,
            "action": self.action,
            "source": self.source,
        }


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("setup-venv", "Setup", "Create venv", "Create the local Python virtual environment.", "python3 -m venv .venv", ("python3", "-m", "venv", ".venv")),
    CommandSpec("install-requirements", "Setup", "Install requirements", "Install Python packages inside the project venv.", "python -m pip install -r requirements.txt", ("{python}", "-m", "pip", "install", "-r", "requirements.txt"), timeout=180),
    CommandSpec("copy-env", "Setup", "Create .env", "Copy env.template to .env only if .env is missing.", "cp env.template .env", action="copy", source_path="env.template", target_path=".env"),
    CommandSpec("copy-watchlist", "Setup", "Create watchlist", "Copy the watchlist example only if your watchlist is missing.", "cp config/watchlist.example.json config/watchlist.json", action="copy", source_path="config/watchlist.example.json", target_path="config/watchlist.json"),
    CommandSpec("copy-market-universe", "Setup", "Create universe config", "Copy the market universe example only if your universe file is missing.", "cp config/market_universe.example.json config/market_universe.json", action="copy", source_path="config/market_universe.example.json", target_path="config/market_universe.json"),
    CommandSpec("build-market-universe", "Setup", "Build market universe", "Refresh the broad stock universe with the default scan cap.", "python scripts/build_market_universe.py --max-scan-symbols 250", ("{python}", "scripts/build_market_universe.py", "--max-scan-symbols", "250"), timeout=180),
    CommandSpec("dashboard-health", "Setup", "Dashboard health", "Confirm this dashboard is already running.", "python scripts/dashboard.py", action="status"),
    CommandSpec("desktop-run", "Desktop", "Open desktop app", "Launch Bonehawk in a desktop window.", "python scripts/desktop_app.py", ("{python}", "scripts/desktop_app.py"), action="background"),
    CommandSpec("desktop-build", "Desktop", "Build Mac app", "Create dist/Bonehawk.app with PyInstaller.", "python scripts/build_desktop_app.py", ("{python}", "scripts/build_desktop_app.py"), timeout=240),
    CommandSpec("telegram-test", "Alerts", "Telegram test", "Send a Bonehawk Telegram smoke-test message.", 'bash scripts/telegram.sh "Bonehawk Alpaca smoke test"', ("bash", "scripts/telegram.sh", "Bonehawk Alpaca smoke test")),
    CommandSpec("pytest", "Checks", "Run tests", "Run the project test suite.", "python -m pytest", ("{python}", "-m", "pytest"), timeout=180),
    CommandSpec("paper-cycle", "Cycles", "Paper cycle", "Run the safe paper trading cycle.", "python scripts/paper_cycle.py", ("{python}", "scripts/paper_cycle.py")),
    CommandSpec("paper-cycle-notify", "Cycles", "Paper + Telegram", "Run the paper cycle and notify Telegram.", "python scripts/paper_cycle.py --notify", ("{python}", "scripts/paper_cycle.py", "--notify")),
    CommandSpec("daily-schedule-copy", "Daily alerts", "Create schedule", "Copy the daily schedule example if missing.", "cp config/daily_schedule.example.json config/daily_schedule.json", action="copy", source_path="config/daily_schedule.example.json", target_path="config/daily_schedule.json"),
    CommandSpec("daily-once-morning", "Daily alerts", "Morning alert", "Send one morning trade-ideas alert.", "python scripts/daily_scheduler.py --once morning", ("{python}", "scripts/daily_scheduler.py", "--once", "morning")),
    CommandSpec("daily-once-midday", "Daily alerts", "Midday alert", "Send one midday scanner alert.", "python scripts/daily_scheduler.py --once midday", ("{python}", "scripts/daily_scheduler.py", "--once", "midday")),
    CommandSpec("daily-once-end", "Daily alerts", "End-of-day alert", "Send one end-of-day portfolio summary.", "python scripts/daily_scheduler.py --once end_of_day", ("{python}", "scripts/daily_scheduler.py", "--once", "end_of_day")),
    CommandSpec("daily-loop", "Daily alerts", "Start scheduler loop", "Start the daily scheduler in the background.", "python scripts/daily_scheduler.py --loop", ("{python}", "scripts/daily_scheduler.py", "--loop"), confirm_phrase="START_LOOP", action="background"),
)

COMMAND_BY_ID = {command.id: command for command in COMMANDS}


def command_catalog() -> dict[str, Any]:
    return {"commands": [command.public() for command in COMMANDS]}


def run_command(root: Path, command_id: str, inputs: dict[str, Any] | None = None, confirm: str = "") -> dict[str, Any]:
    command = COMMAND_BY_ID.get(command_id)
    if command is None:
        return {"ok": False, "status": "unknown_command", "message": "Unknown command."}
    if command.confirm_phrase and confirm != command.confirm_phrase:
        return {
            "ok": False,
            "status": "confirmation_required",
            "message": f"Type {command.confirm_phrase} to run this command.",
            "command": command.public(),
        }
    inputs = inputs or {}
    try:
        normalized_inputs = _validate_inputs(command, inputs)
    except ValueError as error:
        return {"ok": False, "status": "invalid_input", "message": str(error), "command": command.public()}

    if command.action == "copy":
        return _copy_if_missing(root, command)
    if command.action == "status":
        return {"ok": True, "status": "ok", "stdout": "Dashboard is already running at http://127.0.0.1:8765.", "stderr": "", "returncode": 0, "command": command.public()}
    argv = _resolve_argv(root, command.argv, normalized_inputs)
    if command.action == "background":
        return _start_background(root, command, argv)
    return _run_subprocess(root, command, argv)


def _validate_inputs(command: CommandSpec, inputs: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for item in command.inputs:
        value = str(inputs.get(item.name, item.default)).strip()
        if item.required and not value:
            raise ValueError(f"{item.label} is required.")
        if value and not re.fullmatch(item.pattern, value):
            raise ValueError(f"{item.label} is invalid.")
        normalized[item.name] = value
    return normalized


def _resolve_argv(root: Path, argv: tuple[str, ...], inputs: dict[str, str]) -> list[str]:
    project_python = str(root / ".venv" / "bin" / "python") if (root / ".venv" / "bin" / "python").exists() else "python3"
    values = {"python": project_python, **inputs}
    return [part.format(**values) for part in argv]


def _copy_if_missing(root: Path, command: CommandSpec) -> dict[str, Any]:
    source = root / command.source_path
    target = root / command.target_path
    if target.exists():
        return {"ok": True, "status": "skipped", "stdout": f"{command.target_path} already exists; left it unchanged.", "stderr": "", "returncode": 0, "command": command.public()}
    if not source.exists():
        return {"ok": False, "status": "missing_source", "stdout": "", "stderr": f"{command.source_path} does not exist.", "returncode": 1, "command": command.public()}
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return {"ok": True, "status": "created", "stdout": f"Created {command.target_path}.", "stderr": "", "returncode": 0, "command": command.public()}


def _run_subprocess(root: Path, command: CommandSpec, argv: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(argv, cwd=root, text=True, capture_output=True, check=False, timeout=command.timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "timeout", "stdout": "", "stderr": "Command timed out.", "returncode": 124, "command": command.public()}
    return {
        "ok": result.returncode == 0,
        "status": "completed",
        "stdout": redact_output(result.stdout),
        "stderr": redact_output(result.stderr),
        "returncode": result.returncode,
        "command": command.public(),
    }


def _start_background(root: Path, command: CommandSpec, argv: list[str]) -> dict[str, Any]:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    output_path = logs / f"{command.id}.log"
    with output_path.open("ab") as output:
        process = subprocess.Popen(argv, cwd=root, stdout=output, stderr=output)
    return {
        "ok": True,
        "status": "started",
        "stdout": f"Started background command with PID {process.pid}. Output: {output_path}",
        "stderr": "",
        "returncode": 0,
        "pid": process.pid,
        "command": command.public(),
    }


def redact_output(text: str) -> str:
    redacted = re.sub(r'("(?:account_number|api_key|private_key_base64|token|order_id|id)"\s*:\s*")([^"]+)(")', lambda m: f"{m.group(1)}{_mask(m.group(2))}{m.group(3)}", text, flags=re.IGNORECASE)
    redacted = re.sub(r"\b\d{7,}\b", lambda m: _mask(m.group(0)), redacted)
    return redacted


def _mask(value: str, visible: int = 4) -> str:
    if len(value) <= visible:
        return "*" * len(value)
    return f"{'*' * (len(value) - visible)}{value[-visible:]}"
