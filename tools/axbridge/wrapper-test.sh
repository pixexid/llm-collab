#!/bin/bash
# Regression for bin/axsend-ensure (PR78 R2/R6/R7). Uses a stub axsend that
# records its argv and honors env so tests can drive:
#  - --window-index forwarding to the follow-up commands (R2),
#  - ring exit 7 (not delivered) + FRESHNESS-GATED delayed promotion (R7): a
#    delayed promotion requires a STRICTLY NEW turn (turn count increase), never a
#    stale identical earlier turn, and never a resend,
#  - identity-loss exit 9 must NOT be promoted (R7).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root_src="$(cd "$here/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/tools/axbridge" "$tmp/bin"
cp "$root_src/bin/axsend-ensure" "$tmp/bin/axsend-ensure"
: > "$tmp/tools/axbridge/build.sh"   # noop build
log="$tmp/argv.log"
tcount="$tmp/turns_count"
# Stub axsend: RING_EXIT sets the ring exit code; `turns` prints TURNS_BASELINE on
# its first call (the wrapper's pre-ring baseline) and TURNS_AFTER on every call
# after (post-ring polls). `confirm` reports delivered unless CONFIRM_EXIT!=0.
cat > "$tmp/tools/axbridge/axsend" <<'STUB'
#!/bin/bash
echo "$@" >> "$AXSEND_STUB_LOG"
case "$1" in
  ring)
    case "${RING_EXIT:-0}" in
      7) echo "WARN: NOT DELIVERED (stub)";;
      9) echo "identity lost (stub)";;
      0) echo "VERIFIED: stub ring (non-queued)";;
    esac
    exit "${RING_EXIT:-0}";;
  turns)
    n=$(cat "$AXSEND_STUB_TURNS_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$AXSEND_STUB_TURNS_COUNT"
    if [ "$n" -eq 1 ]; then echo "${TURNS_BASELINE:-0}"; else echo "${TURNS_AFTER:-0}"; fi
    exit 0;;
  confirm)
    if [ "${CONFIRM_EXIT:-0}" -eq 0 ]; then echo "delivered: stub"; else echo "not delivered: stub"; fi
    exit "${CONFIRM_EXIT:-0}";;
esac
exit 0
STUB
sed -i '' "s#\$AXSEND_STUB_LOG#$log#g; s#\$AXSEND_STUB_TURNS_COUNT#$tcount#g" "$tmp/tools/axbridge/axsend"
chmod +x "$tmp/tools/axbridge/axsend" "$tmp/bin/axsend-ensure"

fails=0
# run <ring_exit> <baseline> <after> <args...> -> sets $rc to the wrapper exit
# code. set +e so an expected nonzero wrapper exit is captured, not fatal.
run() {
  : > "$log"; : > "$tcount"
  set +e
  RING_EXIT="$1" TURNS_BASELINE="$2" TURNS_AFTER="$3" "$tmp/bin/axsend-ensure" "${@:4}" >/dev/null 2>&1
  rc=$?
  set -e
}
line() { grep "^$1 " "$log" || true; }
count() { grep -c "^$1 " "$log" || true; }
assert() { if eval "$2"; then echo "ok   - $1"; else echo "FAIL - $1 (rc=$rc)"; fails=$((fails+1)); fi; }

# R2: --window-index forwarding on a successful ring's follow-up confirm.
run 0 0 0 ring --app Claude --text hi --submit --window-index 0
assert "explicit --window-index 0 forwarded to confirm" '[[ "$(line confirm)" == *"--window-index 0"* ]]'
run 0 0 0 ring --app Claude --text hi --submit --window-index 1
assert "explicit --window-index 1 forwarded to confirm" '[[ "$(line confirm)" == *"--window-index 1"* ]]'
run 0 0 0 ring --app Claude --text hi --submit
assert "absent --window-index NOT added to confirm" '[[ "$(line confirm)" != *"--window-index"* ]]'

# R7: ring exit 7 + a NEW turn appears (baseline 0 -> after 1) -> promote to 0,
# exactly one ring (no resend).
run 7 0 1 ring --app ZCode --text tok --submit
assert "ring 7 + new turn (0->1) -> wrapper exit 0 (freshness promote)" '(( rc == 0 ))'
assert "freshness promote sends exactly one ring (no resend)" '[[ "$(count ring)" == "1" ]]'

# R7 CORE: a STALE identical prior turn (baseline 1) with a FAILED new ring (count
# stays 1) must NOT promote — the old fix would falsely promote from mere existence.
run 7 1 1 ring --app ZCode --text tok --submit
assert "ring 7 + stale identical turn (1->1, no increase) -> stays nonzero" '(( rc != 0 ))'

# R7: identity loss exits 9 (distinct from 7) and must NEVER be promoted, even if a
# turn count would otherwise look fresh.
run 9 0 1 ring --app ZCode --text tok --submit
assert "ring exit 9 (identity lost) -> propagates, never promoted" '(( rc == 9 ))'

# R7: genuine setup/arg failure (exit 1) propagates and consults no turns/confirm.
run 1 0 1 ring --app ZCode --text tok --submit
assert "ring exit 1 (setup) propagates" '(( rc == 1 ))'
# baseline turns is read once pre-ring (read-only, harmless); the point is exit 1
# does NOT enter the post-ring promotion polling (which would add more turns calls).
assert "ring exit 1 does NOT poll turns after the ring (baseline only)" '[[ "$(count turns)" == "1" ]]'

# R7: explicit --window-index preserved on the pre-ring baseline + post-ring turns.
run 7 0 1 ring --app ZCode --text tok --submit --window-index 1
assert "freshness path forwards explicit --window-index to turns" '[[ "$(line turns)" == *"--window-index 1"* ]]'

if [ "$fails" -eq 0 ]; then echo; echo "ALL PASS (axsend-ensure wrapper)"; else echo; echo "$fails FAILURE(S)"; exit 1; fi
