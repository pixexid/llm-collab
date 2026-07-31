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
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PI_WEB_URL = "http://127.0.0.1:31415"
_BIN = Path(__file__).resolve().parent
AUTOBRIDGE = _BIN / "session_autobridge.py"
# The persistent monitor must reference the stable workspace root, not this
# script's checkout (a worktree/branch disappears; the monitor must survive it).
MONITOR_PYTHON = str(Path(sys.executable).resolve())
# start-pi owns the Pi Web transport; only these records seed a first-start profile.
PI_WEB_ENDPOINT = "endpoint_pi_web_local"


def _workspace_root() -> Path:
    from _helpers import config_get

    root, name = config_get("projects_root"), config_get("workspace_name")
    if not root or not name:
        raise RotateError("workspace root unresolved from config (projects_root/workspace_name)")
    return Path(root) / name


def _monitor_inbox() -> str:
    return str(_workspace_root() / "bin" / "inbox.py")


def _sessions_dir() -> Path:
    return _workspace_root() / "State" / "session_autobridge" / "sessions"

BOOTSTRAP_TEMPLATE = """From Claude, worker provisioning only. You are {agent} in fresh Pi native session {native}. Do not start project work. Start exactly one persistent monitor now with monitor_watch_path (NOT monitor_start — monitor_start needs an attended confirmation and will time out).

Watch this exact file: {event_path}
On each change to that file, run exactly: LLM_COLLAB_READER_RUNTIME_ID={native} {py} '{inbox}' --me {agent} --session {logical} --project {project} --chat {chat} --repo-target {repo}
Then summarize each durable packet and follow it. Waking on an unrelated append is fine — the drain is idempotent. Do not do other work.

After the watcher is running, reply only {marker}"""


class RotateError(Exception):
    """A precondition or Pi Web step failed; no canonical rebind was performed."""


class PiWeb:
    """Thin stdlib client over Pi Web's loopback manager API."""

    def __init__(self, base_url: str, request=None):
        self.base = base_url.rstrip("/")
        self._request = request or self._http

    def _http(self, method: str, path: str, body):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)

    def create_tab(self, cwd: str, title: str) -> dict:
        status, body = self._request("POST", "/api/tabs", {"cwd": cwd, "title": title})
        if status != 201 or not body:
            raise RotateError(f"create tab failed (HTTP {status})")
        tab = body["data"]["tab"]
        return {"id": tab["id"], "cwd": tab["cwd"], "session_file": tab.get("sessionFile")}

    def set_model(self, tab_id: str, provider: str, model_id: str) -> None:
        status, body = self._request(
            "POST", "/api/model", {"tabId": tab_id, "provider": provider, "modelId": model_id}
        )
        if status != 200 or not (body and body.get("success")):
            raise RotateError("set_model refused")

    def set_thinking(self, tab_id: str, level: str) -> None:
        status, body = self._request("POST", "/api/thinking", {"tabId": tab_id, "level": level})
        if status != 200 or not (body and body.get("success") and body["data"].get("level") == level):
            raise RotateError("set_thinking refused")

    def get_state(self, tab_id: str) -> dict:
        status, body = self._request(
            "GET", "/api/state?tabId=" + urllib.parse.quote(tab_id), None
        )
        if status != 200 or not (body and body.get("success")):
            raise RotateError("get_state failed")
        data = body["data"]
        return {
            "native": data["sessionId"],
            "session_file": data["sessionFile"],
            "provider": data["model"]["provider"],
            "model_id": data["model"]["id"],
            "thinking": data["thinkingLevel"],
        }

    def prompt(self, tab_id: str, message: str) -> None:
        status, _ = self._request("POST", "/api/prompt", {"tabId": tab_id, "message": message})
        if status != 200:
            raise RotateError("prompt not accepted")

    def last_assistant_text(self, tab_id: str):
        status, body = self._request(
            "GET", "/api/last-assistant-text?tabId=" + urllib.parse.quote(tab_id), None
        )
        if status != 200 or not (body and body.get("success")):
            return None
        return body["data"].get("text")

    def close_tab(self, tab_id: str) -> None:
        try:
            self._request("DELETE", "/api/tabs/" + urllib.parse.quote(tab_id), None)
        except Exception:
            pass  # best-effort cleanup; the caller already has the real error


