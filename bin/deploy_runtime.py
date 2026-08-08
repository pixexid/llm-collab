#!/usr/bin/env python3
"""Refresh the isolated deployed runtime without touching a source checkout."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = Path(
    os.environ.get(
        "LLM_COLLAB_RUNTIME_ROOT",
        Path.home() / ".local" / "share" / "llm-collab" / "runtime" / "main",
    )
).expanduser()
CONTRACT_MARKER = re.compile(r"CONTRACT_VERSION:\s*(\S+)")
DEFAULT_PM2_TIMEOUT_SECONDS = 15
PM2_JLIST_MAX_BYTES = 16 * 1024 * 1024
PM2_JLIST_MAX_RECORDS = 10_000
PM2_LIVE_STATUSES = frozenset({"online", "launching", "stopping", "waiting restart"})
PM2_READINESS_TIMEOUT_SECONDS = 10.0
PM2_READINESS_POLL_SECONDS = 0.25


class DeployError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise DeployError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def contract_version(text: str) -> str:
    match = CONTRACT_MARKER.search(text[:200])
    if not match:
        raise DeployError("AGENTS.md has no CONTRACT_VERSION marker")
    return match.group(1)


def pm2_binary() -> str:
    configured = os.environ.get("LLM_COLLAB_PM2_BIN")
    binary = configured or shutil.which("pm2")
    if not binary:
        raise DeployError("pm2 not found; install PM2 before deploying the persistent watchers")
    return binary


def pm2_timeout_seconds() -> int:
    raw = os.environ.get("LLM_COLLAB_PM2_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_PM2_TIMEOUT_SECONDS
    try:
        return max(1, int(raw))
    except ValueError:
        raise DeployError(
            f"invalid LLM_COLLAB_PM2_TIMEOUT_SECONDS={raw!r}"
        ) from None


def _pm2_run_bounded(args: list[str], max_output_bytes: int) -> subprocess.CompletedProcess[str]:
    command = [pm2_binary(), *args]
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stream = process.stdout
        if stream is None:
            raise DeployError(f"pm2 {' '.join(args)} returned no stdout pipe")
        output = bytearray()
        deadline = time.monotonic() + pm2_timeout_seconds()
        with selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, pm2_timeout_seconds())
                if not selector.select(remaining):
                    raise subprocess.TimeoutExpired(command, pm2_timeout_seconds())
                chunk = os.read(stream.fileno(), min(64 * 1024, max_output_bytes + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_output_bytes:
                    raise DeployError(
                        f"pm2 {' '.join(args)} output exceeds {max_output_bytes} bytes"
                    )
            returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return subprocess.CompletedProcess(
            command,
            returncode,
            bytes(output).decode(errors="replace"),
            "",
        )
    except DeployError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise DeployError(f"pm2 {' '.join(args)} failed: {error}") from error
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if process is not None and process.stdout is not None:
            process.stdout.close()


def pm2_run(
    args: list[str], *, max_output_bytes: int | None = None
) -> subprocess.CompletedProcess[str]:
    if max_output_bytes is not None:
        result = _pm2_run_bounded(args, max_output_bytes)
    else:
        try:
            result = subprocess.run(
                [pm2_binary(), *args],
                capture_output=True,
                text=True,
                timeout=pm2_timeout_seconds(),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DeployError(f"pm2 {' '.join(args)} failed: {error}") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise DeployError(f"pm2 {' '.join(args)} failed: {detail}")
    return result


def pm2_jlist() -> list[dict]:
    result = pm2_run(["jlist"], max_output_bytes=PM2_JLIST_MAX_BYTES)
    if len(result.stdout.encode("utf-8")) > PM2_JLIST_MAX_BYTES:
        raise DeployError(
            f"pm2 jlist output exceeds {PM2_JLIST_MAX_BYTES} bytes; refusing to parse it"
        )
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DeployError(f"pm2 jlist returned invalid JSON: {error}") from error
    if not isinstance(records, list) or any(
        not isinstance(record, dict) or not isinstance(record.get("name"), str)
        for record in records
    ):
        raise DeployError("pm2 jlist returned a malformed process list")
    if len(records) > PM2_JLIST_MAX_RECORDS:
        raise DeployError(
            f"pm2 jlist returned more than {PM2_JLIST_MAX_RECORDS} processes"
        )
    return records


def managed_processes(records: list[dict], owned_names: frozenset[str]) -> dict[str, dict]:
    managed: dict[str, dict] = {}
    for record in records:
        name = record["name"]
        if name not in owned_names:
            continue
        if name in managed:
            raise DeployError(f"pm2 jlist contains duplicate managed process {name!r}")
        managed[name] = record
    return managed


def process_status(record: dict) -> str:
    environment = record.get("pm2_env")
    if not isinstance(environment, dict) or not isinstance(environment.get("status"), str):
        raise DeployError(f"pm2 process {record['name']!r} has no usable status")
    return environment["status"]


def fence_watchers(owned_names: frozenset[str]) -> tuple[str, ...]:
    before = managed_processes(pm2_jlist(), owned_names)
    for name in sorted(before):
        pm2_run(["stop", name])
    after = managed_processes(pm2_jlist(), owned_names)
    live = sorted(name for name, record in after.items() if process_status(record) in PM2_LIVE_STATUSES)
    if live:
        raise DeployError("persistent watchers remained live after fence: " + ", ".join(live))
    return tuple(sorted(before))


def ecosystem_definitions(target: Path) -> dict[str, dict]:
    ecosystem = target / "pm2" / "ecosystem.config.cjs"
    if not ecosystem.is_file():
        raise DeployError(f"PM2 ecosystem config is missing: {ecosystem}")
    node = os.environ.get("LLM_COLLAB_NODE_BIN") or shutil.which("node")
    if not node:
        raise DeployError("node not found; cannot inspect the PM2 ecosystem")
    script = """
