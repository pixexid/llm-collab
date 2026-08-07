"""bb 0.35.1 client and refusal surface (GH-563 Slice 1A).

This module is the SOLE place a bb response is parsed. Nothing else in the
codebase may read a bb envelope; if a caller needs a field, it comes from a typed
value returned here.

Slice 1A is deliberately unreachable: no router, no dispatch branch, no canonical
write. It exists to freeze the response and failure contract that Slice 1B
(GH-564) calls, so the risky lane is reviewed against a settled contract instead
of inventing one mid-flight.

Four properties are load-bearing and all four come from live observation against
the installed 0.35.1 CLI, not from docs:

* ``thread spawn --json`` returns the thread object at the TOP LEVEL, while
  ``thread show --json`` nests it under a ``thread`` key. One validator reused for
  both silently misreads — during the GH-562 pilot that produced ``status: None``
  read from a thread that was actually ``active``. The two envelopes therefore get
  two separate validators, and that separation is what the mutation proof
  targets.
* The spawn envelope carries no model and no reasoning level, so argv alone
  cannot prove which profile actually ran. The authoritative record is the
  ``execution`` block on the thread's ``client/turn/requested`` event, and that is
  what this client validates the requested profile against.
* ``thread log --json`` caps at 100 events by default. A page returned without
  its own bound is indistinguishable from a complete history, so replay returns an
  explicitly bounded page that declares its own truncation.
* bb has no idempotency concept at any layer (zero ``idempotenc`` occurrences in
  its shipped bundles, and two identical sends produced two ingresses in the
  pilot). This client therefore never retries any native call: one attempt, one
  deadline. A "retry" of a spawn or a tell would be a second real turn, and a
  read that retries inside a deadline gives each attempt the full timeout, which
  is not one deadline at all.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

PINNED_BB_VERSION = "0.35.1"

# Refuse rather than parse past this. A response this large is a contract break,
# and truncating it would turn a resource limit into a correctness bug.
MAX_RESPONSE_CHARS = 1_048_576

# A hung Popen cannot be cancelled, so bound the daemon threads left waiting for
# it. A later successful cleanup returns the slot.
MAX_STALLED_LAUNCHES = 4
_stalled_launches = 0
_stalled_launches_lock = threading.Lock()

# bb's own `thread log --limit` default. Passed EXPLICITLY so the page bound is
# this module's, not an implicit default that can move under us.
MAX_EVENT_PAGE = 100

# The spawn turn request is event seq 1. A small window rather than 1 exactly, so
# a future leading event does not silently break profile validation; if the block
# is not in the window the client refuses instead of assuming.
EXECUTION_PROBE_EVENTS = 3
SPAWN_EVENT_TYPE = "client/turn/requested"
SPAWN_EVENT_SOURCE = "spawn"

# Refusal reasons. Distinct values because callers act differently on each: a
# version mismatch is an environment repair, a malformed response is a contract
# break, a timeout on a task-bearing call is AMBIGUOUS rather than failed, and an
# orphan means a real thread exists that this client refused to hand back.
REFUSAL_DISABLED = "bb_adapter_disabled"
REFUSAL_VERSION_MISMATCH = "bb_version_mismatch"
REFUSAL_MALFORMED_RESPONSE = "bb_malformed_response"
REFUSAL_TRANSPORT_FAILED = "bb_transport_failed"
REFUSAL_TIMED_OUT = "bb_timed_out"
REFUSAL_AMBIGUOUS = "bb_ambiguous_outcome"
REFUSAL_PROFILE_MISMATCH = "bb_profile_mismatch"
REFUSAL_IDENTITY_MISMATCH = "bb_identity_mismatch"
REFUSAL_ORPHANED_THREAD = "bb_orphaned_thread"


class BbTransportTimeout(Exception):
    """Raised by a transport when a call exceeds its deadline."""


@dataclass(frozen=True)
class BbRefusal:
    """A typed refusal. Never carries a partial result.

    Returned rather than raised so a caller must handle it explicitly; an
    exception unwinding past a canonical write is how partial state happens.

    ``native_thread_id`` is set only when a real bb thread was created and then
    refused. That case is not retryable and not a clean failure: the id is the
    evidence a caller needs to reconcile the orphan.
    """

    reason: str
    detail: str
    native_thread_id: str | None = None


@dataclass(frozen=True)
class BbProfile:
    """One exact (provider, model, reasoning level) triple.

    Named for bb's own ``--reasoning-level`` / ``reasoningLevel`` rather than a
    local synonym, so the value and its native flag cannot drift apart.

    Slice 1A neither selects nor defaults one. The caller supplies it and this
    client validates the native result against it. Profile *selection* is Phase 2
    (GH-565).
    """

    provider: str
    model: str
    reasoning_level: str


# The single frozen triple for Slice 1A. `kimi-coding/k3` advertises low/high/max
# with no `medium`, and a live spawn at `high` was read back from the execution
# event as exactly `high` (fixture `thread_log_execution_high.json`), so the
# equality this client enforces is observed rather than assumed.
SLICE_1A_PROFILE = BbProfile(
    provider="pi", model="kimi-coding/k3", reasoning_level="high"
)


@dataclass(frozen=True)
class BbThread:
    """Validated bb thread identity and state."""

    thread_id: str
    project_id: str
    environment_id: str
    provider_id: str
    status: str


@dataclass(frozen=True)
class BbExecution:
    """The profile bb actually ran, read from its authoritative execution block."""

    model: str
    reasoning_level: str


@dataclass(frozen=True)
class BbQueued:
    """A follow-up message bb accepted, bound to the thread it was addressed to."""

    thread_id: str
    mode: str


@dataclass(frozen=True)
class BbEvent:
    """One replayable bb thread event, keyed by its sequence number."""

    seq: int
    event_id: str
    event_type: str


@dataclass(frozen=True)
class BbEventPage:
    """A bounded page of events that declares its own truncation.

    A bare list cannot distinguish "this is the whole history" from "this is the
    first 100 of it". ``truncated`` plus ``next_after_seq`` make the continuation
    explicit, so a caller that ignores it stops early visibly rather than
    silently believing it saw everything.
    """

    events: tuple[BbEvent, ...]
    truncated: bool
    next_after_seq: int | None


@dataclass(frozen=True)
class BbTransportResult:
    exit_code: int
    stdout: str
    stderr: str


# A transport takes argv (without the `bb` prefix) plus a deadline and returns a
# BbTransportResult, or raises BbTransportTimeout/BbResponseReadError. Injecting it is what lets
# the Slice 1A tests run entirely on recorded fixtures with no live bb server.
#
# A transport OWNS its own read bound: by the time it returns, both streams are
# already materialized, so the client's MAX_RESPONSE_CHARS check can refuse an
# oversized response but cannot prevent one from being read into memory. A
# transport that reads a real subprocess must stop at MAX_RESPONSE_CHARS + 1 and
# raise rather than accumulate. No such transport ships in Slice 1A — every
# caller here injects one. GH-570 is a BLOCKING obligation on whichever slice
# first introduces a production transport: implement the read bound in that same
# slice before merge rather than opening a competing lane for it.
BbTransport = Callable[[Sequence[str], float], BbTransportResult]


def _require_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _require_seq(value: Any) -> int | None:
    """A sequence number is an integer. `True` is an int in Python; it is not a seq."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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
    ) -> None:
        self._transport = transport
        self._enabled = enabled
        self._timeout_seconds = timeout_seconds
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

        The profile is an argument, never a default or a selection. Success is
        bound to the request AND to bb's own record: the returned thread must be
        the one asked for, in the project asked for, running the profile asked
        for. Because the spawn envelope carries neither model nor reasoning
        level, the last of those is read back from the execution event.

        A timeout is reported as AMBIGUOUS rather than failed, because bb may
        have created the thread before the response was lost — and this client
        never retries a spawn, since a retry would be a second real thread. Once
        a thread exists, any later refusal is an ORPHAN carrying its native id,
        never a clean failure a caller might retry.
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
            "--reasoning-level",
            profile.reasoning_level,
            "--prompt",
            prompt,
            "--json",
        ]
        payload = self._task_json(argv)
        if isinstance(payload, BbRefusal):
            return payload
        # Read the native id BEFORE full validation. bb has already created the
        # thread by the time this envelope exists, so a refusal on any other
        # field must still carry the id — otherwise a real thread is reported as
        # a clean failure and the caller is invited to retry into a second one.
        spawned_id = _require_str(payload, "id") if isinstance(payload, Mapping) else None

        def orphan(reason: str, detail: str) -> BbRefusal:
            # Routing every post-execution failure through here is not enough on
            # its own: with no recoverable id this once returned the typed reason
            # with native_thread_id=None, which is a CLEAN refusal for a thread
            # bb may already have created. An id makes the refusal an orphan a
            # caller can reconcile; without one there is nothing to reconcile
            # against and the only honest answer is that we cannot tell.
            if spawned_id is None:
                return BbRefusal(
                    REFUSAL_AMBIGUOUS,
                    f"{detail}; no native id was recoverable, so the thread may exist",
                )
            return BbRefusal(reason, detail, native_thread_id=spawned_id)

        thread = self.validate_spawn_envelope(payload)
        if isinstance(thread, BbRefusal):
            return orphan(thread.reason, thread.detail)

        if thread.project_id != project_id:
            return orphan(
                REFUSAL_IDENTITY_MISMATCH,
                f"requested project {project_id!r}; bb reported {thread.project_id!r}",
            )
        if thread.provider_id != profile.provider:
            return orphan(
                REFUSAL_PROFILE_MISMATCH,
                f"requested provider {profile.provider!r}; bb reported {thread.provider_id!r}",
            )
        execution = self._execution_evidence(thread.thread_id)
        if isinstance(execution, BbRefusal):
            return orphan(
                execution.reason,
                f"spawned thread could not be profile-verified: {execution.detail}",
            )
        if execution.model != profile.model:
            return orphan(
                REFUSAL_PROFILE_MISMATCH,
                f"requested model {profile.model!r}; bb ran {execution.model!r}",
            )
        if execution.reasoning_level != profile.reasoning_level:
            return orphan(
                REFUSAL_PROFILE_MISMATCH,
                f"requested reasoning level {profile.reasoning_level!r}; "
                f"bb ran {execution.reasoning_level!r}",
            )
        return thread

    def send(
        self, *, thread_id: str, message: str, mode: str = "queue-if-active"
    ) -> BbQueued | BbRefusal:
        """Deliver a follow-up message. Queues when the thread is active.

        Returns a typed acceptance bound to the requested thread rather than raw
        stdout: this module is the sole bb response validator, so handing a
        caller unparsed text would move validation somewhere it is not allowed to
        live.

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
        payload = self._task_json(
            ["thread", "tell", thread_id, message, "--mode", "queue", "--json"]
        )
        if isinstance(payload, BbRefusal):
            return payload

        # Everything below runs AFTER bb exited 0, so the message may already be
        # queued and bb has no idempotency at any layer. A response we cannot
        # validate tells us nothing about whether the send happened — only that
        # we cannot confirm it — so no check down here may report a clean
        # failure a caller would retry into a second enqueue. Mirrors spawn(),
        # where the same class surfaces as an orphan because a native id exists;
        # here there is no such id, so ambiguous is the honest surface.
        def performed(detail: str) -> BbRefusal:
            return BbRefusal(REFUSAL_AMBIGUOUS, f"{detail}; the message may have been queued")

        if not isinstance(payload, Mapping):
            return performed("tell envelope is not an object")
        if payload.get("ok") is not True:
            return performed(f"tell envelope did not report ok=true: {payload.get('ok')!r}")
        reported_thread = _require_str(payload, "threadId")
        if reported_thread != thread_id:
            return performed(
                f"told thread {thread_id!r}; bb reported {reported_thread!r}"
            )
        reported_mode = _require_str(payload, "mode")
        if reported_mode != "queue":
            return performed(f"requested queue mode; bb reported {reported_mode!r}")
        return BbQueued(thread_id=thread_id, mode=reported_mode)

    def thread_state(self, thread_id: str) -> BbThread | BbRefusal:
        refusal = self._gate()
        if refusal is not None:
            return refusal
        payload = self._read_json(["thread", "show", thread_id, "--json"])
        if isinstance(payload, BbRefusal):
            return payload
        thread = self.validate_show_envelope(payload)
        if isinstance(thread, BbRefusal):
            return thread
        if thread.thread_id != thread_id:
            return BbRefusal(
                REFUSAL_IDENTITY_MISMATCH,
                f"asked for thread {thread_id!r}; bb reported {thread.thread_id!r}",
            )
        return thread

    def events_after(
        self, thread_id: str, after_seq: int, *, limit: int = MAX_EVENT_PAGE
    ) -> BbEventPage | BbRefusal:
        """Replay one bounded page of events strictly after a sequence number.

        Sequence-based replay is the durable path; a `thread:changed` websocket
        signal may only trigger a fetch and is never itself evidence.
        """
        refusal = self._gate()
        if refusal is not None:
            return refusal
        if isinstance(limit, bool) or not isinstance(limit, int):
            return BbRefusal(REFUSAL_MALFORMED_RESPONSE, f"limit {limit!r} is not an integer")
        if not 1 <= limit <= MAX_EVENT_PAGE:
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE,
                f"limit {limit} is outside 1..{MAX_EVENT_PAGE}",
            )
        entries = self._log_entries(thread_id, after_seq=after_seq, limit=limit)
        if isinstance(entries, BbRefusal):
            return entries
        events: list[BbEvent] = []
        for entry in entries:
            event_id = _require_str(entry, "id")
            event_type = _require_str(entry, "type")
            if event_id is None or event_type is None:
                return BbRefusal(REFUSAL_MALFORMED_RESPONSE, "event entry missing id/type")
            events.append(
                BbEvent(
                    seq=int(entry["seq"]), event_id=event_id, event_type=event_type
                )
            )
        truncated = len(events) == limit
        return BbEventPage(
            events=tuple(events),
            truncated=truncated,
            next_after_seq=events[-1].seq if truncated else None,
        )

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

    # ---- native evidence -----------------------------------------------

    def _execution_evidence(self, thread_id: str) -> BbExecution | BbRefusal:
        """Read the profile bb actually ran from its `client/turn/requested` event.

        argv proves what was asked for, not what happened; the spawn envelope
        carries neither model nor reasoning level. This block is the only native
        record of both.

        Both selectors are load-bearing and neither discriminates alone: the
        recorded spawn also emits `client/thread/start` carrying `source:
        "spawn"`, and a later `client/turn/requested` from a `tell` carries the
        same type. Accepting any event with an execution block would let
        unrelated later metadata stand in for the spawn's own profile record.
        """
        entries = self._log_entries(
            thread_id, after_seq=None, limit=EXECUTION_PROBE_EVENTS
        )
        if isinstance(entries, BbRefusal):
            return entries
        for entry in entries:
            if entry.get("type") != SPAWN_EVENT_TYPE:
                continue
            data = entry.get("data")
            if not isinstance(data, Mapping):
                continue
            if data.get("source") != SPAWN_EVENT_SOURCE:
                continue
            execution = data.get("execution")
            if not isinstance(execution, Mapping):
                continue
            model = _require_str(execution, "model")
            reasoning_level = _require_str(execution, "reasoningLevel")
            if model is None or reasoning_level is None:
                return BbRefusal(
                    REFUSAL_MALFORMED_RESPONSE,
                    "execution block missing model/reasoningLevel",
                )
            return BbExecution(model=model, reasoning_level=reasoning_level)
        return BbRefusal(
            REFUSAL_MALFORMED_RESPONSE,
            f"no execution block in the first {EXECUTION_PROBE_EVENTS} events",
        )

    def _log_entries(
        self, thread_id: str, *, after_seq: int | None, limit: int
    ) -> list[Mapping[str, Any]] | BbRefusal:
        """Read a bounded, identity-checked, strictly ordered slice of the log.

        The native limit is always passed. Every entry must belong to the thread
        that was asked for, and sequence numbers must advance: an entry from
        another thread, or a page that restarts at 1 for `after_seq=40`, is a
        contract break rather than data to interpret.
        """
        argv = ["thread", "log", thread_id, "--json", "--limit", str(limit)]
        if after_seq is not None:
            if isinstance(after_seq, bool) or not isinstance(after_seq, int):
                return BbRefusal(
                    REFUSAL_MALFORMED_RESPONSE, f"after_seq {after_seq!r} is not an integer"
                )
            argv += ["--after-seq", str(after_seq)]
        payload = self._read_json(argv)
        if isinstance(payload, BbRefusal):
            return payload
        if not isinstance(payload, list):
            return BbRefusal(REFUSAL_MALFORMED_RESPONSE, "event envelope is not a list")
        if len(payload) > limit:
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE,
                f"bb returned {len(payload)} events for a limit of {limit}",
            )
        entries: list[Mapping[str, Any]] = []
        previous = after_seq
        for entry in payload:
            if not isinstance(entry, Mapping):
                return BbRefusal(REFUSAL_MALFORMED_RESPONSE, "event entry is not an object")
            reported_thread = _require_str(entry, "threadId")
            if reported_thread != thread_id:
                return BbRefusal(
                    REFUSAL_IDENTITY_MISMATCH,
                    f"asked for thread {thread_id!r}; event belongs to {reported_thread!r}",
                )
            seq = _require_seq(entry.get("seq"))
            if seq is None:
                return BbRefusal(
                    REFUSAL_MALFORMED_RESPONSE, f"event seq {entry.get('seq')!r} is not an integer"
                )
            if previous is not None and seq <= previous:
                return BbRefusal(
                    REFUSAL_MALFORMED_RESPONSE,
                    f"event seq {seq} does not advance past {previous}",
                )
            previous = seq
            entries.append(entry)
        return entries

    # ---- transport -----------------------------------------------------

    def _read_json(self, argv: Sequence[str]) -> Any | BbRefusal:
        """Read-only probe: exactly one attempt against one deadline.

        A retry loop here would hand each attempt the full timeout, so the
        caller's deadline would silently become a multiple of itself.
        """
        result = self._call(argv)
        if isinstance(result, BbRefusal):
            return result
        return self._decode(result)

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
        if isinstance(result, BbRefusal) and result.reason == REFUSAL_MALFORMED_RESPONSE:
            # The only malformed refusal _call() raises is its size bound, and it
            # is checked before the exit code — so an exit-0 oversized response
            # reached here having ALREADY performed the operation. Left clean it
            # bypasses both the decode conversion and spawn()'s orphan seam, which
            # is how an oversized spawn response could invite a duplicate spawn.
            # Read paths never come through here and stay malformed.
            return BbRefusal(
                REFUSAL_AMBIGUOUS,
                f"{result.detail}; the operation may have been performed",
            )
        return result

    def _task_json(self, argv: Sequence[str]) -> Any | BbRefusal:
        result = self._task_call(argv)
        if isinstance(result, BbRefusal):
            return result
        decoded = self._decode(result)
        if isinstance(decoded, BbRefusal):
            # bb exited 0, so the thread was created or the message queued; only
            # the report of it was unreadable. Reporting that as a clean
            # malformed-response failure invites the retry that a task-bearing
            # call must never invite. A read can safely call this malformed; a
            # task cannot.
            return BbRefusal(
                REFUSAL_AMBIGUOUS,
                f"{' '.join(argv)} succeeded but its response was unreadable "
                f"({decoded.detail}); the operation may have been performed",
            )
        return decoded

    def _call(self, argv: Sequence[str]) -> BbTransportResult | BbRefusal:
        try:
            result = self._transport(argv, self._timeout_seconds)
        except BbResponseReadError as exc:
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE,
                str(exc) or "response could not be read",
            )
        except BbTransportTimeout as exc:
            return BbRefusal(REFUSAL_TIMED_OUT, str(exc) or " ".join(argv))
        # Bound both streams BEFORE either is parsed or interpolated into a
        # detail string. Refuse rather than truncate: a truncated response that
        # still claims to be a response is worse than no response.
        for name, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
            if len(stream) > MAX_RESPONSE_CHARS:
                detail = f"{name} is {len(stream)} chars, over the {MAX_RESPONSE_CHARS} bound"
                if result.exit_code != 0:
                    # The bound is checked before the exit code so an oversized
                    # stream never reaches a detail string — but the exit code is
                    # still known. This preserves the pre-existing transport-failure
                    # classification for a nonzero exit; it does not claim the call
                    # had no side effect, which stays unestablished pending GH-570.
                    # Without it the size bound would silently broaden that
                    # classification into ambiguous via _task_call(). The detail
                    # stays bounded; only the reason changes.
                    return BbRefusal(
                        REFUSAL_TRANSPORT_FAILED, f"exit {result.exit_code}: {detail}"
                    )
                return BbRefusal(REFUSAL_MALFORMED_RESPONSE, detail)
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
        except RecursionError:
            # Deeply nested JSON exhausts the decoder's stack. Unconverted, this
            # escapes the refusal contract entirely as a bare interpreter error.
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE, "response nesting exceeded the decoder limit"
            )
        except ValueError as exc:
            # A JSON integer longer than sys.get_int_max_str_digits() (4300 by
            # default, far under MAX_RESPONSE_CHARS) raises a BARE ValueError,
            # not a JSONDecodeError — so the size bound never sees it and the
            # JSONDecodeError clause above does not catch it. Ordered last:
            # JSONDecodeError subclasses ValueError and keeps its own message.
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE, f"response exceeded a decoder limit: {exc}"
            )


class BbResponseReadError(Exception):
    """A response stream could not be read completely at the transport boundary."""


class BbResponseTooLarge(BbResponseReadError):
    """A native stream exceeded MAX_RESPONSE_CHARS while it was being read.

    Distinct from the client's own size refusal: this is raised by the transport
    at the read boundary, so the oversized bytes are never accumulated. The
    client's check remains as a second layer for injected transports that do not
    bound themselves.
    """


class BbResponseDecodeError(BbResponseReadError):
    """A response stream contained bytes that could not be decoded as text."""


def _read_bounded(stream, limit: int) -> str:
    """Read at most ``limit + 1`` characters, then raise rather than accumulate.

    The +1 is what makes the bound detectable: reading exactly ``limit`` cannot
    distinguish "fits" from "truncated here", and a truncated response that still
    claims to be a response is the failure this exists to prevent. Reading in
    chunks keeps a single enormous line from defeating the bound, which a
    line-oriented read would not.
    """
    chunks: list[str] = []
    remaining = limit + 1
    try:
        while remaining > 0:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except UnicodeError as exc:
        raise BbResponseDecodeError("native response stream could not be decoded") from exc
    text = "".join(chunks)
    if len(text) > limit:
        raise BbResponseTooLarge(f"native stream exceeded {limit} chars while reading")
    return text


def subprocess_transport(
    bb_executable: Sequence[str], *, max_response_chars: int = MAX_RESPONSE_CHARS
) -> BbTransport:
    """The production transport: one bb subprocess per call, bounded while reading.

    Slice 1A shipped no production transport, so GH-570 is discharged here rather
    than deferred: both streams are bounded at the read boundary, and an overflow
    kills the child and raises instead of returning a truncated result.

    A deadline is enforced with an off-thread launch and ``communicate``-free
    manual reads so neither launch nor reading can block the calling thread past
    the bound. On timeout an available child is killed and
    ``BbTransportTimeout`` is raised, which the client maps to an AMBIGUOUS
    outcome for task-bearing calls.
    """

    def transport(argv: Sequence[str], timeout_seconds: float) -> BbTransportResult:
        global _stalled_launches

        with _stalled_launches_lock:
            if _stalled_launches >= MAX_STALLED_LAUNCHES:
                return BbTransportResult(
                    1,
                    "",
                    f"stalled launch cap ({MAX_STALLED_LAUNCHES}) reached",
                )

        # The budget starts at the launch boundary so whatever Popen costs is
        # subtracted from what the waits below get.
        deadline = time.monotonic() + timeout_seconds
        process = None

        def kill_child() -> None:
            assert process is not None
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()

        def discard_late_process(late_process) -> None:
            try:
                try:
                    late_process.kill()
                except ProcessLookupError:
                    pass
                late_process.wait()
            finally:
                for pipe in (late_process.stdout, late_process.stderr):
                    if pipe is not None:
                        pipe.close()

        # ONE end-to-end deadline, established above at the launch boundary.
        def remaining() -> float:
            # Never pass a non-positive timeout: 0 is "poll once and raise",
            # which is the correct behaviour once the budget is spent, but a
            # negative value is rejected by process.wait().
            return max(0.0, deadline - time.monotonic())

        launch_done = threading.Event()
        launch_decided = threading.Event()
        launch_abandoned = threading.Event()
        launch_result = []
        launch_error = []

        def launch_process() -> None:
            global _stalled_launches

            launched = None
            try:
                launched = subprocess.Popen(
                    [*bb_executable, *argv],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except BaseException as exc:
                launch_error.append(exc)
            else:
                launch_result.append(launched)
            launch_done.set()
            launch_decided.wait()
            if launch_abandoned.is_set():
                try:
                    if launched is not None:
                        discard_late_process(launched)
                finally:
                    with _stalled_launches_lock:
                        _stalled_launches -= 1

        launch_thread = threading.Thread(
            target=launch_process,
            name="bb-subprocess-launch",
            daemon=True,
        )
        launch_thread.start()
        if not launch_done.wait(timeout=remaining()):
            with _stalled_launches_lock:
                _stalled_launches += 1
            launch_abandoned.set()
            launch_decided.set()
            raise BbTransportTimeout(
                f"{' '.join(argv)} exceeded {timeout_seconds}s"
            )
        if launch_error:
            launch_decided.set()
            raise launch_error[0]
        process = launch_result[0]
        launch_decided.set()

        pool = ThreadPoolExecutor(max_workers=2)
        aborting = False
        try:
            # Giving each wait a fresh `timeout_seconds` made the configured
            # bound per-wait: a child closing stdout just under the limit, then
            # stderr just under a second limit, returned after nearly twice it,
            # and process.wait() could add a third interval. The watcher's
            # configured bound has to bound the CALL.
            out = pool.submit(_read_bounded, process.stdout, max_response_chars)
            err = pool.submit(_read_bounded, process.stderr, max_response_chars)
            stdout = out.result(timeout=remaining())
            stderr = err.result(timeout=remaining())
            exit_code = process.wait(timeout=remaining())
        except FuturesTimeout as exc:
            aborting = True
            kill_child()
            raise BbTransportTimeout(f"{' '.join(argv)} exceeded {timeout_seconds}s") from exc
        except subprocess.TimeoutExpired as exc:
            aborting = True
            kill_child()
            raise BbTransportTimeout(f"{' '.join(argv)} exceeded {timeout_seconds}s") from exc
        except BbResponseReadError:
            # Kill before re-raising: the child is still writing, and leaving it
            # running would keep producing output nobody will read.
            aborting = True
            kill_child()
            raise
        finally:
            # A timed-out/oversized reader must never be joined before the child
            # is dead: it may still hold the pipe open and turn the deadline into
            # a watcher-wide hang. The pipes are closed below so those readers
            # can unwind without delaying this call.
            pool.shutdown(wait=not aborting, cancel_futures=True)
            if process is not None:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
        return BbTransportResult(exit_code, stdout, stderr)

    return transport
