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
export AXSEND_ENSURE_FRESHNESS_POLL_INTERVAL_SECONDS="${AXSEND_ENSURE_FRESHNESS_POLL_INTERVAL_SECONDS:-0.01}"
export AXSEND_ENSURE_FRESHNESS_POLL_ATTEMPTS="${AXSEND_ENSURE_FRESHNESS_POLL_ATTEMPTS:-4}"
cp "$root_src/bin/axsend-ensure" "$tmp/bin/axsend-ensure"
: > "$tmp/tools/axbridge/build.sh"   # noop build
log="$tmp/argv.log"
tcount="$tmp/turns_count"
sleep_log="$tmp/sleep.log"
# Stub axsend: RING_EXIT sets the ring exit code; `turns` prints TURNS_BASELINE on
# its first call (the wrapper's pre-ring baseline) and TURNS_AFTER on every call
# after (post-ring polls). `confirm` reports delivered unless CONFIRM_EXIT!=0.
cat > "$tmp/tools/axbridge/axsend" <<'STUB'
#!/bin/bash
echo "$@" >> "$AXSEND_STUB_LOG"
case "$1" in
  ring)
    case "${RING_EXIT:-0}" in
      7) echo "WARN: NOT DELIVERED (stub)"; echo "AX_OUTCOME=NOT_DELIVERED reason=submit_not_landed";;
      9) echo "identity lost (stub)"; echo "AX_OUTCOME=AMBIGUOUS reason=identity_lost";;
      3) echo "ambiguous (stub)"; echo "AX_OUTCOME=AMBIGUOUS reason=queued_unconfirmed";;
      0) echo "VERIFIED: stub ring (non-queued)"; echo "AX_OUTCOME=VERIFIED method=stub_ring";;
    esac
    exit "${RING_EXIT:-0}";;
  turns)
    n=$(cat "$AXSEND_STUB_TURNS_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$AXSEND_STUB_TURNS_COUNT"
    # First call = the wrapper's pre-ring baseline; later calls = post-ring polls.
    # Each can print a value AND exit nonzero (models cmdTurns printing "0" on a
    # resolution failure while returning nonzero — PR78 R8).
    if [ "$n" -eq 1 ]; then echo "${TURNS_BASELINE:-0}"; exit "${TURNS_BASELINE_EXIT:-0}"; fi
    echo "${TURNS_AFTER:-0}"; exit "${TURNS_AFTER_EXIT:-0}";;
  confirm)
    if [ "${CONFIRM_EXIT:-0}" -eq 0 ]; then echo "delivered: stub"; echo "AX_OUTCOME=VERIFIED method=conversation_turn"; else echo "not delivered: stub"; echo "AX_OUTCOME=NOT_DELIVERED reason=text_not_landed"; fi
    exit "${CONFIRM_EXIT:-0}";;
esac
exit 0
STUB
sed -i '' "s#\$AXSEND_STUB_LOG#$log#g; s#\$AXSEND_STUB_TURNS_COUNT#$tcount#g" "$tmp/tools/axbridge/axsend"
chmod +x "$tmp/tools/axbridge/axsend" "$tmp/bin/axsend-ensure"

cat > "$tmp/bin/sleep" <<'STUB'
#!/bin/bash
printf '%s\n' "$@" >> "$AXSEND_STUB_SLEEP_LOG"
STUB
chmod +x "$tmp/bin/sleep"

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

# Invalid polling overrides refuse before the non-idempotent ring. Checking the
# stub log distinguishes a safe pre-submit refusal from a retry-unsafe failure.
: > "$log"; : > "$tcount"
set +e
AXSEND_ENSURE_FRESHNESS_POLL_ATTEMPTS=bad \
  RING_EXIT=7 \
  "$tmp/bin/axsend-ensure" ring --app ZCode --text tok --submit >/dev/null 2>&1
rc=$?
set -e
assert "malformed freshness attempts refuse before ring" '(( rc == 64 )) && [[ "$(count ring)" == "0" ]]'

: > "$log"; : > "$tcount"
set +e
AXSEND_ENSURE_FRESHNESS_POLL_INTERVAL_SECONDS=bad \
  RING_EXIT=7 \
  "$tmp/bin/axsend-ensure" ring --app ZCode --text tok --submit >/dev/null 2>&1
rc=$?
set -e
assert "malformed freshness interval refuses before ring" '(( rc == 64 )) && [[ "$(count ring)" == "0" ]]'

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

# GH-98: freshness promotion replaces the provisional NOT_DELIVERED with one
# final VERIFIED outcome line — a promoted success never exposes the stale failure.
: > "$log"; : > "$tcount"
set +e
promote_out=$(RING_EXIT=7 TURNS_BASELINE=0 TURNS_AFTER=1 "$tmp/bin/axsend-ensure" ring --app ZCode --text tok --submit 2>&1)
rc=$?
set -e
assert "freshness promote emits final AX_OUTCOME=VERIFIED" '(( rc == 0 )) && [[ "$promote_out" == *"AX_OUTCOME=VERIFIED method=freshness_promotion"* ]]'
assert "freshness promote emits exactly ONE outcome, NOT_DELIVERED absent" '[[ "$(grep -c "^AX_OUTCOME=" <<<"$promote_out")" == "1" ]] && [[ "$promote_out" != *"NOT_DELIVERED"* ]]'

