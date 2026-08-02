#!/usr/bin/env bash
# GH-454 mutation proof: removing the pre-session chat guard must fail the
# missing-chat test. Only unittest rc==1 counts as a killed mutation.
set -u
cd "$(dirname "$0")/.."
F=bin/worker_rotate_pi.py
PY=python3.11
export PYTHONDONTWRITEBYTECODE=1
cp "$F" "$F.mutbak"; trap 'mv "$F.mutbak" "$F" 2>/dev/null' EXIT INT TERM

$PY - "$F" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
old = '    if not _chat_exists(cfg.project, cfg.chat):\n'
assert old in s, f"mutation anchor not found: {old!r}"
open(p, "w").write(s.replace(old, '    if False:\n', 1))
PY
if [ $? -ne 0 ]; then
  echo "ANCHOR MISS: drop-chat-existence-guard"
  exit 1
fi

$PY -m unittest tests.test_worker_start_pi.StartPiFlowTest.test_nonexistent_chat_fails_before_pi_web_or_binding >/dev/null 2>&1
rc=$?
if [ "$rc" = 1 ]; then
  echo "killed: drop-chat-existence-guard"
  echo "ALL MUTATIONS KILLED"
  exit 0
fi
echo "SURVIVED/ERR(rc=$rc): drop-chat-existence-guard"
echo "SOME SURVIVED"
exit 1