const config = require(process.argv[1]);
const apps = Array.isArray(config.apps) ? config.apps : null;
if (!apps || apps.some((app) => !app || typeof app.name !== "string" ||
    typeof app.cwd !== "string" || typeof app.script !== "string" ||
    !Array.isArray(app.args))) process.exit(2);
process.stdout.write(JSON.stringify(apps.map((app) => ({
  name: app.name, cwd: app.cwd, script: app.script, args: app.args
}))));
"""
    try:
        result = subprocess.run(
            [node, "-e", script, str(ecosystem)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DeployError(f"cannot inspect PM2 ecosystem {ecosystem}: {error}") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise DeployError(f"cannot inspect PM2 ecosystem {ecosystem}: {detail}")
    try:
        apps = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DeployError(f"PM2 ecosystem inspection returned invalid JSON: {error}") from error
    if not isinstance(apps, list):
        raise DeployError("PM2 ecosystem inspection did not return an app list")
    definitions: dict[str, dict] = {}
    for app in apps:
        if (
            not isinstance(app, dict)
            or not isinstance(app.get("name"), str)
            or not isinstance(app.get("cwd"), str)
            or not isinstance(app.get("script"), str)
            or not isinstance(app.get("args"), list)
            or any(not isinstance(arg, (str, int, float)) for arg in app["args"])
        ):
            raise DeployError("PM2 ecosystem contains an invalid app definition")
        name = app["name"]
        if name in definitions:
            raise DeployError(f"PM2 ecosystem contains duplicate app {name!r}")
        definitions[name] = {
            "name": name,
            "cwd": str(Path(app["cwd"]).resolve()),
            "script": app["script"],
            "args": [str(arg) for arg in app["args"]],
        }
    return definitions


def reconcile_pm2(
    target: Path,
    owned_names: frozenset[str],
    definitions: dict[str, dict],
) -> None:
    desired = set(definitions)
    if not desired.issubset(owned_names):
        raise DeployError("PM2 ecosystem contains an app outside the owned process set")
    current = managed_processes(pm2_jlist(), owned_names)
    for name in sorted(set(current) - desired):
        pm2_run(["delete", name])
    remaining = managed_processes(pm2_jlist(), owned_names)
    omitted = sorted(set(remaining) - desired)
    if omitted:
        raise DeployError("omitted PM2 processes remained after delete: " + ", ".join(omitted))
    pm2_run(["startOrRestart", str(target / "pm2" / "ecosystem.config.cjs"), "--update-env"])


PM2_TERMINAL_FAILURE_STATUSES = frozenset({"errored"})


def _process_is_populated(record: dict) -> bool:
    environment = record.get("pm2_env")
    return (
        isinstance(environment, dict)
        and isinstance(environment.get("status"), str)
        and all(environment.get(field) is not None for field in ("pm_cwd", "script", "args"))
    )


def _process_is_ready(record: dict) -> bool:
    environment = record.get("pm2_env")
    return _process_is_populated(record) and environment["status"] == "online"


def _process_is_terminal_failure(record: dict) -> bool:
    environment = record.get("pm2_env")
    return _process_is_populated(record) and environment["status"] in PM2_TERMINAL_FAILURE_STATUSES


def wait_for_managed_readiness(
    owned_names: frozenset[str],
    definitions: dict[str, dict],
) -> dict[str, dict]:
    """Wait for declared PM2 processes to be online with populated metadata."""
    deadline = time.monotonic() + PM2_READINESS_TIMEOUT_SECONDS
    expected = set(definitions)
    while True:
        records = pm2_jlist()
        actual = managed_processes(records, owned_names)
        if any(
            _process_is_terminal_failure(actual[name])
            for name in expected.intersection(actual)
        ):
            return actual
        if expected.issubset(actual) and all(_process_is_ready(actual[name]) for name in expected):
            return actual
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            details = []
            missing = sorted(expected - set(actual))
            if missing:
                details.append(f"missing={missing!r}")
            not_ready = []
            for name in sorted(expected.intersection(actual)):
                environment = actual[name].get("pm2_env")
                if not isinstance(environment, dict):
                    not_ready.append(f"{name}: missing pm2_env")
                    continue
                status = environment.get("status")
                missing_fields = [
                    field
                    for field in ("pm_cwd", "script", "args")
                    if environment.get(field) is None
                ]
                if status != "online" or missing_fields:
                    detail = f"{name}: status={status!r}"
                    if missing_fields:
                        detail += f" missing={','.join(missing_fields)}"
                    not_ready.append(detail)
            if not_ready:
                details.append("not_ready=" + "; ".join(not_ready))
            suffix = f" ({', '.join(details)})" if details else ""
            raise DeployError(
                "PM2 managed process metadata did not become ready before the deadline"
                + suffix
            )
        time.sleep(min(PM2_READINESS_POLL_SECONDS, remaining))


def _record_status(record: dict) -> str | None:
    """The PM2 status of a record, or None when it is not usable.

    None (rather than raising) so the undeclared-watcher scan can skip records
    that carry no status instead of aborting a deploy over an unrelated process.
    """
    environment = record.get("pm2_env")
    if not isinstance(environment, dict):
        return None
    status = environment.get("status")
    return status if isinstance(status, str) else None


_PYTHON_INTERPRETER_RE = re.compile(r"^python(\d+(\.\d+)*)?$")
# Options that consume the following token as their value, enumerated from
# `python3.11 --help`. Short: -c cmd, -m mod, -W arg, -X opt. Long:
# --check-hash-based-pycs mode. Every other 3.11 option (-b -B -d -E -h -i -I -O
# -OO -P -q -s -S -u -v -V -x --help --version --help-env --help-xoptions
# --help-all) is a flag that consumes nothing. -c/-m terminate the option list
# and run a command string / module, so they execute NO file.
_PYTHON_SHORT_VALUE_OPTIONS = frozenset({"-c", "-m", "-W", "-X"})
_PYTHON_LONG_VALUE_OPTIONS = frozenset({"--check-hash-based-pycs"})
_PYTHON_NO_FILE_OPTIONS = frozenset({"-c", "-m"})


def _is_python_interpreter(exec_path: object) -> bool:
    """True when PM2's resolved ``pm_exec_path`` is a Python interpreter binary.

    The deployed watcher is a Python file, so only a Python interpreter executes
    it as a program. A bare binary (``cat``, ``grep``, ``node``) carrying the
    watcher path as an argument reads it as DATA, not a program. PM2's
    ``exec_interpreter`` cannot make this distinction: it is resolved from the
    script EXTENSION (``.py`` -> python) and is ``'none'`` for every bare-binary
    script -- so a ``python3`` script and a ``cat`` script both report
    ``exec_interpreter='none'``. The basename of PM2's own resolved
    ``pm_exec_path`` is the only discriminator, and Python's binary names are
    stable (``python``, ``python3``, ``python3.11``).
    """
    return isinstance(exec_path, str) and bool(
        _PYTHON_INTERPRETER_RE.match(os.path.basename(exec_path))
    )


def _python_program_operand(args: list[str]) -> str | None:
    """The file a Python interpreter runs, or None when it runs no file.

    Walks the interpreter argv per Python 3.11's option grammar, with every
    value-taking option enumerated from `python3.11 --help`, so an option with a
    SEPARATE value cannot masquerade as the program. `python3 --check-hash-based
    -pycs default <watcher>` is the shape that broke the short-only list: a LONG
    option whose value is the next token. Value options -c/-m run a command
    string / module (no file); -W/-X/--check-hash-based-pycs consume their value
    (separate, short-attached `-Wignore`, or long-`=` `--opt=value`) and continue
    to the program. `--` ends option processing; `-` is stdin. Returns the first
    program token, else None.

    Residual (stated, not silent): the value-option set is fixed to Python 3.11.
    Every 3.11 flag is self-consuming, so a flag is always safe; the only gap is
    a FUTURE Python adding a value-taking option, which would fail OPEN the same
    way --check-hash-based-pycs did. Re-derive from the installed interpreter if
    the runtime moves off 3.11.
    """
    index = 0
    count = len(args)
    while index < count:
        token = args[index]
        if token == "--":
            return args[index + 1] if index + 1 < count else None
        if token == "-":
            return None  # program is stdin
        if token.startswith("--"):
            name = token.split("=", 1)[0]
            if name in _PYTHON_LONG_VALUE_OPTIONS:
                index += 1 if "=" in token else 2  # --opt=value vs --opt value
                continue
            index += 1  # long flag (--help, --version, --help-all, ...)
            continue
        if token.startswith("-"):
            option = token[:2]
            if option in _PYTHON_SHORT_VALUE_OPTIONS:
                if option in _PYTHON_NO_FILE_OPTIONS:
                    return None  # -c cmd / -m mod run no file
                index += 1 if len(token) > 2 else 2  # -Wignore vs -W ignore
                continue
            index += 1  # short flag (-u, -O, -OO, -bb, ...)
            continue
        return token
    return None


def _token_resolves_to(token: str, cwd: str | None, resolved_watch_script: str) -> bool:
    """True when ``token`` refers to the runtime's watch script.

    Absolute tokens compare normalized; relative tokens resolve against the
    process ``pm_cwd`` first, which is exactly the GH-675 orphan shape (a process
    that ran ``bin/watch_inbox.py`` from ``cwd = <runtime>``). Symlinks are
    deliberately not resolved, matching the ecosystem config's convention.
    """
    candidate = Path(token)
    if not candidate.is_absolute():
        if cwd is None:
            return False
        candidate = Path(cwd) / token
    return os.path.normpath(str(candidate)) == resolved_watch_script


def _record_executes_watch_script(record: dict, resolved_watch_script: str) -> bool:
    """True when a LIVE process is executing the runtime's watch script.

    Two execution shapes count, derived from what PM2 reports rather than by
    scanning command tokens:

    * Direct execution -- PM2 resolved ``pm_exec_path`` (the file it runs) to the
      watch script. This is PM2's own resolved answer to "what file does this
      process execute", so it covers a bare-script watcher AND a ``.py`` PM2
      auto-interprets (``exec_interpreter`` set from the extension). No argv
      reasoning at all.

    * Interpreter-as-script -- PM2 ran ``<binary> <args>`` where the binary is a
      Python interpreter (``script: "python3"``; ``exec_interpreter`` is ``'none'``
      because PM2 never marks a bare-binary script as an interpreter) and the
      watch script is the interpreter's program operand. The operand is found with
      a Python-aware parse so interpreter options (``-W ignore``) and the ``--``
      terminator cannot hide or impersonate it. Scoped to Python because the
      watcher is a Python file: a non-interpreter binary (``cat``) carrying the
      path as its first argument reads it as data, not a program.

    Only a process in a live PM2 status can dispatch -- a stopped or errored entry
    has no live process and ``pm2 save`` resurrects it stopped, not live.
    """
    if _record_status(record) not in PM2_LIVE_STATUSES:
        return False
    environment = record["pm2_env"]
    cwd = environment.get("pm_cwd")
    cwd_text = cwd if isinstance(cwd, str) else None
    exec_path = environment.get("pm_exec_path")
    if isinstance(exec_path, str) and _token_resolves_to(
        exec_path, cwd_text, resolved_watch_script
    ):
        return True
    if not _is_python_interpreter(exec_path):
        return False
    raw_args = environment.get("args")
    tokens = (
        [str(arg) for arg in raw_args if isinstance(arg, (str, int, float))]
        if isinstance(raw_args, list)
        else []
    )
    operand = _python_program_operand(tokens)
    return operand is not None and _token_resolves_to(operand, cwd_text, resolved_watch_script)


def refuse_undeclared_runtime_watchers(
    target: Path, declared_names: set[str], records: list[dict]
) -> None:
    """Refuse when a process the ecosystem does not declare runs the deployed
    runtime's own watcher — the converse of the declared-process checks.

    Without this, an undeclared PM2 entry executing ``<target>/bin/watch_inbox.py``
    is a live dispatcher on the same inbox and log surfaces while conformance
    reports green (GH-675). The predicate is the property — a process executing
    that script — never a name pattern, so unrelated PM2 entries on this host are
    not implicated and only THIS runtime's watcher is bound. Read-only: it inspects
    the snapshot, never stop/start/delete/restart.
    """
    resolved_watch_script = os.path.normpath(str(target / "bin" / "watch_inbox.py"))
    offenders = [
        record["name"]
        for record in records
        if isinstance(record.get("name"), str)
        and record["name"] not in declared_names
        and _record_executes_watch_script(record, resolved_watch_script)
    ]
    if offenders:
        raise DeployError(
            "undeclared PM2 process(es) are running the deployed runtime watcher "
            f"(bin/watch_inbox.py): {sorted(offenders)!r}; this is a live dispatcher "
            "outside the ecosystem, so conformance cannot be established until it is "
            "stopped or declared"
        )


def verify_deployment(
    target: Path,
    head: str,
    owned_names: frozenset[str],
    definitions: dict[str, dict],
) -> None:
    if git(target, "rev-parse", "HEAD") != head:
        raise DeployError(f"target head does not match deployed head {head}")
    if git(target, "status", "--porcelain=v1", "--untracked-files=no"):
        raise DeployError("deployed target has tracked changes after reset")
    actual = wait_for_managed_readiness(owned_names, definitions)
    expected = set(definitions)
    if set(actual) != expected:
        missing = sorted(expected - set(actual))
        extra = sorted(set(actual) - expected)
        raise DeployError(f"PM2 roster mismatch: missing={missing} extra={extra}")
    for name, definition in definitions.items():
        record = actual[name]
        environment = record.get("pm2_env")
        if not isinstance(environment, dict):
            raise DeployError(f"PM2 process {name!r} has no environment definition")
        if process_status(record) != "online":
            raise DeployError(f"PM2 process {name!r} is not online")
        if environment.get("pm_cwd") != definition["cwd"]:
            raise DeployError(
                f"PM2 process {name!r} cwd mismatch: "
                f"{environment.get('pm_cwd')!r} != {definition['cwd']!r}"
            )
        if environment.get("script") != definition["script"]:
            raise DeployError(
                f"PM2 process {name!r} script mismatch: "
                f"{environment.get('script')!r} != {definition['script']!r}"
            )
        if environment.get("args") != definition["args"]:
            raise DeployError(f"PM2 process {name!r} args do not match the current ecosystem")
        pm2_run(["logs", name, "--lines", "1", "--nostream"])


def target_preflight(target: Path, head: str) -> str:
    git(target, "fetch", "origin", "main", "--quiet")
    previous = git(target, "rev-parse", "HEAD")
    git(target, "cat-file", "-e", f"{head}^{{commit}}")
    if git(target, "status", "--porcelain=v1", "--untracked-files=no"):
        raise DeployError("target has tracked changes; refusing deployment")
    return previous


def source_head(source: Path) -> tuple[str, str]:
    git(source, "fetch", "origin", "main", "--quiet")
    origin_main = git(source, "rev-parse", "origin/main")
    head = git(source, "rev-parse", "HEAD")
    if head != origin_main:
        raise DeployError(
            f"source must be exact origin/main: origin/main={origin_main} HEAD={head}"
        )
    ancestry = subprocess.run(
        ["git", "-C", str(source), "merge-base", "--is-ancestor", "origin/main", "HEAD"],
        timeout=30,
    )
    if ancestry.returncode:
        raise DeployError(f"source is stale or unrelated: origin/main={origin_main} HEAD={head}")
    local_contract = contract_version((source / "AGENTS.md").read_text(encoding="utf-8"))
    origin_contract = contract_version(git(source, "show", "origin/main:AGENTS.md"))
    if local_contract != origin_contract:
        raise DeployError(
            f"contract mismatch: source={local_contract} origin/main={origin_contract}"
        )
    return head, local_contract


def reset_target(target: Path, head: str) -> None:
    previous = git(target, "rev-parse", "HEAD")
    if git(target, "status", "--porcelain=v1", "--untracked-files=no"):
        raise DeployError("target has tracked changes; refusing deployment")
    command = ["git", "-C", str(target), "reset", "--hard", head]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise DeployError(f"git reset --hard failed: {detail}")
    except (OSError, subprocess.SubprocessError, DeployError) as error:
        try:
            rollback = subprocess.run(
                ["git", "-C", str(target), "reset", "--hard", previous],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as rollback_error:
            raise DeployError(
                f"deployment failed and rollback failed: {rollback_error}"
            ) from error
        rollback_error = (
            rollback.stderr.strip()
            or rollback.stdout.strip()
            or f"exit {rollback.returncode}"
            if rollback.returncode
            else ""
        )
        if not rollback_error:
            try:
                if git(target, "rev-parse", "HEAD") != previous or git(
                    target, "status", "--porcelain=v1", "--untracked-files=no"
                ):
                    rollback_error = "rollback verification failed"
            except (OSError, subprocess.SubprocessError, DeployError) as verify_error:
                rollback_error = str(verify_error)
        if rollback_error:
            raise DeployError(f"deployment failed and rollback failed: {rollback_error}") from error
        raise DeployError(f"deployment failed; restored target HEAD {previous}: {error}") from error


def restore_previous_deployment(
    target: Path,
    previous: str,
    owned_names: frozenset[str],
) -> None:
    """Return the target and its persistent PM2 roster to the pre-deploy state."""
    fence_watchers(owned_names)
    reset_target(target, previous)
    definitions = ecosystem_definitions(target)
    reconcile_pm2(target, owned_names, definitions)
    verify_deployment(target, previous, owned_names, definitions)
    pm2_run(["save"])


def deploy(source: Path, target: Path | None = None) -> dict[str, str]:
    source = source.resolve()
    target = (DEFAULT_TARGET if target is None else target).resolve()
    if source == target:
        raise DeployError("source and deployed runtime must be different paths")
    if not (source / ".git").exists() or not (target / ".git").exists():
        raise DeployError("source and target must both be git worktrees")
    head, contract = source_head(source)
    previous = target_preflight(target, head)
    pm2_binary()
    # Parse the old ecosystem before fencing so a failure can be rejected without
    # taking the existing watcher roster down.
    previous_definitions = ecosystem_definitions(target)
    previous_owned_names = frozenset(previous_definitions)
    # Gate before the FIRST mutation (GH-679): an undeclared process executing
    # this runtime's watcher is a live dispatcher on the same inbox/log surfaces,
    # so conformance cannot be established while it runs. Detecting it only after
    # reset_target would mean code replacement happened under it, and rollback's
    # verify_deployment would hit the same offender and abort. Inspect the live
    # roster now and refuse without mutating anything, so there is nothing to roll
    # back for this condition. verify_deployment deliberately does NOT re-run this
    # check, which is what keeps rollback reachable when a deploy fails for
    # another reason.
    refuse_undeclared_runtime_watchers(target, set(previous_owned_names), pm2_jlist())
    try:
        fence_watchers(previous_owned_names)
        reset_target(target, head)
        definitions = ecosystem_definitions(target)
        owned_names = previous_owned_names | frozenset(definitions)
        reconcile_pm2(target, owned_names, definitions)
        verify_deployment(target, head, owned_names, definitions)
        # Recheck after the mutations (GH-679 TOCTOU): a watcher that started
        # AFTER the preflight snapshot -- while we fenced, reset, restarted, or
        # polled readiness -- is invisible to verify_deployment, which sees only
        # owned processes. Inspect the live roster again before pm2 save so a
        # mid-deploy shadow reaches rollback instead of being persisted into the
        # reboot dump. The preflight gate above stays: it is what prevents code
        # replacement under a live dispatcher.
        refuse_undeclared_runtime_watchers(target, set(owned_names), pm2_jlist())
        pm2_run(["save"])
    except (OSError, subprocess.SubprocessError, DeployError) as error:
        try:
            restore_previous_deployment(target, previous, previous_owned_names)
        except (OSError, subprocess.SubprocessError, DeployError) as recovery_error:
            raise DeployError(
                f"deployment failed: {error}; recovery failed: {recovery_error}"
            ) from error
        raise DeployError(f"deployment failed; restored target HEAD {previous}: {error}") from error
    return {
        "source": str(source),
        "target": str(target),
        "head": head,
        "previous_head": previous,
        "contract_version": contract,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        evidence = deploy(args.source)
    except (OSError, subprocess.SubprocessError, DeployError) as error:
        print(f"[runtime] REFUSED: {error}")
        return 1
    print(
        f"[runtime] deployed contract v{evidence['contract_version']} "
        f"HEAD {evidence['head']} target {evidence['target']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