# GH-98: an unpromoted exit 7 emits exactly one outcome — the retained
# NOT_DELIVERED, re-emitted once by the wrapper (never duplicated, never zero).
: > "$log"; : > "$tcount"
set +e
fail_out=$(RING_EXIT=7 TURNS_BASELINE=1 TURNS_AFTER=1 "$tmp/bin/axsend-ensure" ring --app ZCode --text tok --submit 2>&1)
rc=$?
set -e
assert "unpromoted exit 7 emits exactly one retained NOT_DELIVERED" '(( rc == 7 )) && [[ "$(grep -c "^AX_OUTCOME=" <<<"$fail_out")" == "1" ]] && [[ "$fail_out" == *"AX_OUTCOME=NOT_DELIVERED reason=submit_not_landed"* ]]'

# GH-98: a successful submit ring emits exactly one final outcome — from the
# follow-up confirm, with the ring's own provisional line suppressed.
: > "$log"; : > "$tcount"
set +e
ok_out=$(RING_EXIT=0 "$tmp/bin/axsend-ensure" ring --app ZCode --text tok --submit 2>&1)
rc=$?
set -e
assert "successful submit ring emits exactly one outcome (from confirm)" '(( rc == 0 )) && [[ "$(grep -c "^AX_OUTCOME=" <<<"$ok_out")" == "1" ]] && [[ "$ok_out" == *"AX_OUTCOME=VERIFIED method=conversation_turn"* ]]'

# R7 CORE: a STALE identical prior turn (baseline 1) with a FAILED new ring (count
# stays 1) must NOT promote — the old fix would falsely promote from mere existence.
run 7 1 1 ring --app ZCode --text tok --submit
assert "ring 7 + stale identical turn (1->1, no increase) -> stays nonzero" '(( rc != 0 ))'
assert "stale freshness proof keeps all four polling attempts" '[[ "$(count turns)" == "5" ]]'

# Unset production defaults remain four attempts at two seconds. The sleep stub
# records the requested interval without making this default-path proof slow.
: > "$log"; : > "$tcount"; : > "$sleep_log"
set +e
env -u AXSEND_ENSURE_FRESHNESS_POLL_INTERVAL_SECONDS \
  -u AXSEND_ENSURE_FRESHNESS_POLL_ATTEMPTS \
  PATH="$tmp/bin:$PATH" AXSEND_STUB_SLEEP_LOG="$sleep_log" \
  RING_EXIT=7 TURNS_BASELINE=1 TURNS_AFTER=1 \
  "$tmp/bin/axsend-ensure" ring --app ZCode --text tok --submit >/dev/null 2>&1
rc=$?
set -e
assert "unset freshness defaults keep four polling attempts" '(( rc == 7 )) && [[ "$(wc -l < "$sleep_log" | tr -d " ")" == "4" ]]'
assert "unset freshness poll interval remains exactly two seconds" '[[ "$(sort -u "$sleep_log")" == "2" ]]'

# R8: exit 9 == post-submit identity loss is AMBIGUOUS and is NEVER auto-promoted
# (a later auto-resolution cannot prove it is the same frozen window/thread). Even
# with a turn-count that looks fresh, exit 9 stays 9.
run 9 0 1 ring --app ZCode --text tok --submit
assert "ring exit 9 (identity lost) -> stays 9, never auto-promoted" '(( rc == 9 ))'

# GH-98: ambiguous is not exit-zero success — ring exit 3 (incl. the former
# queued_unconfirmed zero) propagates as 3 and is never promoted or confirmed
# away; ambiguous stays pull/manual.
run 3 0 1 ring --app ZCode --text tok --submit
assert "ring exit 3 (ambiguous) propagates, never zero" '(( rc == 3 ))'
assert "ring exit 3 runs no follow-up confirm" '[[ "$(count confirm)" == "0" ]]'

# R8 CORE: an untrustworthy baseline — `axsend turns` prints "0" but EXITS NONZERO
# (resolution failure) — must NOT be taken as a real baseline of 0. Otherwise a
# later older identical turn (count 1) would falsely promote exit 7. With the
# exit-status-aware turn_count the baseline is empty, so promotion is blocked and
# the original exit 7 remains.
: > "$log"; : > "$tcount"
set +e
RING_EXIT=7 TURNS_BASELINE=0 TURNS_BASELINE_EXIT=1 TURNS_AFTER=1 TURNS_AFTER_EXIT=0 \
  "$tmp/bin/axsend-ensure" ring --app ZCode --text tok --submit >/dev/null 2>&1
rc=$?
set -e
assert "ring 7 + untrustworthy baseline (turns prints 0 but exits nonzero) -> stays 7 (no false promote)" '(( rc == 7 ))'

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
