#!/bin/bash
# Focused axbridge tests (issue #77 / PR78): pure send-resolution + window
# selection unit tests, plus the axsend-ensure wrapper argv-forwarding test.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$(mktemp -t axsend-tests.XXXXXX)"
cli="$(mktemp -t axsend-cli.XXXXXX)"
trap 'rm -f "$out" "$cli"' EXIT
swiftc -O -parse-as-library "$here/send-resolution.swift" "$here/send-resolution-tests.swift" -o "$out"
"$out"
swiftc -O -parse-as-library "$here/axsend.swift" "$here/send-resolution.swift" -o "$cli"
for command in ring type; do
  set +e
  refusal="$("$cli" "$command" --app Claude --text probe 2>&1)"
  status=$?
  set -e
  [[ $status -eq 11 ]]
  [[ "$refusal" == *"Claude receives durable mailbox packets"* ]]
done
echo
bash "$here/wrapper-test.sh"
