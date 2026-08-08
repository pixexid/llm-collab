#!/usr/bin/env python3
"""
session_autobridge.py — experimental session-scoped inbox autobridge registry.

The spike keeps the implementation intentionally small:
- session lease records under State/session_autobridge/sessions/
- a bounded dispatcher that matches unread inbox messages to a parked session
- runtime-trigger support only when an explicit runtime command exists
- human-relay fallback that writes a concrete relay prompt artifact
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python
from current_runtime import require_current_runtime

require_python()

import argparse
import json
import os

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from _helpers import (
    canonical_path,
    ensure_project,
    get_agent,
    now_utc,
    resolve_project_repo_path,
    utc_iso,
)
from _session_autobridge import (
    BindingUnreadable,
    CanonicalBindingNativeMismatch,
    HEURISTIC_RUNTIME_DISCOVERY_FAMILIES,
    HEURISTIC_RUNTIME_DISCOVERY_REFUSED_REASON,
    SESSION_MODES,
    SESSION_STATUSES,
    WAKE_STRATEGIES,
    MAX_CLAUDE_RECORD_PREFIX_BYTES,
    _claude_session_evidence,
    claude_home,
    claude_project_slug,
    discover_runtime_session,
    dispatch_session,
    iter_sessions,
    session_is_dispatchable,
    session_requires_exact_receive_target,
    _session_write_lock,
    load_binding,
    load_session,
    binding_payload_from_session,
    prepare_binding_write,
    prepare_session_write,
    provision_pi_canonical_binding,
    PiProvisioningRefused,
    read_pi_session_fingerprint,
    resolve_active_canonical_binding,
    resolve_active_canonical_owner,
    runtime_home_from_source,
    save_session,
    UnreadableFile,
    update_binding_from_session,
)
from _activation_lease import (
    LeaseRefused,
    assert_lease,
    claim_lease,
    lease_identity,
    load_lease,
    owner_summary,
    release_lease,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Experimental session autobridge registry.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="Create or update a parked session lease")
    register.add_argument("--session", required=True, help="Stable session identifier")
    register.add_argument("--agent", required=True, help="Agent ID the parked session belongs to")
    register.add_argument("--project", default=None, help="Optional project_id filter")
    register.add_argument("--chat", default=None, help="Optional chat_id filter")
    register.add_argument(
        "--repo-target",
        dest="repo_targets",
        action="append",
        default=None,
        help="Explicit repository subscription; repeat for multiple repositories",
    )
    register.add_argument("--mode", default="manual", choices=SESSION_MODES)
    register.add_argument("--status", default="parked", choices=SESSION_STATUSES)
    register.add_argument("--wake-strategy", default="none", choices=WAKE_STRATEGIES)
    register.add_argument("--lease-owner", default=None, help="Who activated this session")
    register.add_argument("--ttl-seconds", type=int, default=3600, help="Lease TTL in seconds")
    register.add_argument("--allowed-action", dest="allowed_actions", action="append", default=[])
    register.add_argument("--runtime-family", default=None, help="Runtime family, e.g. codex_app, claude_app, gemini_cli")
    register.add_argument("--runtime-session-id", default=None, help="Current runtime-native session identifier")
    register.add_argument("--runtime-session-source", default=None, help="Where the runtime session identifier came from")
    register.add_argument(
        "--runtime-home",
        default=None,
        help=(
            "Exact runtime home (e.g. CODEX_HOME) used to resolve the delivery "
            "transport. Canonicalised on write; must match the value the runtime was "
            "launched with, since discovery matches CODEX_HOME literally."
        ),
    )
    register.add_argument(
        "--endpoint-id",
        default=None,
        help="Native-extension-supplied endpoint identity (Pi provisioning). Frozen "
        "by reserve/consume; the bridge derives no second identity.",
    )
    register.add_argument(
        "--runtime-instance-id",
        default=None,
        help="Native-extension-supplied runtime instance identity (Pi provisioning).",
    )
    register.add_argument(
        "--cwd",
        default=None,
        help="Working directory of the native Pi session (its ctx.cwd). Must be a real "
        "directory beneath the project's repository root.",
    )
    register.add_argument(
        "--expect-pi-provider",
        default=None,
        help="Expected Pi provider; requires the matching model and thinking flags.",
    )
    register.add_argument("--expect-pi-model", default=None, help="Expected Pi model ID.")
    register.add_argument("--expect-pi-thinking", default=None, help="Expected Pi thinking level.")
    register.add_argument("--supersedes-session", default=None, help="Older llm-collab session replaced by this registration")
    register.add_argument(
        "--runtime-command",
        default=None,
        help="Optional JSON array command for runtime_trigger, e.g. [\"python3\",\"worker.py\"]",
    )
    register.add_argument("--runtime-timeout", type=int, default=30, help="Runtime trigger timeout in seconds")
    register.add_argument("--json", dest="json_output", action="store_true")

    discover = subparsers.add_parser("discover-runtime", help="Discover current runtime session metadata")
    discover.add_argument("--runtime-family", required=True, choices=("codex_app", "claude_app", "gemini_cli"))
    discover.add_argument("--project-path", default=None, help="Optional project path hint for runtime discovery")
    discover.add_argument("--json", dest="json_output", action="store_true")

    publish = subparsers.add_parser("publish-current", help="Discover and publish current runtime session into a session lease")
    publish.add_argument("--session", required=True, help="Stable llm-collab session identifier")
    publish.add_argument("--agent", required=True, help="Agent ID the parked session belongs to")
    publish.add_argument("--runtime-family", required=True, choices=("codex_app", "claude_app", "gemini_cli"))
    publish.add_argument("--project", default=None, help="Optional project_id filter")
    publish.add_argument("--chat", default=None, help="Optional chat_id filter")
    publish.add_argument(
        "--repo-target",
        dest="repo_targets",
        action="append",
        default=None,
        help="Explicit repository subscription; repeat for multiple repositories",
    )
    publish.add_argument("--project-path", default=None, help="Optional runtime project path hint")
    publish.add_argument("--mode", default="notify", choices=SESSION_MODES)
    publish.add_argument("--status", default="parked", choices=SESSION_STATUSES)
    publish.add_argument("--wake-strategy", default="none", choices=WAKE_STRATEGIES)
    publish.add_argument("--lease-owner", default=None, help="Who activated this session")
    publish.add_argument("--ttl-seconds", type=int, default=3600, help="Lease TTL in seconds")
    publish.add_argument("--supersedes-session", default=None, help="Older llm-collab session replaced by this registration")
    publish.add_argument("--json", dest="json_output", action="store_true")

    show = subparsers.add_parser("show", help="Show a registered session")
    show.add_argument("--session", required=True)
    show.add_argument("--json", dest="json_output", action="store_true")

    show_binding = subparsers.add_parser("show-binding", help="Show a canonical chat/agent runtime binding")
    show_binding.add_argument("--project", required=True)
    show_binding.add_argument("--chat", required=True)
    show_binding.add_argument("--agent", required=True)
    show_binding.add_argument("--json", dest="json_output", action="store_true")

    dispatch = subparsers.add_parser("dispatch", help="Run one bounded dispatch pass")
    dispatch.add_argument("--session", required=True)
    dispatch.add_argument("--project", default=None, help="Invocation project scope")
    dispatch.add_argument(
        "--repo-target",
        dest="repo_targets",
        action="append",
        default=None,
        help="Explicit repository subscription; repeat for multiple repositories",
    )
    dispatch.add_argument("--json", dest="json_output", action="store_true")

    deactivate = subparsers.add_parser("deactivate", help="Stop or supersede a session lease")
    deactivate.add_argument("--session", required=True)
    deactivate.add_argument("--status", default="stopped", choices=("stopped", "superseded"))
    deactivate.add_argument("--superseded-by", default=None)
    deactivate.add_argument("--json", dest="json_output", action="store_true")

    deactivate_pi = subparsers.add_parser(
        "deactivate-pi",
        help="Stop every registered session for one exact native Pi session",
    )
    deactivate_pi.add_argument("--native-session-id", required=True)
    deactivate_pi.add_argument("--json", dest="json_output", action="store_true")

    def add_activation_identity(subparser):
        subparser.add_argument("--project", required=True)
        subparser.add_argument("--chat", required=True)
        subparser.add_argument("--task", required=True)
        subparser.add_argument("--worktree", required=True)
        subparser.add_argument("--branch", required=True)
        subparser.add_argument("--target-agent", dest="target_agent", required=True)
        subparser.add_argument("--json", dest="json_output", action="store_true")

    lease_claim = subparsers.add_parser(
        "lease-claim",
        help="Claim the one-writer activation lease for an exact activation identity",
    )
    add_activation_identity(lease_claim)
    lease_claim.add_argument("--session", required=True, help="Claiming session identifier")
    lease_claim.add_argument("--owner-pid", type=int, default=None, help="Live claiming process id")
    lease_claim.add_argument("--claimant-runtime-id", default=None, help="Current runtime/session id")
    lease_claim.add_argument("--ttl-seconds", type=int, default=3600)
    lease_claim.add_argument("--takeover", action="store_true", help="Explicitly replace an expired or provably dead owner")

    lease_show = subparsers.add_parser("lease-show", help="Show the activation lease for an identity")
    add_activation_identity(lease_show)

    lease_assert = subparsers.add_parser(
        "lease-assert",
        help="Assert current activation authority before mutating",
    )
    add_activation_identity(lease_assert)
    lease_assert.add_argument("--session", required=True, help="Owning session identifier")
    lease_assert.add_argument("--fence-token", type=int, required=True)
    lease_assert.add_argument("--owner-pid", type=int, default=None, help="Asserting process id")
    lease_assert.add_argument("--claimant-runtime-id", default=None, help="Current runtime/session id")

    lease_release = subparsers.add_parser("lease-release", help="Release an activation lease you own")
    add_activation_identity(lease_release)
    lease_release.add_argument("--session", required=True, help="Owning session identifier")
    lease_release.add_argument("--fence-token", type=int, required=True)
    lease_release.add_argument("--owner-pid", type=int, default=None, help="Releasing process id")
    lease_release.add_argument("--claimant-runtime-id", default=None, help="Current runtime/session id")
    lease_release.add_argument("--status", default="released", choices=("released", "superseded"))
    args = parser.parse_args()
    if getattr(args, "repo_targets", None) is not None:
        project = getattr(args, "project", None)
        if project is None:
            parser.error("--repo-target requires --project <id>")
    return args


def emit(payload: dict, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(json.dumps(payload, indent=2, sort_keys=True))


def canonical_runtime_home(value: str | None) -> str | None:
    """Normalise a runtime home using the ONE shared path invariant.

    Delivery resolves an endpoint by matching `CODEX_HOME=<value>` literally against the
    running process, so registration and launch must agree exactly. A separate
    implementation here previously stored a relative `.codex` verbatim while the
    ecosystem launched `<repo>/.codex`, so the two never matched.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return str(canonical_path(text))


