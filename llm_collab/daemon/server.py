"""Default-off same-user Unix control socket for the workspace ledger."""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import socket
import stat
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from llm_collab.daemon.gate import GateStatus, evaluate_observation_gate, read_exact_nofollow
from llm_collab.ledger import LedgerPaths, LedgerStore

REQUEST_LIMIT = 4 * 1024
RESPONSE_LIMIT = 64 * 1024
DEADLINE_SECONDS = 2
LOG_LIMIT = 10 * 1024 * 1024
OBSERVATION_STARTUP_GRACE_SECONDS = 0.5
INTEGRITY_REFRESH_SECONDS = 60
INTEGRITY_SHUTDOWN_JOIN_SECONDS = 0.25
INTEGRITY_ERROR_LIMIT = 256


class ProtocolError(ValueError):
    pass


def _resolve_authoritative_repo(
    store: LedgerStore,
    *,
    workspace_root: Path,
    project_id: str,
    session: Mapping[str, object],
    target: Mapping[str, object],
) -> tuple[str, str, str]:
    """Resolve and verify the target against the immutable project registry."""
    repo_id = target.get("repo_id")
    if not isinstance(repo_id, str) or not repo_id:
        raise ValueError("dispatch repo_id is invalid")
    repo_targets = session.get("repo_targets")
    if not isinstance(repo_targets, (list, tuple)) or repo_id not in repo_targets:
        raise ValueError("dispatch repo_id is not in the worker project scope")
    revision = store.current_registry_revision(workspace_id=store.paths.workspace_id)
    snapshot = store.get_project_snapshot(
        workspace_id=store.paths.workspace_id,
        project_id=project_id,
        registry_revision=revision,
    )
    if snapshot is None:
        raise ValueError("dispatch project is absent from the current registry")
    try:
        project = json.loads(snapshot["snapshot_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("dispatch project authority is malformed") from exc
    repos = project.get("repos") if isinstance(project, dict) else None
    raw_root = repos.get(repo_id) if isinstance(repos, dict) else None
    if not isinstance(raw_root, str) or not raw_root:
        raise ValueError("dispatch repo is absent from project authority")
    raw_path = Path(raw_root).expanduser()
    if raw_path.is_absolute():
        repo_root = raw_path.resolve()
    elif raw_path.parts and raw_path.parts[0] == "..":
        repo_root = (workspace_root / raw_path).resolve()
    else:
        try:
            config = json.loads(
                read_exact_nofollow(workspace_root / "collab.config.json")
                .decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("dispatch cannot resolve relative project repo authority") from exc
        projects_root = config.get("projects_root") if isinstance(config, dict) else None
        if not isinstance(projects_root, str) or not projects_root:
            raise ValueError("dispatch project authority lacks projects_root")
        repo_root = (Path(projects_root).expanduser() / raw_path).resolve()
    requested_root = target.get("repo_root")
    requested_cwd = target.get("cwd")
    if not isinstance(requested_root, str) or not isinstance(requested_cwd, str):
        raise ValueError("dispatch target paths are invalid")
    if Path(requested_root).expanduser().resolve() != repo_root:
        raise ValueError("dispatch repo_root does not match project authority")
    cwd = Path(requested_cwd).expanduser().resolve()
    try:
        beneath = os.path.commonpath((str(repo_root), str(cwd))) == str(repo_root)
    except ValueError as exc:
        raise ValueError("dispatch cwd is not under project authority") from exc
    if not repo_root.is_dir() or not cwd.is_dir() or not beneath:
        raise ValueError("dispatch cwd is not under project authority")
    return repo_id, str(repo_root), str(cwd)


# The probe's COVERAGE, not a claim that verification happened. `ok` alone would
# overclaim: the reader's main database identity is compared against the writer's, but
# SQLite resolves the -wal and -shm sidecars by pathname, so an ancestor or sidecar
# replacement under the same uid is outside what this probe can see. Naming the scope
# lets a consumer size an `ok` correctly.
#
# Deliberately named probe_scope rather than verified_scope: the field is present in
# every state, including unknown, checking, gate_off and failures that occur before any
# identity comparison, where nothing has been verified at all. Calling it "verified"
# there would replace one overclaim with another.
PROBE_SCOPE = "main_database_identity"


def _integrity_snapshot(
    state: str,
    *,
    freshness: str = "unknown",
    checked_at_utc: str | None = None,
    age_seconds: int | None = None,
    error: str | None = None,
    error_truncated: bool = False,
) -> dict[str, object]:
    return {
        "state": state,
        "freshness": freshness,
        "checked_at_utc": checked_at_utc,
        "age_seconds": age_seconds,
        "error": error,
        "error_truncated": error_truncated,
        "probe_scope": PROBE_SCOPE,
    }


def _bounded_integrity_error_parts(error: object) -> tuple[str, bool]:
    text = str(error).replace("\x00", "")
    if len(text) > INTEGRITY_ERROR_LIMIT:
        return text[: INTEGRITY_ERROR_LIMIT - 3] + "...", True
    return text, False


def _bounded_integrity_error(error: object) -> str:
    return _bounded_integrity_error_parts(error)[0]


def _workspace_root_from_cwd() -> Path:
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "collab.config.json").is_file():
            return candidate
    return Path.cwd()


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON member")
        result[key] = value
    return result


def parse_request(payload: bytes) -> str:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
        raise ProtocolError("invalid control request") from exc
    if not isinstance(value, dict) or set(value) != {"version", "op"}:
        raise ProtocolError("control request must contain exactly version and op")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ProtocolError("unsupported control version")
    if not isinstance(value["op"], str) or value["op"] not in {"status", "logs", "shutdown"}:
        raise ProtocolError("unsupported control operation")
    return value["op"]


def parse_dispatch_request(payload: bytes) -> dict[str, object]:
    """Parse the closed daemon-owned worker dispatch envelope."""
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
        raise ProtocolError("invalid dispatch request") from exc
    if not isinstance(value, dict) or set(value) != {"version", "op", "request"}:
        raise ProtocolError("dispatch request must contain exactly version, op and request")
    if type(value["version"]) is not int or value["version"] != 1 or value["op"] != "dispatch":
        raise ProtocolError("unsupported dispatch request")
    request = value["request"]
    if not isinstance(request, dict) or set(request) != {
        "worker_id", "project_id", "session", "message", "endpoint", "target",
        "correlation_id", "observed_at_utc", "timeout_seconds", "model",
    }:
        raise ProtocolError("dispatch request has an invalid shape")
    for key in ("worker_id", "project_id", "correlation_id", "observed_at_utc"):
        if not isinstance(request[key], str) or not request[key] or len(request[key]) > 512:
            raise ProtocolError(f"dispatch request {key} is invalid")
    if not isinstance(request["session"], dict):
        raise ProtocolError("dispatch request session is invalid")
    message = request["message"]
    if not isinstance(message, dict) or set(message) != {"path"}:
        raise ProtocolError("dispatch request message must contain only path")
    if not isinstance(message["path"], str) or not message["path"] or len(message["path"]) > 512:
        raise ProtocolError("dispatch request message path is invalid")
    endpoint = request["endpoint"]
    if not isinstance(endpoint, dict) or set(endpoint) != {"url", "token"}:
        raise ProtocolError("dispatch request endpoint is invalid")
    if not isinstance(endpoint["url"], str) or not endpoint["url"] or len(endpoint["url"]) > 2048:
        raise ProtocolError("dispatch request endpoint url is invalid")
    if endpoint["token"] is not None and (
        not isinstance(endpoint["token"], str) or len(endpoint["token"]) > 8192
    ):
        raise ProtocolError("dispatch request endpoint token is invalid")
    target = request["target"]
    if not isinstance(target, dict) or set(target) != {
        "codex_home", "repo_id", "repo_root", "cwd", "user_agent_prefix"
    }:
        raise ProtocolError("dispatch request target is invalid")
    for key in ("codex_home", "repo_id", "repo_root", "cwd", "user_agent_prefix"):
        if not isinstance(target[key], str) or not target[key] or len(target[key]) > 4096:
            raise ProtocolError(f"dispatch request target {key} is invalid")
    timeout = request["timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > 180
    ):
        raise ProtocolError("dispatch request timeout_seconds is invalid")
    if request["model"] is not None and (
        not isinstance(request["model"], str) or not request["model"] or len(request["model"]) > 512
    ):
        raise ProtocolError("dispatch request model is invalid")
    return request


def peer_uid(connection: socket.socket, *, platform: str | None = None) -> int:
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), 12)
            return struct.unpack("3i", credentials)[1]
        except (AttributeError, OSError, struct.error) as exc:
            raise PermissionError("SO_PEERCRED peer proof unavailable") from exc
    if platform == "darwin":
        getter = getattr(connection, "getpeereid", None)
        if getter is not None:
            try:
                uid, _gid = getter()
                return uid
            except OSError as exc:
                raise PermissionError("getpeereid peer proof unavailable") from exc
        try:
            library = ctypes.CDLL(None, use_errno=True)
            uid = ctypes.c_uint()
            gid = ctypes.c_uint()
            if library.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
                return uid.value
            raise OSError(ctypes.get_errno(), "getpeereid failed")
        except (AttributeError, OSError):
            pass
        try:
            credential = connection.getsockopt(
                getattr(socket, "SOL_LOCAL"), getattr(socket, "LOCAL_PEERCRED"), 12
            )
            return struct.unpack("=I I", credential[:8])[1]
        except (AttributeError, OSError, struct.error) as exc:
            raise PermissionError("Darwin LOCAL_PEERCRED peer proof unavailable") from exc
    raise PermissionError(f"peer credential proof unsupported on {platform}")


