#!/usr/bin/env python3
"""codex_stream.py — watch a worker's App Server thread live.

A peer client that resumes a thread receives the same event fanout as the client
that started the turn: turn/started, item/agentMessage/delta, item/completed,
turn/completed. So "what is Codex doing right now" needs no Accessibility
automation, no foreground window, and no shared-daemon refactor -- only a second
WebSocket connection to the App Server we already run.

Observation only. This never starts, steers, or interrupts a turn, and sends NO
response of any kind to a server-initiated request -- not a result, not an error.
A pending request can be resolved by the first client to answer it, so an observer
that replied could abort work the operator initiated in the desktop app.

Usage:
  python bin/codex_stream.py --agent codex --project amiga --chat CHAT-8976EECB
  python bin/codex_stream.py --agent codex --project amiga --chat last --seconds 60
  python bin/codex_stream.py --agent codex --project amiga --chat last \
      --thread 019f9452-6954-7301-bff9-db1c47232bc8 --raw
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import base64
import hashlib
import urllib.parse
import json
import math
import os
import socket
import time

import _session_autobridge as autobridge
from _helpers import ROOT

DEFAULT_IDLE_TIMEOUT_SECONDS = 5
# Only a liveness failure may be skipped when choosing among chats. Every other refusal from
# the resolver reports that the workspace is INCONSISTENT -- a missing or malformed binding, a
# binding that does not match its own location, two sessions claiming it -- and quietly moving
# on would let a corrupt sibling hand the caller a different worker than the one they asked for.
SKIPPABLE_RESOLVER_REASONS = frozenset({autobridge.EXACT_BINDING_NOT_DISPATCHABLE_REASON})
# This client speaks to a Codex App Server. The shared resolver only checks that a binding and
# its session AGREE on the family, so a consistent claude_app or gemini_cli pair passes it --
# correct for dispatch, wrong here, where the session id would go to the wrong runtime.
CODEX_RUNTIME_FAMILY = "codex_app"
# One cumulative budget over an untrusted bindings tree, charged at the enumeration boundary.
MAX_SCANNED_CHATS = 2000
# The session tree is untrusted too. Before this budget, delegating per chat meant the shared
# resolver rescanned every session file once per candidate -- up to MAX_SCANNED_CHATS full passes
# over an unbounded directory. One scan now serves the whole lookup, and both its count and each
# file's size are capped.
MAX_SCANNED_SESSIONS = 5000
MAX_SESSION_BYTES = 256 * 1024
# projects.json is workspace-local and therefore untrusted like the trees above. read_text()
# allocated and parsed all of it before any lookup limit existed, so the earliest parse boundary in
# the whole run was also the only unbounded one -- AGENTS.md:165-168 puts the budget exactly there.
MAX_REGISTRY_BYTES = 256 * 1024
# Notifications buffered while initialize/thread/resume is in flight. A server that stalls a
# response while still emitting events would otherwise grow this list without limit, and the
# default run has no duration at all. Aborting beats silently dropping: a dropped event at the
# subscription boundary is the exact loss this buffer exists to prevent.
MAX_PENDING_EVENTS = 4096
MAX_PENDING_EVENT_BYTES = 8 * 1024 * 1024
# A ceiling on ONE frame, enforced by the shared client the moment the length is decoded. The
# buffer budget above is charged on the DECODED message, which is far too late: _recv_frame trusts
# the peer's 64-bit length field and calls recv() with it, so a frame advertising 1 TiB reached
# recv(1099511627776) before any accounting ran. A budget at the wrong layer is not a budget.
MAX_FRAME_BYTES = 8 * 1024 * 1024
BINDINGS_DIR = ROOT / "State" / "session_autobridge" / "bindings"


class ObserverClient(autobridge.JsonRpcWebSocketClient):
    """A connection that answers nothing at all.

    The base client refuses a server request with a correlated JSON-RPC error, which is
    correct for the role it serves: a connection that OWNS a turn must answer, or the turn
    hangs waiting. (It used to send `{"result": {}}` instead -- an unauthorized success
    envelope, invalid for all eleven members of the experimental ServerRequest union, since
    none of their response schemas can be satisfied by an empty object. That was removed
    from main in #308.)

    Refusing is nonetheless wrong HERE. App Server fans a pending request out to the
    subscribed connections, and the FIRST response -- result or error -- can resolve it. An
    observer that auto-errors can therefore abort work the operator initiated in ChatGPT.app
    before the UI has answered, turning their approval into someone else's refusal. So the
    policy belongs to the role, not to the protocol: `refuse` for a turn owner, silence for
    a watcher.

    Silence is also the right default under uncertainty, which is how this was decided before
    the fan-out was confirmed: if requests fan out, silence protects the operator and an
    error harms them; if they only ever reach the turn owner, this connection never sees one
    and silence costs nothing. Never worse either way.

    So: log the request with its identity, respond nothing, keep reading. Receiving one at
    all is itself evidence of fan-out and is reported as such.
    """

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("max_frame_bytes", MAX_FRAME_BYTES)
        super().__init__(*args, **kwargs)
        self.observed_requests: list[str] = []
        # Notifications seen while a request/response correlation is in flight. The inherited
        # request() loop DISCARDS them, so an event emitted after this socket was registered
        # for the thread but before thread/resume answered was silently lost -- exactly at the
        # subscription boundary, where turn/started and the first items live.
        self.pending_events: list[dict] = []
        self.pending_event_bytes = 0

    def request(self, method: str, params: dict | None = None, **kwargs) -> object:
        """Correlate a response while BUFFERING anything else that arrives.

        The inherited loop drops every non-matching message. That is harmless for a client
        which only issues requests, and lossy for one that subscribes: App Server can emit a
        notification once this socket is registered for the thread and before thread/resume's
        response comes back, and those events are the beginning of the stream.
        """
        self.counter += 1
        request_id = f"llm-collab-{self.counter}"
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": params or {}}
        self.send_json(payload)
        while True:
            message = self.recv_json()
            if message.get("id") == request_id:
                error = message.get("error")
                if error:
                    raise RuntimeError(f"{method}: {error.get('message', 'unknown error')}")
                return message.get("result")
            if message.get("method"):
                self.buffer_pending_event(message)

    def buffer_pending_event(self, message: dict) -> None:
        """Retain one setup-window notification, under a cumulative count and byte budget.

        Charged on both axes because either alone is evadable: many tiny notifications exhaust the
        count, one enormous one exhausts memory. Raising aborts setup with no partial state, which
        AGENTS.md:165-168 requires over trimming the queue -- the events at the subscription
        boundary are precisely the ones this buffer exists to keep.
        """
        size = len(json.dumps(message, separators=(",", ":")))
        if len(self.pending_events) + 1 > MAX_PENDING_EVENTS:
            raise SystemExit(
                f"[error] more than {MAX_PENDING_EVENTS} notifications arrived while setup was "
                "still in flight; aborting rather than buffering without limit"
            )
        if self.pending_event_bytes + size > MAX_PENDING_EVENT_BYTES:
            raise SystemExit(
                f"[error] setup-window notifications exceeded {MAX_PENDING_EVENT_BYTES} bytes; "
                "aborting rather than buffering without limit"
            )
        self.pending_events.append(message)
        self.pending_event_bytes += size

    def take_pending_events(self) -> list[dict]:
        """Hand over anything buffered during setup, so the loop can replay it in order."""
        drained, self.pending_events = self.pending_events, []
        self.pending_event_bytes = 0
        return drained

    def recv_json(self) -> dict:
        """Read one JSON message, rechecking the deadline on every frame.

        Its own frame loop rather than the base client's, because ping frames are consumed
        inside that loop: a peer sending them steadily reset the wait each time and a 0.1s
        budget returned after roughly 0.21s. The deadline is absolute, so pings, control
        frames and refused server requests all cost time against it instead of extending it.
        """
        while True:
            self._check_deadline("while reading")
            if self.sock is not None:
                self.sock.settimeout(self.remaining_wait())
            opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise ConnectionError("websocket closed")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode != 0x1:
                continue
            message = json.loads(payload.decode("utf-8"))
            if message.get("id") is not None and message.get("method"):
                method = str(message["method"])
                self.observed_requests.append(method)
                params = message.get("params") or {}
                print(f"[request] {method} id={message['id']} "
                      f"thread={params.get('threadId', '?')} turn={params.get('turnId', '?')}",
                      file=sys.stderr)
                print("[action] observer answers nothing; only the turn's owner may respond",
                      file=sys.stderr)
                continue
            return message



def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch a worker's Codex thread live.")
    p.add_argument("--agent", help="Agent id whose bound thread to watch")
    p.add_argument("--project", help="Project id (with --agent)")
    p.add_argument("--chat", help="Chat id, or 'last' for the newest binding (with --agent)")
    p.add_argument("--thread", help="Assert the resolved thread id; refuses on mismatch. Not a "
                                    "bypass -- --agent and --project are still required")
    # There is deliberately NO --runtime-home. The home selects which App Server this connects
    # to, so a caller-supplied home redirects the endpoint -- and it did: a thread that passed
    # every identity check, plus a home from another project, connected to that project's server.
    # Asserting the thread id did not help, because the mismatch check passed first and the
    # substitution happened afterwards. The home comes from the VALIDATED PAIR: the binding's
    # runtime_home preferred, the session's home as fallback when the binding records none, and
    # caller input never. Both fallback sources were checked by resolve_exact_dispatch_pair; the
    # caller's was not, which is the whole distinction.
    p.add_argument("--seconds", type=finite_seconds,
                   help="Stop after this long (default: until Ctrl-C)")
    p.add_argument("--raw", action="store_true", help="Print every notification as JSON")
    return p.parse_args()


def finite_seconds(value: str) -> float:
    """A real, positive duration.

    argparse's float() happily accepts "nan" and "inf", and both defeat the stopping limit this
    option advertises: every comparison against a NaN deadline is false, and an infinite deadline
    is never reached, so an otherwise idle run continues until interrupted.
    """
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if not math.isfinite(seconds):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a finite duration; it would never stop"
        )
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be greater than zero")
    return seconds


def one_path_component(value: str, *, field: str) -> str:
    """A selector must name exactly ONE literal path component.

    Rejecting glob metacharacters was not enough: a selector is still joined into a path, so
    `--project 'amiga/../nuvyr'` walked out of the segment it named and reached another
    project's thread. A record-versus-location check cannot catch that either, because it
    compares the record against the LEXICAL destination component -- nuvyr -- not against what
    the caller actually asked for, so the record looks perfectly consistent with where it
    landed. The selector has to be validated before it is ever joined into a path.

    Validated before any filesystem read: no separators, no . or .., non-empty, and unchanged
    by Path().name so no platform-specific spelling slips through.
    """
    text = str(value)
    if (not text or text in {".", ".."} or "/" in text or "\\" in text
            or Path(text).name != text):
        raise SystemExit(
            f"[error] --{field} must be one literal name, not a path: {value!r}"
        )
    return text


def registered_project_ids() -> set[str]:
    """Project ids from projects.json, so an unregistered directory cannot be watched."""
    path = ROOT / "projects.json"
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_REGISTRY_BYTES + 1)
    except OSError:
        return set()
    if len(raw) > MAX_REGISTRY_BYTES:
        # Raise rather than return empty: an unreadable registry and an oversized one are
        # different failures, and the caller turns an empty set into "cannot verify the project",
        # which would misreport this one.
        raise SystemExit(
            f"[error] {path} exceeds the {MAX_REGISTRY_BYTES} byte limit; refusing to parse it"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    projects = payload.get("projects")
    if not isinstance(projects, list):
        return set()
    return {str(entry["id"]) for entry in projects
            if isinstance(entry, dict) and entry.get("id")}


def resolve_thread(args: argparse.Namespace) -> tuple[str, str, str | None]:
    """Return (thread_id, provenance, runtime_home) for ONE live, exactly-bound worker.

    Resolution is DELEGATED to autobridge.resolve_exact_dispatch_pair(), which shares its
    validation with the resolve_exact_dispatch_target() wrapper that production dispatch calls.
    The difference between the two is not incidental: `pair` returns the exact binding snapshot
    the validation was performed against, and `target` returns only the session. Reading the
    binding again afterwards -- which the binding-less API forces -- is the TOCTOU this PR
    exists to close, so a maintainer must not be sent back to it.

    Six consecutive review rounds found holes in my own reimplementation of that validation --
    project and chat not compared, the runtime family unchecked, a deactivated session still
    winning, identity trusted from the record's own claims -- and each fix exposed the next
    adjacent one. That is the signature of duplicating an audited invariant, not of an unlucky
    sequence of bugs. The audited path requires an exact binding whose project, chat and agent
    match its location, a session whose id, runtime family and runtime thread all match the
    binding, and that the session be dispatchable.

    What remains here is only what that function does not do: choosing WHICH chat when the
    caller did not name one, and reading the runtime home for the endpoint.
    """
    # --thread used to return right here, before --project was required or validated. That let a
    # thread id from another project -- or from no registered project at all -- be observed
    # whenever projects share a CODEX_HOME and App Server, which AGENTS.md forbids outright: a
    # project-aware reader must require an exact project match. So --thread is no longer a bypass
    # around the binding lookup; it is an ASSERTION over it. You still name the project and agent,
    # resolution still goes through the audited exact-binding path, and the thread you named must
    # be the one that path arrives at. An unbound thread is not project-scoped by construction and
    # is therefore not observable here at all.
    if not args.agent:
        raise SystemExit("[error] pass --agent with --project (and optionally --thread)")

    agent = one_path_component(args.agent, field="agent")
    if args.project is None:
        raise SystemExit(
            "[error] --project is required: this watches one project's worker, and "
            "enumerating every project could select a thread you did not name"
        )
    project = one_path_component(args.project, field="project")
    registered = registered_project_ids()
    if not registered:
        raise SystemExit(
            "[error] cannot read any registered project from projects.json, so "
            f"{project!r} cannot be verified; refusing rather than trusting the directory"
        )
    if project not in registered:
        raise SystemExit(
            f"[error] project {project!r} is not registered in projects.json "
            f"(known: {', '.join(sorted(registered))})"
        )
    if args.chat is not None and args.chat != "last":
        one_path_component(args.chat, field="chat")

    if args.chat is not None and args.chat != "last":
        # Named exactly: a dead or mismatched binding is an ERROR, because the caller asked
        # for this one specifically and silently substituting another would be worse.
        chosen = [(args.chat, *resolve_one(project, args.chat, agent, fatal=True))]
    else:
        # Broad selection: a dead binding is EXCLUDED, not fatal. deactivate_session() updates
        # the session and deliberately leaves the binding behind, so one ordinary deactivation
        # would otherwise break `--chat last` for that agent permanently.
        chosen = []
        sessions = bounded_sessions(agent)
        for chat in bounded_chat_ids(project, agent):
            resolved = resolve_one(project, chat, agent, fatal=False, sessions=sessions)
            if resolved is not None:
                chosen.append((chat, *resolved))

    if not chosen:
        raise SystemExit(
            f"[error] no live exactly-bound {agent!r} session in {project!r}"
        )
    if len(chosen) > 1 and args.chat != "last":
        names = "\n  ".join(f"{project}/{chat}" for chat, _binding, _session in sorted(
            chosen, key=lambda triple: triple[0]))
        raise SystemExit(
            f"[error] {len(chosen)} live bindings match agent {agent!r} in {project!r}; name "
            f"one with --chat, or pass --chat last:\n  {names}"
        )

    # `last` is advertised as the newest BINDING, and the binding is also where runtime_home
    # lives. Every field below comes from the ONE snapshot that was validated -- see
    # resolve_one() for why re-reading the path here was a TOCTOU.
    chat, binding, session = max(chosen, key=lambda triple: str(triple[1].get("updated_utc") or ""))
    runtime = session.get("runtime") or {}
    thread_id = str(binding.get("runtime_session_id") or runtime.get("session_id") or "")
    if not thread_id:
        raise SystemExit(f"[error] {project}/{chat} records no runtime thread id")
    # The home comes from the validated pair and nothing else. Any caller-supplied value is
    # ignored by construction rather than by a comparison that could be ordered wrongly.
    if args.thread and args.thread != thread_id:
        raise SystemExit(
            f"[error] --thread {args.thread!r} is not the thread bound to {project}/{chat} for "
            f"{agent!r}, which is {thread_id!r}. Refusing rather than observing a thread this "
            "project does not own."
        )
    # Binding preferred, session as fallback -- both come from the pair this resolution validated,
    # so neither is caller-controlled. There is deliberately no third branch.
    home = binding.get("runtime_home") or runtime.get("home")
    return thread_id, f"{project}/{chat}", str(home) if home else None


def bounded_sessions(agent: str) -> list[dict]:
    """Every session for this agent, read ONCE, under a count and a per-file byte cap.

    The shared resolver scans the session directory itself, which is right for a single lookup
    and wrong for a loop: delegating per chat repeated that whole scan once per candidate. This
    scan happens once and is handed to each call.

    Bounded because the directory is untrusted: the count is charged at the enumeration boundary
    before any filtering, and each file is read with one bounded read rather than a stat followed
    by an unbounded one.
    """
    sessions: list[dict] = []
    scanned = 0
    try:
        with os.scandir(autobridge.SESSIONS_DIR) as scan:
            entries = []
            for entry in scan:
                scanned += 1
                if scanned > MAX_SCANNED_SESSIONS:
                    raise SystemExit(
                        f"[error] more than {MAX_SCANNED_SESSIONS} entries under "
                        f"{autobridge.SESSIONS_DIR}; refusing to scan further"
                    )
                if entry.is_file() and entry.name.endswith(".json"):
                    entries.append(Path(entry.path))
    except FileNotFoundError:
        return []
    except OSError as error:
        raise SystemExit(f"[error] cannot scan session records: {error}")

    for path in sorted(entries):
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_SESSION_BYTES + 1)
        except OSError as error:
            raise SystemExit(f"[error] cannot read session record {path}: {error}")
        if len(raw) > MAX_SESSION_BYTES:
            raise SystemExit(
                f"[error] session record {path} exceeds the {MAX_SESSION_BYTES} byte limit; "
                "refusing to parse it"
            )
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # a malformed session cannot describe a live worker; the resolver's own scan skips
            # these too, so skipping matches the behaviour this replaces
            continue
        if isinstance(record, dict) and record.get("agent_id") == agent:
            sessions.append(record)
    return sessions


def resolve_one(project: str, chat: str, agent: str, *, fatal: bool,
                sessions: list[dict] | None = None):
    """Resolve one chat to the (binding, session) pair the resolver validated, or None if dead.

    The snapshot comes FROM the validation, not from a second read. Four re-reads of the path
    were four chances to see a different file, and narrowing that to one still left a window --
    which a local cross-check could not close, because `runtime_home` is a field the binding owns
    and the session deliberately does not mirror. So a swap that moved only the home passed every
    comparison and still redirected the endpoint.

    resolve_exact_dispatch_pair() hands back the exact binding it validated against, so there is
    nothing left to reopen and nothing to reconcile.
    """
    pair, reason = autobridge.resolve_exact_dispatch_pair(project, chat, agent, sessions=sessions)
    if pair is None:
        if not fatal and reason in SKIPPABLE_RESOLVER_REASONS:
            return None
        raise SystemExit(
            f"[error] {project}/{chat} is not an exact live binding for {agent!r}: {reason}"
            + ("" if fatal else ". Fix or remove that binding; broad lookup will not step over it.")
        )
    session, binding = pair

    family = str(binding.get("runtime_family") or "")
    if family != CODEX_RUNTIME_FAMILY:
        raise SystemExit(
            f"[error] {project}/{chat} is runtime_family={family or 'unset'!r}; only "
            f"{CODEX_RUNTIME_FAMILY} can be watched through a Codex App Server"
        )
    return binding, session


def bounded_chat_ids(project: str, agent: str) -> list[str]:
    """Chat ids under a project that have a binding for this agent, within one budget.

    The budget is charged at the enumeration boundary, before any filtering: charging only
    directories let an untrusted tree spend unbounded work on entries that were then discarded.
    """
    chats: list[str] = []
    scanned = 0
    try:
        with os.scandir(BINDINGS_DIR / project) as scan:
            for entry in scan:
                scanned += 1
                if scanned > MAX_SCANNED_CHATS:
                    raise SystemExit(
                        f"[error] more than {MAX_SCANNED_CHATS} entries under {project!r}; "
                        "name one with --chat"
                    )
                if entry.is_dir() and (Path(entry.path) / f"{agent}.json").is_file():
                    chats.append(entry.name)
    except OSError:
        return []
    return sorted(chats)




def message_started_id(method: str, params: dict) -> str | None:
    """The agentMessage id this event announces the START of, if any.

    item/started is the ONLY evidence that we were present from a message's beginning. Marking
    the first delta we happen to SEE instead labelled a suffix-only subscription as complete,
    which then suppressed the completion payload carrying the text we missed -- so a late
    observer printed the tail and never the rest.
    """
    if method != "item/started":
        return None
    item = params.get("item") or {}
    if item.get("type") != "agentMessage":
        return None
    return str(item.get("id") or "") or None


def unstreamed_message_text(item: dict, streamed_from_start: set[str]) -> str | None:
    """The message text the caller has NOT already seen streamed, or None.

    An observer that subscribes mid-message receives only the later deltas, while the
    completion payload carries the whole thing. Discarding that completion on the assumption
    that every delta was observed left the default view showing a suffix -- or nothing at all
    when every delta preceded subscription. Reprinting it unconditionally would duplicate
    every message instead.

    Consumes the id, so a message followed from its first delta is reported once as already
    seen and its completion stays silent.
    """
    item_id = str(item.get("id") or "")
    if item_id and item_id in streamed_from_start:
        streamed_from_start.discard(item_id)
        return None
    text = str(item.get("text") or "")
    return text or None


def file_change_paths(item: dict) -> str:
    """The paths a fileChange item touches.

    A fileChange item has NO top-level `path`: the schema requires a `changes` array and each
    entry carries its own. Reading `item["path"]` printed an empty `edit` line for every
    protocol-valid item -- the tool reported that an edit happened while withholding the one
    fact that matters about it.
    """
    changes = item.get("changes")
    if not isinstance(changes, list):
        return str(item.get("path") or "(unspecified path)")
    paths = [str(change.get("path")) for change in changes
             if isinstance(change, dict) and change.get("path")]
    if not paths:
        return "(unspecified path)"
    if len(paths) == 1:
        return paths[0]
    return f"{len(paths)} files: {', '.join(paths)}"


def elide(value: object, limit: int = 160) -> str:
    """Shorten for display, but never let a shortened value look complete.

    A command or path cut at the limit still read like the whole thing, which can hide the
    distinguishing -- or destructive -- suffix of what a worker is actually running.
    """
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [+{len(text) - limit} chars truncated]"


def describe(method: str, params: dict) -> str | None:
    """One human line per event, or None to stay quiet."""
    if method == "turn/started":
        return f"turn started  {params.get('turn', {}).get('id', '?')}"
    if method == "turn/completed":
        turn = params.get("turn", {})
        return f"turn {turn.get('status', 'completed')}  {turn.get('id', '?')}"
    if method == "thread/status/changed":
        status = params.get("status") or {}
        return f"status: {status.get('type', status)}"
    if method == "item/started":
        item = params.get("item", {})
        kind = item.get("type", "item")
        if kind == "commandExecution":
            return f"  $ {elide(item.get('command', ''))}"
        if kind == "fileChange":
            return f"  edit {elide(file_change_paths(item))}"
        return f"  {kind} started"
    if method == "item/completed":
        item = params.get("item", {})
        if item.get("type") == "agentMessage":
            return None  # already streamed as deltas
        if item.get("type") == "commandExecution":
            return f"  $ exit={item.get('exitCode')}"
        return f"  {item.get('type', 'item')} done"
    if method == "thread/tokenUsage/updated":
        total = (params.get("tokenUsage") or {}).get("total") or {}
        if total.get("totalTokens"):
            return f"tokens: {total['totalTokens']:,}"
    return None


def main() -> None:
    args = parse_args()
    thread_id, provenance, runtime_home = resolve_thread(args)
    if not runtime_home:
        raise SystemExit(
            "[error] neither the selected binding nor its session records a runtime_home. "
            "Re-register the session with one; the home is read from the validated pair "
            "(binding first, then session) and never from caller input, because the home "
            "decides which App Server this connects to."
        )

    endpoint = autobridge.discover_codex_app_server(runtime_home)
    if endpoint is None:
        raise SystemExit(
            "[error] no Codex App Server endpoint found for CODEX_HOME "
            f"{runtime_home}.\n"
            "       Discovery looks for a process whose command contains `app-server` and\n"
            "       `--listen`, launched with CODEX_HOME set to exactly that path. Confirm\n"
            "       one is running:\n"
            "         ps ax -o pid=,command= | grep '[a]pp-server'\n"
            "       See docs/adapters/pm2.md for the supported launch procedure."
        )

    token = autobridge._codex_app_server_token(endpoint.get("token_file"))
    print(f"[stream] {provenance} thread {thread_id} via {endpoint['url']}")

    # A monotonic deadline: a wall-clock one drifts across a clock adjustment, and the socket
    # timeout must be clamped to whatever remains or `--seconds 0.1` blocks for the full idle
    # timeout before the deadline is even rechecked.
    deadline = time.monotonic() + args.seconds if args.seconds else None
    # A boolean, not an accumulator. Appending every delta retained the whole response to read
    # only its truthiness, copying the string repeatedly while each chunk was already written
    # straight to stdout.
    text_line_open = False
    # Item ids whose text we streamed in full. A message that BEGAN before we subscribed
    # delivers only its later deltas, so its completion payload is the only source for the
    # earlier part -- but a message we followed from the start must not be printed twice.
    streamed_from_start: set[str] = set()

    failed = False
    # The inherited __enter__ uses timeout_seconds for socket.create_connection() and for every
    # handshake recv(), and it runs BEFORE any deadline could be installed. So the requested
    # duration has to be folded into the timeout itself, or `--seconds 0.1` waits seconds on a
    # stalled connect or a handshake that trickles bytes.
    connect_timeout = DEFAULT_IDLE_TIMEOUT_SECONDS
    if args.seconds:
        connect_timeout = max(0.05, min(DEFAULT_IDLE_TIMEOUT_SECONDS, args.seconds))
    client = ObserverClient(str(endpoint["url"]), token=token,
                            timeout_seconds=connect_timeout)
    # Set before __enter__ so the very first blocking read is already bounded.
    client.set_deadline(deadline)
    with client:
        client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "llm-collab-codex-stream", "version": "0.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        client.notify("initialized")
        client.request("thread/resume", {"threadId": thread_id})
        print("[stream] subscribed; Ctrl-C to stop")
        # Replay anything that arrived during setup, in order, before reading anything new.
        replay = client.take_pending_events()

        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            try:
                message = replay.pop(0) if replay else client.recv_json()
            except (TimeoutError, socket.timeout):
                continue
            except Exception as error:
                # Supervision must be able to tell a dead live view from a finished one.
                print(f"[stream] connection ended: {type(error).__name__}: {error}",
                      file=sys.stderr)
                failed = True
                break

            method = message.get("method")
            if not method:
                continue

            params = message.get("params") or {}
            if params.get("threadId") not in (None, thread_id):
                continue

            if args.raw:
                print(json.dumps(message, default=str))
                continue

            started_id = message_started_id(method, params)
            if started_id:
                streamed_from_start.add(started_id)

            if method == "item/agentMessage/delta":
                chunk = params.get("delta") or params.get("text") or ""
                sys.stdout.write(chunk)
                sys.stdout.flush()
                text_line_open = True
                continue

            if method == "item/completed" and (params.get("item") or {}).get(
                    "type") == "agentMessage":
                missing = unstreamed_message_text(params["item"], streamed_from_start)
                if text_line_open:
                    sys.stdout.write("\n")
                    text_line_open = False
                if missing:
                    print(f"[reconciled] {missing}")
                continue

            if text_line_open:
                sys.stdout.write("\n")
                text_line_open = False

            line = describe(method, params)
            if line:
                print(f"[{time.strftime('%H:%M:%S')}] {line}")

    if text_line_open:
        sys.stdout.write("\n")
    if client.observed_requests:
        print(f"[stream] saw {len(client.observed_requests)} server request(s) on this "
              f"observer socket and answered none: "
              f"{', '.join(sorted(set(client.observed_requests)))}. Receiving these here "
              f"means App Server fans requests to non-owner clients.", file=sys.stderr)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
