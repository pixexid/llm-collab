"""bb 0.35.1 client and refusal surface (GH-563 Slice 1A).

This module is the SOLE place a bb response is parsed. Nothing else in the
codebase may read a bb envelope; if a caller needs a field, it comes from a typed
value returned here.

Slice 1A is deliberately unreachable: no router, no dispatch branch, no canonical
write. It exists to freeze the response and failure contract that Slice 1B
(GH-564) calls, so the risky lane is reviewed against a settled contract instead
of inventing one mid-flight.

Two properties are load-bearing and both come from live observation, not docs:

* ``thread spawn --json`` returns the thread object at the TOP LEVEL, while
  ``thread show --json`` nests it under a ``thread`` key. One validator reused for
  both silently misreads — during the GH-562 pilot that produced ``status: None``
  read from a thread that was actually ``active``. The two envelopes therefore get
  two separate validators, and that separation is what the mutation proof
  targets.
* bb has no idempotency concept at any layer (zero ``idempotenc`` occurrences in
  its shipped bundles, and two identical sends produced two ingresses in the
  pilot). This client therefore never retries a task-bearing call: a duplicate
  would be a second real turn, not a retry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

PINNED_BB_VERSION = "0.35.1"

# Refusal reasons. Distinct values because callers act differently on each: a
# version mismatch is an environment repair, a malformed response is a contract
# break, a timeout on a task-bearing call is AMBIGUOUS rather than failed.
REFUSAL_DISABLED = "bb_adapter_disabled"
REFUSAL_VERSION_MISMATCH = "bb_version_mismatch"
REFUSAL_MALFORMED_RESPONSE = "bb_malformed_response"
REFUSAL_TRANSPORT_FAILED = "bb_transport_failed"
REFUSAL_TIMED_OUT = "bb_timed_out"
REFUSAL_AMBIGUOUS = "bb_ambiguous_outcome"
REFUSAL_PROFILE_MISMATCH = "bb_profile_mismatch"


class BbTransportTimeout(Exception):
    """Raised by a transport when a call exceeds its deadline."""


@dataclass(frozen=True)
class BbRefusal:
    """A typed refusal. Never carries a partial result.

    Returned rather than raised so a caller must handle it explicitly; an
    exception unwinding past a canonical write is how partial state happens.
    """

    reason: str
    detail: str


@dataclass(frozen=True)
class BbProfile:
    """One exact (provider, model, effort) triple.

    Slice 1A neither selects nor defaults one. The caller supplies it and this
    client validates the native result against it. Profile *selection* is Phase 2
    (GH-565).
    """

    provider: str
    model: str
    effort: str


# The single frozen triple for Slice 1A. It is the only triple this workspace has
# live evidence for (the GH-562 pilot ran on it end to end), and `kimi-coding/k3`
# advertises low/high/max with no `medium`, so `low` is a supported setting
# rather than a guess at a value that does not exist.
SLICE_1A_PROFILE = BbProfile(provider="pi", model="kimi-coding/k3", effort="low")


@dataclass(frozen=True)
class BbThread:
    """Validated bb thread identity and state."""

    thread_id: str
    project_id: str
    environment_id: str
    provider_id: str
    status: str


@dataclass(frozen=True)
class BbEvent:
    """One replayable bb thread event, keyed by its sequence number."""

    seq: int
    event_id: str
    event_type: str


@dataclass(frozen=True)
class BbTransportResult:
    exit_code: int
    stdout: str
    stderr: str


# A transport takes argv (without the `bb` prefix) plus a deadline and returns a
# BbTransportResult, or raises BbTransportTimeout. Injecting it is what lets the
# Slice 1A tests run entirely on recorded fixtures with no live bb server.
BbTransport = Callable[[Sequence[str], float], BbTransportResult]


def _require_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


class BbClient:
    """Validating client for bb 0.35.1.

    Default OFF. With the adapter disabled, every operation returns
    ``REFUSAL_DISABLED`` without touching the transport at all — no process, no
    HTTP, no state read.
    """

    def __init__(
        self,
        transport: BbTransport,
        *,
        enabled: bool = False,
        timeout_seconds: float = 30.0,
        read_probe_attempts: int = 2,
    ) -> None:
        self._transport = transport
        self._enabled = enabled
        self._timeout_seconds = timeout_seconds
        # Read-only probes may retry inside one deadline; task-bearing calls
        # never do. Guard the value so a caller cannot turn a read into a storm.
        self._read_probe_attempts = max(1, int(read_probe_attempts))
        self._verified_version: str | None = None

    # ---- version -------------------------------------------------------

    def verify_version(self) -> str | BbRefusal:
        """Confirm the runtime is exactly the pinned version.

        The version cannot be known without asking, so this performs exactly one
        bounded native probe and caches the result for the client's lifetime. On
        mismatch, no task-bearing operation is ever attempted.
        """
        if not self._enabled:
            return BbRefusal(REFUSAL_DISABLED, "bb adapter is disabled")
        if self._verified_version is not None:
            return self._verified_version

        outcome = self._read_json(["settings", "version", "--json"])
        if isinstance(outcome, BbRefusal):
            return outcome
        if not isinstance(outcome, Mapping):
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE, "version envelope is not an object"
            )
        current = _require_str(outcome, "currentVersion")
        if current is None:
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE, "version envelope has no currentVersion"
            )
        if current != PINNED_BB_VERSION:
            return BbRefusal(
                REFUSAL_VERSION_MISMATCH,
                f"bb is {current}; this adapter is pinned to {PINNED_BB_VERSION}",
            )
        self._verified_version = current
        return current

    def _gate(self) -> BbRefusal | None:
        checked = self.verify_version()
        return checked if isinstance(checked, BbRefusal) else None

    # ---- envelope validators -------------------------------------------

    @staticmethod
    def validate_spawn_envelope(payload: Any) -> BbThread | BbRefusal:
        """`thread spawn --json` puts the thread at the TOP LEVEL.

        Kept separate from the show validator on purpose: the two envelopes
        differ, and sharing one validator is a silent misread rather than an
        error.
        """
        if not isinstance(payload, Mapping):
            return BbRefusal(REFUSAL_MALFORMED_RESPONSE, "spawn envelope is not an object")
        if "thread" in payload and "id" not in payload:
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE,
                "spawn envelope is nested under 'thread'; expected top-level thread object",
            )
        return BbClient._thread_from(payload, envelope="spawn")

    @staticmethod
    def validate_show_envelope(payload: Any) -> BbThread | BbRefusal:
        """`thread show --json` NESTS the thread under a `thread` key."""
        if not isinstance(payload, Mapping):
            return BbRefusal(REFUSAL_MALFORMED_RESPONSE, "show envelope is not an object")
        inner = payload.get("thread")
        if not isinstance(inner, Mapping):
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE,
                "show envelope has no nested 'thread' object",
            )
        return BbClient._thread_from(inner, envelope="show")

    @staticmethod
    def _thread_from(payload: Mapping[str, Any], *, envelope: str) -> BbThread | BbRefusal:
        thread_id = _require_str(payload, "id")
        project_id = _require_str(payload, "projectId")
        environment_id = _require_str(payload, "environmentId")
        provider_id = _require_str(payload, "providerId")
        status = _require_str(payload, "status")
        missing = [
            name
            for name, value in (
                ("id", thread_id),
                ("projectId", project_id),
                ("environmentId", environment_id),
                ("providerId", provider_id),
                ("status", status),
            )
            if value is None
        ]
        if missing:
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE,
                f"{envelope} envelope missing required field(s): {', '.join(missing)}",
            )
        return BbThread(
            thread_id=str(thread_id),
            project_id=str(project_id),
            environment_id=str(environment_id),
            provider_id=str(provider_id),
            status=str(status),
        )

    # ---- operations ----------------------------------------------------

    def spawn(
        self, *, project_id: str, prompt: str, profile: BbProfile
    ) -> BbThread | BbRefusal:
        """Create one bb thread with an explicitly supplied profile.

        The profile is an argument, never a default or a selection. A timeout is
        reported as AMBIGUOUS rather than failed, because bb may have created the
        thread before the response was lost — and this client never retries a
        spawn, since a retry would be a second real thread.
        """
        refusal = self._gate()
        if refusal is not None:
            return refusal
        argv = [
            "thread",
            "spawn",
            "--project",
            project_id,
            "--provider",
            profile.provider,
            "--model",
            profile.model,
            "--effort",
            profile.effort,
            "--prompt",
            prompt,
            "--json",
        ]
        payload = self._task_json(argv)
        if isinstance(payload, BbRefusal):
            return payload
        thread = self.validate_spawn_envelope(payload)
        if isinstance(thread, BbRefusal):
            return thread
        if thread.provider_id != profile.provider:
            return BbRefusal(
                REFUSAL_PROFILE_MISMATCH,
                f"requested provider {profile.provider!r}; bb reported {thread.provider_id!r}",
            )
        return thread

    def send(
        self, *, thread_id: str, message: str, mode: str = "queue-if-active"
    ) -> str | BbRefusal:
        """Deliver a follow-up message. Queues when the thread is active.

        Slice 1A implements queue-if-active only. Urgent steering is GH-562 case
        4 and stays Phase 2, so there is no steer mode here to reach for by
        accident.
        """
        refusal = self._gate()
        if refusal is not None:
            return refusal
        if mode != "queue-if-active":
            return BbRefusal(
                REFUSAL_PROFILE_MISMATCH,
                f"mode {mode!r} is not implemented in Slice 1A; only queue-if-active",
            )
        argv = ["thread", "tell", thread_id, message, "--mode", "queue"]
        result = self._task_call(argv)
        if isinstance(result, BbRefusal):
            return result
        return result.stdout.strip()

    def thread_state(self, thread_id: str) -> BbThread | BbRefusal:
        refusal = self._gate()
        if refusal is not None:
            return refusal
        payload = self._read_json(["thread", "show", thread_id, "--json"])
        if isinstance(payload, BbRefusal):
            return payload
        return self.validate_show_envelope(payload)

    def events_after(self, thread_id: str, after_seq: int) -> list[BbEvent] | BbRefusal:
        """Replay events strictly after a sequence number.

        Sequence-based replay is the durable path; a `thread:changed` websocket
        signal may only trigger a fetch and is never itself evidence.
        """
        refusal = self._gate()
        if refusal is not None:
            return refusal
        payload = self._read_json(
            ["thread", "log", thread_id, "--json", "--after-seq", str(after_seq)]
        )
        if isinstance(payload, BbRefusal):
            return payload
        if not isinstance(payload, list):
            return BbRefusal(REFUSAL_MALFORMED_RESPONSE, "event envelope is not a list")
        events: list[BbEvent] = []
        for entry in payload:
            if not isinstance(entry, Mapping):
                return BbRefusal(REFUSAL_MALFORMED_RESPONSE, "event entry is not an object")
            seq = entry.get("seq")
            event_id = _require_str(entry, "id")
            event_type = _require_str(entry, "type")
            if not isinstance(seq, int) or event_id is None or event_type is None:
                return BbRefusal(
                    REFUSAL_MALFORMED_RESPONSE,
                    "event entry missing seq/id/type",
                )
            events.append(BbEvent(seq=seq, event_id=event_id, event_type=event_type))
        return events

    def queued_messages(self, thread_id: str) -> int | BbRefusal:
        """Count durably queued messages, for drain observation.

        `thread wait` immediately after a queued send can observe the PRE-queue
        idle state, so a caller gates on this draining rather than on one wait.
        """
        refusal = self._gate()
        if refusal is not None:
            return refusal
        payload = self._read_json(["thread", "queue", "list", thread_id, "--json"])
        if isinstance(payload, BbRefusal):
            return payload
        if not isinstance(payload, list):
            return BbRefusal(REFUSAL_MALFORMED_RESPONSE, "queue envelope is not a list")
        return len(payload)

    # ---- transport -----------------------------------------------------

    def _read_json(self, argv: Sequence[str]) -> Any | BbRefusal:
        """Read-only probe. May retry within one deadline."""
        last: BbRefusal | None = None
        for _ in range(self._read_probe_attempts):
            result = self._call(argv)
            if isinstance(result, BbRefusal):
                # A timeout on a read is safe to retry; a malformed response is
                # not going to become well-formed.
                if result.reason != REFUSAL_TIMED_OUT:
                    return result
                last = result
                continue
            return self._decode(result)
        return last or BbRefusal(REFUSAL_TIMED_OUT, "read probe exhausted its attempts")

    def _task_call(self, argv: Sequence[str]) -> BbTransportResult | BbRefusal:
        """Task-bearing call. NEVER retried."""
        result = self._call(argv)
        if isinstance(result, BbRefusal) and result.reason == REFUSAL_TIMED_OUT:
            # bb may have performed the action before the response was lost.
            # Reporting this as a failure would invite a retry that creates a
            # second real turn.
            return BbRefusal(
                REFUSAL_AMBIGUOUS,
                f"{' '.join(argv)} timed out; the operation may have been performed",
            )
        return result

    def _task_json(self, argv: Sequence[str]) -> Any | BbRefusal:
        result = self._task_call(argv)
        if isinstance(result, BbRefusal):
            return result
        return self._decode(result)

    def _call(self, argv: Sequence[str]) -> BbTransportResult | BbRefusal:
        try:
            result = self._transport(argv, self._timeout_seconds)
        except BbTransportTimeout as exc:
            return BbRefusal(REFUSAL_TIMED_OUT, str(exc) or " ".join(argv))
        if result.exit_code != 0:
            return BbRefusal(
                REFUSAL_TRANSPORT_FAILED,
                f"exit {result.exit_code}: {result.stderr.strip() or result.stdout.strip()}",
            )
        return result

    @staticmethod
    def _decode(result: BbTransportResult) -> Any | BbRefusal:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return BbRefusal(REFUSAL_MALFORMED_RESPONSE, f"response is not JSON: {exc}")
