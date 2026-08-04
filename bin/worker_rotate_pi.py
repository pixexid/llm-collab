#!/usr/bin/env python3.11
"""worker_rotate_pi.py — rotate a context-bloated Pi worker to a fresh native session.

V1 is rotation only: it requires ``--supersedes-session`` and never mints a
first binding (a first start has no predecessor to supply the trusted
endpoint/runtime-home/scope). It drives Pi Web's loopback HTTP manager to create
a fresh tab, proves the tab's native state matches what was requested, waits for
the successor to bootstrap its own event monitor, then delegates the canonical
rebind to ``session_autobridge.py register --supersedes-session``.

Trust boundary: endpoint id, runtime home, project/chat/repo scope, mode, and
wake strategy come from the predecessor's registered record (``show`` and
``show-binding`` must agree on the whole binding tuple). The tab cwd is derived
from the registered project + repo target, never from the caller. Native session
id/source come from Pi Web. The Pi Web URL must be loopback.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from livecraft_health import LivecraftHealthError, ensure_livecraft_ready

DEFAULT_PI_WEB_URL = "http://127.0.0.1:8504"
DEFAULT_LIVECRAFT_BACKEND_URL = "http://127.0.0.1:43121"
_BIN = Path(__file__).resolve().parent
AUTOBRIDGE = _BIN / "session_autobridge.py"
# The persistent monitor must reference the stable workspace root, not this
# script's checkout (a worktree/branch disappears; the monitor must survive it).
MONITOR_PYTHON = str(Path(sys.executable).resolve())
# start-pi owns the Pi Web transport; only these records seed a first-start profile.
PI_WEB_ENDPOINT = "endpoint_pi_web_local"
LIVECRAFT_ENDPOINT = "endpoint_pi_livecraft_local"
MESSAGE_PAGE_SIZE = 500
MESSAGE_SCAN_LIMIT = 5000
HTTP_TIMEOUT_SECONDS = 30.0
HTTP_RESPONSE_LIMIT = 1024 * 1024
HTTP_READ_CHUNK = 64 * 1024


def _workspace_root() -> Path:
    from _helpers import config_get

    root, name = config_get("projects_root"), config_get("workspace_name")
    if not root or not name:
        raise RotateError("workspace root unresolved from config (projects_root/workspace_name)")
    return Path(root) / name


def _monitor_inbox() -> str:
    from _helpers import RUNTIME_ROOT

    return str(RUNTIME_ROOT / "bin" / "inbox.py")


def _sessions_dir() -> Path:
    return _workspace_root() / "State" / "session_autobridge" / "sessions"


def _resolve_chat(project: str, chat: str) -> str | None:
    from _helpers import MAX_CHAT_SCAN_ENTRIES, find_chat_by_partial, load_chat_meta

    try:
        chat_dir = find_chat_by_partial(
            chat, project=project, max_entries=MAX_CHAT_SCAN_ENTRIES,
        )
    except ValueError as error:
        raise RotateError(f"chat_not_unique: {chat}") from error
    except RuntimeError as error:
        raise RotateError(f"chat_scan_bound: {chat}") from error
    if chat_dir is None:
        return None
    try:
        meta = load_chat_meta(chat_dir)
    except (OSError, ValueError) as error:
        raise RotateError(f"chat_metadata_unreadable: {chat}") from error
    canonical = meta.get("chat_id", chat_dir.name)
    if not isinstance(canonical, str) or not canonical.strip():
        raise RotateError(f"chat_not_found: {chat}")
    return canonical


BOOTSTRAP_TEMPLATE = """Automated llm-collab worker provisioning. You are {agent} in fresh Pi native session {native}. Do not start project work during this bootstrap turn. Start exactly one persistent monitor now with monitor_watch_path (NOT monitor_start — monitor_start needs an attended confirmation and will time out).

The bootstrap hold applies only to this setup turn. Once the watcher is running, a valid non-empty durable packet addressed to this worker and scoped to this project/repository is the explicit collaboration task authorization. Follow that packet; do not ask a user to reconcile it with the bootstrap instruction or wait for an interactive answer. If no such packet arrives, remain idle after replying with the marker.

Watch this exact wake file: {event_path}
On each change to that file, run exactly: LLM_COLLAB_READER_RUNTIME_ID={native} LLM_COLLAB_READER_RUNTIME_FAMILY=pi {py} '{inbox}' --me {agent} --session {logical} --project {project} --chat {chat} --repo-target {repo} --acknowledge
Then summarize each valid durable packet and follow it. This file contains wake events only; do not watch the diagnostic event log. Do not do other work without such a packet.

