#!/usr/bin/env bash
# GH-467 mutation proofs: revert each fail-closed guard in the bounded registry
# read seam and assert the matching test fails. Only unittest rc==1 counts.
set -u
cd "$(dirname "$0")/.."
F=bin/_helpers.py
PY=python3.11
export PYTHONDONTWRITEBYTECODE=1
find bin tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
cp "$F" "$F.mutbak"; trap 'mv "$F.mutbak" "$F" 2>/dev/null' EXIT INT TERM
T=tests.test_helpers_registry_bounds.RegistryBoundedReadTest
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

mutate_and_check "M1 drop-cap-guard" \
  'if len(raw) > MAX_REGISTRY_FILE_BYTES:' \
  'if False:' \
  "$T.test_oversized_agents_fails_closed_with_no_partial_result"

mutate_and_check "M2 drop-regular-file-guard" \
  'if not stat.S_ISREG(info.st_mode):' \
  'if False:' \
  "$T.test_non_regular_file_is_refused"

mutate_and_check "M3 not-strict-utf8" \
  'text = raw.decode("utf-8")' \
  'text = raw' \
  "$T.test_utf16_registry_is_refused_matching_the_daemon"

mutate_and_check "M4 json-parse-not-fail-closed" \
  'except ValueError as error:' \
  'except KeyError as error:' \
  "$T.test_corrupt_json_fails_closed"

mutate_and_check "M5 read-to-fstat-size-not-cap" \
  'remaining = MAX_REGISTRY_FILE_BYTES + 1' \
  'remaining = info.st_size' \
  "$T.test_grow_past_cap_after_fstat_is_refused_not_truncated"

mutate_and_check "M6 no-read-deadline" \
  'signal.setitimer(signal.ITIMER_REAL, REGISTRY_READ_DEADLINE_SECONDS)' \
  'signal.setitimer(signal.ITIMER_REAL, 0)' \
  "$T.test_read_deadline_fails_closed_on_a_stalled_read"

echo "---"; [ "$fail" = 0 ] && echo "ALL MUTATIONS KILLED" || echo "SOME SURVIVED"
exit $fail