def plan_superseded_retirement(superseded_id: str, replacement: dict):
    """Pure preflight for retiring a superseded predecessor: load, validate scope, and
    build its terminal candidate with a prepared tuple — but write nothing.

    Returns ``(record, prepared_retirement)`` to commit later, with a ``None``
    retirement for an already-terminal predecessor, or ``None`` when absent. Raises
    ValueError on an unreadable or cross-scope predecessor. Keeping the record in the
    terminal case lets the successor inherit its duplicate-suppression authority without
    rewriting it.
    """
    try:
        record = load_session(superseded_id)
    except FileNotFoundError:
        return None
    except (ValueError, UnreadableFile) as error:
        raise ValueError(
            f"refusing to supersede unreadable session {superseded_id}: {error}"
        ) from error
    if any(
        record.get(field) != replacement.get(field)
        for field in ("agent_id", "project_id", "chat_id")
    ):
        raise ValueError(
            "refusing to retire a superseded session in a different agent/project/chat scope"
        )
    if record.get("status") not in {"active", "parked"}:
        return record, None
    record["status"] = "superseded"
    record["superseded_by"] = str(replacement["session_id"])
    return record, prepare_session_write(record)


def commit_superseded_retirement(record: dict, prepared) -> None:
    """Write the pre-validated terminal predecessor record, carrying its prepared tuple
    so nothing is reopened or recomputed (no second refusal window)."""
    if prepared is not None:
        save_session(record, prepared=prepared)


