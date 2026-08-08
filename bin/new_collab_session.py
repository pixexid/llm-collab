#!/usr/bin/env python3
"""
new_collab_session.py — Set up a new collaboration session deterministically.

"New collab session" means a fresh session for EVERY worker involved, not the
reuse of whatever binding happened to be lying around. This helper does the part
the initiator can do safely on its own, and hands every co-worker an exact
copy-paste setup prompt for the part only they can do — registering their OWN
native session. Nobody's native session id is ever guessed.

Freshness is a convention this helper asks each worker to follow, not something
enforced end to end: session_autobridge registration now REFUSES a registration
whose native session already backs a dispatchable lease (active, or the default
`parked` when unexpired) in another chat (GH-468), so a reused native id fails
closed at register time. The setup prompt still instructs each worker to start a
fresh native session — that keeps the flow clean and avoids the refusal.

It:
  1. Refuses if this checkout is behind origin/main (co-workers must run current
     code, not a parked/dirty operator checkout).
  2. Preflights the initiator's native session against existing dispatchable
     scopes, before any chat directory or file is written.
  3. Creates the chat.
  4. Establishes the initiator's transport first when dispatch requires one.
  5. Registers ONLY the initiator's own, explicitly-supplied native session.
  6. Prints the initiator's own pickup command, branched by its wake channel
     (every watcher-backed initiator arms an inbox watcher, Codex included) — do
     it, a packet you never see is a packet you never answer.
  7. Emits a per-co-worker setup prompt: the exact `session_autobridge register`
     plus the pickup command for that worker's real wake channel. Contract v12:
     routine exact-session dispatch is the wake for every watcher-backed worker,
     and AX is only the fallback deliver.py selects.

Usage:
  python bin/new_collab_session.py \
    --project llm-collab --title "Paseo Phase-1 conformance" \
    --me claude --my-runtime-session-id <native-id> --my-runtime-family claude_app \
    --with codex:codex_app --repo-target app
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse
import json
import shutil
import subprocess

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import ROOT, ensure_project, get_project, load_agents
from _ax_trust import has_ax_doorbell_capability


def _supports_native_registration(agent_id: str, activation: dict) -> bool:
    """Whether an agent can back an autonomously-registered NATIVE session (the
    only kind this helper sets up). True iff it is watcher-backed (cli_session with
    a watcher) or has a routine-doorbell AX app (Codex/ChatGPT per Contract v10).

    A human_relay has no native session and must be refused. `ax_attended_only`
    only disables the ROUTINE AX doorbell, not a native watcher — a watcher-backed
    agent that is also attended keeps a working native session (deliver.py gives
    the watcher precedence via is_watcher_only_target), so it must NOT be refused
    here; only the doorbell path honours the attended flag (has_ax_doorbell_
    capability already does). wake_channel() alone is too loose: it returns
    "ax_doorbell" for ANY non-empty ax_app before checking the activation type or
    the routine-doorbell profile allowlist, so it would accept a bogus AX identity;
    has_ax_doorbell_capability() applies the shared allowlist deliver.py uses.
    """
    activation = activation or {}
    if activation.get("type") == "human_relay":
        return False
    watcher_backed = (
        activation.get("type") == "cli_session" and activation.get("watcher_enabled")
    )
    doorbell = has_ax_doorbell_capability({"id": agent_id, "activation": activation})
    return bool(watcher_backed or doorbell)

BIN = Path(__file__).parent
DEFAULT_HOMES = {
    "claude_app": "~/.claude",
    "codex_app": "~/.codex",
    "gemini_cli": "~/.gemini",
}
# This helper only knows the discover-runtime families. Pi workers (glmpi/relay/
# kimi) must use `worker.py start-livecraft-pi`, and a human_relay (zcode) has no native
# session — routing them through discover+register would misbind, so they are
# refused rather than guessed.
SUPPORTED_FAMILIES = ("codex_app", "claude_app", "gemini_cli")


def _git(*args: str, timeout: float | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True,
        timeout=timeout,
    ).stdout.strip()


# The origin/main fetch is the only network op here; an unreachable or
# auth-blocked origin would otherwise hang setup before any chat is created.
FETCH_TIMEOUT_SECONDS = 20.0


def assert_current_checkout() -> None:
    """Fail closed if this checkout is not exact origin/main. A co-worker
    pointed at a stale checkout runs outdated code — the exact trap that made a
    'new' session pick up 100+ commits of old behaviour."""
    try:
        _git("fetch", "origin", "main", "--quiet", timeout=FETCH_TIMEOUT_SECONDS)
        origin_main = _git("rev-parse", "origin/main")
        head = _git("rev-parse", "HEAD")
        dirty = _git("status", "--porcelain")
    except subprocess.TimeoutExpired:
        # An unreachable/auth-blocked origin must fail closed as a currency
        # refusal, not hang setup before any chat is created.
        sys.exit(
            f"[error] git fetch origin main timed out after {FETCH_TIMEOUT_SECONDS}s; "
            "cannot verify this checkout is current. Run from the deployed runtime or "
            "a fresh origin/main worktree with a reachable origin."
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - env-dependent
        sys.exit(f"[error] could not compare against origin/main: {exc.stderr or exc}")
    if head != origin_main:
        behind = _git("rev-list", "--count", f"HEAD..{origin_main}") or "?"
        sys.exit(
            "[error] this checkout is not origin/main "
            f"(HEAD={head[:8]} origin/main={origin_main[:8]}, {behind} behind).\n"
            "        Run the helper from the deployed runtime or a fresh "
            "origin/main worktree, never a parked/dirty operator checkout."
        )
    # Same HEAD is not enough: a tracked edit or untracked replacement means the
    # tree is not the verified origin/main code, which is exactly the "dirty
    # operator checkout" the docstring/quickstart promise never to use.
    if dirty:
        sys.exit(
            "[error] working tree is dirty at origin/main "
            f"({len(dirty.splitlines())} change(s)); the new session must run "
            "the verified origin/main tree.\n        Use the deployed runtime or "
            "a clean fresh worktree, never a parked/dirty operator checkout."
        )


def agent_activation(agents: dict, agent_id: str) -> dict:
    for a in agents:
        if a.get("id") == agent_id:
            return a.get("activation", {}) or {}
    sys.exit(f"[error] agent '{agent_id}' is not in agents.json")


def wake_channel(activation: dict) -> str:
    """How this worker actually receives a packet.

    Contract v12: routine exact-session dispatch is the wake for every
    watcher-backed worker, Codex included, so `watcher_enabled` wins over
    `ax_app`. This ordering was inverted until 2026-08-06, on a premise about
    Codex pickup that the app-server delivery proof disproved. An `ax_app` now
    means only that AX is available as the fallback `deliver.py` selects, never
    that the worker lacks pickup."""
    if activation.get("type") == "cli_session" and activation.get("watcher_enabled"):
        return "watcher"
    if activation.get("ax_attended_only"):
        return "ax_attended"
    if activation.get("ax_app"):
        return "ax_doorbell"
    if activation.get("type") == "human_relay":
        return "relay"
    return "unknown"


def register_session(session, agent, project, chat, repo_target, family, rsid, home) -> None:
    cmd = [
        sys.executable, str(BIN / "session_autobridge.py"), "register",
        "--session", session, "--agent", agent, "--project", project,
        "--chat", chat, "--repo-target", repo_target,
        "--mode", "auto-read", "--status", "active",
        "--wake-strategy", "runtime_trigger",
        "--runtime-family", family, "--runtime-session-id", rsid,
        "--runtime-home", home, "--runtime-session-source", "first_read",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"registering {agent} session failed:\n{r.stderr or r.stdout}")


def ensure_codex_transport() -> None:
    r = subprocess.run(
        [sys.executable, str(BIN / "pm2_watchers.py"), "ensure",
         "--agent", "codex-appserver"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"ensuring Codex app-server transport failed:\n{r.stderr or r.stdout}"
        )


def preflight_starter_binding(*, agent: str, project: str, runtime_session_id: str,
                              runtime_family: str) -> None:
    """Refuse a new chat before creation when the starter native is already routed."""
    from session_autobridge import preflight_native_session_registration

    # The real chat id does not exist yet. The ownership guard only needs a
    # different scope, so use a non-persisted sentinel rather than creating a
    # directory just to discover the refusal.
    pending_chat = f"__pending-new-collab__{agent}__{runtime_session_id}"
    pending_session = f"__pending-new-collab__{agent}__{runtime_session_id}"
    try:
        preflight_native_session_registration(
            session_id=pending_session,
            project_id=project,
            chat_id=pending_chat,
            native_session_id=runtime_session_id,
            native_family=runtime_family,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        sys.exit(
            f"[error] starter native session cannot own a new chat before creation: {exc}"
        )


# Every generated command goes through the deployed launcher, which selects a
# Python 3.10+ — raw `python` is absent or is 3.9 on the supported macs.
LAUNCH = "<runtime_root>/bin/llm-collab"


def watch_cmd(agent, project, chat, session, repo_target, rsid, family) -> str:
    # No --skip-existing: on a fresh chat there is no legitimate backlog to
    # suppress, and skipping would drop a packet delivered in the window between
    # the binding going active and the watcher starting.
    #
    # Export the family alongside the id: an activation reader's native id IS this
    # worker's ordinary native, and native identity is (family, id), so the reader
    # must carry the real family (GH-468) rather than synthesize a placeholder.
    return (
        f"export LLM_COLLAB_READER_RUNTIME_ID={rsid}\n"
        f"export LLM_COLLAB_READER_RUNTIME_FAMILY={family}\n"
        f"{LAUNCH} watch_inbox.py \\\n"
        f"  --me {agent} --project {project} --chat {chat} \\\n"
        f"  --session {session} --repo-target {repo_target} --json"
    )


def needs_dispatch_wake(activation: dict) -> bool:
    """True when this worker's turn is STARTED by autobridge dispatch rather
    than by reading an announcement — today, a worker carrying an `ax_app`
    (Codex). Such a worker must run the agent-wide dispatching watcher, because
    an exact-session watcher never calls dispatch_autobridge."""
    return bool((activation or {}).get("ax_app"))


def pickup_block(channel, agent, project, chat, session, repo_target, rsid, family,
                 needs_dispatch: bool = False) -> list[str]:
    """How THIS agent picks up packets, keyed to its real wake channel. Every
    watcher-backed worker arms a persistent native watcher — Codex included,
    per contract v12; the polling block below is for a worker that genuinely has
    no watcher to arm.

    Two watcher shapes, and the difference is not cosmetic. An exact-session
    watcher (`--session`) OBSERVES: watch_inbox.py runs dispatch_autobridge only
    when `--session` is absent, so it announces `new_message` and stops. That is
    right for a worker that reads its own inbox on the announcement. A worker
    whose turn is STARTED by autobridge dispatch — one carrying an `ax_app`,
    i.e. Codex — needs the agent-wide dispatching watcher instead; give it the
    exact-session command and a bound packet would suppress AX while nothing
    ever wakes it."""
    if channel == "watcher" and needs_dispatch:
        return [
            "# Ensure the MANAGED dispatching watcher (one per agent,",
            "#    not per chat). Your turn is started by autobridge dispatch,",
            "#    and watch_inbox only dispatches when --session is absent —",
            "#    but a raw second poller alongside the PM2 one would",
            "#    double-dispatch: both read processed_messages before invoking",
            "#    the runtime and record after, so each can issue turn/start for",
            "#    the same unread packet. `ensure` is idempotent: it starts the",
            "#    singleton only if missing.",
            f"{LAUNCH} pm2_watchers.py ensure --agent {agent}",
        ]
    if channel == "watcher":
        return [
            "# Arm your own inbox watcher in a persistent Monitor:",
            watch_cmd(agent, project, chat, session, repo_target, rsid, family),
        ]
    if channel == "ax_doorbell":
        return [
            "# This activation has no watcher to arm. A bound session receives",
            "# autobridge dispatch; between turns, poll your inbox — senders",
            "# wake you with the AX doorbell deliver.py prints when you are",
            "# unbound:",
            f"{LAUNCH} inbox.py --me {agent} --project {project} --chat {chat} \\",
            f"  --session {session} --repo-target {repo_target} --peek --limit 5",
        ]
    return [
        f"# Wake channel '{channel}': pickup follows this agent's activation",
        "# contract (see docs/workflows/session-autobridge-runbook.md).",
    ]


def coworker_prompt(agent, channel, project, chat, repo_target, family,
                    needs_dispatch: bool = False) -> str:
    session = f"SESSION-{agent.upper()}-{chat.split('-')[-1]}"
    # claude_app discovery refuses an unscoped read (it could pick another
    # project's session), so the worker must pass its own checkout path.
    discover_scope = " --project-path <your-checkout>" if family == "claude_app" else ""
    lines = [
        f"# Setup prompt for {agent} — new collab session on {chat}",
        f"# Project {project}, repo-target {repo_target}. Run from the deployed",
        f"# runtime ({LAUNCH}), never a parked/dirty operator checkout.",
        "",
        "# 1. Start a FRESH native session for THIS chat and read its id + home",
        "#    (verify it is the thread you are in). Use a FRESH native session —",
        "#    registration REFUSES a native id already dispatchable (active or",
        "#    unexpired parked) in a different (project_id, chat_id) scope — the",
        "#    exact-dispatch key, so two projects reusing one chat_id are also",
        "#    refused (GH-468); deactivate the old lease first.",
        "#    Use the session_id AND home this prints when registering below:",
        f"{LAUNCH} session_autobridge.py discover-runtime --runtime-family {family}{discover_scope} --json",
    ]
    register_step = 2
    if needs_dispatch:
        lines += [
            "",
            "# 2. Establish the App Server transport before registration. STOP",
            "#    if this fails; no binding may become dispatchable without it:",
            f"{LAUNCH} pm2_watchers.py ensure --agent codex-appserver",
        ]
        register_step = 3
    lines += [
        "",
        f"# {register_step}. Register THAT id + THAT home (never guess, never substitute a default):",
        f"{LAUNCH} session_autobridge.py register \\",
        f"  --session {session} --agent {agent} \\",
        f"  --project {project} --chat {chat} --repo-target {repo_target} \\",
        f"  --mode auto-read --status active --wake-strategy runtime_trigger \\",
        f"  --runtime-family {family} --runtime-session-id <YOUR_ID> \\",
        f"  --runtime-home <YOUR_HOME_FROM_STEP_1> --runtime-session-source first_read",
        "",
        f"# {register_step + 1}. Pick up packets on YOUR wake channel:",
    ]
    lines += pickup_block(channel, agent, project, chat, session, repo_target, "<YOUR_ID>",
                          family, needs_dispatch)
    return "\n".join(lines)


def parse_args():
    p = argparse.ArgumentParser(description="Set up a new collaboration session.")
    p.add_argument("--project", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--me", required=True, help="initiator agent id")
    p.add_argument("--my-runtime-session-id", required=True,
                   help="the initiator's OWN native session id (read it, do not guess)")
    p.add_argument("--my-runtime-family", required=True,
                   choices=("codex_app", "claude_app", "gemini_cli"))
    p.add_argument("--my-runtime-home", default=None,
                   help="defaults per family (~/.claude, ~/.codex, ~/.gemini)")
    p.add_argument("--with", dest="coworkers", required=True,
                   help="comma-separated co-worker agent:family pairs, e.g. "
                        "codex:codex_app,gemini:gemini_cli (family is explicit — "
                        "never guessed; Pi/relay workers are not supported here)")
    p.add_argument("--repo-target", required=True)
    p.add_argument("--skip-currency-check", action="store_true",
                   help="skip the origin/main guard (tests only)")
    return p.parse_args()


def main():
    args = parse_args()
    ensure_project(args.project, allow_none=False)
    if not args.skip_currency_check:
        assert_current_checkout()

    agents = load_agents()

    # Parse "agent:family" pairs; family is explicit, never guessed.
    coworkers = []
    for spec in (s.strip() for s in args.coworkers.split(",") if s.strip()):
        if ":" not in spec:
            sys.exit(f"[error] --with entry {spec!r} must be agent:family "
                     f"(one of {', '.join(SUPPORTED_FAMILIES)})")
        agent_id, family = spec.split(":", 1)
        coworkers.append((agent_id.strip(), family.strip()))

    # Validate EVERYTHING before any side effect (fail closed): initiator and
    # coworkers must exist, and every family must be one this helper supports —
    # otherwise new_chat.py leaves an orphan chat behind. Pi/relay are refused
    # here, not routed through an unusable claude_app prompt.
    # The initiator registers its OWN native session (below), and every coworker
    # must too, so ALL of them — not just coworkers — need native-registration
    # capability validated BEFORE any chat side effect. A supported family is a
    # native family; a human_relay/attended-only/non-routine-AX identity cannot
    # back one and would otherwise produce a bogus registration.
    if not _supports_native_registration(args.me, agent_activation(agents, args.me)):
        sys.exit(
            f"[error] initiator {args.me}: this activation cannot back an "
            "autonomously-registered native session (human_relay / attended-only / "
            "non-routine AX). Run this helper only from a native worker."
        )
    for agent_id, family in coworkers:
        activation = agent_activation(agents, agent_id)
        if family not in SUPPORTED_FAMILIES:
            sys.exit(f"[error] {agent_id}: family {family!r} not supported by this "
                     f"helper (use {', '.join(SUPPORTED_FAMILIES)}). Pi workers use "
                     f"`worker.py start-livecraft-pi`; a human_relay has no native session.")
        if not _supports_native_registration(agent_id, activation):
            sys.exit(
                f"[error] {agent_id}: activation type {activation.get('type')!r} "
                f"cannot back a native session (human_relay / attended-only / "
                f"non-routine AX), so it cannot back family {family!r}. Reach it by "
                "its own activation path, not this native-session setup."
            )

    # Validate --repo-target against the project's configured repos before creating
    # the chat: a typo would otherwise persist into chat/session scope and only
    # fail later at delivery.
    project = get_project(args.project) or {}
    configured_repos = project.get("repos") or {}
    if args.repo_target not in configured_repos:
        sys.exit(
            f"[error] --repo-target {args.repo_target!r} is not a configured repo of "
            f"project {args.project!r} (configured: "
            f"{', '.join(sorted(configured_repos)) or 'none'})."
        )

    # GH-536: validate the starter's native routing scope before new_chat.py
    # writes the directory. Ordinary new_chat.py remains deliberately unaware
    # of worker bindings; this guard belongs only to the worker-aware path.
    preflight_starter_binding(
        agent=args.me, project=args.project,
        runtime_session_id=args.my_runtime_session_id,
        runtime_family=args.my_runtime_family,
    )

    created = subprocess.run(
        [sys.executable, str(BIN / "new_chat.py"),
         "--project", args.project, "--title", args.title],
        capture_output=True, text=True,
    )
    if created.returncode != 0:
        sys.exit(f"[error] creating chat failed:\n{created.stderr or created.stdout}")
    created_json = json.loads(created.stdout)
    chat = created_json["chat_id"]
    suffix = chat.split("-")[-1]

    my_session = f"SESSION-{args.me.upper()}-{suffix}"
    my_home = args.my_runtime_home or DEFAULT_HOMES.get(args.my_runtime_family, f"~/.{args.me}")
    my_activation = agent_activation(agents, args.me)
    try:
        if needs_dispatch_wake(my_activation):
            ensure_codex_transport()
        register_session(my_session, args.me, args.project, chat, args.repo_target,
                         args.my_runtime_family, args.my_runtime_session_id, my_home)
    except RuntimeError as exc:
        # Roll back exactly the chat we just created so a failed run is
        # all-or-nothing rather than leaving a hidden orphan chat.
        shutil.rmtree(created_json["path"], ignore_errors=True)
        sys.exit(f"[error] {exc}\n[rolled back] removed orphan chat {chat}")

    print(f"chat_id: {chat}")
    print(f"initiator session: {my_session} ({args.me} / {args.my_runtime_session_id})")
    my_channel = wake_channel(my_activation)
    print("\n=== YOUR OWN PICKUP (do this now) ===")
    print("\n".join(pickup_block(my_channel, args.me, args.project, chat,
                                 my_session, args.repo_target, args.my_runtime_session_id,
                                 args.my_runtime_family, needs_dispatch_wake(my_activation))))
    for agent_id, family in coworkers:
        activation = agent_activation(agents, agent_id)
        channel = wake_channel(activation)
        print(f"\n=== SETUP PROMPT — share with {agent_id} ===")
        print(coworker_prompt(agent_id, channel, args.project, chat, args.repo_target, family,
                              needs_dispatch_wake(activation)))


if __name__ == "__main__":
    main()