def require_loopback(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost",):
        return url
    try:
        if ipaddress.ip_address(host).is_loopback:
            return url
    except ValueError:
        pass
    raise RotateError(f"--pi-web-url must be loopback, got host {host!r}")


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
    return str(_workspace_root() / "State" / "session_autobridge" / "events" / f"{logical_id}.jsonl")


def _default_resolve_cwd(project: str, repo_target: str):
    from _helpers import resolve_project_repo_path

    path = resolve_project_repo_path(project, repo_target)
    return None if path is None else str(path)


def _load_json(run_autobridge, args: list[str], what: str) -> dict:
    rc, out = run_autobridge(args)
    if rc != 0 or not out.strip():
        raise RotateError(f"{what} failed (rc={rc})")
    return json.loads(out)


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


def resolve_pi_profile(agent: str, project: str, *, sessions_dir=None, override=None) -> dict:
    """Reduce the agent's prior Pi Web records FOR THIS PROJECT to one transport
    profile (Codex #271 ruling): eligible = endpoint_pi_web_local + exact
    project_id, wake forced to runtime_trigger, provider/model/thinking from the
    greatest canonical binding_generation (strict int) with a complete tuple.
    Zero eligible, conflicting home, or a greatest-generation tie fails closed
    `pi_profile_required`. An unreadable/oversized/corrupt candidate also fails
    closed — never skipped into an apparently-complete older profile."""
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
        if d.get("endpoint_id") != PI_WEB_ENDPOINT or not runtime.get("home"):
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
        raise RotateError(f"pi_profile_required: no eligible Pi Web records for {agent} in {project}")
    if len(homes) != 1:
        raise RotateError(f"pi_profile_required: conflicting runtime homes for {agent}: {sorted(homes)}")

    if override is not None:
        if not all(isinstance(v, str) and v for v in override):
            raise RotateError("pi_profile_required: override needs all of provider/model/thinking")
        provider, model, thinking = override
    else:
        complete = [r for r in records if all(r["tuple"])]
        if not complete:
            raise RotateError(f"pi_profile_required: no complete fingerprint for {agent}")
        top_gen = max(r["gen"] for r in complete)
        distinct = {r["tuple"] for r in complete if r["gen"] == top_gen}
        if len(distinct) != 1:
            raise RotateError(
                f"pi_profile_required: generation {top_gen} has {len(distinct)} conflicting tuples for {agent}"
            )
        provider, model, thinking = distinct.pop()

    return {
        "endpoint_id": PI_WEB_ENDPOINT,
        "runtime_home": homes.pop(),
        "wake_strategy": "runtime_trigger",
        "provider": provider,
        "model": model,
        "thinking": thinking,
        "display": display or agent.upper(),
    }


def _await_marker(piweb, tab_id, marker, *, timeout, interval, sleep, clock) -> None:
    deadline = clock() + timeout
    while clock() < deadline:
        if piweb.last_assistant_text(tab_id) == marker:  # exact match only
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
        wake_strategy=owned["wake_strategy"], mode=owned["mode"],
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
    override = None
    if any((cfg.provider, cfg.model, cfg.thinking)):
        override = (cfg.provider, cfg.model, cfg.thinking)
    profile = resolve_profile(cfg.agent, cfg.project, override=override)
    repo_cwd = resolve_cwd(cfg.project, cfg.repo_target)
    if not repo_cwd:
        raise RotateError(f"no repo path for project {cfg.project} repo {cfg.repo_target}")
    suffix = cfg.chat[len("CHAT-"):] if cfg.chat.startswith("CHAT-") else cfg.chat
    r = _provision_and_bind(
        agent=cfg.agent, project=cfg.project, chat=cfg.chat, repo_target=cfg.repo_target,
        repo_cwd=repo_cwd, provider=profile["provider"], model=profile["model"], thinking=profile["thinking"],
        endpoint_id=profile["endpoint_id"], runtime_home=profile["runtime_home"],
        wake_strategy=profile["wake_strategy"], mode="manual",
        make_logical=lambda native: f"SESSION-PIWEB-{profile['display']}-{suffix}-{native[:8].upper()}",
        supersedes=None, title=f"{cfg.agent} llm-collab start",
        piweb=piweb, run_autobridge=run_autobridge, event_path_for=event_path_for,
        bootstrap_timeout=cfg.bootstrap_timeout, poll_interval=cfg.poll_interval, sleep=sleep, clock=clock,
        prepare_event=prepare_event,
    )
    binding = _load_json(
        run_autobridge,
        ["show-binding", "--project", cfg.project, "--chat", cfg.chat, "--agent", cfg.agent, "--json"],
        "start-pi postcondition",
    )
    verified = binding.get("session_id") == r["logical"] and binding.get("status") == "active"
    return {
        "session": r["logical"], "native_session_id": r["native"], "tab_id": r["tab_id"],
        "profile": {k: profile[k] for k in ("provider", "model", "thinking")},
        "verified": verified, "generation": binding.get("binding_generation"),
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="worker rotate-pi", description=__doc__)
    add_rotate_pi_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