def inherit_superseded_delivery_state(replacement: dict, predecessor: dict) -> None:
    """Carry paired duplicate-suppression authority into a same-scope successor."""
    predecessor_processed = predecessor.get("processed_messages", [])
    replacement_processed = replacement.get("processed_messages", [])
    predecessor_settled = predecessor.get("canonical_settled_messages", {})
    replacement_settled = replacement.get("canonical_settled_messages", {})
    if (
        not isinstance(predecessor_processed, list)
        or not isinstance(replacement_processed, list)
        or not all(
            isinstance(path, str)
            for path in [*predecessor_processed, *replacement_processed]
        )
        or not isinstance(predecessor_settled, dict)
        or not isinstance(replacement_settled, dict)
    ):
        raise ValueError("refusing supersession with malformed duplicate-suppression state")
    replacement["processed_messages"] = list(
        dict.fromkeys([*predecessor_processed, *replacement_processed])
    )
    replacement["canonical_settled_messages"] = {
        **predecessor_settled,
        **replacement_settled,
    }


def retire_superseded_session(superseded_id: str, replacement: dict) -> None:
    """Close a session that a new registration supersedes.

    The predecessor must be the SAME agent, project and chat as the replacement.
    `--supersedes-session` names an id, and retiring whatever it names would let a
    mistaken or hostile continuation destroy an unrelated live session — another
    agent's, or another chat's. A continuation retires only its own predecessor.

    Idempotent and tolerant otherwise: an absent or already-terminal record is
    nothing to do — the point is only that no `active`/`parked` record for the old
    session outlives its replacement. A still-live, same-scope record is rewritten
    to the terminal shape `deactivate --status superseded` produces, so
    dispatchability refuses it by `status` alone.
    """
    plan = plan_superseded_retirement(superseded_id, replacement)
    if plan is not None:
        commit_superseded_retirement(*plan)


def _provision_pi_binding_or_refuse(
    args, runtime: dict, native_session_id, *, predecessor=None, actor_id=None
) -> dict:
    """Mint the canonical Pi binding from native context, then return its fields.

    Absent native context, keep #380's fail-closed. Partial context or the wrong
    repo-target count is refused explicitly. Every path refuses before any write.

    When `predecessor` is supplied the mint is a native-session replacement: the
    successor is swapped in for the named active owner via rebind (#319).
    """
    endpoint_id = getattr(args, "endpoint_id", None)
    runtime_instance_id = getattr(args, "runtime_instance_id", None)
    cwd = getattr(args, "cwd", None)
    runtime_home = runtime.get("home")
    targets = getattr(args, "repo_targets", None) or []
    supplied = [endpoint_id, runtime_instance_id, cwd, runtime_home]
    if not any(supplied):
        raise SystemExit(
            "[error] canonical_binding_required: no active canonical binding for "
            f"{args.agent} in {args.project}/{args.chat}, and no native Pi context to "
            "provision one. Supply --endpoint-id/--runtime-instance-id/--cwd/--runtime-home; "
            "no session was written."
        )
    if not all(supplied):
        raise SystemExit(
            "[error] canonical_provisioning_incomplete: Pi provisioning needs all of "
            "--endpoint-id, --runtime-instance-id, --cwd, --runtime-home; no session was written."
        )
    if len(targets) != 1:
        raise SystemExit(
            "[error] canonical_provisioning_requires_one_repo_target: pass exactly one "
            "--repo-target naming the project repos key; no session was written."
        )
    try:
        canonical = provision_pi_canonical_binding(
            args.project,
            args.chat,
            args.agent,
            native_session_id=native_session_id,
            endpoint_id=endpoint_id,
            runtime_instance_id=runtime_instance_id,
            cwd=cwd,
            runtime_home_path=runtime_home,
            repo_target=targets[0],
            predecessor=predecessor,
            actor_id=actor_id,
        )
    except PiProvisioningRefused as refusal:
        raise SystemExit(f"[error] {refusal}. No session was written.")
    return canonical


def _authorize_pi_replacement_or_refuse(args, mismatch) -> dict:
    """PURE authorization for a native-session replacement (#319): no writes.

    Reached only when an active binding exists for a DIFFERENT native session than the
    one registering. Authorized ONLY when `--supersedes-session` names a session that
    is EXACTLY the current active owner: same agent/project/chat, and the same stored
    binding id / generation / old native session. Returns the predecessor tuple
    ({binding_id, generation}) to hand to rebind; otherwise raises a bounded SystemExit
    and leaves the predecessor active and unchanged. Runs as a preflight BEFORE any
    mutation or retirement plan, so a bad supersedes refuses with this specific reason.
    """
    if not args.supersedes_session:
        raise SystemExit(
            "[error] canonical_binding_native_session_mismatch: "
            f"{mismatch}. Pass --supersedes-session naming the current owner to "
            "replace it. No session was written."
        )
    owner = resolve_active_canonical_owner(args.project, args.chat, args.agent)
    if owner is None:
        raise SystemExit(
            "[error] canonical_binding_native_session_mismatch: "
            f"{mismatch}. No session was written."
        )
    try:
        predecessor_record = load_session(str(args.supersedes_session))
    except (FileNotFoundError, ValueError, UnreadableFile):
        predecessor_record = None
    predecessor_runtime = (predecessor_record or {}).get("runtime") or {}
    if (
        predecessor_record is None
        or predecessor_record.get("agent_id") != args.agent
        or predecessor_record.get("project_id") != args.project
        or predecessor_record.get("chat_id") != args.chat
        or predecessor_runtime.get("session_id") != owner["native_session_id"]
        or predecessor_record.get("binding_id") != owner["binding_id"]
        or predecessor_record.get("binding_generation") != owner["generation"]
    ):
        raise SystemExit(
            "[error] canonical_replacement_predecessor_mismatch: "
            "--supersedes-session does not name the current active canonical owner "
            f"(binding {owner['binding_id']} gen {owner['generation']} native "
            f"{owner['native_session_id']}); no session was written."
        )
    return {"binding_id": owner["binding_id"], "generation": owner["generation"]}


