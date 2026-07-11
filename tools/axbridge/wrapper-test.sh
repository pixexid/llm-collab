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
cat > "$tmp/tools/axbridge/axsend" <<STUB
#!/bin/bash
echo "\$@" >> "$log"
case "\$1" in
  ring) echo "VERIFIED: stub ring (non-queued)";;
  confirm) echo "delivered: stub";;
esac
exit 0
STUB
chmod +x "$tmp/tools/axbridge/axsend" "$tmp/bin/axsend-ensure"

fails=0
run() { : > "$log"; "$tmp/bin/axsend-ensure" "$@" >/dev/null 2>&1 || true; }
confirm_line() { grep '^confirm ' "$log" || true; }
assert() { if eval "$2"; then echo "ok   - $1"; else echo "FAIL - $1 (confirm='$(confirm_line)')"; fails=$((fails+1)); fi; }

run ring --app Claude --text hi --submit --window-index 0
assert "explicit --window-index 0 forwarded to confirm" '[[ "$(confirm_line)" == *"--window-index 0"* ]]'

run ring --app Claude --text hi --submit --window-index 1
assert "explicit --window-index 1 forwarded to confirm" '[[ "$(confirm_line)" == *"--window-index 1"* ]]'

run ring --app Claude --text hi --submit
assert "absent --window-index NOT added to confirm" '[[ "$(confirm_line)" != *"--window-index"* ]]'

if [ "$fails" -eq 0 ]; then echo; echo "ALL PASS (axsend-ensure wrapper)"; else echo; echo "$fails FAILURE(S)"; exit 1; fi
