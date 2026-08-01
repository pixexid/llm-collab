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
  'if not native_session_id or status not in {"active", "parked"}:
        return' \
  'if not native_session_id or status not in {"active", "parked"}:
        return
    return' \
  "$T.test_gh468_native_session_active_in_another_chat_is_refused"

mutate_and_check "M2 drop-chat-from-same-scope" \
  'and other.get("chat_id") == chat_id' 'and True' \
  "$T.test_gh468_same_id_move_to_a_different_chat_is_refused"

mutate_and_check "M3 drop-project-from-same-scope" \
  'other.get("project_id") == project_id
            and other.get("chat_id") == chat_id' \
  'True
            and other.get("chat_id") == chat_id' \
  "$T.test_gh468_same_chat_id_in_a_different_project_is_refused"

mutate_and_check "M4 drop-other-dispatchable" \
  'and session_is_dispatchable(other)[0]' 'and True' \
  "$T.test_gh468_reuse_after_other_lease_stopped_is_allowed"

mutate_and_check "M5 guard-terminal-status" \
  'if not native_session_id or status not in {"active", "parked"}:' \
  'if not native_session_id or False:' \
  "$T.test_gh468_terminal_status_registration_is_not_guarded"

mutate_and_check "M7 drop-parked-from-new-side" \
  'if not native_session_id or status not in {"active", "parked"}:' \
  'if not native_session_id or status not in {"active"}:' \
  "$T.test_gh468_parked_registration_against_dispatchable_owner_is_refused"

mutate_and_check "M6 scan-not-strict-fails-open" \
  'for other in iter_sessions(strict=True):' \
  'for other in iter_sessions():' \
  "$T.test_gh468_malformed_lease_fails_the_ownership_scan_closed"

echo "---"; [ "$fail" = 0 ] && echo "ALL MUTATIONS KILLED" || echo "SOME SURVIVED"
exit $fail