def _replace_pi_binding_or_refuse(args, runtime: dict, native_session_id, predecessor) -> dict:
    """Commit the authorized native-session replacement: reserve/consume the successor
    pre-active and rebind it in for the verified predecessor tuple."""
    return _provision_pi_binding_or_refuse(
        args,
        runtime,
        native_session_id,
        predecessor=predecessor,
        actor_id=str(args.session),
    )


class AmbiguousNativeFamily(ValueError):
    """Several live runtime families share one native id — a reader cannot pick."""


class NativeSessionOwnedElsewhere(ValueError):
    """The native (family, id) already backs a dispatchable lease in another scope.

    A distinct type so callers (e.g. the activation gate) can catch ONLY the
    ownership collision and report a stable reason, rather than conflating it with
    registry corruption, save-validation failures, or family ambiguity — JSON
    clients branch on the reason to choose remediation.
    """


def resolve_native_family(native_session_id: str | None) -> str | None:
    """The runtime family that currently owns a native id, or None if none does.

    Activation readers only learn the native id (exported as
    LLM_COLLAB_READER_RUNTIME_ID), not its family — yet native IDENTITY is
    (family, id). A reader must adopt the family of the ordinary lease that owns
    this native so the GH-468 guard compares like-for-like; a synthetic label
    would let (reader, id) slip past a real (family, id) owner.

    Ownership is defined by DISPATCHABLE ordinary leases only:
      * a stopped/expired lease no longer owns the native, so its (possibly
        different) family must not be chosen over the live owner's — selecting by
        first-match regardless of status would guard the wrong family and miss
        the live collision;
      * synthetic `reader` leases are skipped so resolution returns a real
        family, never another reader's placeholder.
    Return the family iff EXACTLY ONE live family owns the id; None if none does.
    If several live families share the id (which the identity model permits), a
    reader has no basis to choose and must FAIL CLOSED, so raise rather than guess.
    """
    if not native_session_id:
        return None
    families: set[str] = set()
    for other in iter_sessions(strict=True):
        runtime = other.get("runtime") or {}
        family = runtime.get("family")
        if (
            family
            and family != "reader"
            and runtime.get("session_id") == native_session_id
            and session_is_dispatchable(other)[0]
        ):
            families.add(family)
    if len(families) > 1:
        raise AmbiguousNativeFamily(
            f"native session {native_session_id} is owned by multiple live runtime "
            f"families {sorted(families)}; a reader cannot resolve one — refusing "
            "rather than guessing"
        )
    return next(iter(families)) if families else None


def refuse_native_session_active_elsewhere(
    session_id: str,
    project_id: str,
    chat_id: str,
    native_session_id: str | None,
    native_family: str | None,
    status: str,
    readers_only: bool = False,
) -> None:
    """GH-468: a native runtime session may back a DISPATCHABLE lease in only one
    (project, chat) routing scope.

    The canonical binding layer already enforces one mutation owner per native
    session; this mirrors it for the ordinary session-lease register so two
    routing destinations cannot reach the same native conversation. The routing
    invariant is defined by `session_is_dispatchable()`, which counts BOTH an
    `active` lease and an unexpired `parked` lease as routable — and `register`/
    `publish-current` default to `parked`. Guarding only `active` would leave the
    common path open: two scopes could each register the same native as `parked`,
    both stay dispatchable, and both route to one conversation.

    Native IDENTITY is (runtime_family, runtime_session_id) — the exact key
    `resolve_exact_dispatch_pair()` matches a target by (it requires BOTH the
    bound family and the bound runtime session id). Comparing the runtime id
    alone would over-block two DISTINCT family-scoped natives that happen to share
    a textual id; comparing family too keeps the guard exactly as wide as
    dispatch. A native with no family cannot be exact-dispatched, so it cannot be
    a second routing target and is not guarded.

    The collision SCOPE is the (project_id, chat_id) pair — again the exact key
    dispatch resolves by — NOT the individual lease. Refuse only when the SAME
    native identity backs a DISPATCHABLE lease (active, or parked-and-unexpired)
    held by a DIFFERENT session in a DIFFERENT (project, chat) scope. Both scope
    conjuncts are load-bearing:
      * A record is unique per session_id, so a re-registration of the SAME
        session_id (even one that moves it to another scope — a rebind/takeover)
        just overwrites that one record; it is a move, not a second owner.
      * Within one scope, binding-scoped dispatch legitimately disambiguates
        several leases that share a native (a wildcard lease alongside a
        binding-pinned one), so a same-scope second lease is not a collision.
    Allowed: same-session (re-)registration or move, a same-scope second lease, a
    different native id, a different runtime family on the same id, reuse after
    the other lease is stopped/superseded/expired, and any non-dispatchable
    registration.

    Must run inside `_session_write_lock()` together with the register/reader
    write so the scan and the ownership-establishing write are one critical
    section (no check-then-write race between concurrent registrations).
    """
    # A fresh registration is born unexpired, so its live statuses are exactly the
    # dispatchable set; the other-lease side below applies the full
    # session_is_dispatchable() rule (which also drops expired parked owners). A
    # native without a family is not exact-dispatchable, so it is never a routing
    # collision — mirror the resolver, which requires a truthy bound family.
    if not native_session_id or not native_family or status not in {"active", "parked"}:
        return
    # strict=True: an unreadable/malformed lease must fail this authority scan
    # closed (refuse the registration) rather than be skipped as absent — a
    # corrupt record could be the dispatchable owner of this very native session.
    for other in iter_sessions(strict=True):
        # readers_only: the Pi path already has the canonical one-owner-per-native
        # constraint for canonical bindings, which raises its own specific reason;
        # there this scan only needs to catch a JSON activation-reader lease (no
        # ledger row), so skip non-reader records to avoid preempting the canonical
        # refusal. Every other caller scans all records (default).
        if readers_only and not other.get("ephemeral_reader"):
            continue
        other_runtime = other.get("runtime") or {}
        different_scope = (
            other.get("project_id") != project_id
            or other.get("chat_id") != chat_id
        )
        if (
            other.get("session_id") != session_id
            and different_scope
            and session_is_dispatchable(other)[0]
            and other_runtime.get("session_id") == native_session_id
            and other_runtime.get("family") == native_family
        ):
            raise NativeSessionOwnedElsewhere(
                "native session already owns a dispatchable binding in "
                f"{other.get('project_id')}/{other.get('chat_id')}/{other.get('agent_id')} "
                f"(session {other.get('session_id')}); a native session (family "
                f"{native_family}) may back a dispatchable lease in only one "
                "project/chat scope — deactivate the other lease or use a fresh "
                "native session"
            )


