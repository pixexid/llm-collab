#!/usr/bin/env bash
# GH-468 mutation proofs: revert each guard check and assert the matching test
# fails. Snapshot/restore with an EXIT trap; only unittest rc==1 counts as a kill.
set -u
cd "$(dirname "$0")/.."
F=bin/session_autobridge.py
PY=python3.11
# Deterministic: a restored source can share mtime/size with a cached .pyc from a
# prior mutation, so a stale bytecode cache can mask a kill. Disable caching.
export PYTHONDONTWRITEBYTECODE=1
find bin tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
cp "$F" "$F.mutbak"; trap 'mv "$F.mutbak" "$F" 2>/dev/null' EXIT INT TERM
T=tests.test_session_autobridge.SessionAutobridgeTest
fail=0

mutate_and_check() {
  local name="$1" test="$4"
  $PY - "$F" "$2" "$3" <<'PY'
import sys
p,old,new=sys.argv[1],sys.argv[2],sys.argv[3]
s=open(p).read()
assert old in s, f"anchor not found: {old!r}"
open(p,"w").write(s.replace(old,new,1))
PY
  if [ $? -ne 0 ]; then echo "ANCHOR MISS: $name"; fail=1; cp "$F.mutbak" "$F"; return; fi
  $PY -m unittest "$test" >/dev/null 2>&1
  local rc=$?
  if [ "$rc" = 1 ]; then echo "killed: $name"; else echo "SURVIVED/ERR(rc=$rc): $name"; fail=1; fi
  cp "$F.mutbak" "$F"
}

mutate_and_check "M1 helper-noop" \
  'if not native_session_id or not native_family or status not in {"active", "parked"}:
        return' \
  'if not native_session_id or not native_family or status not in {"active", "parked"}:
        return
    return' \
  "$T.test_gh468_native_session_active_in_another_chat_is_refused"

mutate_and_check "M2 drop-chat-discrimination" \
  'or other.get("chat_id") != chat_id' 'or False' \
  "$T.test_gh468_native_session_active_in_another_chat_is_refused"

mutate_and_check "M3 drop-project-discrimination" \
  'other.get("project_id") != project_id' 'False' \
  "$T.test_gh468_different_project_same_chat_is_refused"

mutate_and_check "M4 drop-session-discrimination" \
  'other.get("session_id") != session_id' 'True' \
  "$T.test_gh468_same_session_move_to_a_different_chat_is_allowed"

# Guard-specific anchor: session_is_dispatchable(other)[0] also appears in
# resolve_native_family(); pin this to the guard via the preceding line.
mutate_and_check "M5 drop-other-dispatchable" \
  'and different_scope
            and session_is_dispatchable(other)[0]' \
  'and different_scope
            and True' \
  "$T.test_gh468_reuse_after_other_lease_stopped_is_allowed"

mutate_and_check "M6 drop-family-discrimination" \
  'and other_runtime.get("family") == native_family' 'and True' \
  "$T.test_gh468_same_native_id_different_family_is_allowed"

mutate_and_check "M7 guard-terminal-status" \
  'if not native_session_id or not native_family or status not in {"active", "parked"}:' \
  'if not native_session_id or not native_family or False:' \
  "$T.test_gh468_terminal_status_registration_is_not_guarded"

mutate_and_check "M8 drop-parked-from-new-side" \
  'if not native_session_id or not native_family or status not in {"active", "parked"}:' \
  'if not native_session_id or not native_family or status not in {"active"}:' \
  "$T.test_gh468_parked_registration_against_dispatchable_owner_is_refused"

# Anchor includes the guard-specific next line so it does not match the
# identical loop header in resolve_native_family().
mutate_and_check "M9 scan-not-strict-fails-open" \
  'for other in iter_sessions(strict=True):
        other_runtime = other.get("runtime") or {}' \
  'for other in iter_sessions():
        other_runtime = other.get("runtime") or {}' \
  "$T.test_gh468_malformed_lease_fails_the_ownership_scan_closed"

# M10: the reader must resolve the native's REAL family; if resolution yields
# nothing, the reader falls back to "reader" and the (family,id) guard misses a
# cross-scope collision with the ordinary owner. Killed by an inbox test.
mutate_and_check "M10 reader-family-not-resolved" \
  'return next(iter(families)) if families else None' \
  'return None' \
  "tests.test_inbox.InboxMarkAllReadTest.test_gh468_reader_session_refuses_cross_scope_native_and_writes_nothing"

# M11: resolution must consider only DISPATCHABLE leases; without it a stopped
# old-family lease is chosen over (or alongside) the live owner. Resolver-
# specific anchor via the preceding line.
mutate_and_check "M11 resolver-ignores-dispatchability" \
  'and runtime.get("session_id") == native_session_id
            and session_is_dispatchable(other)[0]' \
  'and runtime.get("session_id") == native_session_id
            and True' \
  "$T.test_gh468_resolve_native_family_ignores_stopped_prefers_live"

# M12: several live families sharing an id must fail closed, not pick arbitrarily.
mutate_and_check "M12 resolver-ambiguity-not-fail-closed" \
  'if len(families) > 1:' 'if False:' \
  "$T.test_gh468_resolve_native_family_ambiguous_multiple_live_fails_closed"

echo "---"; [ "$fail" = 0 ] && echo "ALL MUTATIONS KILLED" || echo "SOME SURVIVED"
exit $fail
