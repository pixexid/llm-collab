#!/usr/bin/env bash
# GH-94 mutation proofs. Each mutation reverts one fix verbatim (exact-string
# replace) and asserts the matching test then FAILS. Files are snapshotted and
# restored from the snapshot (never `git checkout`, which would also discard
# uncommitted work).
set -u
cd "$(dirname "$0")/.."
SAB=bin/_session_autobridge.py
WI=bin/watch_inbox.py
PY=python3.11
fail=0

# mutate <file> <old> <new>  -- exact literal replacement, asserts it changed something
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
expect_fail() { # <name> <test-target>
  if $PY -m unittest "$2" >/dev/null 2>&1; then
    echo "MUTATION SURVIVED (BAD): $1"; fail=1
  else
    echo "killed: $1"
  fi
}

T=tests.test_gh94_dup_delivery.PostAcceptanceIsDeliveredTest
R=tests.test_gh94_dup_delivery.RealRecvJsonRejectsMalformedFramesTest
S=tests.test_gh94_dup_delivery.SeenPathsCommittedBeforeDispatchTest

# M1 defect#2: a lost reply view after acceptance must NOT raise.
snapshot "$SAB"
mutate "$SAB" "            except (TimeoutError, OSError, ValueError):" "            except ():"
expect_fail "defect#2 post-accept-must-not-raise" $T.test_timeout_after_acceptance_is_delivered_unobserved
restore "$SAB"

# M2 defect#2: a malformed post-accept frame is in the delivered-unobserved contract.
snapshot "$SAB"
mutate "$SAB" "            except (TimeoutError, OSError, ValueError):" "            except (TimeoutError, OSError):"
expect_fail "defect#2 malformed-frame-in-contract" $T.test_malformed_frame_after_acceptance_is_delivered_unobserved
restore "$SAB"

# M3 defect#2: a delivered turn is returncode 0 (not 1 on non-complete).
snapshot "$SAB"
mutate "$SAB" '        "returncode": 0,
        "stdout": assistant_text.strip(),' '        "returncode": 0 if status == "completed" else 1,
        "stdout": assistant_text.strip(),'
expect_fail "defect#2 failed-turn-returncode-1" $T.test_failed_turn_after_acceptance_is_still_delivered_not_retried
restore "$SAB"

# M4 defect#3: the non-object guard in recv_json.
snapshot "$SAB"
mutate "$SAB" '                if not isinstance(message, dict):
                    raise ValueError("websocket text frame is not a JSON-RPC object")
' ''
expect_fail "defect#3 recv_json-non-object-guard" $R.test_non_object_json_frame_is_value_error
restore "$SAB"

# M6 defect#1: seen_paths committed after dispatch (relocate).
snapshot "$WI"
$PY - "$WI" <<'PYEOF'
import re, sys
p = sys.argv[1]; s = open(p).read()
block = [ln for ln in s.splitlines(keepends=True)]
# remove the pre-dispatch commit + its comment
import re as _re
s2 = _re.sub(r"                # seen_paths records what has been ANNOUNCED.*?\n                seen_paths = seen_paths \| new_msgs\n", "", s, count=1, flags=_re.S)
assert s2 != s, "pre-dispatch commit not found"
# re-add it after the dispatch block (before the except)
s3 = s2.replace("                    )\n        except Exception as e:",
                "                    )\n                seen_paths = seen_paths | new_msgs\n        except Exception as e:", 1)
assert s3 != s2, "dispatch block anchor not found"
open(p, "w").write(s3)
PYEOF
expect_fail "defect#1 seen_paths-after-dispatch" $S.test_seen_paths_commit_precedes_dispatch_call
restore "$WI"

echo "---"
if [ "$fail" = 0 ]; then echo "ALL MUTATIONS KILLED"; else echo "SOME MUTATIONS SURVIVED"; fi
exit $fail