def preflight_native_session_registration(
    *, session_id: str, project_id: str, chat_id: str,
    native_session_id: str | None, native_family: str | None,
    status: str = "active",
) -> None:
    """Check native-session scope ownership without writing a lease.

    A caller that will create a chat before registering its starter needs the
    same ownership refusal without first creating the chat. Keep the check on
    the existing guard and lock; the later registration remains authoritative
    if another writer wins the gap after this read-only preflight.
    """
    with _session_write_lock():
        refuse_native_session_active_elsewhere(
            session_id, project_id, chat_id, native_session_id, native_family, status,
        )


def register_session(args) -> dict:
    agent = get_agent(args.agent)
    now = now_utc()
    expires_at = now.timestamp() + args.ttl_seconds
    lease_expires_utc = __import__("datetime").datetime.fromtimestamp(
        expires_at, tz=now.tzinfo
    ).isoformat(timespec="seconds")

    existing = {}
    try:
        existing = load_session(args.session)
    except FileNotFoundError:
        existing = {}

    runtime = existing.get("runtime") if isinstance(existing.get("runtime"), dict) else {}
    runtime = dict(runtime)
    if args.runtime_family is not None:
        runtime["family"] = args.runtime_family
    if args.runtime_session_id is not None:
        runtime["session_id"] = args.runtime_session_id
    if args.runtime_session_source is not None:
        runtime["session_source"] = args.runtime_session_source
    explicit_runtime_home = canonical_runtime_home(getattr(args, "runtime_home", None))
    runtime_home = explicit_runtime_home
    if runtime_home is None and runtime.get("family") and runtime.get("session_source"):
        # The derived fallback must pass through the SAME invariant as an explicit
        # --runtime-home. Stored raw, a relative or non-normalized source produced a home
        # that could never match the sidecar's absolute CODEX_HOME process marker, so
        # discovery found no endpoint and delivery failed with no diagnostic.
        runtime_home = canonical_runtime_home(
            runtime_home_from_source(str(runtime["family"]), runtime.get("session_source"))
        )
    if runtime_home is not None:
        runtime["home"] = runtime_home
    if args.runtime_command:
        runtime["command"] = json.loads(args.runtime_command)
        runtime["timeout_seconds"] = args.runtime_timeout
        if not isinstance(runtime["command"], list) or not all(
            isinstance(token, str) for token in runtime["command"]
        ):
            raise ValueError("--runtime-command must be a JSON array of strings")
    elif "command" in runtime and "timeout_seconds" not in runtime:
        runtime["timeout_seconds"] = args.runtime_timeout

    # GH-468: native-session ownership is checked INSIDE the write lock below,
    # together with the register write, so the scan and the ownership-establishing
    # write are one critical section (no check-then-write race between concurrent
    # registrations). Capture the native id before runtime may be set to None.
    gh468_native_session_id = runtime.get("session_id") if runtime else None
    gh468_native_family = runtime.get("family") if runtime else None

    # An explicitly named Claude session is registrable only when its own artifact
    # proves membership in the target checkout. The project slug is only a lookup
    # hint (it can collide), so the existing evidence reader validates cwd + native
    # identity together before any binding/session write.
    if args.runtime_session_id is not None and gh468_native_family == "claude_app":
        project_path = resolve_project_repo_path(str(args.project), "app")
        evidence = "unprovable"
        if project_path is not None:
            artifact_home = (
                Path(explicit_runtime_home)
                if explicit_runtime_home is not None
                else claude_home()
            )
            artifact = (
                artifact_home
                / "projects"
                / str(claude_project_slug(str(project_path)))
                / f"{args.runtime_session_id}.jsonl"
            )
            evidence = _claude_session_evidence(
                artifact,
                MAX_CLAUDE_RECORD_PREFIX_BYTES,
                os.path.realpath(project_path),
                str(args.runtime_session_id),
            )
        if evidence != "match":
            detail = (
                "belongs to another project"
                if evidence == "other_project"
                else "project membership could not be proven"
            )
            raise SystemExit(
                f"[error] Claude session artifact {detail}; refusing registration "
                "and no session was written."
            )

    if not runtime:
        runtime = None

    payload = {
        **existing,
        "session_id": args.session,
        "agent_id": args.agent,
        "agent_activation_type": agent.get("activation", {}).get("type"),
        "project_id": args.project,
        "chat_id": args.chat,
        "mode": args.mode,
        "status": args.status,
        "wake_strategy": args.wake_strategy,
        "allowed_actions": sorted(set(args.allowed_actions)),
        "lease_owner": args.lease_owner,
        "lease_expires_utc": lease_expires_utc,
        "runtime": runtime,
        "supersedes_session_id": args.supersedes_session,
        "created_utc": existing.get("created_utc", utc_iso()),
        "processed_messages": existing.get("processed_messages", []),
    }
    repo_targets = getattr(args, "repo_targets", None)
    if repo_targets is not None:
        payload["repo_targets"] = repo_targets
    # Pi dispatch requires the canonical binding to exist. If the native extension
    # supplied its identity (endpoint / native session / runtime instance / home /
    # one repo-target), register mints it once through reserve/consume (#378);
    # otherwise it fails closed rather than publish a pair every later packet would
    # refuse. Every refusal is before any write. Only pi pays the ledger read.
    if runtime and runtime.get("family") == "pi":
        native_session_id = runtime.get("session_id")
        # Read + validate the fingerprint from the EXACT registered native session
        # source BEFORE any canonical write. A missing/malformed/incomplete source, or
        # a source whose own cwd is not the checkout this registration binds, must
        # refuse before provisioning mints any lifecycle/binding row — otherwise a
        # fingerprint refusal would leave canonical partial state. The parsed dict is
        # reused as the pin, and dispatch re-reads + compares it before every wake.
        fingerprint = read_pi_session_fingerprint(
            runtime.get("session_source"), native_session_id
        )
        if fingerprint is None:
            raise SystemExit(
                "[error] canonical_pi_fingerprint_required: could not read a complete "
                "provider/model/thinking/cwd fingerprint from the exact native session "
                "source; no session was written."
            )
        expected_fingerprint = (
            getattr(args, "expect_pi_provider", None),
            getattr(args, "expect_pi_model", None),
            getattr(args, "expect_pi_thinking", None),
        )
        if any(value is not None for value in expected_fingerprint):
            if not all(isinstance(value, str) and value for value in expected_fingerprint):
                raise SystemExit(
                    "[error] canonical_pi_fingerprint_precondition_incomplete: "
                    "--expect-pi-provider, --expect-pi-model, and --expect-pi-thinking "
                    "must be supplied together; no session was written."
                )
            observed_fingerprint = (
                fingerprint["provider"],
                fingerprint["model_id"],
                fingerprint["thinking_level"],
            )
            if observed_fingerprint != expected_fingerprint:
                raise SystemExit(
                    "[error] canonical_pi_fingerprint_precondition_mismatch: the exact "
                    "native session source no longer matches the expected provider/model/"
                    "thinking tuple; no session was written."
                )
        # The source's own cwd must be the exact checkout the binding attests to
        # (same canonical realpath as --cwd); otherwise the binding proves checkout A
        # while the session runs in B and the pinned fingerprint (B) passes forever.
        claimed_cwd = getattr(args, "cwd", None)
        if not claimed_cwd or os.path.realpath(fingerprint["cwd"]) != os.path.realpath(
            claimed_cwd
        ):
            raise SystemExit(
                "[error] canonical_pi_cwd_mismatch: the native session source cwd "
                f"{fingerprint['cwd']!r} is not the registering checkout {claimed_cwd!r}; "
                "no session was written."
            )
        # The fingerprint is pure data known now; carry it so the capacity preflight
        # below sizes the final record. The canonical resolve/mint/replace is DEFERRED
        # until after the pure preflights: it can mutate the ledger (a fresh mint, or a
        # rebind that supersedes the current owner), and a capacity/binding refusal
        # after that mutation would leave canonical swapped with no session written.
        payload["pi_fingerprint"] = fingerprint
        pi_native_session_id = native_session_id
    else:
        pi_native_session_id = None
    # Validate everything that can refuse this registration BEFORE any ledger mutation
    # or retiring the predecessor. A retire or canonical swap that ran first and was
    # then followed by a preflight refusal would have destroyed a valid live session /
    # superseded a valid owner on behalf of a continuation that never completed.
    # prepare_session_write (capacity) and existing_binding_snapshot_or_refuse (binding
    # readability) are the two refusal gates; both are pure checks, so running them
    # ahead opens no two-writer window. This is purely the refusal gate — save_session
    # recomputes over the final payload once the real canonical fields land below.
    #
    # The canonical fields (binding_id/binding_generation/endpoint_id) are added only
    # AFTER the mutation, so size the capacity check against their schema maxima now.
    # Otherwise a near-limit record could pass here, perform the rebind, then overflow
    # the final save on the added bytes — leaving the owner superseded with no session.
    if pi_native_session_id is None:
        # Ordinary (non-canonical-mutating) registration. The GH-468 native-session
        # ownership scan and the ownership-establishing writes run under ONE
        # reentrant write lock (save_session re-enters it), so two concurrent
        # registrations for the same native in different scopes cannot both pass
        # the scan and both publish. The pi branch below is instead guarded by the
        # canonical one-mutation-owner-per-native-session constraint.
        # GH-468: the ownership guard keys on (family, id) and returns early when
        # the family is absent — but a routable session (exact-receive target) with
        # a runtime command + session id is dispatchable WITHOUT a family, so it
        # would slip past the scan and become an unguarded phantom owner. Its native
        # identity is incomplete, so refuse before writing rather than route it.
        if (
            gh468_native_session_id
            and not gh468_native_family
            and session_requires_exact_receive_target(payload)
        ):
            raise ValueError(
                "a routable session (runtime command or exact-receive target) must "
                "declare --runtime-family so its native identity (family, session id) "
                "is complete for the GH-468 ownership scan; refusing a family-less "
                "routable registration"
            )
        with _session_write_lock():
            refuse_native_session_active_elsewhere(
                args.session, args.project, args.chat,
                gh468_native_session_id, gh468_native_family, args.status
            )
            retirement = None
            if args.supersedes_session and str(args.supersedes_session) != str(args.session):
                retirement = plan_superseded_retirement(
                    str(args.supersedes_session), payload
                )
                if retirement is not None:
                    inherit_superseded_delivery_state(payload, retirement[0])
            prepared = prepare_session_write(payload)
            existing_binding = existing_binding_snapshot_or_refuse(payload)
            if retirement is not None:
                commit_superseded_retirement(*retirement)
            binding = update_binding_from_session(payload, existing=existing_binding)
            save_session(payload, prepared=prepared, allow_reactivation=True)
        if binding is not None:
            payload["binding"] = binding
        return payload
    # GH-468: the canonical one-owner-per-native constraint governs CANONICAL
    # bindings only. A JSON activation-reader lease for this native (persisted by
    # ensure_reader_session in another scope) has no ledger row, so the canonical
    # check below cannot see it — a reader-first-then-Pi registration for the same
    # (family, id) in a different scope would otherwise publish a second
    # dispatchable owner. Run the session-registry ownership scan here too, and
    # HOLD the cross-process session write lock continuously from the scan through
    # the canonical mint and the ownership-establishing save_session below — if it
    # were released after the scan, an activation reader for the same Pi native
    # could acquire it in the gap, see no Pi record, and publish its own cross-scope
    # lease while this registration proceeds unrescanned. This mirrors the non-pi
    # branch, which already holds the lock across its binding write (same lock
    # order, so no new deadlock surface).
    with _session_write_lock():
        refuse_native_session_active_elsewhere(
            args.session, args.project, args.chat,
            gh468_native_session_id, gh468_native_family, args.status,
            readers_only=True,
        )
        # Pi registration can MUTATE the ledger (mint, or a rebind that supersedes the
        # current owner), so EVERY pure refusal must run and be CARRIED before it. Sizing
        # the capacity preflight against the canonical fields' schema maxima (added only
        # after the mutation) is not enough on its own: prepare_session_write compacts the
        # agent inbox when oversized, and recomputing after the swap would re-read a
        # possibly-changed inbox — the TOCTOU this file already fights. So compact ONCE
        # here and carry the settled candidate forward; the post-swap serialize re-adds the
        # real (never-larger) canonical fields and cannot re-enter the oversized branch.
        # Classify the registration read-only FIRST (no writes): an exact-native match
        # resolves the binding; a DIFFERENT active native is a replacement iff
        # --supersedes-session names that exact owner — authorize it purely here so a bad
        # supersedes refuses with canonical_replacement_predecessor_mismatch BEFORE the
        # retirement plan's own scope check could raise a less specific error.
        replacement_predecessor = None
        try:
            canonical = resolve_active_canonical_binding(
                args.project, args.chat, args.agent, pi_native_session_id
            )
        except CanonicalBindingNativeMismatch as mismatch:
            canonical = None
            replacement_predecessor = _authorize_pi_replacement_or_refuse(args, mismatch)
        # Pre-validate the predecessor retirement before the canonical swap, then
        # include its dedup ledger in the successor's one carried capacity preflight.
        retirement = None
        if args.supersedes_session and str(args.supersedes_session) != str(args.session):
            retirement = plan_superseded_retirement(str(args.supersedes_session), payload)
            if retirement is not None:
                inherit_superseded_delivery_state(payload, retirement[0])
        preflight_candidate, _ = prepare_session_write(
            {
                **payload,
                "binding_id": "x" * 128,
                "binding_generation": 9223372036854775807,
                "endpoint_id": "x" * 128,
            }
        )
        for placeholder in ("binding_id", "binding_generation", "endpoint_id"):
            preflight_candidate.pop(placeholder, None)
        payload = preflight_candidate
        existing_binding = existing_binding_snapshot_or_refuse(payload)
        binding_candidate = binding_payload_from_session(
            {
                **payload,
                "binding_id": "x" * 128,
                "binding_generation": 9223372036854775807,
                "endpoint_id": "x" * 128,
            },
            existing=existing_binding,
        )
        try:
            prepared_binding_candidate, _ = prepare_binding_write(binding_candidate)
        except BindingUnreadable as error:
            raise SystemExit(
                f"[error] {error}. Refusing to register before the canonical transition; "
                "no session was written."
            )
        # No pure refusal remains: NOW mint or swap. A fresh native session mints its
        # binding (#378); an authorized replacement rebinds the successor in for the
        # verified predecessor (#319); an exact-native re-registration already resolved
        # read-only above.
        if canonical is None:
            if replacement_predecessor is not None:
                canonical = _replace_pi_binding_or_refuse(
                    args, runtime, pi_native_session_id, replacement_predecessor
                )
            else:
                canonical = _provision_pi_binding_or_refuse(args, runtime, pi_native_session_id)
        payload.update(canonical)
        for key in ("binding_id", "binding_generation", "endpoint_id"):
            prepared_binding_candidate[key] = payload[key]
        prepared_binding = (
            prepared_binding_candidate,
            json.dumps(prepared_binding_candidate, indent=2, sort_keys=True),
        )
        prepared_candidate = dict(payload)
        prepared = (
            prepared_candidate,
            json.dumps(prepared_candidate, indent=2, sort_keys=True),
        )
        # Swap done. Retire the predecessor legacy record (carried prepared tuple, no
        # reopen), then write the new binding, then the new session (carried prepared).
        if retirement is not None:
            commit_superseded_retirement(*retirement)
        binding = update_binding_from_session(payload, prepared=prepared_binding)
        save_session(payload, prepared=prepared, allow_reactivation=True)
        if binding is not None:
            payload["binding"] = binding
        return payload


