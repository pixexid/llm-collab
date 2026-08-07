#!/usr/bin/env bash
# GH-585 mutation proofs. Each mutation removes one accepted P1 guard and the
# named focused test must fail. Source is restored even if the script is stopped.
set -u
cd "$(dirname "$0")/.."
PY=python3.11
DELIVER=bin/deliver.py
fail=0

restore() { [ -f "$DELIVER.mutbak" ] && mv "$DELIVER.mutbak" "$DELIVER"; }
trap restore EXIT INT TERM

mutate() {
  $PY - "$1" "$2" "$3" <<'PYEOF'
import sys
path, old, new = sys.argv[1:]
src = open(path).read()
assert old in src, f"mutation anchor not found in {path}"
open(path, "w").write(src.replace(old, new, 1))
PYEOF
}

expect_fail() {
  "$PY" -m unittest "$1" >/dev/null 2>&1
  rc=$?
  if [ "$rc" = 1 ]; then
    echo "killed: $2"
  elif [ "$rc" = 0 ]; then
    echo "MUTATION SURVIVED (BAD): $2"
    fail=1
  else
    echo "INFRA ERROR (rc=$rc), not a kill: $2"
    fail=1
  fi
}

D=tests.test_session_autobridge.SessionAutobridgeTest
BASE="$D.test_deliver_refuses_unreadable_pair_before_packet_write $D.test_deliver_falls_back_to_pair_when_sender_binding_is_not_authoritative"
if ! $PY -m unittest $BASE >/dev/null 2>&1; then
  echo "BASELINE NOT GREEN — aborting"
  exit 2
fi

cp "$DELIVER" "$DELIVER.mutbak"
mutate "$DELIVER" \
  '        raise SenderSessionProvenanceRefusal(
            f"thread pair could not be read before delivery: {error}"
        ) from error' \
  '        pair = None'
expect_fail "$D.test_deliver_refuses_unreadable_pair_before_packet_write" \
  "P1-1 pre-write pair refusal"
restore

cp "$DELIVER" "$DELIVER.mutbak"
mutate "$DELIVER" \
  '    if resolved is None:
        return None' \
  '    if False:
        return None'
expect_fail "$D.test_deliver_falls_back_to_pair_when_sender_binding_is_not_authoritative" \
  "P1-2 exact live binding validation"
restore

if [ "$fail" = 0 ]; then
  echo "ALL GH-585 MUTATIONS KILLED"
else
  echo "GH-585 MUTATION PROOF FAILED"
fi
exit "$fail"
