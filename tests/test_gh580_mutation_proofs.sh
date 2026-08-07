#!/usr/bin/env bash
# GH-582 mutation proof: the validator must inspect pre-sync db evidence.
# Only unittest rc==1 plus both named project assertions count as a kill.
set -u
cd "$(dirname "$0")/.."
F=bin/task_contract.py
PY=python3.11
T=tests.test_task_contract.TaskContractInvalidDbImpactTest.test_legacy_substituted_marker_rejects_transition_for_both_projects
BACKUP="$F.gh580.mutbak"
OUT=/tmp/gh580-m1.out
export PYTHONDONTWRITEBYTECODE=1

purge_pycache() { find bin tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true; }
restore() { cp "$BACKUP" "$F"; rm -f "$BACKUP" "$OUT"; purge_pycache; }
trap restore EXIT INT TERM

purge_pycache
if ! $PY -m unittest "$T" >/dev/null 2>&1; then
  echo "BASELINE NOT GREEN — aborting"
  exit 2
fi

cp "$F" "$BACKUP"
$PY - "$F" <<'PY'
import sys

path = sys.argv[1]
source = open(path, encoding="utf-8").read()
old = "    evidence_frontmatter = frontmatter if original_frontmatter is None else original_frontmatter\n"
new = "    evidence_frontmatter = frontmatter\n"
assert source.count(old) == 1, "GH-582 provenance anchor is not unique"
open(path, "w", encoding="utf-8").write(source.replace(old, new, 1))
PY
purge_pycache
if $PY -m unittest -v "$T" >"$OUT" 2>&1; then
  rc=0
else
  rc=$?
fi
if [ "$rc" = 1 ] \
  && grep -q 'failures=gh580_llm-collab_transition_must_be_rejected' "$OUT" \
  && grep -q 'failures=gh580_amiga_transition_must_be_rejected' "$OUT"; then
  echo "killed: M1 pre-sync-db-evidence-provenance"
else
  echo "M1 survived or infrastructure failed (rc=$rc)"
  cat "$OUT"
  exit 1
fi

echo "ALL MUTATIONS KILLED"
exit 0