def existing_binding_snapshot_or_refuse(payload: dict) -> dict:
    """The existing binding, read ONCE, or a refusal before anything has been written.

    Returns the snapshot the update must be built from -- not merely a verdict -- so nothing reopens
    the path afterwards. An unreadable existing binding is not an absent one, so it is never silently
    replaced: the old file stays as it is and no session is written. FileNotFoundError is the
    ordinary first-registration case and yields an empty snapshot.

    This does not make registration atomic against arbitrary I/O failure or a crash between the two
    writes -- two independent file writes cannot be. It removes the specific failure that an
    unreadable or swapped binding can no longer produce partial state.
    """
    project_id = payload.get("project_id")
    chat_id = payload.get("chat_id")
    agent_id = payload.get("agent_id")
    if not project_id or not chat_id or not agent_id:
        return {}
    try:
        return load_binding(str(project_id), str(chat_id), str(agent_id))
    except FileNotFoundError:
        return {}
    except BindingUnreadable as error:
        raise SystemExit(
            f"[error] {error}. Refusing to register: the existing binding could not be read, so "
            "it is left unchanged and no session was written. Repair or remove it first."
        )


def show_session(args) -> dict:
    return load_session(args.session)


def show_binding(args) -> dict:
    try:
        return load_binding(args.project, args.chat, args.agent)
    except BindingUnreadable as error:
        # An inspection command answering with a traceback is just a worse error message.
        raise SystemExit(f"[error] {error}")


