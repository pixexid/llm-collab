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
import shlex
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

    A RELATIVE path (after ~-expansion) is REFUSED — it has no
    CWD-independent meaning, so readers and dispatchers would derive
    different lease keys for the same packet. This invariant lives here, in
    the shared validator, so no consumer can drift."""
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise ValueError(
            "--worktree must be an absolute path: a relative path has no "
            "CWD-independent meaning, so readers and dispatchers would derive "
            "different activation identities for the same packet"
        )
    return str(expanded.resolve())


def normalized_identity_field(field: str, value: Any) -> str:
    """Shared per-field validator: strip, refuse missing/blank, refuse
    control characters.

    A newline (or any C0/DEL control) inside an identity field would be
    serialized by dump_frontmatter as ADDITIONAL frontmatter lines — a
    frontmatter-injection channel that could rewrite project/chat/target in
    the emitted packet. Refused before any mutation."""
    if value is None:
        raise ValueError(f"activation identity requires --{field.replace('_', '-')}")
    raw = str(value)
    # Controls are checked on the ORIGINAL value, BEFORE any trimming: a
    # leading/trailing C0, DEL, NEL (U+0085), U+2028, or U+2029 must fail
    # closed, never be silently normalized away. The parser iterates
    # frontmatter with str.splitlines(), which breaks on all of these \u2014 an
    # injectable frontmatter line. Ordinary printable non-ASCII stays valid.
    if any(
        ord(ch) < 32 or ord(ch) == 127 or ch in "\x85\u2028\u2029"
        for ch in raw
    ):
        raise ValueError(
            f"--{field.replace('_', '-')} contains control or line-breaking "
            "characters; identity fields must be single-line printable text"
        )
    # Only after the raw value is proven control-free: trim ordinary outer
    # whitespace (all line-breaking whitespace was already refused above).
    value = raw.strip()
    if not value:
        raise ValueError(f"activation identity requires --{field.replace('_', '-')}")
    if not frontmatter_roundtrips(value):
        raise ValueError(
            f"--{field.replace('_', '-')} value {value!r} would be coerced by "
            "frontmatter serialization (true/false/null, integer, or "
            "bracketed forms do not round-trip as strings); choose a value "
            "that survives serialization byte-exact"
        )
    return value


def frontmatter_roundtrips(value: str) -> bool:
    """True when the YAML-lite frontmatter parser returns this exact string.

    The parser coerces true/false/null (case-insensitive), integers, and
    [bracketed] forms; the serializer has no quoting mechanism, so a value in
    one of those families CANNOT be transported byte-exact — sender and
    receiver would derive different lease keys. Such values are refused."""
    if value.lower() in {"true", "false", "null"}:
        return False
    if value.startswith("[") and value.endswith("]"):
        return False
    try:
        int(value)
    except ValueError:
        return True
    return False


def lease_identity(args_or_mapping: Any) -> dict[str, str]:
    """Build the exact activation identity — the ONE shared validation path.

    ValueError on any missing/blank (whitespace-only) field and on a
    relative worktree."""
    identity: dict[str, str] = {}
    for field in IDENTITY_FIELDS:
        value = (
            args_or_mapping.get(field)
            if isinstance(args_or_mapping, dict)
            else getattr(args_or_mapping, field, None)
        )
        identity[field] = normalized_identity_field(field, value)
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
    # Marker PRESENCE, not truthiness: `activation: false`, empty worktree,
    # or a null branch are activation-SHAPED packets with a broken identity —
    # they must classify malformed, never downgrade to ordinary.
    if not any(field in frontmatter for field in ACTIVATION_MARKER_FIELDS):
        return "none", None
    # Only `activation: true` marks a writer packet (schema): a PRESENT
    # activation key with any other value is malformed even when every other
    # identity field is valid.
    if "activation" in frontmatter and frontmatter["activation"] is not True:
        return "malformed", {
            "detail": "activation must be exactly true when present; "
            f"got {frontmatter['activation']!r}"
        }
    # Parsed identity values must arrive as strings. The YAML-lite parser
    # coerces unquoted true/false/null/integers, so a packet whose identity
    # field parsed into a non-str was NOT produced by the validated delivery
    # path (which refuses coercible scalars) — its identity cannot match the
    # sender's byte-exact and it is malformed.
    for source_field in ("project_id", "chat_id", "related_task", "worktree", "branch"):
        value = frontmatter.get(source_field)
        if source_field not in frontmatter or value is None:
            continue
        if not isinstance(value, str):
            return "malformed", {
                "detail": f"{source_field} parsed as {type(value).__name__} "
                f"({value!r}); activation identity fields must round-trip as strings"
            }
    # Receiver/classification boundary: the legitimate delivery path always
    # serializes an ALREADY-ABSOLUTE canonical worktree, so a raw packet
    # value that is not absolute (`~/lane`, `../lane`) was hand-written and
    # would expand against the CONSUMER's home/CWD — a per-consumer identity.
    # Fail closed BEFORE any expansion; sender-side expanduser convenience
    # lives only in the delivery CLI.
    raw_worktree = frontmatter.get("worktree")
    if isinstance(raw_worktree, str) and not Path(raw_worktree.strip()).is_absolute():
        return "malformed", {
            "detail": "packet worktree must be serialized absolute; got "
            f"{raw_worktree!r} (home/CWD-relative spellings are consumer-dependent)"
        }
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


def build_activation_consume_command(
    recipient: str, project: str, chat_id: str, packet_name: str
) -> str:
    """The exact, absolute claim command for one activation packet.

    Absolute canonical launcher (a product worktree has no bin/llm-collab),
    no placeholders, and EXACT scoping: collision-free allocation is
    per-chat-dir, so the selector pairs `--chat <id>` with `--packet <name>`
    — two chats holding the same basename can never make the command
    ambiguous. The consuming behavior ships with the runtime-integration
    lane; the serialized command is the stable contract."""
    launcher = ROOT / "bin" / "llm-collab"
    return shlex.join(
        [
            str(launcher),
            "inbox.py",
            "--me",
            recipient,
            "--project",
            project,
            "--chat",
            chat_id,
            "--packet",
            packet_name,
        ]
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