After the watcher is running, reply only {marker}"""


class RotateError(Exception):
    """A precondition or Pi Web step failed; no canonical rebind was performed."""


def _set_response_timeout(response, timeout: float) -> None:
    candidates = [response, getattr(response, "fp", None), getattr(response, "raw", None)]
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    candidates.extend((raw, getattr(raw, "_sock", None)))
    for candidate in candidates:
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            try:
                setter(timeout)
            except OSError:
                continue
            return


def _read_http_body(response, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RotateError("HTTP response deadline exceeded")
        _set_response_timeout(response, remaining)
        chunk = response.read(min(HTTP_READ_CHUNK, HTTP_RESPONSE_LIMIT + 1 - total))
        if not chunk:
            return b"".join(chunks)
        if not isinstance(chunk, (bytes, bytearray)):
            raise RotateError("HTTP response body is not bytes")
        total += len(chunk)
        if total > HTTP_RESPONSE_LIMIT:
            raise RotateError("HTTP response exceeds the byte limit")
        chunks.append(bytes(chunk))


def _http_json_request(base_url: str, method: str, path: str, body):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    deadline = time.monotonic() + HTTP_TIMEOUT_SECONDS

    def decode(response, status: int):
        raw = _read_http_body(response, deadline)
        if deadline - time.monotonic() <= 0:
            raise RotateError("HTTP response deadline exceeded")
        return status, (json.loads(raw) if raw else None)

    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RotateError("HTTP response deadline exceeded")
        with urllib.request.urlopen(req, timeout=remaining) as response:
            return decode(response, response.status)
    except urllib.error.HTTPError as exc:
        try:
            return decode(exc, exc.code)
        finally:
            exc.close()


class PiWeb:
    """Thin stdlib client over jmfederico/pi-web's loopback session API."""

    def __init__(self, base_url: str, request=None):
        self.base = base_url.rstrip("/")
        self._request = request or self._http
        self._sessions = {}

    def _http(self, method: str, path: str, body):
        return _http_json_request(self.base, method, path, body)

    def create_tab(self, cwd: str, title: str) -> dict:
        status, body = self._request("POST", "/api/sessions", {"cwd": cwd})
        if status != 200 or not body:
            raise RotateError(f"create session failed (HTTP {status})")
        session = {"id": body["id"], "cwd": body["cwd"], "session_file": body.get("path")}
        self._sessions[session["id"]] = session
        return session

    def set_model(self, tab_id: str, provider: str, model_id: str) -> None:
        cwd = self._sessions[tab_id]["cwd"]
        status, body = self._request(
            "POST", f"/api/sessions/{urllib.parse.quote(tab_id)}/model",
            {"cwd": cwd, "provider": provider, "modelId": model_id},
        )
        if status != 200 or not body:
            raise RotateError("set_model refused")

    def set_thinking(self, tab_id: str, level: str) -> None:
        cwd = self._sessions[tab_id]["cwd"]
        status, body = self._request(
            "POST", f"/api/sessions/{urllib.parse.quote(tab_id)}/thinking-level",
            {"cwd": cwd, "level": level},
        )
        if status != 200 or not body or body.get("thinkingLevel") != level:
            raise RotateError("set_thinking refused")

    def get_state(self, tab_id: str) -> dict:
        session = self._sessions[tab_id]
        status, body = self._request(
            "GET", f"/api/sessions/{urllib.parse.quote(tab_id)}/status?cwd="
            + urllib.parse.quote(session["cwd"]), None,
        )
        if status != 200 or not body:
            raise RotateError("get_state failed")
        return {
            "native": body["sessionId"],
            "session_file": session["session_file"],
            "provider": body["model"]["provider"],
            "model_id": body["model"]["id"],
            "thinking": body["thinkingLevel"],
        }

    def prompt(self, tab_id: str, message: str) -> None:
        cwd = self._sessions[tab_id]["cwd"]
        status, _ = self._request(
            "POST", f"/api/sessions/{urllib.parse.quote(tab_id)}/prompt",
            {"cwd": cwd, "text": message},
        )
        if status != 200:
            raise RotateError("prompt not accepted")

    def last_assistant_text(self, tab_id: str):
        cwd = self._sessions[tab_id]["cwd"]
        before = None
        scanned = 0
        while True:
            path = (
                f"/api/sessions/{urllib.parse.quote(tab_id)}/messages?cwd="
                + urllib.parse.quote(cwd) + f"&limit={MESSAGE_PAGE_SIZE}"
            )
            if before is not None:
                path += f"&before={before}"
            status, body = self._request("GET", path, None)
            if status != 200:
                return None
            if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
                raise RotateError("messages response malformed")
            messages = body["messages"]
            scanned += len(messages)
            if scanned > MESSAGE_SCAN_LIMIT:
                raise RotateError("messages history exceeds scan limit")
            for message in reversed(messages):
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return "".join(
                        block["text"] for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ) or None
                return None
            start = body.get("start")
            total = body.get("total")
            if (
                not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(total, int) or isinstance(total, bool)
                or start < 0 or total < start
            ):
                raise RotateError("messages pagination malformed")
            if start == 0:
                return None
            if before is not None and start >= before:
                raise RotateError("messages pagination did not advance")
            before = start

    def close_tab(self, tab_id: str) -> None:
        try:
            cwd = self._sessions[tab_id]["cwd"]
            self._request(
                "POST", f"/api/sessions/{urllib.parse.quote(tab_id)}/stop", {"cwd": cwd}
            )
        except Exception:
            pass  # best-effort stop; the caller already has the real error


class Livecraft:
    """Thin stdlib client for Livecraft's backend session/RPC API."""

    def __init__(self, base_url: str, request=None):
        self.base = base_url.rstrip("/")
        self._request = request or self._http
        self._sessions = {}

    def _http(self, method: str, path: str, body):
        return _http_json_request(self.base, method, path, body)

    def create_session(self, cwd: str) -> dict:
        status, body = self._request("POST", "/api/sessions", {"cwd": cwd})
        if status != 201 or not isinstance(body, dict):
            raise RotateError(f"Livecraft create session failed (HTTP {status})")
        session_id, returned_cwd = body.get("id"), body.get("cwd")
        if not all(isinstance(value, str) and value for value in (session_id, returned_cwd)):
            raise RotateError("Livecraft create session response is incomplete")
        session = {"id": session_id, "cwd": returned_cwd}
        self._sessions[session_id] = session
        return session

    def _command(self, session_id: str, command: dict) -> dict:
        if session_id not in self._sessions:
            raise RotateError(f"unknown Livecraft session {session_id}")
        status, body = self._request(
            "POST", f"/api/sessions/{urllib.parse.quote(session_id)}/commands", command,
        )
        if status != 200 or not isinstance(body, dict):
            raise RotateError(f"Livecraft RPC {command.get('type')} failed (HTTP {status})")
        return body

    def set_model(self, session_id: str, provider: str, model_id: str) -> None:
        self._command(session_id, {"type": "set_model", "provider": provider, "modelId": model_id})

    def set_thinking(self, session_id: str, level: str) -> None:
        self._command(session_id, {"type": "set_thinking_level", "level": level})

    def get_state(self, session_id: str) -> dict:
        status, body = self._request(
            "GET", f"/api/sessions/{urllib.parse.quote(session_id)}/snapshot", None,
        )
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("state"), dict):
            raise RotateError("Livecraft snapshot/state response is malformed")
        state = body["state"]
        model = state.get("model")
        if not isinstance(model, dict):
            raise RotateError("Livecraft state has no model fingerprint")
        values = (state.get("sessionId"), state.get("sessionFile"), model.get("provider"),
                  model.get("id"), state.get("thinkingLevel"))
        if not all(isinstance(value, str) and value for value in values):
            raise RotateError("Livecraft state fingerprint is incomplete")
        return {
            "native": values[0], "session_file": values[1], "provider": values[2],
            "model_id": values[3], "thinking": values[4], "cwd": self._sessions[session_id]["cwd"],
        }

    def prompt(self, session_id: str, message: str) -> None:
        self._command(session_id, {"type": "prompt", "message": message})

    def last_assistant_text(self, session_id: str):
        status, body = self._request(
            "GET", f"/api/sessions/{urllib.parse.quote(session_id)}/snapshot", None,
        )
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return None
        for message in reversed(body["messages"]):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                text = "".join(
                    block["text"] for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                )
                return text or None
            return None
        return None

    def close_session(self, session_id: str) -> None:
        try:
            self._command(session_id, {"type": "abort"})
        except Exception:
            pass  # Livecraft has no delete endpoint; abort is best-effort cleanup.


def require_loopback(url: str, option: str = "--pi-web-url") -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost",):
        return url
    try:
        if ipaddress.ip_address(host).is_loopback:
            return url
    except ValueError:
        pass
    raise RotateError(f"{option} must be loopback, got host {host!r}")


