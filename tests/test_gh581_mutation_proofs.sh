#!/usr/bin/env bash
# GH-581 mutation proof: each P1's discriminating test must fail when its fix is
# removed. Restore the source between mutants so each result is independent.
set -u
cd "$(dirname "$0")/.."
PY=python3.11
F=llm_collab/bb_client.py
BACKUP="$F.mutbak"
cp "$F" "$BACKUP"
restore() { [ -f "$BACKUP" ] && mv "$BACKUP" "$F"; }
trap restore EXIT INT TERM

TIMEOUT_TEST=tests.test_bb_client.ProductionTransportTest.test_timeout_kills_child_before_waiting_for_readers
OVERFLOW_TEST=tests.test_bb_client.BoundedDecodingTest.test_native_overflow_is_ambiguous_for_tasks_but_malformed_for_reads

if ! $PY -m unittest "$TIMEOUT_TEST" "$OVERFLOW_TEST" >/dev/null 2>&1; then
  echo "BASELINE NOT GREEN — aborting"
  exit 2
fi

# M1: join the reader pool before killing the child. The child holds both pipes
# open past the deadline, so the timeout test must fail by exceeding its bound.
$PY - "$F" <<'PY'
import sys

path = sys.argv[1]
source = open(path, encoding="utf-8").read()
old = """        except FuturesTimeout as exc:
            aborting = True
            kill_child()
            raise BbTransportTimeout(f\"{' '.join(argv)} exceeded {timeout_seconds}s\") from exc
"""
new = """        except FuturesTimeout as exc:
            aborting = True
            pool.shutdown(wait=True, cancel_futures=True)
            kill_child()
            raise BbTransportTimeout(f\"{' '.join(argv)} exceeded {timeout_seconds}s\") from exc
"""
assert source.count(old) == 1, "timeout-order mutation anchor is not unique"
open(path, "w", encoding="utf-8").write(source.replace(old, new, 1))
PY
$PY -m unittest "$TIMEOUT_TEST" >/dev/null 2>&1
rc=$?
if [ "$rc" = 1 ]; then
  echo "killed: M1 join-readers-before-kill"
else
  echo "M1 survived or infrastructure failed (rc=$rc)"
  exit 1
fi
cp "$BACKUP" "$F"

# M2: let the native overflow escape _call. Task/read refusal mapping then
# cannot preserve AMBIGUOUS versus MALFORMED, so the combined test must fail.
$PY - "$F" <<'PY'
import sys

path = sys.argv[1]
source = open(path, encoding="utf-8").read()
old = """        except BbResponseTooLarge as exc:
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE,
                str(exc) or \"response exceeded the transport bound\",
            )
"""
new = """        except BbResponseTooLarge:
            raise
"""
assert source.count(old) == 1, "native-overflow mutation anchor is not unique"
open(path, "w", encoding="utf-8").write(source.replace(old, new, 1))
PY
$PY -m unittest "$OVERFLOW_TEST" >/dev/null 2>&1
rc=$?
if [ "$rc" = 1 ]; then
  echo "killed: M2 native-overflow-refusal-mapping"
else
  echo "M2 survived or infrastructure failed (rc=$rc)"
  exit 1
fi

echo "ALL MUTATIONS KILLED"
exit 0
