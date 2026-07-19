"""Canonical activation identity and delivery-foundation helpers (Lane A).

An ACTIVATION packet grants a lane to exactly one writer. This module owns the
NON-MUTATING foundation: the exact activation identity tuple
(project, chat, task, worktree, branch, target_agent), its canonicalization
and lease-key derivation, the malformed-packet classifier, and the claim
command / bounded AX ring-prompt builders.

Activation AUTHORITY (lease grant/assert/release), inbox consumption, runtime
dispatch, and cleanup land in later lanes; nothing in this module mutates
state or enforces policy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from _helpers import ROOT

IDENTITY_FIELDS = ("project", "chat", "task", "worktree", "branch", "target_agent")

# Frontmatter fields whose presence marks a packet as activation-intent. A
# packet carrying ANY of them must form a complete valid identity or be
# treated as malformed — partial activation never downgrades to an ordinary
# message.
ACTIVATION_MARKER_FIELDS = ("activation", "worktree", "branch")

AX_DOORBELL_MAX_CHARS = 240


def canonical_worktree(value: str) -> str:
    """Absolute canonical form: expanduser + resolve (symlinks, `..`).

    A RELATIVE path is refused by callers before reaching here — it has no
    CWD-independent meaning, so readers and dispatchers would derive
    different lease keys for the same packet."""
    return str(Path(value).expanduser().resolve())


def lease_identity(args_or_mapping: Any) -> dict[str, str]:
    """Build the exact activation identity; ValueError on any missing field."""
    identity: dict[str, str] = {}
    for field in IDENTITY_FIELDS:
        value = (
            args_or_mapping.get(field)
            if isinstance(args_or_mapping, dict)
            else getattr(args_or_mapping, field, None)
        )
        if value is not None:
            value = str(value).strip()
        if not value:
            raise ValueError(f"activation identity requires --{field.replace('_', '-')}")
        identity[field] = value
    identity["worktree"] = canonical_worktree(identity["worktree"])
    return identity


def lease_key(identity: dict[str, str]) -> str:
    canonical = "\x1f".join(identity[field] for field in IDENTITY_FIELDS)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def classify_activation(
    frontmatter: dict[str, Any], *, target_agent: str
) -> tuple[str, dict[str, Any] | None]:
    """Classify a message's activation intent for a consuming boundary.

    Returns one of:
    - ("none", None): no activation markers — an ordinary message.
    - ("activation", identity): complete valid identity.
    - ("malformed", {"detail": ...}): activation markers present but the
      identity is incomplete/invalid — consumers must fail closed, never
      downgrade.
    """
    if not any(frontmatter.get(field) for field in ACTIVATION_MARKER_FIELDS):
        return "none", None
    try:
        identity = lease_identity(
            {
                "project": frontmatter.get("project_id"),
                "chat": frontmatter.get("chat_id"),
                "task": frontmatter.get("related_task"),
                "worktree": frontmatter.get("worktree"),
                "branch": frontmatter.get("branch"),
                "target_agent": target_agent,
            }
        )
    except ValueError as exc:
        return "malformed", {"detail": str(exc)}
    return "activation", identity


def build_activation_consume_command(recipient: str, project: str, packet_name: str) -> str:
    """The exact, absolute claim command for one activation packet.

    Absolute canonical launcher (a product worktree has no bin/llm-collab),
    no placeholders, exact-packet scoped via --packet. The --packet consuming
    behavior ships with the runtime-integration lane; the serialized command
    is the stable contract."""
    launcher = ROOT / "bin" / "llm-collab"
    return (
        f"{launcher} inbox.py --me {recipient} --project {project} "
        f"--packet {packet_name}"
    )


def build_activation_ring_prompt(sender: str, related_task: str, command: str) -> str:
    """AX ring for an activation packet, guaranteed <= AX_DOORBELL_MAX_CHARS.

    Tiers drop prose, never the runnable command or its packet selector. If
    even the minimal marker + backticked command cannot fit, raise — callers
    decide the enforcement policy (a later lane makes AX-path deliveries fail
    closed; foundation callers may fall back)."""
    tiers = (
        f"[from {sender}] ACTIVATION {related_task}: claim via `{command}` — do not Read the packet file.",
        f"[from {sender}] ACTIVATION {related_task}: claim via `{command}`",
        f"[from {sender}] `{command}`",
    )
    for prompt in tiers:
        if len(prompt) <= AX_DOORBELL_MAX_CHARS:
            return prompt
    overhead = len(f"[from {sender}] ``")
    raise ValueError(
        f"activation ring cannot fit the {AX_DOORBELL_MAX_CHARS}-char AX budget: "
        f"the command alone is {len(command)} chars (max {AX_DOORBELL_MAX_CHARS - overhead} "
        f"with the minimal marker) — shorten the workspace path or packet title"
    )


def activation_body_banner(consume_command: str) -> str:
    """Prepended to every activation packet body. States the canonical claim
    step without claiming that lease ENFORCEMENT is active — activation
    authority ships in a later lane."""
    return "\n".join(
        [
            "> ACTIVATION PACKET — reading this file directly is not a writer grant.",
            "> Canonical claim step (activation authority is enforced once the",
            "> lease-authority lanes land; treat it as required now):",
            f"> `{consume_command}`",
        ]
    )
