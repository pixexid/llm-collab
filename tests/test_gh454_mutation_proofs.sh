#!/usr/bin/env bash
# GH-454 mutation proofs: removing the chat guard, canonical-id use, or scan
# bound must fail the matching tests. Only unittest rc==1 counts as killed.
set -u
cd "$(dirname "$0")/.."
F=bin/worker_rotate_pi.py
H=bin/_helpers.py
PY=python3.11
export PYTHONDONTWRITEBYTECODE=1
cp "$F" "$F.mutbak"; cp "$H" "$H.mutbak"
trap 'mv "$F.mutbak" "$F" 2>/dev/null; mv "$H.mutbak" "$H" 2>/dev/null' EXIT INT TERM

mutate() {
  $PY - "$1" "$2" "$3" <<'PY'
import sys
p, old, new = sys.argv[1:]
s = open(p).read()
assert old in s, f"mutation anchor not found: {old!r}"
open(p, "w").write(s.replace(old, new, 1))
PY
}

run_kill() {
  local name="$1" test="$2" rc
  $PY -m unittest "$test" >/dev/null 2>&1
  rc=$?
  if [ "$rc" = 1 ]; then
    echo "killed: $name"
    return 0
  fi
  echo "SURVIVED/ERR(rc=$rc): $name"
  return 1
}

fail=0
mutate "$F" $'    chat = _resolve_chat(cfg.project, cfg.chat)\n' $'    chat = cfg.chat\n'
run_kill "drop-chat-existence-guard" \
  tests.test_worker_start_pi.StartPiFlowTest.test_nonexistent_chat_fails_before_pi_web_or_binding
 [ "$?" = 0 ] || fail=1
cp "$F.mutbak" "$F"

mutate "$F" $'        agent=cfg.agent, project=cfg.project, chat=chat, repo_target=cfg.repo_target,\n' \
  $'        agent=cfg.agent, project=cfg.project, chat=cfg.chat, repo_target=cfg.repo_target,\n'
run_kill "bind-raw-chat-selector" \
  tests.test_worker_start_pi.StartPiFlowTest.test_selector_registers_under_canonical_chat_id
 [ "$?" = 0 ] || fail=1
cp "$F.mutbak" "$F"

mutate "$H" $'        if max_entries is not None and index > max_entries:\n' \
  $'        if False:\n'
run_kill "drop-chat-scan-bound" \
  tests.test_worker_start_pi.StartPiFlowTest.test_chat_scan_bound_fails_before_pi_web_or_binding
 [ "$?" = 0 ] || fail=1
cp "$H.mutbak" "$H"

if [ "$fail" = 0 ]; then
  echo "ALL MUTATIONS KILLED"
else
  echo "SOME MUTATIONS SURVIVED"
fi
exit "$fail"
