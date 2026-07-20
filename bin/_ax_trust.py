"""Read-only, bounded status reporting for the optional macOS AX doorbell."""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


AX_PROBE_TIMEOUT_SECONDS = 5
AX_STATUSES = frozenset({"trusted", "DOWN", "unavailable", "n/a"})
DEFAULT_AXSEND = Path(__file__).resolve().parents[1] / "tools" / "axbridge" / "axsend"


@dataclass(frozen=True)
class AxTrustStatus:
    status: str
    reason: str | None = None
    remediation: str | None = None

    def __post_init__(self) -> None:
        if self.status not in AX_STATUSES:
            raise ValueError(f"invalid AX trust status: {self.status!r}")

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def has_ax_doorbell_capability(agent: dict) -> bool:
    """Match deliver.py's activation capability allowlist."""
    activation = agent.get("activation", {})
    ax_app = activation.get("ax_app")
    return (
        activation.get("type") == "cli_session"
        and isinstance(ax_app, str)
        and bool(ax_app.strip())
        and not activation.get("ax_attended_only")
    )


def probe_ax_trust(
    agent: dict,
    *,
    platform_name: str | None = None,
    binary_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> AxTrustStatus:
    """Probe the prebuilt axsend binary without raising or building it."""
    host_platform = platform_name if platform_name is not None else platform.system()
    if host_platform != "Darwin":
        return AxTrustStatus("n/a", reason="host platform is not Darwin")
    if not has_ax_doorbell_capability(agent):
        return AxTrustStatus("n/a", reason="agent has no routine AX doorbell capability")

    axsend = binary_path if binary_path is not None else DEFAULT_AXSEND
    if not axsend.is_file() or not os.access(axsend, os.X_OK):
        return AxTrustStatus(
            "unavailable",
            reason="prebuilt tools/axbridge/axsend is missing or not executable",
            remediation=(
                "Build the optional bridge with tools/axbridge/build.sh, "
                "then rerun status."
            ),
        )

    try:
        result = runner(
            [str(axsend), "check"],
            capture_output=True,
            text=True,
            timeout=AX_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AxTrustStatus(
            "unavailable",
            reason=f"AX trust probe timed out after {AX_PROBE_TIMEOUT_SECONDS}s",
            remediation="Run tools/axbridge/axsend check directly to diagnose the optional doorbell.",
        )
    except Exception as exc:
        return AxTrustStatus(
            "unavailable",
            reason=f"AX trust probe failed with {type(exc).__name__}",
            remediation="Run tools/axbridge/axsend check directly to diagnose the optional doorbell.",
        )

    if result.returncode == 0:
        return AxTrustStatus("trusted")
    if result.returncode == 2:
        return AxTrustStatus(
            "DOWN",
            reason="durable mailbox remains authoritative; the AX doorbell is degraded",
            remediation=(
                "Grant Accessibility access to the controlling process in System Settings, "
                "then rerun tools/axbridge/axsend check."
            ),
        )
    return AxTrustStatus(
        "unavailable",
        reason=f"AX trust probe exited with unexpected status {result.returncode}",
        remediation="Run tools/axbridge/axsend check directly to diagnose the optional doorbell.",
    )


def format_ax_status(result: AxTrustStatus, *, agent_id: str | None = None) -> str:
    """Render one portable human-status line."""
    line = f"[ax] {result.status}"
    if agent_id is not None:
        line += f" agent={agent_id}"
    if result.reason:
        line += f" — {result.reason}"
    if result.remediation:
        line += f". Remediation: {result.remediation}"
    return line
