#!/bin/bash
# Focused regression for bin/axsend-ensure --window-index forwarding (PR78 R2):
# the follow-up `confirm` must receive an explicitly-supplied --window-index
# (incl. 0), and must NOT add one when it was absent. Uses a stub axsend that
# records its argv, so no real AX/app is needed.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root_src="$(cd "$here/../.." && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/tools/axbridge" "$tmp/bin"
cp "$root_src/bin/axsend-ensure" "$tmp/bin/axsend-ensure"
: > "$tmp/tools/axbridge/build.sh"   # noop build
log="$tmp/argv.log"
# Stub axsend: records argv, and honors RING_EXIT / CONFIRM_EXIT env so tests can
# drive the not-delivered (exit 7) + delayed-confirm promotion path (PR78 R6).
cat > "$tmp/tools/axbridge/axsend" <<'STUB'
#!/bin/bash
echo "$@" >> "$AXSEND_STUB_LOG"
case "$1" in
  ring)
    if [ "${RING_EXIT:-0}" -eq 7 ]; then echo "WARN: NOT DELIVERED (stub)"; else echo "VERIFIED: stub ring (non-queued)"; fi
    exit "${RING_EXIT:-0}";;
  confirm)
    # Optional: fail until the Nth confirm attempt, then deliver (models a target
    # whose window is transiently unresolvable right after the ring — PR78 R6).
    if [ -n "${CONFIRM_DELIVER_ON_ATTEMPT:-}" ]; then
      n=$(cat "$AXSEND_STUB_CONFIRM_COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$AXSEND_STUB_CONFIRM_COUNT"
      if [ "$n" -ge "$CONFIRM_DELIVER_ON_ATTEMPT" ]; then echo "delivered: stub (attempt $n)"; exit 0; else echo "no windows: stub (attempt $n)"; exit 1; fi
    fi
    if [ "${CONFIRM_EXIT:-0}" -eq 0 ]; then echo "delivered: stub"; else echo "not delivered: stub"; fi
    exit "${CONFIRM_EXIT:-0}";;
esac
exit 0
STUB
ccount="$tmp/confirm_count"
sed -i '' "s#\$AXSEND_STUB_LOG#$log#g; s#\$AXSEND_STUB_CONFIRM_COUNT#$ccount#g" "$tmp/tools/axbridge/axsend"
chmod +x "$tmp/tools/axbridge/axsend" "$tmp/bin/axsend-ensure"

fails=0
# run <ring_exit> <confirm_exit> <args...> -> sets $rc to the wrapper exit code.
# set +e around the call so a nonzero wrapper exit (expected in the R6 cases) is
# captured in $rc instead of killing this script under `set -e`.
run() {
  : > "$log"
  set +e
  RING_EXIT="$1" CONFIRM_EXIT="$2" "$tmp/bin/axsend-ensure" "${@:3}" >/dev/null 2>&1
  rc=$?
  set -e
}
confirm_line() { grep '^confirm ' "$log" || true; }
assert() { if eval "$2"; then echo "ok   - $1"; else echo "FAIL - $1 (rc=$rc confirm='$(confirm_line)')"; fails=$((fails+1)); fi; }

# --window-index forwarding (PR78 R2), ring succeeds.
run 0 0 ring --app Claude --text hi --submit --window-index 0
assert "explicit --window-index 0 forwarded to confirm" '[[ "$(confirm_line)" == *"--window-index 0"* ]]'
run 0 0 ring --app Claude --text hi --submit --window-index 1
assert "explicit --window-index 1 forwarded to confirm" '[[ "$(confirm_line)" == *"--window-index 1"* ]]'
run 0 0 ring --app Claude --text hi --submit
assert "absent --window-index NOT added to confirm" '[[ "$(confirm_line)" != *"--window-index"* ]]'

# PR78 R6: ring exits 7 (not delivered) but a delayed confirm shows delivered ->
# wrapper promotes to success (exit 0), and NEVER resends (no second ring).
run 7 0 ring --app ZCode --text tok --submit
assert "ring 7 + confirm delivered -> wrapper exit 0 (promoted)" '(( rc == 0 ))'
assert "ring 7 + confirm delivered -> exactly one ring (no resend)" '[[ "$(grep -c "^ring " "$log")" == "1" ]]'
assert "ring 7 promotion still runs the confirm" '[[ -n "$(confirm_line)" ]]'

# ring exits 7 and confirm also NOT delivered -> wrapper stays nonzero (7).
run 7 7 ring --app ZCode --text tok --submit
assert "ring 7 + confirm not-delivered -> wrapper stays nonzero" '(( rc != 0 ))'

# PR78 R6: confirm is transiently unresolvable ("no windows") right after the
# ring, then delivers on a later attempt -> bounded retry promotes to success.
: > "$ccount"; : > "$log"
set +e
RING_EXIT=7 CONFIRM_DELIVER_ON_ATTEMPT=2 "$tmp/bin/axsend-ensure" ring --app ZCode --text tok --submit >/dev/null 2>&1
rc=$?
set -e
assert "ring 7 + confirm delivers on 2nd attempt -> wrapper exit 0 (bounded retry)" '(( rc == 0 ))'
assert "bounded retry still sends exactly one ring (no resend)" '[[ "$(grep -c "^ring " "$log")" == "1" ]]'

# Genuine setup/identity failure (exit 1) must propagate immediately, NOT masked
# by a confirm, and must NOT run confirm at all.
run 1 0 ring --app ZCode --text tok --submit
assert "ring exit 1 (setup/identity) propagates, not masked" '(( rc == 1 ))'
assert "ring exit 1 does NOT run a confirm" '[[ -z "$(confirm_line)" ]]'

# Forwarded explicit window index preserved in the exit-7 confirm path too.
run 7 0 ring --app ZCode --text tok --submit --window-index 1
assert "ring 7 promotion forwards explicit --window-index to confirm" '[[ "$(confirm_line)" == *"--window-index 1"* ]]'

if [ "$fails" -eq 0 ]; then echo; echo "ALL PASS (axsend-ensure wrapper)"; else echo; echo "$fails FAILURE(S)"; exit 1; fi
