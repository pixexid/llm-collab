#!/usr/bin/env bash
# GH-581 mutation proof: each P1's discriminating test must fail when its fix is
# removed. Restore the source between mutants so each result is independent.
set -u
cd "$(dirname "$0")/.."
PY=python3.11
F=llm_collab/bb_client.py
WATCH_F=bin/watch_inbox.py
BACKUP="$F.mutbak"
WATCH_BACKUP="$WATCH_F.mutbak"
cp "$F" "$BACKUP"
restore() {
  [ -f "$BACKUP" ] && cp "$BACKUP" "$F"
  [ -f "$WATCH_BACKUP" ] && cp "$WATCH_BACKUP" "$WATCH_F"
  rm -f "$BACKUP" "$WATCH_BACKUP"
}
purge_pycache() { find . -type d -name __pycache__ -prune -exec rm -rf {} +; }
trap restore EXIT INT TERM

TIMEOUT_TEST=tests.test_bb_client.ProductionTransportTest.test_timeout_kills_child_before_waiting_for_readers
OVERFLOW_TEST=tests.test_bb_client.BoundedDecodingTest.test_native_overflow_is_ambiguous_for_tasks_but_malformed_for_reads

purge_pycache
if ! PYTHONDONTWRITEBYTECODE=1 $PY -m unittest "$TIMEOUT_TEST" "$OVERFLOW_TEST" >/dev/null 2>&1; then
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
purge_pycache
PYTHONDONTWRITEBYTECODE=1 $PY -m unittest "$TIMEOUT_TEST" >/dev/null 2>&1
rc=$?
if [ "$rc" = 1 ]; then
  echo "killed: M1 join-readers-before-kill"
else
  echo "M1 survived or infrastructure failed (rc=$rc)"
  exit 1
fi
cp "$BACKUP" "$F"
purge_pycache

# M2: let the native overflow escape _call. Task/read refusal mapping then
# cannot preserve AMBIGUOUS versus MALFORMED, so the combined test must fail.
$PY - "$F" <<'PY'
import sys

path = sys.argv[1]
source = open(path, encoding="utf-8").read()
old = """        except BbResponseReadError as exc:
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE,
                str(exc) or \"response could not be read\",
            )
"""
new = """        except BbResponseReadError:
            raise
"""
assert source.count(old) == 1, "native-overflow mutation anchor is not unique"
open(path, "w", encoding="utf-8").write(source.replace(old, new, 1))
PY
PYTHONDONTWRITEBYTECODE=1 $PY -m unittest "$OVERFLOW_TEST" >/dev/null 2>&1
rc=$?
if [ "$rc" = 1 ]; then
  echo "killed: M2 native-overflow-refusal-mapping"
else
  echo "M2 survived or infrastructure failed (rc=$rc)"
  exit 1
fi
cp "$BACKUP" "$F"
purge_pycache

# M3: replace the packet-selected repository with the legacy app default. The
# unscoped docs packet must then fail its named target assertion.
REPO_BOOTSTRAP_TEST=tests.test_watch_inbox_bb_bootstrap.BbWatcherBootstrapTest.test_packet_repo_target_binds_unscoped_watcher_and_missing_refuses
cp "$WATCH_F" "$WATCH_BACKUP"
purge_pycache
if ! PYTHONDONTWRITEBYTECODE=1 $PY -m unittest "$REPO_BOOTSTRAP_TEST" >/dev/null 2>&1; then
  echo "REPO BASELINE NOT GREEN — aborting"
  exit 2
fi
$PY - "$WATCH_F" <<'PY'
import sys

path = sys.argv[1]
source = open(path, encoding="utf-8").read()
old = 'inputs = _bb_start_inputs(project_id, project or {}, repo_id)'
new = 'inputs = _bb_start_inputs(project_id, project or {}, "app")'
assert source.count(old) == 1, "bootstrap-repo mutation anchor is not unique"
open(path, "w", encoding="utf-8").write(source.replace(old, new, 1))
PY
purge_pycache
if PYTHONDONTWRITEBYTECODE=1 $PY -m unittest "$REPO_BOOTSTRAP_TEST" >/tmp/gh581-m3.out 2>&1; then
  rc=0
else
  rc=$?
fi
if [ "$rc" = 1 ] && grep -q 'failures=gh581_unscoped_docs_packet_must_not_select_app' /tmp/gh581-m3.out; then
  echo "killed: M3 packet-repo-target-over-legacy-app"
else
  echo "M3 survived or infrastructure failed (rc=$rc)"
  cat /tmp/gh581-m3.out
  exit 1
fi
rm -f /tmp/gh581-m3.out

# M4: map all reader failures as malformed at the transport boundary. Removing
# the exhaustive mapping must fail both the task and read classification tests.
DECODE_TASK_TEST=tests.test_bb_client.BoundedDecodingTest.test_reader_decode_failure_is_ambiguous_for_tasks
DECODE_READ_TEST=tests.test_bb_client.BoundedDecodingTest.test_reader_decode_failure_is_malformed_for_reads
purge_pycache
if ! PYTHONDONTWRITEBYTECODE=1 $PY -m unittest "$DECODE_TASK_TEST" "$DECODE_READ_TEST" >/dev/null 2>&1; then
  echo "DECODE BASELINE NOT GREEN — aborting"
  exit 2
fi
$PY - "$F" <<'PY'
import sys

path = sys.argv[1]
source = open(path, encoding="utf-8").read()
old = """        except BbResponseReadError as exc:
            return BbRefusal(
                REFUSAL_MALFORMED_RESPONSE,
                str(exc) or \"response could not be read\",
            )
"""
new = """        except BbResponseReadError as exc:
            return BbRefusal(REFUSAL_TRANSPORT_FAILED, str(exc) or \"response could not be read\")
"""
assert source.count(old) == 1, "reader-error mutation anchor is not unique"
open(path, "w", encoding="utf-8").write(source.replace(old, new, 1))
PY
purge_pycache
if PYTHONDONTWRITEBYTECODE=1 $PY -m unittest "$DECODE_TASK_TEST" "$DECODE_READ_TEST" >/tmp/gh586-m4.out 2>&1; then
  rc=0
else
  rc=$?
fi
if [ "$rc" = 1 ] \
  && grep -q 'failures=gh586_task_decode_must_be_ambiguous' /tmp/gh586-m4.out \
  && grep -q 'failures=gh586_read_decode_must_be_malformed' /tmp/gh586-m4.out; then
  echo "killed: M4 exhaustive-reader-error-mapping"
else
  echo "M4 survived or infrastructure failed (rc=$rc)"
  cat /tmp/gh586-m4.out
  exit 1
fi
rm -f /tmp/gh586-m4.out

echo "ALL MUTATIONS KILLED"
exit 0
