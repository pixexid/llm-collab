#!/usr/bin/env bash
# GH-94 mutation proofs. Each mutation reverts one fix verbatim (exact-string
# replace) and asserts the matching test then FAILS. Files are snapshotted and
# restored from the snapshot; an EXIT/signal trap restores any active snapshot
# so an interrupt cannot leave a mutated tracked file behind (bot #465 @13). A
# clean baseline is required first, so an infra failure (missing interpreter,
# import error) is never miscounted as a killed mutation (bot #465 @20).
set -u
cd "$(dirname "$0")/.."
# GH-503: authorize the freshness-gate bypass for the CLIs this script subprocesses,
# via the same per-run token+sentinel the Python testkit uses.
eval "$(PYTHONPATH=tests python3.11 -c 'import _runtime_gate_testkit as k, os; print(f"export {list(k.gate_bypass_env())[0]}={k.gate_bypass_env()[list(k.gate_bypass_env())[0]]}; export {list(k.gate_bypass_env())[1]}={k.gate_bypass_env()[list(k.gate_bypass_env())[1]]}")')"
SAB=bin/_session_autobridge.py
WI=bin/watch_inbox.py
PY=python3.11
fail=0

restore_all() { for f in "$SAB" "$WI"; do [ -f "$f.mutbak" ] && mv "$f.mutbak" "$f"; done; }
trap restore_all EXIT INT TERM

mutate() { $PY - "$1" "$2" "$3" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
assert old in src, f"mutation anchor not found in {path}"
open(path, "w").write(src.replace(old, new, 1))
PYEOF
}
snapshot() { cp "$1" "$1.mutbak"; }
restore()  { mv "$1.mutbak" "$1"; }

D=tests.test_gh94_dup_delivery
T=$D.PostAcceptanceIsDeliveredTest
R=$D.RealRecvJsonRejectsMalformedFramesTest
S=$D.SeenPathsCommittedBeforeDispatchTest

# Baseline must be green BEFORE any mutation, else a nonzero exit below is an
# infra failure, not a kill.
if ! $PY -m unittest "$D" >/dev/null 2>&1; then
  echo "BASELINE NOT GREEN — aborting (fix the environment/tests first)"; exit 2
fi

expect_fail() { # <name> <test-target>
  $PY -m unittest "$2" >/dev/null 2>&1
  rc=$?
  # unittest exits 1 on assertion/test failure; >1 (e.g. 2) is a usage/collection
  # error we must NOT count as a kill.
  if [ "$rc" = 1 ]; then echo "killed: $1"
  elif [ "$rc" = 0 ]; then echo "MUTATION SURVIVED (BAD): $1"; fail=1
  else echo "INFRA ERROR (rc=$rc), not a kill: $1"; fail=1; fi
}

# M1 defect#2: a lost reply view after acceptance must NOT raise.
snapshot "$SAB"; mutate "$SAB" "            except (TimeoutError, OSError, ValueError):" "            except ():"
expect_fail "defect#2 post-accept-must-not-raise" $T.test_timeout_after_acceptance_is_delivered_unobserved
restore "$SAB"

# M2 defect#2: a malformed post-accept frame is in the delivered-unobserved contract.
snapshot "$SAB"; mutate "$SAB" "            except (TimeoutError, OSError, ValueError):" "            except (TimeoutError, OSError):"
expect_fail "defect#2 malformed-frame-in-contract" $T.test_malformed_frame_after_acceptance_is_delivered_unobserved
restore "$SAB"

# M3 defect#2: a delivered turn is returncode 0 (not 1 on non-complete).
snapshot "$SAB"; mutate "$SAB" '        "returncode": 0,
        "stdout": assistant_text.strip(),' '        "returncode": 0 if status == "completed" else 1,
        "stdout": assistant_text.strip(),'
expect_fail "defect#2 failed-turn-returncode-1" $T.test_failed_turn_after_acceptance_is_still_delivered_not_retried
restore "$SAB"

# M4 defect#3: the non-object guard in recv_json.
snapshot "$SAB"; mutate "$SAB" '                if not isinstance(message, dict):
                    raise ValueError("websocket text frame is not a JSON-RPC object")
' ''
expect_fail "defect#3 recv_json-non-object-guard" $R.test_non_object_json_frame_is_value_error
restore "$SAB"

# M5 (#465 @3054): the absolute deadline install.
snapshot "$SAB"; mutate "$SAB" "        client.set_deadline(deadline)
" ""
expect_fail "bot#3054 deadline-installed" $T.test_absolute_deadline_is_installed_before_observation
restore "$SAB"

# M6 (#465 @3085): terminal/output correlation to our turn.
snapshot "$SAB"; mutate "$SAB" '                if frame_turn_id is not None and frame_turn_id != started_turn_id:
                    continue
' ''
expect_fail "bot#3085 foreign-turn-not-observed" $T.test_foreign_turn_terminal_is_not_observed
restore "$SAB"

# M7 defect#1: seen_paths committed after dispatch (announcement dedup ordering).
snapshot "$WI"
$PY - "$WI" <<'PYEOF'
import re, sys
p = sys.argv[1]; s = open(p).read()
s2 = re.sub(r"                # seen_paths records what has been ANNOUNCED.*?\n                seen_paths = seen_paths \| new_msgs\n", "", s, count=1, flags=re.S)
assert s2 != s, "pre-dispatch commit not found"
s3 = s2.replace("                    )\n        except Exception as e:",
                "                    )\n                seen_paths = seen_paths | new_msgs\n        except Exception as e:", 1)
assert s3 != s2, "dispatch block anchor not found"
open(p, "w").write(s3)
PYEOF
expect_fail "defect#1 seen_paths-order (announcement dedup)" $S.test_seen_paths_commit_precedes_dispatch_call
restore "$WI"

echo "---"
if [ "$fail" = 0 ]; then echo "ALL MUTATIONS KILLED"; else echo "SOME MUTATIONS SURVIVED/ERRORED"; fi
exit $fail