def _default_run_autobridge(args: list[str]):
    # Run the autobridge from the canonical workspace, not the caller's checkout:
    # session_autobridge resolves State/ledger via find_workspace_root() from cwd,
    # so an isolated lane must pass the canonical cwd or it writes worktree-local
    # State the dispatcher can't see.
    proc = subprocess.run(
        [sys.executable, str(AUTOBRIDGE), *args], capture_output=True, text=True,
        cwd=str(_workspace_root()),
    )
    # On success stdout is the JSON the callers parse; on failure surface stderr
    # too so a register/partial-state failure reports the real cause, not "".
    out = proc.stdout if proc.returncode == 0 else (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def _default_event_path(logical_id: str) -> str:
    return str(_workspace_root() / "State" / "session_autobridge" / "events" / "wake" / f"{logical_id}.jsonl")


def _default_resolve_cwd(project: str, repo_target: str):
    from _helpers import resolve_project_repo_path

    path = resolve_project_repo_path(project, repo_target)
    return None if path is None else str(path)


def _load_json(run_autobridge, args: list[str], what: str) -> dict:
    rc, out = run_autobridge(args)
    if rc != 0 or not out.strip():
        raise RotateError(f"{what} failed (rc={rc})")
    return json.loads(out)


def _load_optional_binding(project: str, chat: str, agent: str) -> dict | None:
    from _session_autobridge import BindingUnreadable, load_binding

    try:
        return load_binding(project, chat, agent)
    except FileNotFoundError:
        return None
    except BindingUnreadable as exc:
        raise RotateError(f"binding for {agent} is unreadable: {exc}") from exc


def _resolve_starter(
    *, starter_agent: str, starter_session_id: str | None, project: str, chat: str,
    require_active_binding: bool = True,
) -> tuple[str, str]:
    binding = _load_optional_binding(project, chat, starter_agent)
    if not require_active_binding:
        if not starter_session_id:
            raise RotateError("starter session id is required for the disposable pilot path")
        return starter_agent, starter_session_id
    if not binding or binding.get("status") != "active":
        raise RotateError(
            f"starter binding is not active for {project}/{chat}/{starter_agent}; "
            "register the starter session before launching a worker"
        )
    native = binding.get("runtime_session_id")
    if not isinstance(native, str) or not native.strip():
        raise RotateError("starter binding has no exact native runtime session")
    if starter_session_id is not None and starter_session_id != native:
        raise RotateError(
            f"starter session mismatch: requested {starter_session_id!r}, binding has {native!r}"
        )
    return starter_agent, native


def _resolve_livecraft_predecessor(
    *, agent: str, project: str, chat: str, requested: str | None,
    strict: bool = True,
) -> str | None:
    if not strict:
        return requested
    binding = _load_optional_binding(project, chat, agent)
    current = binding.get("session_id") if binding and binding.get("status") == "active" else None
    if requested is not None and requested != current:
        raise RotateError(
            f"--supersedes-session {requested!r} does not name the current active "
            f"binding {current!r} for {project}/{chat}/{agent}"
        )
    return current


def resolve_predecessor(run_autobridge, agent, project, chat, repo, pred) -> dict:
    """Load the predecessor from both show and show-binding and require them to
    describe the exact same active binding tuple before anything native happens."""
    show = _load_json(run_autobridge, ["show", "--session", pred, "--json"], "show")
    binding = _load_json(
        run_autobridge,
        ["show-binding", "--project", project, "--chat", chat, "--agent", agent, "--json"],
        "show-binding",
    )
    runtime = show.get("runtime") or {}
    # (label, show value, show-binding value, required caller value or None)
    tuple_checks = [
        ("session_id", show.get("session_id"), binding.get("session_id"), pred),
        ("status", show.get("status"), binding.get("status"), "active"),
        ("binding_id", show.get("binding_id"), binding.get("binding_id"), None),
        ("binding_generation", show.get("binding_generation"), binding.get("binding_generation"), None),
        ("endpoint_id", show.get("endpoint_id"), binding.get("endpoint_id"), None),
        ("runtime_home", runtime.get("home"), binding.get("runtime_home"), None),
        ("project_id", show.get("project_id"), binding.get("project_id"), project),
        ("chat_id", show.get("chat_id"), binding.get("chat_id"), chat),
        ("agent_id", show.get("agent_id"), binding.get("agent_id"), agent),
        ("repo_targets", show.get("repo_targets"), binding.get("repo_targets"), [repo]),
    ]
    for label, a, b, required in tuple_checks:
        if a != b:
            raise RotateError(f"predecessor {label} disagrees: show={a!r} show-binding={b!r}")
        if required is not None and a != required:
            raise RotateError(f"predecessor {label}={a!r} != required {required!r}")
    return {
        "endpoint_id": show["endpoint_id"],
        "runtime_home": runtime["home"],
        "mode": show.get("mode", "manual"),
        "wake_strategy": show.get("wake_strategy", "none"),
        "binding_generation": show.get("binding_generation"),
    }


def successor_id(predecessor: str, native: str) -> str:
    return predecessor.rsplit("-", 1)[0] + "-" + native[:8].upper()


def _display_alias(session_id) -> str | None:
    # SESSION-PIWEB-<DISPLAY>-<chatsuffix>-<native8>
    parts = str(session_id or "").split("-")
    return parts[2] if len(parts) >= 5 and parts[0] == "SESSION" and parts[1] == "PIWEB" else None


MAX_SESSION_BYTES = 1_000_000
MAX_TOTAL_SESSION_BYTES = 200_000_000


def resolve_pi_profile(
    agent: str, project: str, *, sessions_dir=None, override=None, runtime_home=None,
    endpoint_filter=PI_WEB_ENDPOINT,
) -> dict:
    """Reduce stored Pi records for one agent/project to one profile.

    ``endpoint_filter`` preserves the Pi-Web start path's historical scope. The
    Livecraft first-start path deliberately passes ``None``: provider/model/
    thinking are the agent's stored profile, while Livecraft is the selected
    transport rather than an old Pi-Web record.
    """
    sessions_dir = Path(sessions_dir) if sessions_dir is not None else _sessions_dir()
    records, homes, display, total = [], set(), None, 0
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RotateError(f"pi_profile_required: unreadable candidate {path.name}: {exc}")
        if size > MAX_SESSION_BYTES:
            raise RotateError(f"pi_profile_required: oversized candidate {path.name} ({size} bytes)")
        total += size
        if total > MAX_TOTAL_SESSION_BYTES:
            raise RotateError("pi_profile_required: session records exceed cumulative byte budget")
        try:
            d = json.loads(path.read_text())
        except Exception as exc:
            raise RotateError(f"pi_profile_required: corrupt candidate {path.name}: {exc}")
        runtime = d.get("runtime") or {}
        if d.get("agent_id") != agent or runtime.get("family") != "pi":
            continue
        if d.get("project_id") != project:
            continue
        if endpoint_filter is not None and d.get("endpoint_id") != endpoint_filter:
            continue
        if not runtime.get("home"):
            continue
        gen = d.get("binding_generation")
        if not isinstance(gen, int) or isinstance(gen, bool):
            continue
        homes.add(runtime["home"])
        display = display or _display_alias(d.get("session_id"))
        fp = d.get("pi_fingerprint") or {}
        records.append({
            "gen": gen,
            "tuple": (fp.get("provider"), fp.get("model_id"), fp.get("thinking_level")),
            "home": runtime["home"],
        })
    if not records:
        if override is None or not isinstance(runtime_home, str) or not runtime_home.strip():
            raise RotateError(f"pi_profile_required: no eligible Pi Web records for {agent} in {project}")
        if not all(isinstance(v, str) and v.strip() for v in override):
            raise RotateError("pi_profile_required: override needs all of provider/model/thinking")
        provider, model, thinking = override
        return {
            "endpoint_id": endpoint_filter or LIVECRAFT_ENDPOINT,
            "runtime_home": runtime_home,
            "wake_strategy": "runtime_trigger",
            "provider": provider,
            "model": model,
            "thinking": thinking,
            "display": agent.upper(),
        }
    if len(homes) != 1:
        raise RotateError(f"pi_profile_required: conflicting runtime homes for {agent}: {sorted(homes)}")

    if override is not None:
        if not all(isinstance(v, str) and v.strip() for v in override):
            raise RotateError("pi_profile_required: override needs all of provider/model/thinking")
        provider, model, thinking = override
    else:
        top_gen = max(r["gen"] for r in records)
        latest = [r for r in records if r["gen"] == top_gen]
        if not all(all(r["tuple"]) for r in latest):
            raise RotateError(f"pi_profile_required: generation {top_gen} has an incomplete fingerprint for {agent}")
        distinct = {r["tuple"] for r in latest}
        if len(distinct) != 1:
            raise RotateError(
                f"pi_profile_required: generation {top_gen} has {len(distinct)} conflicting tuples for {agent}"
            )
        provider, model, thinking = distinct.pop()

    return {
        "endpoint_id": endpoint_filter or LIVECRAFT_ENDPOINT,
        "runtime_home": homes.pop(),
        "wake_strategy": "runtime_trigger",
        "provider": provider,
        "model": model,
        "thinking": thinking,
        "display": display or agent.upper(),
    }


def resolve_livecraft_profile(
    agent: str, project: str, *, sessions_dir=None, override=None, runtime_home=None,
) -> dict:
    return resolve_pi_profile(
        agent, project, sessions_dir=sessions_dir, override=override,
        runtime_home=runtime_home, endpoint_filter=None,
    )


def _await_marker(piweb, tab_id, marker, *, timeout, interval, sleep, clock) -> None:
    deadline = clock() + timeout
    while clock() < deadline:
        text = piweb.last_assistant_text(tab_id)
        if (
            isinstance(text, str)
            and text.strip().splitlines()
            and text.strip().splitlines()[-1].strip() == marker
        ):
            return
        sleep(interval)
    raise RotateError("bootstrap marker never observed within timeout")


def verify_postcondition(run_autobridge, agent, project, chat, pred, successor, pred_generation) -> dict:
    """Prove the rebind actually happened: successor is the active binding at a
    higher generation and the predecessor is superseded."""
    binding = _load_json(
        run_autobridge,
        ["show-binding", "--project", project, "--chat", chat, "--agent", agent, "--json"],
        "postcondition show-binding",
    )
    pred_show = _load_json(run_autobridge, ["show", "--session", pred, "--json"], "postcondition show")
    new_generation = binding.get("binding_generation")
    verified = (
        binding.get("session_id") == successor
        and binding.get("status") == "active"
        and pred_show.get("status") == "superseded"
        and isinstance(new_generation, int)
        and isinstance(pred_generation, int)
        and new_generation > pred_generation
    )
    return {
        "verified": verified,
        "successor_generation": new_generation,
        "predecessor_generation": pred_generation,
        "predecessor_status": pred_show.get("status"),
    }


def _touch_event(event_path: str) -> None:
    # monitor_watch_path needs the exact event file to exist before it watches.
    p = Path(event_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)


def _provision_and_bind(
    *, agent, project, chat, repo_target, repo_cwd, provider, model, thinking,
    endpoint_id, runtime_home, wake_strategy, mode, make_logical, supersedes, title,
    piweb, run_autobridge, event_path_for, bootstrap_timeout, poll_interval, sleep, clock,
    prepare_event=_touch_event,
) -> dict:
    """Shared flow for rotate and start: create a fresh Pi Web tab, prove its
    native state, bootstrap the successor's monitor, and register the binding.
    Rotation passes a predecessor via `supersedes`; first start passes None."""
    tab = piweb.create_tab(repo_cwd, title)
    tab_id = tab["id"]
    try:
        if tab["cwd"] != repo_cwd:
            raise RotateError(f"tab cwd {tab['cwd']} != {repo_cwd}")
        piweb.set_model(tab_id, provider, model)
        piweb.set_thinking(tab_id, thinking)
        state = piweb.get_state(tab_id)
        if (state["provider"], state["model_id"], state["thinking"]) != (provider, model, thinking):
            raise RotateError(
                f"native state mismatch: {state['provider']}/{state['model_id']}/{state['thinking']} "
                f"!= {provider}/{model}/{thinking}"
            )
        native = state["native"]
        logical = make_logical(native)
        marker = f"BOOTSTRAP_READY_{logical}"
        event_path = event_path_for(logical)
        prepare_event(event_path)
        piweb.prompt(tab_id, BOOTSTRAP_TEMPLATE.format(
            agent=agent, native=native, logical=logical,
            event_path=event_path, py=MONITOR_PYTHON, inbox=_monitor_inbox(),
            project=project, chat=chat, repo=repo_target, marker=marker,
        ))
        _await_marker(piweb, tab_id, marker, timeout=bootstrap_timeout,
                      interval=poll_interval, sleep=sleep, clock=clock)
        # Fresh snapshot as close to register as possible; register pins its own
        # authoritative fingerprint read against this expected tuple.
        final = piweb.get_state(tab_id)
        if final["native"] != native or (
            final["provider"], final["model_id"], final["thinking"]
        ) != (provider, model, thinking):
            raise RotateError(f"native state drifted before register: {final}")
    except Exception as exc:  # any pre-register failure closes the fresh tab
        piweb.close_tab(tab_id)
        if isinstance(exc, RotateError):
            raise
        raise RotateError(f"pre-register failure closed tab {tab_id}: {exc!r}") from exc

    # `active` sessions ignore the lease clock by design (validity follows the
    # native task, not a TTL) — registering parked would expire the worker.
    argv = [
        "register", "--session", logical, "--agent", agent, "--project", project,
        "--chat", chat, "--repo-target", repo_target, "--runtime-family", "pi",
        "--runtime-session-id", final["native"], "--runtime-session-source", final["session_file"],
        "--runtime-home", runtime_home, "--endpoint-id", endpoint_id, "--runtime-instance-id", tab_id,
        "--cwd", repo_cwd, "--status", "active", "--mode", mode, "--wake-strategy", wake_strategy,
        "--expect-pi-provider", final["provider"], "--expect-pi-model", final["model_id"],
        "--expect-pi-thinking", final["thinking"],
    ]
    if supersedes:
        argv += ["--supersedes-session", supersedes]
    argv.append("--json")
    rc, out = run_autobridge(argv)
    if rc != 0:
        # No rollback after the register attempt; report the exact partial state.
        raise RotateError(
            f"register failed (rc={rc}); tab {tab_id} native {native} left as partial state: {out}"
        )
    return {"logical": logical, "native": native, "tab_id": tab_id, "register_out": out}


def rotate(
    cfg, *, piweb, run_autobridge, event_path_for, resolve_cwd,
    sleep=time.sleep, clock=time.monotonic, prepare_event=_touch_event,
) -> dict:
    owned = resolve_predecessor(
        run_autobridge, cfg.agent, cfg.project, cfg.chat, cfg.repo_target, cfg.supersedes_session
    )
    repo_cwd = resolve_cwd(cfg.project, cfg.repo_target)
    if not repo_cwd:
        raise RotateError(f"no repo path for project {cfg.project} repo {cfg.repo_target}")
    r = _provision_and_bind(
        agent=cfg.agent, project=cfg.project, chat=cfg.chat, repo_target=cfg.repo_target,
        repo_cwd=repo_cwd, provider=cfg.provider, model=cfg.model, thinking=cfg.thinking,
        endpoint_id=owned["endpoint_id"], runtime_home=owned["runtime_home"],
        wake_strategy=owned["wake_strategy"], mode="auto-read",
        make_logical=lambda native: successor_id(cfg.supersedes_session, native),
        supersedes=cfg.supersedes_session, title=f"{cfg.agent} llm-collab continuation",
        piweb=piweb, run_autobridge=run_autobridge, event_path_for=event_path_for,
        bootstrap_timeout=cfg.bootstrap_timeout, poll_interval=cfg.poll_interval, sleep=sleep, clock=clock,
        prepare_event=prepare_event,
    )
    proof = verify_postcondition(
        run_autobridge, cfg.agent, cfg.project, cfg.chat,
        cfg.supersedes_session, r["logical"], owned["binding_generation"],
    )
    return {
        "successor_session": r["logical"], "native_session_id": r["native"],
        "tab_id": r["tab_id"], "supersedes": cfg.supersedes_session, **proof,
    }


def start_pi(
    cfg, *, piweb, run_autobridge, event_path_for, resolve_cwd, resolve_profile,
    sleep=time.sleep, clock=time.monotonic, prepare_event=_touch_event,
) -> dict:
    chat = _resolve_chat(cfg.project, cfg.chat)
    if chat is None:
        raise RotateError(f"chat_not_found: {cfg.chat}")
    override = None
    if any((cfg.provider, cfg.model, cfg.thinking)):
        override = (cfg.provider, cfg.model, cfg.thinking)
    profile = resolve_profile(
        cfg.agent, cfg.project, override=override, runtime_home=cfg.runtime_home,
    )
    repo_cwd = resolve_cwd(cfg.project, cfg.repo_target)
    if not repo_cwd:
        raise RotateError(f"no repo path for project {cfg.project} repo {cfg.repo_target}")
    suffix = chat[len("CHAT-"):] if chat.startswith("CHAT-") else chat
    r = _provision_and_bind(
        agent=cfg.agent, project=cfg.project, chat=chat, repo_target=cfg.repo_target,
        repo_cwd=repo_cwd, provider=profile["provider"], model=profile["model"], thinking=profile["thinking"],
        endpoint_id=profile["endpoint_id"], runtime_home=profile["runtime_home"],
        wake_strategy=profile["wake_strategy"], mode="auto-read",
        make_logical=lambda native: f"SESSION-PIWEB-{profile['display']}-{suffix}-{native[:8].upper()}",
        supersedes=None, title=f"{cfg.agent} llm-collab start",
        piweb=piweb, run_autobridge=run_autobridge, event_path_for=event_path_for,
        bootstrap_timeout=cfg.bootstrap_timeout, poll_interval=cfg.poll_interval, sleep=sleep, clock=clock,
        prepare_event=prepare_event,
    )
    binding = _load_json(
        run_autobridge,
        ["show-binding", "--project", cfg.project, "--chat", chat, "--agent", cfg.agent, "--json"],
        "start-pi postcondition",
    )
    verified = binding.get("session_id") == r["logical"] and binding.get("status") == "active"
    return {
        "session": r["logical"], "native_session_id": r["native"], "tab_id": r["tab_id"],
        "profile": {k: profile[k] for k in ("provider", "model", "thinking")},
        "verified": verified, "generation": binding.get("binding_generation"),
    }


BOOTSTRAP_HANDSHAKE_KIND = "llm_collab.pi.bootstrap.v1"
MAX_HANDSHAKE_INBOX_BYTES = 4 * 1024 * 1024

LIVECRAFT_BOOTSTRAP_TEMPLATE = """Automated llm-collab worker provisioning. You are {agent} in fresh Livecraft native session {native}. Do not start project work during this bootstrap turn. The worker who started this session is {starter_agent}, native runtime session {starter_session_id}.

This bootstrap hold applies only to this setup turn. Before claiming ready, send exactly one durable bootstrap handshake back to {starter_agent}. The starter must receive and validate that packet before registering this session. Do not guess or change any value in the JSON below:

{handshake_json}

Send it through the deployed runtime with this exact command:
printf '%s\\n' {handshake_command_json} | {runtime_command} deliver.py --chat {chat_command} --from {agent_command} --to {starter_agent_command} --title {title_command} --priority high --tags {tags_command} --project {project_command} --repo-targets {repo_command} --sender-session-id {native_command} --target-session-id {starter_session_command} --body-file -

If delivery fails, do not claim ready. After successful delivery, reply only {marker}
The starter will arm the background wake path with this exact reader identity; do not start a Pi event monitor yourself and do not use Pi-Web.

After setup, remain idle until the starter presents a valid durable packet for this exact project and repository."""


def _livecraft_declaration_path() -> Path:
    from _helpers import RUNTIME_ROOT

    return Path(RUNTIME_ROOT) / "docs" / "protocols" / "standalone-v1-feature-declarations.json"


def _require_current_project_authority(project: str, *, mode: str) -> None:
    """Require the current ledger snapshot to authorize canonical project writes."""
    from _helpers import config_get, project_state_root
    from llm_collab.ledger import LedgerPaths, LedgerStore

    workspace_id = config_get("workspace_id")
    if not workspace_id:
        raise RotateError(f"{mode} gate: workspace_id is unset")
    try:
        paths = LedgerPaths.derive(project_state_root(), str(workspace_id))
        with LedgerStore.open_reader(paths) as store:
            revision = store.current_registry_revision(workspace_id=str(workspace_id))
            snapshot = store.get_project_snapshot(
                workspace_id=str(workspace_id), project_id=project, registry_revision=revision,
            )
        payload = json.loads(snapshot["snapshot_json"]) if snapshot else None
    except Exception as exc:
        raise RotateError(f"{mode} gate: current project authority unavailable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("canonical_writes") is not True:
        raise RotateError(f"{mode} gate: current project authority does not enable canonical writes")


def require_livecraft_pilot_gate(cfg, *, declaration_path=None, environ=None) -> None:
    """Keep Livecraft native mutation off unless every pilot conjunct is explicit."""
    from llm_collab.daemon.gate import evaluate_observation_gate

    environment = os.environ if environ is None else environ
    if cfg.disposable is not True:
        raise RotateError("Livecraft pilot gate: --disposable is required")
    if cfg.pilot_scope != f"{cfg.project}/{cfg.agent}":
        raise RotateError("Livecraft pilot gate: --pilot-scope must equal <project>/<agent>")
    gate = evaluate_observation_gate(
        Path(declaration_path) if declaration_path is not None else _livecraft_declaration_path(),
        environ=environment,
    )
    if not gate.dispatch_effective:
        raise RotateError("Livecraft pilot gate: runtime dispatch is not enabled for an exact thread")
    if environment.get("LLM_COLLAB_CANONICAL_CONTROL") != "enabled":
        raise RotateError("Livecraft pilot gate: canonical control is not enabled")
    _require_current_project_authority(cfg.project, mode="Livecraft pilot")


def require_livecraft_production_gate(cfg, *, environ=None) -> None:
    """Allow a real binding when the current project authority permits writes."""
    _require_current_project_authority(cfg.project, mode="Livecraft production")


def require_livecraft_gate(cfg, *, declaration_path=None, environ=None) -> None:
    """Select the production launcher or retain the explicit pilot test path."""
    if getattr(cfg, "production", True):
        require_livecraft_production_gate(cfg, environ=environ)
    else:
        require_livecraft_pilot_gate(cfg, declaration_path=declaration_path, environ=environ)


def _require_livecraft_worker_scope(cfg) -> None:
    from _helpers import is_agent_disabled, load_agents

    agent = next((item for item in load_agents() if item.get("id") == cfg.agent), None)
    if agent is None:
        raise RotateError(f"unknown worker: {cfg.agent}")
    activation = agent.get("activation") or {}
    if is_agent_disabled(agent) or activation.get("type") != "cli_session":
        raise RotateError(f"worker {cfg.agent} is not an enabled cli_session worker")
    if activation.get("watcher_enabled") is not True:
        raise RotateError(f"worker {cfg.agent} has no enabled background watcher")


def _starter_inbox_json(
    *, starter_agent: str, project: str, chat: str, repo_target: str,
    packet: str | None = None,
) -> dict | list:
    """Read or acknowledge one starter packet through the deployed runtime."""
    command = [
        MONITOR_PYTHON, _monitor_inbox(), "--me", starter_agent,
        "--project", project, "--chat", chat, "--repo-target", repo_target,
        "--json",
    ]
    if packet is None:
        # The inbox command's ordinary limit is oldest-first and can hide a
        # just-delivered handshake behind a large unread backlog. Read the
        # bounded project/chat view and select the exact handshake below.
        command.extend(("--all", "--peek", "--limit", str(MESSAGE_SCAN_LIMIT)))
    else:
        command.extend(("--packet", packet))
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=HTTP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RotateError(f"bootstrap handshake inbox read failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RotateError(f"bootstrap handshake inbox command failed: {detail}")
    if len(result.stdout.encode("utf-8")) > MAX_HANDSHAKE_INBOX_BYTES:
        raise RotateError("bootstrap handshake inbox response exceeds the byte limit")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RotateError("bootstrap handshake inbox response is not JSON") from exc
    if not isinstance(payload, (dict, list)):
        raise RotateError("bootstrap handshake inbox response is malformed")
    return payload


def _starter_handshake_messages(
    *, starter_agent: str, project: str, chat: str, repo_target: str,
) -> list[dict]:
    payload = _starter_inbox_json(
        starter_agent=starter_agent, project=project, chat=chat, repo_target=repo_target,
    )
    messages = payload if isinstance(payload, list) else payload.get("messages", [])
    if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
        raise RotateError("bootstrap handshake inbox messages are malformed")
    return messages


def _ack_starter_handshake(
    *, starter_agent: str, project: str, chat: str, repo_target: str, packet: str,
) -> None:
    payload = _starter_inbox_json(
        starter_agent=starter_agent, project=project, chat=chat,
        repo_target=repo_target, packet=packet,
    )
    messages = payload if isinstance(payload, list) else payload.get("messages", [])
    if not isinstance(messages, list) or [message.get("path") for message in messages] != [packet]:
        raise RotateError("bootstrap handshake acknowledgement did not select the exact packet")


def _await_bootstrap_handshake(
    *, starter_agent: str, starter_session_id: str, agent: str, project: str,
    chat: str, repo_target: str, native: str, session_source: str,
    handshake_id: str, timeout: float, interval: float, sleep, clock,
) -> dict:
    expected_body = {
        "kind": BOOTSTRAP_HANDSHAKE_KIND,
        "handshake_id": handshake_id,
        "starter_agent": starter_agent,
        "starter_runtime_session_id": starter_session_id,
        "worker_agent": agent,
        "worker_runtime_family": "pi",
        "worker_native_session_id": native,
        "worker_runtime_session_source": session_source,
        "project_id": project,
        "chat_id": chat,
        "repo_target": repo_target,
    }
    expected_title = f"Pi worker bootstrap handshake {handshake_id}"
    deadline = clock() + timeout
    while clock() < deadline:
        for message in _starter_handshake_messages(
            starter_agent=starter_agent, project=project, chat=chat, repo_target=repo_target,
        ):
            frontmatter = message.get("frontmatter") or {}
            if frontmatter.get("title") != expected_title:
                continue
            if frontmatter.get("from") != agent or frontmatter.get("to") != starter_agent:
                raise RotateError("bootstrap handshake sender/recipient mismatch")
            if frontmatter.get("sender_agent_id") != agent or frontmatter.get("sender_session_id") != native:
                raise RotateError("bootstrap handshake sender session mismatch")
            if frontmatter.get("project_id") != project or frontmatter.get("chat_id") != chat:
                raise RotateError("bootstrap handshake project/chat mismatch")
            if frontmatter.get("repo_targets") != [repo_target]:
                raise RotateError("bootstrap handshake repository scope mismatch")
            if frontmatter.get("target_session_id") != starter_session_id:
                raise RotateError("bootstrap handshake target session mismatch")
            try:
                body = json.loads(str(message.get("body", "")))
            except json.JSONDecodeError as exc:
                raise RotateError("bootstrap handshake body is not JSON") from exc
            if body != expected_body:
                raise RotateError("bootstrap handshake body identity mismatch")
            packet = message.get("path")
            if not isinstance(packet, str) or not packet.strip():
                raise RotateError("bootstrap handshake packet path is missing")
            _ack_starter_handshake(
                starter_agent=starter_agent, project=project, chat=chat,
                repo_target=repo_target, packet=packet,
            )
            return {"path": packet, "body": body}
        sleep(interval)
    raise RotateError(f"bootstrap handshake not received within {timeout:g} seconds")


def _cleanup_livecraft_session(*, livecraft, run_autobridge, native: str) -> list[str]:
    errors: list[str] = []
    try:
        rc, out = run_autobridge([
            "deactivate-pi", "--native-session-id", native, "--json",
        ])
        if rc != 0:
            errors.append(f"registry cleanup failed (rc={rc}): {out}")
    except Exception as exc:
        errors.append(f"registry cleanup failed: {exc}")
    try:
        livecraft.close_session(native)
    except Exception as exc:
        errors.append(f"Livecraft abort failed: {exc}")
    return errors


def _provision_livecraft_and_bind(
    *, agent, project, chat, repo_target, repo_cwd, provider, model, thinking,
    runtime_home, starter_agent, starter_session_id, supersedes_session, livecraft_backend_url,
    livecraft, run_autobridge,
    bootstrap_timeout, poll_interval, sleep, clock,
) -> dict:
    session = livecraft.create_session(repo_cwd)
    session_id = session["id"]
    try:
        if session["cwd"] != repo_cwd:
            raise RotateError(f"Livecraft session cwd {session['cwd']} != {repo_cwd}")
        livecraft.set_model(session_id, provider, model)
        livecraft.set_thinking(session_id, thinking)
        state = livecraft.get_state(session_id)
        if state["native"] != session_id or state["cwd"] != repo_cwd:
            raise RotateError(f"Livecraft native identity/cwd mismatch: {state}")
        if (state["provider"], state["model_id"], state["thinking"]) != (provider, model, thinking):
            raise RotateError(
                f"Livecraft native state mismatch: {state['provider']}/{state['model_id']}/{state['thinking']} "
                f"!= {provider}/{model}/{thinking}"
            )
        native = state["native"]
        suffix = chat[len("CHAT-"):] if chat.startswith("CHAT-") else chat
        logical = f"SESSION-LIVECRAFT-{agent.upper()}-{suffix}-{native[:8].lower()}"
        handshake_id = secrets.token_hex(16)
        handshake_body = {
            "kind": BOOTSTRAP_HANDSHAKE_KIND,
            "handshake_id": handshake_id,
            "starter_agent": starter_agent,
            "starter_runtime_session_id": starter_session_id,
            "worker_agent": agent,
            "worker_runtime_family": "pi",
            "worker_native_session_id": native,
            "worker_runtime_session_source": state["session_file"],
            "project_id": project,
            "chat_id": chat,
            "repo_target": repo_target,
        }
        from _helpers import RUNTIME_ROOT

        marker = "BOOTSTRAP_READY"
        handshake_json = json.dumps(handshake_body, sort_keys=True, separators=(",", ":"))
        livecraft.prompt(session_id, LIVECRAFT_BOOTSTRAP_TEMPLATE.format(
            agent=agent, native=native, marker=marker,
            starter_agent=starter_agent, starter_session_id=starter_session_id,
            handshake_id=handshake_id, handshake_json=handshake_json,
            handshake_command_json=shlex.quote(handshake_json),
            runtime_command=shlex.quote(str(Path(RUNTIME_ROOT) / "bin" / "llm-collab")),
            chat_command=shlex.quote(chat), agent_command=shlex.quote(agent),
            starter_agent_command=shlex.quote(starter_agent),
            title_command=shlex.quote(f"Pi worker bootstrap handshake {handshake_id}"),
            tags_command=shlex.quote("pi-worker-bootstrap,session-handshake"),
            project_command=shlex.quote(project), repo_command=shlex.quote(repo_target),
            native_command=shlex.quote(native),
            starter_session_command=shlex.quote(starter_session_id),
        ))
        _await_marker(livecraft, session_id, marker, timeout=bootstrap_timeout,
                      interval=poll_interval, sleep=sleep, clock=clock)
        final = livecraft.get_state(session_id)
        if final["native"] != native or final["cwd"] != repo_cwd or (
            final["provider"], final["model_id"], final["thinking"]
        ) != (provider, model, thinking) or final["session_file"] != state["session_file"]:
            raise RotateError(f"Livecraft native state drifted before register: {final}")
        handshake = _await_bootstrap_handshake(
            starter_agent=starter_agent, starter_session_id=starter_session_id,
            agent=agent, project=project, chat=chat, repo_target=repo_target,
            native=native, session_source=final["session_file"],
            handshake_id=handshake_id, timeout=bootstrap_timeout, interval=poll_interval,
            sleep=sleep, clock=clock,
        )
    except Exception as exc:
        _cleanup_livecraft_session(
            livecraft=livecraft, run_autobridge=run_autobridge, native=session_id,
        )
        if isinstance(exc, RotateError):
            raise
        raise RotateError(f"pre-register Livecraft failure closed session {session_id}: {exc!r}") from exc

    argv = [
        "register", "--session", logical, "--agent", agent, "--project", project,
        "--chat", chat, "--repo-target", repo_target, "--runtime-family", "pi",
        "--runtime-session-id", final["native"], "--runtime-session-source", final["session_file"],
        "--runtime-home", runtime_home, "--endpoint-id", LIVECRAFT_ENDPOINT,
        "--runtime-instance-id", session_id, "--cwd", repo_cwd, "--status", "active",
        "--mode", "auto-read", "--wake-strategy", "runtime_trigger",
        "--runtime-command", json.dumps([
            MONITOR_PYTHON, str(RUNTIME_ROOT / "bin" / "livecraft_wake.py"),
            "--backend-url", livecraft_backend_url, "--runtime-root", str(RUNTIME_ROOT),
        ]),
        "--expect-pi-provider", final["provider"], "--expect-pi-model", final["model_id"],
        "--expect-pi-thinking", final["thinking"], "--json",
    ]
    if supersedes_session:
        argv.extend(("--supersedes-session", supersedes_session))
    rc, out = run_autobridge(argv)
    if rc != 0:
        cleanup_errors = _cleanup_livecraft_session(
            livecraft=livecraft, run_autobridge=run_autobridge, native=session_id,
        )
        cleanup = f" cleanup: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
        raise RotateError(
            f"register failed (rc={rc}); Livecraft session {session_id} native {native} was aborted.{cleanup} {out}"
        )
    return {
        "logical": logical, "native": native, "tab_id": session_id,
        "register_out": out, "bootstrap_handshake": handshake,
    }


def start_livecraft(
    cfg, *, livecraft, run_autobridge, resolve_cwd, gate_check=require_livecraft_gate,
    sleep=time.sleep, clock=time.monotonic, health_check=ensure_livecraft_ready,
) -> dict:
    gate_check(cfg)
    _require_livecraft_worker_scope(cfg)
    chat = _resolve_chat(cfg.project, cfg.chat)
    if chat is None:
        raise RotateError(f"chat_not_found: {cfg.chat}")
    if chat != cfg.chat:
        raise RotateError("Livecraft requires the canonical --chat id")
    repo_cwd = resolve_cwd(cfg.project, cfg.repo_target)
    if not repo_cwd:
        raise RotateError(f"no repo path for project {cfg.project} repo {cfg.repo_target}")
    repo_cwd = str(Path(repo_cwd).resolve())
    provided_profile = tuple(
        getattr(cfg, key, None) for key in ("provider", "model", "thinking", "runtime_home")
    )
    if all(isinstance(value, str) and value.strip() for value in provided_profile):
        profile = dict(zip(("provider", "model", "thinking", "runtime_home"), provided_profile))
    else:
        override_values = tuple(getattr(cfg, key, None) for key in ("provider", "model", "thinking"))
        profile = resolve_livecraft_profile(
            cfg.agent, cfg.project,
            override=override_values if any(override_values) else None,
            runtime_home=getattr(cfg, "runtime_home", None),
        )
    starter_agent, starter_session_id = _resolve_starter(
        starter_agent=getattr(cfg, "starter_agent", "claude"),
        starter_session_id=getattr(cfg, "starter_session_id", None),
        project=cfg.project, chat=chat,
        require_active_binding=getattr(cfg, "production", True),
    )
    supersedes_session = _resolve_livecraft_predecessor(
        agent=cfg.agent, project=cfg.project, chat=chat,
        requested=getattr(cfg, "supersedes_session", None),
        strict=getattr(cfg, "production", True),
    )
    try:
        health_check(cfg.livecraft_backend_url, sleep=sleep, clock=clock)
    except LivecraftHealthError as exc:
        raise RotateError(str(exc)) from exc
    result = _provision_livecraft_and_bind(
        agent=cfg.agent, project=cfg.project, chat=chat, repo_target=cfg.repo_target,
        repo_cwd=repo_cwd, provider=profile["provider"], model=profile["model"],
        thinking=profile["thinking"], runtime_home=profile["runtime_home"],
        starter_agent=starter_agent, starter_session_id=starter_session_id,
        supersedes_session=supersedes_session,
        livecraft_backend_url=cfg.livecraft_backend_url,
        livecraft=livecraft,
        run_autobridge=run_autobridge,
        bootstrap_timeout=cfg.bootstrap_timeout, poll_interval=cfg.poll_interval,
        sleep=sleep, clock=clock,
    )
    try:
        binding = _load_json(
            run_autobridge,
            ["show-binding", "--project", cfg.project, "--chat", chat, "--agent", cfg.agent, "--json"],
            "start-livecraft-pi postcondition",
        )
    except Exception as exc:
        cleanup_errors = _cleanup_livecraft_session(
            livecraft=livecraft, run_autobridge=run_autobridge, native=result["native"],
        )
        cleanup = f" cleanup: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
        raise RotateError(f"start-livecraft-pi postcondition failed; native session was aborted.{cleanup}") from exc
    verified = binding.get("session_id") == result["logical"] and binding.get("status") == "active"
    if not verified:
        cleanup_errors = _cleanup_livecraft_session(
            livecraft=livecraft, run_autobridge=run_autobridge, native=result["native"],
        )
        cleanup = f" cleanup: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
        raise RotateError(f"start-livecraft-pi postcondition mismatch; native session was aborted.{cleanup}")
    return {
        "session": result["logical"], "native_session_id": result["native"],
        "runtime_instance_id": result["tab_id"], "verified": verified,
        "generation": binding.get("binding_generation"),
        "profile": {key: profile[key] for key in ("provider", "model", "thinking", "runtime_home")},
        "bootstrap_handshake": result["bootstrap_handshake"],
    }


def add_rotate_pi_arguments(p) -> None:
    p.add_argument("--pi-web-url", default=DEFAULT_PI_WEB_URL, help="Loopback Pi Web manager URL")
    p.add_argument("--agent", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--chat", required=True)
    p.add_argument("--repo-target", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--thinking", required=True)
    p.add_argument("--supersedes-session", required=True, help="Active predecessor logical session")
    p.add_argument("--bootstrap-timeout", type=float, default=180.0)
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.add_argument("--json", dest="json_output", action="store_true")


def run(cfg) -> int:
    try:
        require_loopback(cfg.pi_web_url)
        result = rotate(
            cfg,
            piweb=PiWeb(cfg.pi_web_url),
            run_autobridge=_default_run_autobridge,
            event_path_for=_default_event_path,
            resolve_cwd=_default_resolve_cwd,
        )
    except RotateError as exc:
        print(f"[rotate-pi] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result) if cfg.json_output else
          f"[rotate-pi] {result['successor_session']} active "
          f"gen {result['successor_generation']} (verified={result['verified']})")
    return 0 if result.get("verified") else 2


def _sole_repo_target(project):
    from _helpers import get_project

    repos = (get_project(project) or {}).get("repos") or {}
    return next(iter(repos)) if len(repos) == 1 else None


def add_start_pi_arguments(p) -> None:
    p.add_argument("--pi-web-url", default=DEFAULT_PI_WEB_URL, help="Loopback Pi Web manager URL")
    p.add_argument("--agent", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--chat", required=True)
    p.add_argument("--repo-target", default=None, help="Defaults to the project's sole repository")
    # All-or-none disambiguation / first-profile override; normal path derives them.
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--thinking", default=None)
    p.add_argument("--runtime-home", default=None,
                   help="Required with provider/model/thinking when no prior project profile exists")
    p.add_argument("--bootstrap-timeout", type=float, default=180.0)
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.add_argument("--json", dest="json_output", action="store_true")


def run_start_pi(cfg) -> int:
    try:
        require_loopback(cfg.pi_web_url)
        if not cfg.repo_target:
            cfg.repo_target = _sole_repo_target(cfg.project)
            if not cfg.repo_target:
                raise RotateError("project has zero or multiple repositories; pass --repo-target")
        result = start_pi(
            cfg, piweb=PiWeb(cfg.pi_web_url), run_autobridge=_default_run_autobridge,
            event_path_for=_default_event_path, resolve_cwd=_default_resolve_cwd,
            resolve_profile=resolve_pi_profile,
        )
    except RotateError as exc:
        print(f"[start-pi] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result) if cfg.json_output else
          f"[start-pi] {result['session']} active gen {result['generation']} "
          f"({result['profile']['provider']}/{result['profile']['model']}, verified={result['verified']})")
    return 0 if result.get("verified") else 2


def add_start_livecraft_pi_arguments(p) -> None:
    p.add_argument("--livecraft-backend-url", default=DEFAULT_LIVECRAFT_BACKEND_URL,
                   help="Loopback Livecraft backend URL")
    p.add_argument("--agent", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--chat", required=True, help="Canonical CHAT-... id")
    p.add_argument("--repo-target", required=True)
    p.add_argument("--provider", default=None,
                   help="Optional first-profile provider override; normally read from the stored Pi profile")
    p.add_argument("--model", default=None,
                   help="Optional first-profile model override; normally read from the stored Pi profile")
    p.add_argument("--thinking", default=None,
                   help="Optional first-profile thinking override; normally read from the stored Pi profile")
    p.add_argument("--runtime-home", default=None,
                   help="Optional first-profile runtime home; normally read from the stored Pi profile")
    p.add_argument("--starter-agent", default="claude",
                   help="Agent that created this worker session and receives the bootstrap handshake")
    p.add_argument("--starter-session-id", default=None,
                   help="Native runtime session id of --starter-agent")
    p.add_argument("--supersedes-session", default=None,
                   help="Existing logical worker session to replace when rebinding a stale worker")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--production", dest="production", action="store_true",
                      help="Explicitly select the persistent worker path (the default)")
    mode.add_argument("--disposable", dest="production", action="store_false",
                      help="Use the existing gated disposable pilot path")
    p.set_defaults(production=True, disposable=False)
    p.add_argument("--pilot-scope", default=None, help="Exact <project>/<agent> pilot scope")
    p.add_argument("--bootstrap-timeout", type=float, default=180.0)
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.add_argument("--json", dest="json_output", action="store_true")


def run_start_livecraft_pi(cfg) -> int:
    try:
        require_loopback(cfg.livecraft_backend_url, "--livecraft-backend-url")
        result = start_livecraft(
            cfg, livecraft=Livecraft(cfg.livecraft_backend_url),
            run_autobridge=_default_run_autobridge, resolve_cwd=_default_resolve_cwd,
        )
    except RotateError as exc:
        print(f"[start-livecraft-pi] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result) if cfg.json_output else
          f"[start-livecraft-pi] {result['session']} active gen {result['generation']} "
          f"(verified={result['verified']})")
    return 0 if result.get("verified") else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="worker rotate-pi", description=__doc__)
    add_rotate_pi_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
