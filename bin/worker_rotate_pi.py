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
# Derived from the running installation, not one workstation: the monitor shell
# needs the same interpreter/inbox this rotation runs under.
MONITOR_PYTHON = str(Path(sys.executable).resolve())
INBOX = str(_BIN / "inbox.py")

BOOTSTRAP_TEMPLATE = """From Claude, worker provisioning only. You are {agent} in fresh Pi native session {native}. Do not start project work. Start exactly one persistent pi-event-monitor now.

Description: llm-collab {agent} inbox {logical}
Command: tail -n 0 -F '{event_path}' | jq --unbuffered -c 'select(.event == "pi_inbox_wake")'
Instruction: On each event, run exactly: LLM_COLLAB_READER_RUNTIME_ID={native} {py} '{inbox}' --me {agent} --session {logical} --project {project} --chat {chat} --repo-target {repo} --acknowledge. Then summarize each durable packet and follow it. Do not do other work.

After monitor_start succeeds, reply only {marker}"""


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
    proc = subprocess.run(
        [sys.executable, str(AUTOBRIDGE), *args], capture_output=True, text=True
    )
    return proc.returncode, proc.stdout


def _default_event_path(logical_id: str) -> str:
    from _session_autobridge import autobridge_event_log_path

    return str(autobridge_event_log_path(logical_id))


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


def rotate(
    cfg,
    *,
    piweb,
    run_autobridge,
    event_path_for,
    resolve_cwd,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict:
    owned = resolve_predecessor(
        run_autobridge, cfg.agent, cfg.project, cfg.chat, cfg.repo_target, cfg.supersedes_session
    )
    repo_cwd = resolve_cwd(cfg.project, cfg.repo_target)
    if not repo_cwd:
        raise RotateError(f"no repo path for project {cfg.project} repo {cfg.repo_target}")

    tab = piweb.create_tab(repo_cwd, f"{cfg.agent} llm-collab continuation")
    tab_id = tab["id"]
    try:
        if tab["cwd"] != repo_cwd:
            raise RotateError(f"tab cwd {tab['cwd']} != {repo_cwd}")
        piweb.set_model(tab_id, cfg.provider, cfg.model)
        piweb.set_thinking(tab_id, cfg.thinking)

        state = piweb.get_state(tab_id)
        if (state["provider"], state["model_id"], state["thinking"]) != (
            cfg.provider, cfg.model, cfg.thinking,
        ):
            raise RotateError(
                "native state mismatch: "
                f"{state['provider']}/{state['model_id']}/{state['thinking']} "
                f"!= {cfg.provider}/{cfg.model}/{cfg.thinking}"
            )

        native = state["native"]
        logical = successor_id(cfg.supersedes_session, native)
        marker = f"BOOTSTRAP_READY_{logical}"
        piweb.prompt(tab_id, BOOTSTRAP_TEMPLATE.format(
            agent=cfg.agent, native=native, logical=logical,
            event_path=event_path_for(logical), py=MONITOR_PYTHON, inbox=INBOX,
            project=cfg.project, chat=cfg.chat, repo=cfg.repo_target, marker=marker,
        ))
        _await_marker(
            piweb, tab_id, marker,
            timeout=cfg.bootstrap_timeout, interval=cfg.poll_interval, sleep=sleep, clock=clock,
        )
        # Fresh snapshot taken as close to register as possible; register pins its
        # own authoritative fingerprint read against this expected tuple, so the
        # config we prove and the config register persists are the same snapshot.
        final = piweb.get_state(tab_id)
        if final["native"] != native or (
            final["provider"], final["model_id"], final["thinking"]
        ) != (cfg.provider, cfg.model, cfg.thinking):
            raise RotateError(f"native state drifted before register: {final}")
    except Exception as exc:  # any pre-register failure must close the fresh tab
        piweb.close_tab(tab_id)  # nothing rebound yet; predecessor stays active
        if isinstance(exc, RotateError):
            raise
        raise RotateError(f"pre-register failure closed tab {tab_id}: {exc!r}") from exc

    # `active` sessions ignore the lease clock by design (validity follows the
    # native task, not a TTL) — registering parked would expire the successor.
    rc, out = run_autobridge([
        "register",
        "--session", logical,
        "--agent", cfg.agent,
        "--project", cfg.project,
        "--chat", cfg.chat,
        "--repo-target", cfg.repo_target,
        "--runtime-family", "pi",
        "--runtime-session-id", final["native"],
        "--runtime-session-source", final["session_file"],
        "--runtime-home", owned["runtime_home"],
        "--endpoint-id", owned["endpoint_id"],
        "--runtime-instance-id", tab_id,
        "--cwd", repo_cwd,
        "--status", "active",
        "--mode", owned["mode"],
        "--wake-strategy", owned["wake_strategy"],
        "--expect-pi-provider", final["provider"],
        "--expect-pi-model", final["model_id"],
        "--expect-pi-thinking", final["thinking"],
        "--supersedes-session", cfg.supersedes_session,
        "--json",
    ])
    if rc != 0:
        # After the register attempt we never reactivate the predecessor; report
        # the exact partial state instead of a rollback that would hide it.
        raise RotateError(
            f"register failed (rc={rc}); tab {tab_id} native {native} left as partial state: {out}"
        )

    proof = verify_postcondition(
        run_autobridge, cfg.agent, cfg.project, cfg.chat,
        cfg.supersedes_session, logical, owned["binding_generation"],
    )
    return {
        "successor_session": logical,
        "native_session_id": native,
        "tab_id": tab_id,
        "supersedes": cfg.supersedes_session,
        **proof,
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="worker rotate-pi", description=__doc__)
    add_rotate_pi_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
