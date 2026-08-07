#!/usr/bin/env bash
# GH-554 mutation proofs. Each mutation removes one routing identity guard and
# requires its named regression assertion to fail. A test error is infrastructure,
# not a killed mutation.
set -u
cd "$(dirname "$0")/.."
PY=python3.11
DEL=bin/deliver.py
WATCH=bin/watch_inbox.py
PM2=pm2/ecosystem.config.cjs
OUT=/tmp/gh554-mutation.out
fail=0

cleanup() {
  for file in "$DEL" "$WATCH" "$PM2"; do
    [ -f "$file.gh554bak" ] && cp "$file.gh554bak" "$file"
    rm -f "$file.gh554bak"
  done
  rm -f "$OUT"
}
trap cleanup EXIT INT TERM

purge_pycache() { find . -type d -name __pycache__ -prune -exec rm -rf {} +; }
snapshot() { cp "$1" "$1.gh554bak"; }
restore() { cp "$1.gh554bak" "$1"; rm -f "$1.gh554bak"; purge_pycache; }
mutate() { "$PY" - "$1" "$2" "$3" <<'PYEOF'
import sys

path, old, new = sys.argv[1:]
source = open(path, encoding="utf-8").read()
assert source.count(old) == 1, f"mutation anchor is not unique: {path}"
open(path, "w", encoding="utf-8").write(source.replace(old, new, 1))
PYEOF
}

BASELINE=(
  tests.test_session_autobridge.SessionAutobridgeTest.test_unresolved_worker_target_refuses_before_write_for_amiga_and_non_amiga
  tests.test_session_autobridge.SessionAutobridgeTest.test_resolved_binding_stays_targeted_for_amiga_and_non_amiga
  tests.test_session_autobridge.SessionAutobridgeTest.test_watcherless_human_receives_an_explicit_broadcast_for_both_projects
  tests.test_watch_inbox_refusal_progress.ChatScopedWatcherTest.test_chat_scope_surfaces_bound_and_null_target_packets_only
  tests.test_pm2_ecosystem.Pm2EcosystemTest.test_agent_wide_pm2_watchers_are_notification_only
)

purge_pycache
if ! PYTHONDONTWRITEBYTECODE=1 "$PY" -m unittest "${BASELINE[@]}" >/dev/null 2>&1; then
  echo "BASELINE NOT GREEN — aborting"
  exit 2
fi

expect_killed() {
  local label=$1 test=$2 marker=$3 rc
  if PYTHONDONTWRITEBYTECODE=1 "$PY" -m unittest "$test" >"$OUT" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  if [ "$rc" -eq 1 ] && grep -q "failures=$marker" "$OUT"; then
    echo "killed: $label"
  else
    echo "SURVIVED OR INFRA ERROR: $label (rc=$rc)"
    cat "$OUT"
    fail=1
  fi
}

snapshot "$DEL"
mutate "$DEL" '            "repair the binding and retry.",
            file=sys.stderr,
        )
        sys.exit(2)

    body = read_body' '            "repair the binding and retry.",
            file=sys.stderr,
        )
        args.routing_mode = "broadcast"

    body = read_body'
purge_pycache
expect_killed "unresolved-target-prewrite-guard" \
  tests.test_session_autobridge.SessionAutobridgeTest.test_unresolved_worker_target_refuses_before_write_for_amiga_and_non_amiga \
  gh554_unresolved_target_refuses_before_write
restore "$DEL"

snapshot "$DEL"
mutate "$DEL" '            args.target_session_id = str(resolved_binding_target)' '            args.target_session_id = "wrong-target"'
purge_pycache
expect_killed "resolved-target-stamping" \
  tests.test_session_autobridge.SessionAutobridgeTest.test_resolved_binding_stays_targeted_for_amiga_and_non_amiga \
  gh554_resolved_binding_stamped
restore "$DEL"

snapshot "$DEL"
mutate "$DEL" '        args.routing_mode = "broadcast"' '        args.routing_mode = "targeted"'
purge_pycache
expect_killed "broadcast-marker" \
  tests.test_session_autobridge.SessionAutobridgeTest.test_watcherless_human_receives_an_explicit_broadcast_for_both_projects \
  gh554_broadcast_mode
restore "$DEL"

snapshot "$WATCH"
mutate "$WATCH" '        and message.get("frontmatter", {}).get("chat_id") == args.chat' '        and message.get("frontmatter", {}).get("chat_id") == args.chat
        and message.get("frontmatter", {}).get("target_session_id") is not None'
purge_pycache
expect_killed "chat-scope-includes-null-target" \
  tests.test_watch_inbox_refusal_progress.ChatScopedWatcherTest.test_chat_scope_surfaces_bound_and_null_target_packets_only \
  gh554_chat_scope_surfaces_bound_and_unbound
restore "$WATCH"

snapshot "$PM2"
mutate "$PM2" '      "--no-autobridge",' ''
purge_pycache
expect_killed "pm2-notification-only" \
  tests.test_pm2_ecosystem.Pm2EcosystemTest.test_agent_wide_pm2_watchers_are_notification_only \
  gh554_pm2_notification_only
restore "$PM2"

if [ "$fail" -eq 0 ]; then
  echo "ALL MUTATIONS KILLED"
else
  echo "SOME MUTATIONS SURVIVED OR ERRORED"
fi
exit "$fail"