def discover_runtime(args) -> dict:
    try:
        return discover_runtime_session(args.runtime_family, project_path=args.project_path)
    except FileNotFoundError as error:
        raise SystemExit(f"[error] {error}")


def publish_current_session(args) -> dict:
    class RegisterArgs:
        pass

    if args.runtime_family in HEURISTIC_RUNTIME_DISCOVERY_FAMILIES:
        return {
            "published": False,
            "reason": HEURISTIC_RUNTIME_DISCOVERY_REFUSED_REASON,
            "runtime_family": args.runtime_family,
            "hint": (
                "Use discover-runtime for read-only diagnostics, or register "
                "--runtime-session-id with an exact runtime session id."
            ),
        }

    discovered = discover_runtime_session(args.runtime_family, project_path=args.project_path)
    register_args = RegisterArgs()
    register_args.session = args.session
    register_args.agent = args.agent
    register_args.project = args.project
    register_args.chat = args.chat
    register_args.repo_targets = args.repo_targets
    register_args.mode = args.mode
    register_args.status = args.status
    register_args.wake_strategy = args.wake_strategy
    register_args.lease_owner = args.lease_owner
    register_args.ttl_seconds = args.ttl_seconds
    register_args.allowed_actions = []
    register_args.runtime_family = discovered["family"]
    register_args.runtime_session_id = discovered["session_id"]
    register_args.runtime_session_source = discovered["session_source"]
    register_args.runtime_home = discovered.get("home")
    register_args.supersedes_session = args.supersedes_session
    register_args.runtime_command = None
    register_args.runtime_timeout = 30
    payload = register_session(register_args)
    return {"discovered": discovered, "session": payload}


