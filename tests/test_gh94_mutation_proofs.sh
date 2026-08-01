#!/usr/bin/env bash
# GH-94 mutation proofs. Each mutation reverts one defect's fix verbatim and
# asserts the matching test then FAILS. Files are snapshotted and restored from
# the snapshot (never `git checkout`, which would also discard uncommitted work).
set -u
cd "$(dirname "$0")/.."
SAB=bin/_session_autobridge.py
WI=bin/watch_inbox.py
PY=python3.11
fail=0

snapshot() { cp "$1" "$1.mutbak"; }
restore()  { mv "$1.mutbak" "$1"; }

expect_fail() { # <name> <test-target>
  if $PY -m unittest "$2" >/dev/null 2>&1; then
    echo "MUTATION SURVIVED (BAD): $1 — test passed with fix reverted"; fail=1
  else
    echo "killed: $1"
  fi
}

# --- Defect #2a: post-acceptance timeout must not raise ---
snapshot "$SAB"
perl -0pi -e 's/            except \(TimeoutError, OSError\):\n                # The reply view is lost after acceptance \(deadline hit inside\n                # recv, dropped connection, peer reset\)\. Delivered-but-\n                # unobserved; stop observing, do not raise\.\n                break/            except (TimeoutError, OSError):\n                raise/' "$SAB"
expect_fail "defect#2a timeout-raises" tests.test_gh94_dup_delivery.PostAcceptanceIsDeliveredTest.test_timeout_after_acceptance_is_delivered_unobserved
restore "$SAB"

# --- Defect #2b: delivered turn must be returncode 0 (not 1 on non-complete) ---
snapshot "$SAB"
perl -0pi -e 's/        "returncode": 0,\n        "stdout": assistant_text\.strip\(\),/        "returncode": 0 if status == "completed" else 1,\n        "stdout": assistant_text.strip(),/' "$SAB"
expect_fail "defect#2b failed-turn-returncode-1" tests.test_gh94_dup_delivery.PostAcceptanceIsDeliveredTest.test_failed_turn_after_acceptance_is_still_delivered_not_retried
restore "$SAB"

# --- Defect #3: non-object frame guard removed ---
snapshot "$SAB"
perl -0pi -e 's/            if not isinstance\(message_payload, dict\):\n                continue\n//' "$SAB"
expect_fail "defect#3 non-object-frame-raises" tests.test_gh94_dup_delivery.PostAcceptanceIsDeliveredTest.test_non_object_frame_after_acceptance_does_not_raise
restore "$SAB"

# --- Defect #1: seen_paths committed after dispatch (relocate) ---
snapshot "$WI"
perl -0pi -e 's/                # seen_paths records what has been ANNOUNCED.*?\n                seen_paths = seen_paths \| new_msgs\n                if not args\.session and not args\.no_autobridge:/                if not args.session and not args.no_autobridge:/s' "$WI"
perl -0pi -e 's/(                        \)\n                    \)\n)(        except Exception as e:)/$1                seen_paths = seen_paths | new_msgs\n$2/' "$WI"
expect_fail "defect#1 seen_paths-after-dispatch" tests.test_gh94_dup_delivery.SeenPathsCommittedBeforeDispatchTest.test_seen_paths_commit_precedes_dispatch_call
restore "$WI"

echo "---"
if [ "$fail" = 0 ]; then echo "ALL MUTATIONS KILLED"; else echo "SOME MUTATIONS SURVIVED"; fi
exit $fail
