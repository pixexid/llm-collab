"""Runtime fingerprint pinning for Pi-native workers (GH-319).

A Pi GUI window is not a routing authority. Pi keeps model selection on the *thread*, so
two windows showing one thread mirror each other's model changes while separate threads
keep independent models -- which makes the dangerous shape "two logical workers bound to
the same native thread", not "two windows".

So a binding pins the triple that decides what a turn costs and how it reasons -- provider,
model, reasoning level -- and every automatic turn proves the live session still matches it
before anything is injected. Three failures are one refusal: a mismatch, a native session
claimed by more than one binding, and an inability to prove the live configuration at all.
The third is deliberately not treated as a pass; an unprovable fingerprint is the case
where a stale presentation catalogue silently substitutes a model, which is the whole
reason the pin exists.

Refusing leaves the packet durable and pull-pending. That is the correct direction here:
the mailbox is durable-first, so refusing to wake costs a delay, and waking the wrong
model costs a turn charged to the wrong plan against a worker that was never configured
for it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# The pinned triple, in the order a human reads it: who serves it, what it is, how hard it
# thinks. Stored on the endpoint/session binding and never on the logical identity record,
# because an identity is universal and these are per-endpoint facts (GH-319).
FINGERPRINT_FIELDS = ("provider", "model", "reasoning_level")

REFUSE_FINGERPRINT_MISMATCH = "pi_fingerprint_mismatch"
REFUSE_FINGERPRINT_UNPROVEN = "pi_fingerprint_unproven"
REFUSE_DUPLICATE_NATIVE_SESSION = "pi_duplicate_native_session"
REFUSE_FINGERPRINT_INCOMPLETE = "pi_fingerprint_incomplete"
REFUSE_REASONING_LEVEL_UNSUPPORTED = "pi_reasoning_level_unsupported"
REFUSE_SESSION_REGISTRY_UNPROVABLE = "pi_session_registry_unprovable"

# Records we will read before giving up on proving exclusivity. An untrusted registry
# that never contains a claimant would otherwise be scanned in full at every
# registration, so its size decides how long a verdict takes.
MAX_SESSIONS_SCANNED = 10_000

# The levels Pi itself defines. A pinned value outside this set is a typo, not a
# pass-through, and is caught at registration rather than at every wake.
KNOWN_REASONING_LEVELS = ("minimal", "low", "medium", "high")


class PiFingerprintRefused(RuntimeError):
    """A wake was refused. Carries the reason code and the evidence for the record."""

    def __init__(self, reason: str, detail: str, *, observed: dict | None = None):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.observed = observed or {}


def pinned_fingerprint(runtime: Any) -> dict[str, str]:
    """The fingerprint a binding pins, or {} when it pins nothing.

    Returns a plain dict of the three fields as strings. A partial pin is not a pin: a
    binding that names a model but no reasoning level cannot detect a reasoning-level
    change, and reporting it as pinned would advertise a guarantee it does not have.
    """
    if not isinstance(runtime, dict):
        return {}
    raw = runtime.get("fingerprint")
    if not isinstance(raw, dict):
        return {}
    values = {}
    for field in FINGERPRINT_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            return {}
        values[field] = value.strip()
    return values


def observed_fingerprint(effective: Any) -> dict[str, str]:
    """Read the live session's triple out of Pi's effective configuration.

    Accepts the field spellings Pi uses at its own boundary rather than re-deriving them:
    `reasoning_level`, `reasoningLevel` and `thinking_level` all mean the same thing to a
    reader and only one of them will be present. Anything missing or non-string leaves the
    key absent, so the caller refuses as UNPROVEN rather than comparing against a guess.
    """
    if not isinstance(effective, dict):
        return {}
    aliases = {
        "provider": ("provider",),
        "model": ("model", "model_id", "modelId"),
        "reasoning_level": (
            "reasoning_level",
            "reasoningLevel",
            "thinking_level",
            "thinkingLevel",
        ),
    }
    observed: dict[str, str] = {}
    for field, names in aliases.items():
        for name in names:
            value = effective.get(name)
            if isinstance(value, str) and value.strip():
                observed[field] = value.strip()
                break
    return observed


def assert_reasoning_level_supported(runtime: Any, thinking_level_map: Any) -> None:
    """A pinned reasoning level the model cannot serve is a pin on nothing.

    Pi's model records carry a sparse `thinkingLevelMap`: a key mapped to null means the
    model does not support that level, an absent key means no override, and a key mapped to
    another level means it is silently remapped. Read from the model record rather than
    re-derived, because the three levels differ per model -- `k3` maps `medium` to null
    while `gpt-5.6-sol` does not mention it at all, so the same string means "unsupported"
    for one worker and "passes through" for another.

    A binding pinning a null-mapped level would compare a level the model never runs
    against whatever it actually ran, and refuse forever. A binding pinning a REMAPPED
    level has the mirror problem: it pins `minimal` and the live session honestly reports
    `low`. Both are caught here, at registration, rather than at every wake.
    """
    pinned = pinned_fingerprint(runtime)
    if not pinned or not isinstance(thinking_level_map, dict):
        return
    level = pinned["reasoning_level"]
    if level not in thinking_level_map:
        # Absent means no override -- but only for a level Pi actually has. A typo like
        # "medum" is absent for the same reason, and treating it as pass-through pinned a
        # value the model can only reject or substitute, leaving the binding mismatched at
        # every wake with nothing to point at.
        known = {name for name in thinking_level_map} | set(KNOWN_REASONING_LEVELS)
        if level not in known:
            raise PiFingerprintRefused(
                REFUSE_REASONING_LEVEL_UNSUPPORTED,
                f"reasoning level {level!r} is not one this model declares "
                f"({sorted(thinking_level_map)}) nor a level Pi defines "
                f"({sorted(KNOWN_REASONING_LEVELS)}); an unknown level cannot pass through "
                "because nothing would ever match it",
            )
        return
    mapped = thinking_level_map[level]
    if mapped is None:
        raise PiFingerprintRefused(
            REFUSE_REASONING_LEVEL_UNSUPPORTED,
            f"this model maps reasoning level {level!r} to null, meaning it does not "
            "support it -- pinning it would compare a level the model never runs against "
            "whatever it actually ran",
        )
    if mapped != level:
        raise PiFingerprintRefused(
            REFUSE_REASONING_LEVEL_UNSUPPORTED,
            f"this model remaps reasoning level {level!r} to {mapped!r}, so a live session "
            f"honestly reports {mapped!r} and a pin of {level!r} would never match; pin the "
            "effective level instead",
        )


def assert_fingerprint_matches(runtime: Any, effective: Any) -> dict[str, str]:
    """Prove the live session is the one this binding pinned, or refuse.

    Returns the observed fingerprint on success so the caller can record what it proved
    rather than restating what it expected.
    """
    pinned = pinned_fingerprint(runtime)
    if not pinned:
        raise PiFingerprintRefused(
            REFUSE_FINGERPRINT_INCOMPLETE,
            "this binding pins no complete runtime fingerprint, so a model substitution "
            f"could not be detected; register it with all of {list(FINGERPRINT_FIELDS)}",
        )
    observed = observed_fingerprint(effective)
    missing = [field for field in FINGERPRINT_FIELDS if field not in observed]
    if missing:
        raise PiFingerprintRefused(
            REFUSE_FINGERPRINT_UNPROVEN,
            "could not read the live session's "
            f"{', '.join(missing)} from Pi's effective configuration, so the pinned "
            "fingerprint is unproven. An unprovable fingerprint is refused rather than "
            "assumed: a stale presentation catalogue substituting a model looks exactly "
            "like this.",
            observed=observed,
        )
    # `.get` rather than `[]`, deliberately. The `missing` guard above is what turns an
    # unreadable configuration into a clean refusal, and if it were ever bypassed this
    # comparison would raise KeyError INTO the dispatch path -- unwinding past the point
    # where the drift record is written, which is how an uncaught OverflowError produced a
    # redelivery in llm-collab#316. A refusal must stay a refusal even when the guard above
    # it is wrong.
    differing = {
        field: (pinned.get(field), observed.get(field))
        for field in FINGERPRINT_FIELDS
        if pinned.get(field) != observed.get(field)
    }
    if differing:
        parts = ", ".join(
            f"{field}: pinned {want!r}, live {got!r}" for field, (want, got) in differing.items()
        )
        raise PiFingerprintRefused(
            REFUSE_FINGERPRINT_MISMATCH,
            f"the live Pi session does not match this binding's pinned fingerprint ({parts})",
            observed=observed,
        )
    return observed


def _lease_is_live(session: dict, now: datetime) -> bool:
    """Is this claimant's autobridge lease still valid?

    Expiry is carried by `lease_expires_utc`; the status stays `active`/`parked` -- there
    is no `expired` status, so reading status alone kept a lapsed session claiming its
    native thread forever and no replacement binding could ever be registered.

    An unparseable or absent expiry counts as LIVE: a claimant we cannot date is one we
    cannot prove is gone, and the safe direction here is refusing a second binding.
    """
    raw = session.get("lease_expires_utc")
    if not isinstance(raw, str) or not raw.strip():
        return True
    try:
        expires = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > now


def assert_native_session_is_exclusive(
    native_session_id: Any,
    sessions: Any,
    *,
    owner_session_id: Any = None,
    now_utc: datetime | None = None,
) -> None:
    """One native Pi thread, one worker binding.

    Pi keeps model selection on the thread, so two logical workers sharing one native
    thread means either can silently change the other's model -- the issue's own named
    unsafe case. Checked against the registered sessions rather than against window state,
    because window title, order and frontmost app are explicitly not routing authority.
    """
    if not isinstance(native_session_id, str) or not native_session_id.strip():
        raise PiFingerprintRefused(
            REFUSE_FINGERPRINT_INCOMPLETE,
            "a Pi binding needs a native session id; window identity is not routing "
            "authority",
        )
    # An unreadable registry is not an empty one. `None`, a mapping, or a record we
    # cannot parse used to mean "no claimant found", so exclusivity was APPROVED exactly
    # when an existing claimant could not be inspected -- the one case where approving is
    # least defensible.
    if not isinstance(sessions, (list, tuple)):
        raise PiFingerprintRefused(
            REFUSE_SESSION_REGISTRY_UNPROVABLE,
            "the session registry could not be enumerated as a sequence "
            f"(got {type(sessions).__name__}), so exclusivity is unproven rather than "
            "satisfied",
        )
    native = native_session_id.strip()
    owner = str(owner_session_id) if owner_session_id is not None else None
    now = now_utc or datetime.now(timezone.utc)
    claimants = []
    for index, session in enumerate(sessions):
        if index >= MAX_SESSIONS_SCANNED:
            raise PiFingerprintRefused(
                REFUSE_SESSION_REGISTRY_UNPROVABLE,
                f"stopped after {MAX_SESSIONS_SCANNED} session records without reaching a "
                "verdict; an unbounded registry decides how long registration takes and "
                "is refused rather than scanned to exhaustion",
            )
        if not isinstance(session, dict):
            raise PiFingerprintRefused(
                REFUSE_SESSION_REGISTRY_UNPROVABLE,
                f"session record {index} is a {type(session).__name__}, not a record; a "
                "registry we cannot read cannot show the native thread is unclaimed",
            )
        runtime = session.get("runtime")
        if not isinstance(runtime, dict) or str(runtime.get("family", "")) != "pi":
            continue
        if str(runtime.get("session_id", "")).strip() != native:
            continue
        holder = str(session.get("session_id", ""))
        if owner is not None and holder == owner:
            continue
        dispatchable = session.get("status") in {"active", "parked"}
        if dispatchable and _lease_is_live(session, now):
            claimants.append(holder)
    if claimants:
        raise PiFingerprintRefused(
            REFUSE_DUPLICATE_NATIVE_SESSION,
            f"native Pi session {native!r} is already claimed by "
            f"{sorted(claimants)}; one native thread may back only one worker binding, "
            "because Pi keeps model selection on the thread and either worker could then "
            "change the other's model",
        )