def deactivate_session(args) -> dict:
    payload = load_session(args.session)
    payload["status"] = args.status
    if args.status == "superseded":
        payload["superseded_by"] = args.superseded_by
    save_session(payload)
    return payload


def deactivate_pi_session(args) -> dict:
    stopped = []
    for payload in iter_sessions():
        runtime = payload.get("runtime")
        if (
            not isinstance(runtime, dict)
            or runtime.get("family") != "pi"
            or runtime.get("session_id") != args.native_session_id
            or payload.get("status") not in {"active", "parked"}
        ):
            continue
        payload["status"] = "stopped"
        save_session(payload)
        stopped.append(str(payload["session_id"]))
    return {
        "native_session_id": args.native_session_id,
        "deactivated_sessions": stopped,
    }


def _refusal_payload(kind: str, identity: dict, refusal: LeaseRefused) -> dict:
    payload = {
        kind: False,
        "reason": refusal.reason,
        "identity": identity,
        "owner": refusal.owner,
    }
    if kind == "claimed":
        payload["hint"] = "activation authority was not granted; hold read-only and do not mutate the worktree"
    return payload


def _require_registered_lease_project(args) -> None:
    ensure_project(args.project, allow_none=False)


def lease_claim_command(args) -> tuple[dict, int]:
    _require_registered_lease_project(args)
    identity = lease_identity(args)
    try:
        lease = claim_lease(
            identity,
            owner_session_id=args.session,
            owner_pid=args.owner_pid,
            claimant_runtime_id=args.claimant_runtime_id,
            ttl_seconds=args.ttl_seconds,
            takeover=args.takeover,
        )
    except LeaseRefused as refusal:
        return (_refusal_payload("claimed", identity, refusal), 75)
    return ({"claimed": True, "lease": lease}, 0)


def lease_show_command(args) -> tuple[dict, int]:
    _require_registered_lease_project(args)
    identity = lease_identity(args)
    try:
        lease = load_lease(identity)
    except LeaseRefused as refusal:
        # The fail-closed legacy/bounded guards escape load_lease as LeaseRefused; return
        # the same structured refusal/exit status as the other lease verbs, not a traceback.
        return (_refusal_payload("shown", identity, refusal), 75)
    if lease is None:
        return ({"identity": identity, "lease": None}, 0)
    return ({"identity": identity, "lease": lease, "owner": owner_summary(lease)}, 0)


def lease_assert_command(args) -> tuple[dict, int]:
    _require_registered_lease_project(args)
    identity = lease_identity(args)
    try:
        lease = assert_lease(
            identity,
            owner_session_id=args.session,
            fence_token=args.fence_token,
            owner_pid=args.owner_pid,
            claimant_runtime_id=args.claimant_runtime_id,
        )
    except LeaseRefused as refusal:
        return (_refusal_payload("asserted", identity, refusal), 75)
    return ({"asserted": True, "lease": lease}, 0)


def lease_release_command(args) -> tuple[dict, int]:
    _require_registered_lease_project(args)
    identity = lease_identity(args)
    try:
        lease = release_lease(
            identity,
            owner_session_id=args.session,
            fence_token=args.fence_token,
            owner_pid=args.owner_pid,
            claimant_runtime_id=args.claimant_runtime_id,
            status=args.status,
        )
    except LeaseRefused as refusal:
        return (_refusal_payload("released", identity, refusal), 75)
    return ({"released": True, "lease": lease}, 0)


def main():
    args = parse_args()
    # GH-503: gate the mutation-capable commands on exact-current origin/main.
    # Read-only commands (show, show-binding, lease-show, discover-runtime) are
    # not gated so diagnostics stay available.
    _MUTATION_COMMANDS = {
        "register", "publish-current", "dispatch",
        "deactivate", "deactivate-pi",
        "lease-claim", "lease-assert", "lease-release",
    }
    if args.command in _MUTATION_COMMANDS:
        require_current_runtime(f"autobridge:{args.command}")
    exit_code = 0
    if args.command == "register":
        result = register_session(args)
    elif args.command == "discover-runtime":
        result = discover_runtime(args)
    elif args.command == "publish-current":
        result = publish_current_session(args)
    elif args.command == "show":
        result = show_session(args)
    elif args.command == "show-binding":
        result = show_binding(args)
    elif args.command == "dispatch":
        result = dispatch_session(
            args.session,
            project_id=args.project,
            repo_targets=args.repo_targets,
        )
    elif args.command == "lease-claim":
        result, exit_code = lease_claim_command(args)
    elif args.command == "lease-show":
        result, exit_code = lease_show_command(args)
    elif args.command == "lease-assert":
        result, exit_code = lease_assert_command(args)
    elif args.command == "lease-release":
        result, exit_code = lease_release_command(args)
    elif args.command == "deactivate-pi":
        result = deactivate_pi_session(args)
    else:
        result = deactivate_session(args)
    emit(result, getattr(args, "json_output", False))
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
