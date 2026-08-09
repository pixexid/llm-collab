"""Recipient-side bb bootstrap entry condition (GH-564 Slice 1B, AC0/AC8).

This module owns ONE decision: may a first delivery bootstrap a bb thread for this
participant, right now? It performs no native call, writes no canonical row, and
holds no transport. The caller — the recipient's own watcher, before autobridge
session enumeration — acts on the decision.

Three properties are load-bearing and each is a separate refusal rather than a
combined truth test, because a combined test cannot say WHY it refused and a
caller that cannot distinguish "not enabled" from "already bound" will eventually
bootstrap over a live thread:

* **Default off.** Absent or false project configuration refuses before anything
  is read. AC8 requires no bb process, no HTTP call, no canonical row and no
  routing change when disabled, so the disabled answer must come first and must
  not depend on any other lookup succeeding.
* **Exact absence only.** Bootstrap is for the case where no session exists at
  all. Any existing session for the exact (project, chat, participant) — whatever
  its state — means this is not a first delivery, and the ordinary path owns it.
* **Terminal states refuse, never bootstrap.** An unreadable, mismatched,
  ambiguous or scope-refused binding is a repair. Bootstrapping through one would
  mint a second owner for a participant that already has a contested one, which is
  the failure the whole exact-binding contract exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .bb_client import BbProfile, SLICE_1A_PROFILE

# Refusal reasons. Distinct values because the caller logs them and an operator
# reads them: "not enabled" and "already bound" are different situations with
# different repairs, and collapsing them hides which one happened.
BOOTSTRAP_DISABLED = "bb_bootstrap_disabled"
BOOTSTRAP_NOT_ENABLED_FOR_PROJECT = "bb_bootstrap_project_not_enabled"
BOOTSTRAP_SESSION_EXISTS = "bb_bootstrap_session_exists"
BOOTSTRAP_TERMINAL_BINDING = "bb_bootstrap_terminal_binding"
BOOTSTRAP_NO_PACKET = "bb_bootstrap_no_first_packet"
BOOTSTRAP_REPO_TARGET_REQUIRED = "bb_bootstrap_repo_target_required"
BOOTSTRAP_REPO_TARGET_AMBIGUOUS = "bb_bootstrap_repo_target_ambiguous"

# A binding in any of these states is contested or unreadable. None of them is a
# first delivery, and none may be bootstrapped through.
TERMINAL_BINDING_STATES = frozenset(
    {"unreadable", "mismatch", "ambiguous", "scope_refused"}
)

# Frontmatter fields whose presence marks a packet as activation-intent. A
# packet carrying ANY of them is not an ordinary message: per
# docs/schema-reference.md a partial or malformed activation marker must fail
# closed before execution. Mirrors ``ACTIVATION_MARKER_FIELDS`` in
# ``bin/_activation_identity.py``; kept independent here so the library
# decision does not import from ``bin/``.
ACTIVATION_MARKER_FIELDS = ("activation", "worktree", "branch")

# A first delivery classifies into exactly one of these at the
# profile-resolution seam (GH-596). read_only launches on SLICE_1A_PROFILE;
# authoring and malformed_activation both refuse before any spawn, with distinct
# reasons so a malformed packet is never misreported as a profile decision.
ASSIGNMENT_READ_ONLY = "read_only"
ASSIGNMENT_AUTHORING = "authoring"
ASSIGNMENT_MALFORMED_ACTIVATION = "malformed_activation"


def _worktree_is_lexical_canonical(value: Any) -> bool:
    """The schema's lexical canonical form for an activation worktree.

    Pure string check (no filesystem access): the receiver must compare the
    serialized value byte-exact, so a worktree that is not already in lexical
    normal form is malformed. Existence is the activation authority's
    strict-resolve lane, not this one. Rules from docs/schema-reference.md:
    absolute, no double-leading slash, no dot/dotdot segments, no duplicate
    separators, no non-root trailing separator.
    """
    if not isinstance(value, str):
        return False
    if not value.startswith("/") or value.startswith("//"):
        return False
    if len(value) > 1 and value.endswith("/"):
        return False
    for index, segment in enumerate(value.split("/")):
        if index == 0:
            continue
        if segment in ("", ".", ".."):
            return False
    return True


# The one triple the GH-596 bake-off measured. Spelled literally, not derived
# from SLICE_1A_PROFILE: retargeting that constant must not silently qualify a
# new, unmeasured profile.
AUTHORING_QUALIFIED_PROFILES = frozenset(
    {BbProfile(provider="pi", model="kimi-coding/k3", reasoning_level="high")}
)


def profile_is_authoring_qualified(profile: BbProfile) -> bool:
    """Return whether the resolved profile is in the measured qualified set."""
    return profile in AUTHORING_QUALIFIED_PROFILES


def classify_first_delivery_assignment(packet: Mapping[str, Any] | None) -> str:
    """Classify a first delivery for the profile-resolution seam (GH-596).

    Returns ``ASSIGNMENT_READ_ONLY``, ``ASSIGNMENT_AUTHORING``, or
    ``ASSIGNMENT_MALFORMED_ACTIVATION``. The work type is read from the
    activation markers the packet carries, never from its body — a guard bound
    to how a caller phrased the prompt is the wrong proxy.

    A packet carrying ANY activation marker (``activation``/``worktree``/``branch``
    present in the frontmatter) is NOT read-only: the schema marks a partial or
    malformed marker malformed and requires consumers to fail closed before
    execution, never treating it as an ordinary message. Only a packet with no
    marker of any kind may take the read-only launch.

    A complete, well-formed writer-lane identity — ``activation`` exactly boolean
    ``True`` plus a canonical-absolute ``worktree`` and a non-blank ``branch`` —
    is an authoring assignment (-> profile_unavailable: no profile is
    authoring-qualified). Any other marker-bearing packet is
    ``malformed_activation`` (-> a distinct refusal, so it is not misreported as
    a profile decision). The ``to``-field/target-agent match is validated by the
    activation authority lane, which holds the claiming-target context this
    classifier does not; such a packet is still refused, never launched.
    """
    if not isinstance(packet, Mapping):
        return ASSIGNMENT_READ_ONLY
    if not any(field in packet for field in ACTIVATION_MARKER_FIELDS):
        return ASSIGNMENT_READ_ONLY
    if (
        packet.get("activation") is True
        and _worktree_is_lexical_canonical(packet.get("worktree"))
        and isinstance(packet.get("branch"), str)
        and packet.get("branch").strip()
    ):
        return ASSIGNMENT_AUTHORING
    return ASSIGNMENT_MALFORMED_ACTIVATION


@dataclass(frozen=True)
class BootstrapRefusal:
    reason: str
    detail: str


def resolve_bootstrap_repo_id(
    project: Mapping[str, Any] | None,
    packet_repo_targets: Any,
) -> str | BootstrapRefusal:
    """Resolve the repository a bootstrap prompt is allowed to execute in.

    An explicit project setting is authoritative. Otherwise the packet must
    name exactly one repository; bootstrap must not invent a default for a
    prompt that is about to execute in a real project.
    """
    bb = project.get("bb") if isinstance(project, Mapping) else None
    configured = bb.get("repo_id") if isinstance(bb, Mapping) else None
    if configured is not None:
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return BootstrapRefusal(
            BOOTSTRAP_REPO_TARGET_AMBIGUOUS,
            "bb.repo_id must be a non-empty string when configured",
        )
    if not isinstance(packet_repo_targets, list) or not packet_repo_targets:
        return BootstrapRefusal(
            BOOTSTRAP_REPO_TARGET_REQUIRED,
            "packet must declare exactly one repo target for bb bootstrap",
        )
    if len(packet_repo_targets) != 1:
        return BootstrapRefusal(
            BOOTSTRAP_REPO_TARGET_AMBIGUOUS,
            "packet must declare exactly one repo target for bb bootstrap",
        )
    repo_id = packet_repo_targets[0]
    if not isinstance(repo_id, str) or not repo_id.strip():
        return BootstrapRefusal(
            BOOTSTRAP_REPO_TARGET_AMBIGUOUS,
            "packet repo target must be a non-empty string",
        )
    return repo_id.strip()


@dataclass(frozen=True)
class BootstrapPlan:
    """One first delivery that may create exactly one bb thread.

    Carries the packet identity so the caller can dedup on it BEFORE spawning:
    bb has no idempotency, so a duplicate first delivery that reaches the spawn
    produces a second real thread.
    """

    project_id: str
    conversation_id: str
    participant_id: str
    agent_id: str
    canonical_message_id: str
    packet_path: str


def project_enables_bb(project: Mapping[str, Any] | None) -> bool:
    """True only for an explicit boolean true under the project's `bb.enabled`.

    Deliberately strict: a missing project, a missing `bb` block, a non-mapping
    `bb`, a missing key, or a truthy non-boolean (`"yes"`, `1`) all read as
    disabled. Default-off is the contract, so anything ambiguous is off rather
    than "probably meant on".
    """
    if not isinstance(project, Mapping):
        return False
    bb = project.get("bb")
    if not isinstance(bb, Mapping):
        return False
    return bb.get("enabled") is True


def plan_bootstrap(
    *,
    enabled: bool,
    project: Mapping[str, Any] | None,
    project_id: str,
    conversation_id: str,
    participant_id: str,
    agent_id: str,
    existing_session_ids: Sequence[str],
    binding_state: str | None,
    first_packet: Mapping[str, Any] | None,
) -> BootstrapPlan | BootstrapRefusal:
    """Decide whether this delivery may bootstrap. Pure: no I/O, no native call.

    Order is part of the contract. The adapter-disabled check runs first so a
    disabled deployment reaches no lookup at all, and the existence check runs
    before the packet read so an already-bound participant never causes a packet
    to be parsed for a decision that was already made.
    """
    if not enabled:
        return BootstrapRefusal(BOOTSTRAP_DISABLED, "bb adapter is disabled")
    if not project_enables_bb(project):
        return BootstrapRefusal(
            BOOTSTRAP_NOT_ENABLED_FOR_PROJECT,
            f"project {project_id!r} does not set bb.enabled: true",
        )
    if binding_state in TERMINAL_BINDING_STATES:
        return BootstrapRefusal(
            BOOTSTRAP_TERMINAL_BINDING,
            f"binding state {binding_state!r} is terminal; repair it rather than bootstrapping",
        )
    if existing_session_ids:
        return BootstrapRefusal(
            BOOTSTRAP_SESSION_EXISTS,
            f"{len(existing_session_ids)} session(s) already exist for this participant",
        )
    if not isinstance(first_packet, Mapping):
        return BootstrapRefusal(BOOTSTRAP_NO_PACKET, "no first packet to bootstrap from")
    canonical_message_id = first_packet.get("canonical_message_id")
    packet_path = first_packet.get("path")
    if not isinstance(canonical_message_id, str) or not canonical_message_id:
        return BootstrapRefusal(
            BOOTSTRAP_NO_PACKET, "first packet has no canonical_message_id to dedup on"
        )
    if not isinstance(packet_path, str) or not packet_path:
        return BootstrapRefusal(BOOTSTRAP_NO_PACKET, "first packet has no path")
    return BootstrapPlan(
        project_id=project_id,
        conversation_id=conversation_id,
        participant_id=participant_id,
        agent_id=agent_id,
        canonical_message_id=canonical_message_id,
        packet_path=packet_path,
    )


@dataclass(frozen=True)
class BootstrapOutcome:
    """What one bootstrap attempt did, in terms the watcher can log and act on."""

    state: str
    native_thread_id: str | None = None
    detail: str = ""


BOOTSTRAP_STARTED = "bb_bootstrap_started"
BOOTSTRAP_DUPLICATE = "bb_bootstrap_duplicate_first_packet"
BOOTSTRAP_AMBIGUOUS = "bb_bootstrap_ambiguous_start"
BOOTSTRAP_ORPHANED = "bb_bootstrap_orphaned"
BOOTSTRAP_FAILED = "bb_bootstrap_failed"
# No authoring-qualified BB profile exists yet (GH-596): the only measured
# profile (SLICE_1A_PROFILE) is read-only. A writer-lane first delivery refuses
# here rather than launching an analysis-only model on implementation work. This
# is the implemented fail-closed half of the prospective profile_unavailable
# selector; a later authoring evaluation lifts it per model.
BOOTSTRAP_PROFILE_UNAVAILABLE = "bb_bootstrap_profile_unavailable"
# A first delivery carrying a partial or malformed activation marker (any of
# activation/worktree/branch present without a complete, well-formed identity)
# refuses here too — never launched — but as a distinct reason so it is not
# misreported as a profile decision. Per docs/schema-reference.md a malformed
# activation marker must fail closed before execution; it is never an ordinary
# message.
BOOTSTRAP_MALFORMED_ACTIVATION = "bb_bootstrap_malformed_activation"


def execute_bootstrap(
    plan: BootstrapPlan,
    *,
    already_started: Callable[[str], bool],
    start: Callable[[BootstrapPlan], Any],
    on_ambiguous: type[Exception],
    on_orphaned: type[Exception],
) -> BootstrapOutcome:
    """Run one bootstrap: dedup, then exactly one start, then classify the result.

    Dependencies are injected rather than imported so this is provable without a
    ledger, a bb process, or a watcher. The three saga shapes are mapped here
    because the watcher must log them differently and must never retry two of
    them.

    Dedup comes FIRST and is the whole reason AC4 exists: bb has no idempotency,
    so a duplicate first delivery that reaches the start produces a second real
    thread. Checking after the start would be a check of something already done.
    """
    if already_started(plan.canonical_message_id):
        return BootstrapOutcome(
            BOOTSTRAP_DUPLICATE,
            detail=f"{plan.canonical_message_id} already started a thread",
        )
    try:
        result = start(plan)
    except on_orphaned as error:  # a thread exists; hand its id back to be reconciled
        return BootstrapOutcome(
            BOOTSTRAP_ORPHANED,
            native_thread_id=getattr(error, "native_session_id", None),
            detail=str(error),
        )
    except on_ambiguous as error:  # bb may have created it; NEVER retried
        return BootstrapOutcome(
            BOOTSTRAP_AMBIGUOUS,
            native_thread_id=getattr(error, "native_session_id", None),
            detail=str(error),
        )
    except Exception as error:  # no thread was created; a plain failure is honest
        return BootstrapOutcome(BOOTSTRAP_FAILED, detail=str(error))
    return BootstrapOutcome(
        BOOTSTRAP_STARTED, native_thread_id=str(result), detail="one thread started"
    )
