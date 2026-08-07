#!/usr/bin/env bash
# GH-546 mutation proof: removing the pre-write empty-body guard must kill the
# named real-stdin test. Only unittest rc==1 counts as a killed mutation.
set -u
cd "$(dirname "$0")/.."
F=bin/deliver.py
PY=python3.11
export PYTHONDONTWRITEBYTECODE=1
BACKUP="$F.mutbak"
cp "$F" "$BACKUP"
restore() { [ -f "$BACKUP" ] && mv "$BACKUP" "$F"; }
trap restore EXIT INT TERM
T=tests.test_deliver_empty_body.DeliverEmptyBodyTest.test_empty_stdin_refuses_before_any_write

if ! $PY -m unittest tests.test_deliver_empty_body >/dev/null 2>&1; then
  echo "BASELINE NOT GREEN — aborting"
  exit 2
fi

$PY - "$F" <<'PY'
import sys

path = sys.argv[1]
source = open(path, encoding="utf-8").read()
old = "    if not body:\n"
assert source.count(old) == 1, "empty-body guard anchor is not unique"
open(path, "w", encoding="utf-8").write(source.replace(old, "    if False:\n", 1))
PY

$PY -m unittest "$T" >/dev/null 2>&1
rc=$?
if [ "$rc" = 1 ]; then
  echo "killed: M1 remove-pre-write-empty-body-guard"
  echo "ALL MUTATIONS KILLED"
  exit 0
fi
if [ "$rc" = 0 ]; then
  echo "MUTATION SURVIVED (BAD): M1 remove-pre-write-empty-body-guard"
else
  echo "INFRA ERROR (rc=$rc), not a kill: M1 remove-pre-write-empty-body-guard"
fi
exit 1
