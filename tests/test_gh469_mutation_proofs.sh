#!/usr/bin/env bash
# GH-469 mutation proofs: revert each pre-chat guard / fetch bound and assert the
# matching test fails. Only unittest rc==1 counts as a kill.
set -u
cd "$(dirname "$0")/.."
F=bin/new_collab_session.py
PY=python3.11
export PYTHONDONTWRITEBYTECODE=1
find bin tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
cp "$F" "$F.mutbak"; trap 'mv "$F.mutbak" "$F" 2>/dev/null' EXIT INT TERM
T=tests.test_new_collab_session.MainPathTest
fail=0

mutate_and_check() {
  local name="$1" test="$4"
  $PY - "$F" "$2" "$3" <<'PY'
import sys
p, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(p).read()
assert old in s, f"anchor not found: {old!r}"
open(p, "w").write(s.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "ANCHOR MISS: $name"; fail=1; cp "$F.mutbak" "$F"; return; fi
  $PY -m unittest "$test" >/dev/null 2>&1
  local rc=$?
  if [ "$rc" = 1 ]; then echo "killed: $name"; else echo "SURVIVED/ERR(rc=$rc): $name"; fail=1; fi
  cp "$F.mutbak" "$F"
}

mutate_and_check "M1 drop-native-capability-guard" \
  'if channel not in ("watcher", "ax_doorbell"):' \
  'if False:' \
  "$T.test_human_relay_coworker_is_refused_before_chat"

mutate_and_check "M2 drop-repo-target-guard" \
  'if args.repo_target not in configured_repos:' \
  'if False:' \
  "$T.test_unknown_repo_target_is_refused_before_chat"

mutate_and_check "M3 unbounded-fetch" \
  '_git("fetch", "origin", "main", "--quiet", timeout=FETCH_TIMEOUT_SECONDS)' \
  '_git("fetch", "origin", "main", "--quiet", timeout=None)' \
  "$T.test_fetch_timeout_surfaces_currency_refusal_not_a_hang"

mutate_and_check "M4 fetch-timeout-not-translated" \
  'except subprocess.TimeoutExpired:' \
  'except subprocess.CalledProcessError as _ignored_timeout:' \
  "$T.test_fetch_timeout_surfaces_currency_refusal_not_a_hang"

echo "---"; [ "$fail" = 0 ] && echo "ALL MUTATIONS KILLED" || echo "SOME SURVIVED"
exit $fail