def _identity(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f"refusing non-socket control path: {path}")
    return info.st_dev, info.st_ino


class DaemonServer:
    """Own the ledger and the sole default-off worker dispatch boundary."""

    def __init__(
        self,
        paths: LedgerPaths,
        *,
        owner_uid: int | None = None,
        peer_uid_getter: Callable[[socket.socket], int] = peer_uid,
        clock: Callable[[], float] = time.monotonic,
        workspace_root: Path | None = None,
        declaration_path: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self._peer_uid_getter = peer_uid_getter
        self._clock = clock
        self._stopping = False
        self._socket_identity: tuple[int, int] | None = None
        self.workspace_root = (
            _workspace_root_from_cwd() if workspace_root is None else workspace_root
        )
        self.declaration_path = (
            Path(__file__).parents[2]
            / "docs"
            / "protocols"
            / "standalone-v1-feature-declarations.json"
            if declaration_path is None
            else declaration_path
        )
        self._environment = environment
        self._gate_status: GateStatus | None = None
        self._observation: object | None = None
        self._observation_error: str | None = None
        self._store: LedgerStore | None = None
        self._integrity_lock = threading.Lock()
        self._integrity_snapshot = _integrity_snapshot("unknown")
        self._integrity_completed_monotonic: float | None = None
        self._integrity_stop: threading.Event | None = None
        self._integrity_thread: threading.Thread | None = None

    def run(self) -> None:
        self._gate_status = evaluate_observation_gate(
            self.declaration_path, environ=self._environment
        )
        if self._gate_status.effective:
            with LedgerStore.open_writer(self.paths) as store:
                self._serve(store)
            return
        self.paths.ensure_directories()
        writer_lock = LedgerStore._acquire_writer_lock(self.paths)
        try:
            self._serve(None)
        finally:
            writer_lock.close()

    def _serve(self, store: LedgerStore | None) -> None:
        if store is not None:
            if not store.owns_writer_lock:
                raise RuntimeError("daemon did not acquire the ledger writer lock")
        self._store = store
        listener: socket.socket | None = None
        try:
            # Establish the control surface before starting optional background
            # hints so every later setup failure shares the cleanup below.
            listener = self._open_listener()
            if store is not None:
                self._start_integrity_probe()
            if self._gate_status is not None and self._gate_status.effective:
                from llm_collab.daemon.observe import ObservationEngine

                self._observation = ObservationEngine(
                    workspace_root=self.workspace_root,
                    workspace_id=self.paths.workspace_id,
                    projects_path=self.workspace_root / "projects.json",
                    monotonic=self._clock,
                )
                try:
                    self._observation.start()
                except Exception as exc:
                    self._observation_error = (
                        f"watchdog unavailable: {type(exc).__name__}: {exc}"
                    )
            self._write_log({"event": "started"})
            initial_reconcile = True
            observation_not_before = self._clock() + OBSERVATION_STARTUP_GRACE_SECONDS
            while not self._stopping:
                try:
                    connection, _address = listener.accept()
                except socket.timeout:
                    pass
                else:
                    with connection:
                        self._handle(connection)
                if not self._stopping and self._clock() >= observation_not_before:
                    self._tick_observation(force=initial_reconcile)
                    initial_reconcile = False
        finally:
            if listener is not None:
                listener.close()
            self._remove_owned_socket()
            if self._observation is not None:
                self._observation.close()
                self._observation = None
            self._stop_integrity_probe()
            self._store = None
            self._write_log({"event": "stopped"})

    def _start_integrity_probe(self) -> None:
        self._stop_integrity_probe()
        stop = threading.Event()
        with self._integrity_lock:
            self._integrity_snapshot = _integrity_snapshot("checking")
            self._integrity_completed_monotonic = None
        self._integrity_stop = stop
        thread = threading.Thread(
            target=self._run_integrity_probe,
            args=(stop,),
            name="llm-collab-integrity-probe",
            daemon=True,
        )
        self._integrity_thread = thread
        thread.start()

    def _stop_integrity_probe(self) -> None:
        stop = self._integrity_stop
        thread = self._integrity_thread
        self._integrity_stop = None
        self._integrity_thread = None
        if stop is None or thread is None:
            return
        stop.set()
        if thread is not threading.current_thread():
            thread.join(timeout=INTEGRITY_SHUTDOWN_JOIN_SECONDS)

    def _run_integrity_probe(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                writer = self._store
                if writer is None:
                    raise RuntimeError("integrity probe has no writer")
                writer_identity = writer.database_identity
                with LedgerStore.open_reader(
                    self.paths,
                    validate_integrity=False,
                ) as reader:
                    if reader.database_identity != writer_identity:
                        raise RuntimeError(
                            "integrity probe opened a different ledger file than the writer"
                        )
                    result = reader.integrity_check()
                if result == "ok":
                    self._record_integrity_result("ok")
                else:
                    self._record_integrity_result("failed", error=result)
            except Exception as exc:
                self._record_integrity_result("failed", error=exc)
            if stop.wait(INTEGRITY_REFRESH_SECONDS):
                return

    def _record_integrity_result(self, state: str, *, error: object | None = None) -> None:
        checked_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        bounded_error = None
        error_truncated = False
        if error is not None:
            bounded_error, error_truncated = _bounded_integrity_error_parts(error)
        with self._integrity_lock:
            self._integrity_snapshot = _integrity_snapshot(
                state,
                freshness="current",
                checked_at_utc=checked_at_utc,
                age_seconds=0,
                error=bounded_error,
                error_truncated=error_truncated,
            )
            self._integrity_completed_monotonic = self._clock()

    def _integrity_status(self) -> dict[str, object]:
        with self._integrity_lock:
            result = dict(self._integrity_snapshot)
            completed = self._integrity_completed_monotonic
        if completed is None:
            return result
        age_seconds = max(0, int(self._clock() - completed))
        result["age_seconds"] = age_seconds
        result["freshness"] = (
            "current" if age_seconds <= INTEGRITY_REFRESH_SECONDS else "stale"
        )
        return result

    def _tick_observation(self, *, force: bool = False) -> None:
        if self._observation is None or self._store is None:
            return
        try:
            self._observation.reconcile_due(self._store, force=force)
        except Exception as exc:
            self._observation_error = f"{type(exc).__name__}: {exc}"
            self._write_log(
                {"event": "observation_error", "error": self._observation_error}
            )

    def _open_listener(self) -> socket.socket:
        self._recover_stale_socket()
        old_mask = os.umask(0o077)
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(os.fspath(self.paths.socket))
        finally:
            os.umask(old_mask)
        try:
            os.chmod(self.paths.socket, 0o600)
            self._socket_identity = _identity(self.paths.socket)
            listener.listen(8)
            listener.settimeout(0.1)
            return listener
        except BaseException:
            listener.close()
            self._remove_owned_socket()
            raise

    def _recover_stale_socket(self) -> None:
        try:
            first = _identity(self.paths.socket)
        except FileNotFoundError:
            return
        except RuntimeError:
            raise
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(DEADLINE_SECONDS)
            try:
                probe.connect(os.fspath(self.paths.socket))
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    raise RuntimeError("cannot prove control socket is non-listening") from exc
            else:
                raise RuntimeError(f"control socket is already listening: {self.paths.socket}")
        finally:
            probe.close()
        if _identity(self.paths.socket) != first:
            raise RuntimeError("control socket changed during stale recovery")
        os.unlink(self.paths.socket)

    def _remove_owned_socket(self) -> None:
        if self._socket_identity is None:
            return
        try:
            if _identity(self.paths.socket) == self._socket_identity:
                os.unlink(self.paths.socket)
        except (FileNotFoundError, RuntimeError):
            pass
        finally:
            self._socket_identity = None

    def _handle(self, connection: socket.socket) -> None:
        deadline = self._clock() + DEADLINE_SECONDS
        try:
            if self._peer_uid_getter(connection) != self.owner_uid:
                raise PermissionError("control peer UID mismatch")
            chunks: list[bytes] = []
            total = 0
            while True:
                self._set_remaining(connection, deadline)
                chunk = connection.recv(min(1024, REQUEST_LIMIT + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > REQUEST_LIMIT:
                    raise ProtocolError("control request exceeds 4096 bytes")
                chunks.append(chunk)
            if not chunks:
                raise ProtocolError("empty control request")
            payload = b"".join(chunks)
            try:
                op = parse_request(payload)
                request = None
            except ProtocolError:
                request = parse_dispatch_request(payload)
                op = "dispatch"
            if op == "status":
                response: object = self._status_response()
            elif op == "logs":
                response = {"version": 1, "logs": self._read_logs()}
            elif op == "dispatch":
                response = {"version": 1, **self._dispatch(request)}
            else:
                self._stopping = True
                response = {"version": 1, "stopping": True}
            self._send(connection, response)
        except (OSError, PermissionError, ProtocolError) as exc:
            self._send(connection, {"version": 1, "error": str(exc)})

    def _dispatch(self, request: Mapping[str, object] | None) -> dict[str, object]:
        if request is None:
            raise ProtocolError("dispatch request is missing")
        gate = self._gate_status
        if gate is None or not gate.dispatch_effective:
            return {
                "outcome": "gate_disabled",
                "dispatch_enabled": False,
                "transport_connected": False,
            }
        store = self._store
        if store is None:
            return {"outcome": "gate_disabled", "reason": "ledger_not_opened"}
        try:
            # ponytail: keep the first daemon-owned slice on the writer thread;
            # add a fenced worker pool only with the lease/reconcile slice.
            from llm_collab.canonical.codex_delivery import (
                deliver_worker_turn,
                resolve_worker_delivery_context,
            )
            from llm_collab.codex_app_server_live_probe import (
                _WebSocketJsonRpcTransport,
                observe_exact_thread,
                probe_exact_thread,
            )
            from llm_collab.codex_runtime_home import bind_runtime_home
            from llm_collab.session_lifecycle import (
                CodexLifecycleProvider,
                TrustedProjectRoot,
            )

            project_id = str(request["project_id"])
            session = request["session"]
            if not isinstance(session, Mapping):
                raise ValueError("dispatch session is not an object")
            context = resolve_worker_delivery_context(
                worker_id=str(request["worker_id"]),
                project_id=project_id,
                workspace_id=self.paths.workspace_id,
                session=session,
            )
            target = request["target"]
            endpoint = request["endpoint"]
            if not isinstance(target, Mapping) or not isinstance(endpoint, Mapping):
                raise ValueError("dispatch endpoint/target is not an object")
            if str(target["codex_home"]) != context.runtime_home_source:
                raise ValueError("dispatch CODEX_HOME does not match the selected session")
            repo_id, repo_root, cwd = _resolve_authoritative_repo(
                store,
                workspace_root=self.workspace_root,
                project_id=project_id,
                session=session,
                target=target,
            )
            runtime_home = bind_runtime_home(target["codex_home"])
            trusted_root = TrustedProjectRoot(
                project_id,
                repo_id,
                repo_root,
                cwd,
            )
            endpoint_url = str(endpoint["url"])
            token = endpoint["token"] if isinstance(endpoint["token"], str) else None
            prefix = str(target["user_agent_prefix"])
            provider = CodexLifecycleProvider(
                exact_thread_probe=lambda thread_id: probe_exact_thread(
                    thread_id,
                    endpoint_url=endpoint_url,
                    token=token,
                    timeout_seconds=5,
                )
            )
            return deliver_worker_turn(
                store,
                workspace_root=self.workspace_root,
                context=context,
                message=request["message"],
                provider=provider,
                runtime_home=runtime_home,
                trusted_project_root=trusted_root,
                observed_at_utc=str(request["observed_at_utc"]),
                correlation_id=str(request["correlation_id"]),
                dispatch_enabled=True,
                make_observe=lambda: lambda thread_id: observe_exact_thread(
                    thread_id,
                    expected_runtime_home=runtime_home.runtime_home_realpath,
                    supported_user_agent_prefixes=(prefix,),
                    endpoint_url=endpoint_url,
                    token=token,
                    timeout_seconds=5,
                ),
                make_transport=lambda: _WebSocketJsonRpcTransport(
                    endpoint_url, timeout_seconds=float(request["timeout_seconds"]), token=token
                ).__enter__(),
                model=request["model"] if isinstance(request["model"], str) else None,
                timeout_seconds=float(request["timeout_seconds"]),
            )
        except Exception as exc:
            return {
                "outcome": "dispatch_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "transport_connected": False,
            }

    def _status_response(self) -> dict[str, object]:
        gate = None if self._gate_status is None else self._gate_status.as_dict()
        response: dict[str, object] = {
            "version": 1,
            "running": True,
            "pid": os.getpid(),
            "observation_gate": gate,
        }
        integrity: object | None = None
        if self._store is not None:
            integrity = self._integrity_status()
            response["ledger"] = {
                "schema_version": self._store.schema_version(),
                "integrity": integrity,
                "migration_state": "exact_latest",
            }
        else:
            response["ledger"] = {
                "state": "present_not_opened_gate_off" if self.paths.ledger.exists() else "absent",
                "schema_version": "not_checked_gate_off",
                "integrity": _integrity_snapshot("gate_off"),
                "migration_state": "not_checked_gate_off",
            }
        if self._observation is None:
            response["observation"] = {
                "state": "gated_off",
                "source_reachability": "not_checked",
            }
        else:
            response["observation"] = self._observation.diagnostics(
                self._store, integrity=integrity
            )
            if self._observation_error is not None:
                response["observation"]["error"] = self._observation_error
        return response

    def _send(self, connection: socket.socket, response: object) -> None:
        encoded = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) > RESPONSE_LIMIT:
            encoded = b'{"version":1,"error":"response exceeds 65536 bytes"}'
        try:
            self._set_remaining(connection, self._clock() + DEADLINE_SECONDS)
            connection.sendall(encoded)
        except OSError:
            pass

    def _set_remaining(self, connection: socket.socket, deadline: float) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("control I/O deadline exceeded")
        connection.settimeout(remaining)

    def _read_logs(self) -> list[str]:
        try:
            return self.paths.log.read_text(encoding="utf-8").splitlines()[-100:]
        except FileNotFoundError:
            return []

    def _write_log(self, event: dict[str, object]) -> None:
        self.paths.logs.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.paths.logs, 0o700)
        sanitized = {key: "[redacted]" if any(word in key.lower() for word in ("body", "secret", "token", "password", "payload")) else value for key, value in event.items()}
        encoded = (json.dumps(sanitized, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
        staged = any(
            self.paths.log.with_name(self.paths.log.name + f".{number}.new").exists()
            for number in range(1, 6)
        )
        if staged or self.paths.log.exists() and self.paths.log.stat().st_size + len(encoded) >= LOG_LIMIT:
            self._rotate_logs()
        fd = os.open(self.paths.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, encoded)
        finally:
            os.close(fd)

    def _rotate_logs(self) -> None:
        stages = {number: self.paths.log.with_name(self.paths.log.name + f".{number}.new") for number in range(1, 6)}
        if self.paths.log.exists():
            for source_number, stage_number in ((4, 5), (3, 4), (2, 3), (1, 2)):
                source = self.paths.log.with_name(self.paths.log.name + f".{source_number}")
                stage = stages[stage_number]
                if stage.exists() and source.exists():
                    raise RuntimeError("log rotation state is ambiguous")
                if not stage.exists() and source.exists():
                    os.replace(source, stage)
            if stages[1].exists():
                raise RuntimeError("log rotation state is ambiguous")
            os.replace(self.paths.log, stages[1])
        for number in range(1, 6):
            stage = stages[number]
            target = self.paths.log.with_name(self.paths.log.name + f".{number}")
            if stage.exists():
                if number != 5 and target.exists():
                    raise RuntimeError("log rotation state is ambiguous")
                os.replace(stage, target)
