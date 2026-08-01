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

# baseline must be green first
if ! $PY -m unittest \
  $T.test_gh468_native_session_active_in_another_chat_is_refused \
  $T.test_gh468_same_chat_reregistration_is_allowed \
  $T.test_gh468_different_native_id_is_allowed \
  $T.test_gh468_reuse_after_other_lease_stopped_is_allowed \
  $T.test_gh468_non_active_registration_is_not_guarded >/dev/null 2>&1; then
  echo "BASELINE NOT GREEN — aborting"; exit 2
fi

# apply <old-python-literal-file-marker> via a python heredoc; args: NAME OLD NEW TEST
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
  'if not native_session_id or status != "active":
        return' \
  'if not native_session_id or status != "active":
        return
    return' \
  "$T.test_gh468_native_session_active_in_another_chat_is_refused"

mutate_and_check "M2 drop-chat-diff" \
  'and other.get("chat_id") != chat_id' 'and True' \
  "$T.test_gh468_same_chat_reregistration_is_allowed"

mutate_and_check "M3 drop-other-active" \
  'and other.get("status") == "active"' 'and True' \
  "$T.test_gh468_reuse_after_other_lease_stopped_is_allowed"

mutate_and_check "M4 guard-non-active" \
  'if not native_session_id or status != "active":' \
  'if not native_session_id or status == "__never__":' \
  "$T.test_gh468_non_active_registration_is_not_guarded"

echo "---"; [ "$fail" = 0 ] && echo "ALL MUTATIONS KILLED" || echo "SOME SURVIVED"
exit $fail
