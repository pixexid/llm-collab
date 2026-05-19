#!/usr/bin/env python3
"""
claude_desktop_bridge_health.py — non-bridge diagnostics for Claude Desktop.

This script does not read or operate Claude Desktop content. It only reports
coarse machine/app state that helps distinguish "Claude is not running" from
"Claude appears visible but Computer Use cannot inspect it".

Use Computer Use, not this script, for actual Claude desktop interaction.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass


CLAUDE_BUNDLE_ID = "com.anthropic.claudefordesktop"
CLAUDE_APP_PROCESS = "/Applications/Claude.app/Contents/MacOS/Claude"


@dataclass
class CommandResult:
    ok: bool
    stdout: str
    stderr: str


def run_command(command: list[str]) -> CommandResult:
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        return CommandResult(
            ok=False,
            stdout="",
            stderr=f"command not found: {command[0]} ({error})",
        )
    return CommandResult(
        ok=result.returncode == 0,
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
    )


def osascript(script: str) -> CommandResult:
    return run_command(["osascript", "-e", script])


def frontmost_app() -> CommandResult:
    return osascript('tell application "System Events" to get name of first application process whose frontmost is true')


def visible_apps() -> CommandResult:
    return osascript('tell application "System Events" to get (name of application processes whose visible is true)')


def claude_running() -> CommandResult:
    return run_command(["pgrep", "-fl", CLAUDE_APP_PROCESS])


def parse_pids(pgrep_stdout: str) -> list[str]:
    pids: list[str] = []
    for line in pgrep_stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if parts and parts[0].isdigit():
            pids.append(parts[0])
    return pids


def claude_main_process_metrics(pids: list[str]) -> dict:
    if not pids:
        return {"ok": True, "processes": [], "cpu_percent_total": 0.0, "busy": False}

    result = run_command(["ps", "-o", "pid=,stat=,etime=,pcpu=,pmem=", "-p", ",".join(pids)])
    if not result.ok:
        return {
            "ok": False,
            "processes": [],
            "cpu_percent_total": 0.0,
            "busy": False,
            "error": result.stderr,
        }

    processes: list[dict] = []
    cpu_total = 0.0
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        try:
            cpu_percent = float(parts[3])
            memory_percent = float(parts[4])
        except ValueError:
            continue
        cpu_total += cpu_percent
        processes.append(
            {
                "pid": int(parts[0]),
                "stat": parts[1],
                "elapsed": parts[2],
                "cpu_percent": cpu_percent,
                "memory_percent": memory_percent,
            }
        )

    return {
        "ok": True,
        "processes": processes,
        "cpu_percent_total": round(cpu_total, 1),
        "busy": cpu_total >= 10.0,
    }


def claude_local_agent_count() -> int:
    result = run_command(["pgrep", "-fl", "Claude/claude-code/.*/claude"])
    if not result.ok:
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])


def power_assertions_summary() -> dict:
    result = run_command(["pmset", "-g", "assertions"])
    if not result.ok:
        return {"ok": False, "error": result.stderr}
    text = result.stdout
    return {
        "ok": True,
        "claude_assertion_present": "pid " in text and "(Claude)" in text,
        "computer_use_assertion_present": "Codex Computer Use interaction" in text,
        "prevent_system_sleep_present": "PreventSystemSleep" in text,
    }


def collect_health() -> dict:
    frontmost = frontmost_app()
    visible = visible_apps()
    running = claude_running()
    visible_names = [name.strip() for name in visible.stdout.split(",") if name.strip()] if visible.ok else []
    claude_pids = parse_pids(running.stdout) if running.ok else []
    main_process_metrics = claude_main_process_metrics(claude_pids)
    return {
        "claude_bundle_id": CLAUDE_BUNDLE_ID,
        "claude_process_running": running.ok and bool(running.stdout),
        "claude_process_count": len([line for line in running.stdout.splitlines() if line.strip()]) if running.ok else 0,
        "claude_main_process_metrics": main_process_metrics,
        "claude_frontmost": frontmost.ok and frontmost.stdout == "Claude",
        "frontmost_app": frontmost.stdout if frontmost.ok else None,
        "claude_visible": "Claude" in visible_names,
        "visible_app_count": len(visible_names),
        "claude_local_agent_process_count": claude_local_agent_count(),
        "power_assertions": power_assertions_summary(),
        "computer_use_required_for_bridge": True,
        "diagnostic_scope": "shell-only app/process visibility; not proof that the Claude Desktop prompt is inspectable or usable",
        "errors": {
            "frontmost": frontmost.stderr if not frontmost.ok else "",
            "visible": visible.stderr if not visible.ok else "",
            "running": running.stderr if not running.ok else "",
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report coarse Claude Desktop bridge health.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    health = collect_health()
    if args.json:
        print(json.dumps(health, indent=2, sort_keys=True))
        return 0

    print(f"Claude process running: {health['claude_process_running']} ({health['claude_process_count']} process match(es))")
    metrics = health["claude_main_process_metrics"]
    if metrics.get("ok"):
        print(f"Claude main process CPU total: {metrics['cpu_percent_total']}% (busy: {metrics['busy']})")
    else:
        print(f"Claude main process metrics unavailable: {metrics.get('error')}")
    print(f"Claude frontmost: {health['claude_frontmost']} (frontmost: {health['frontmost_app']})")
    print(f"Claude visible: {health['claude_visible']} ({health['visible_app_count']} visible app(s))")
    print(f"Claude local agent process count: {health['claude_local_agent_process_count']}")
    power = health["power_assertions"]
    if power.get("ok"):
        print(f"Claude power assertion present: {power['claude_assertion_present']}")
        print(f"Computer Use power assertion present: {power['computer_use_assertion_present']}")
    else:
        print(f"Power assertions unavailable: {power.get('error')}")
    print("Bridge interaction still requires Computer Use app inspection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
