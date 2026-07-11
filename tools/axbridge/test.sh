#!/bin/bash
# Focused axbridge tests (issue #77 / PR78): pure send-resolution + window
# selection unit tests, plus the axsend-ensure wrapper argv-forwarding test.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$(mktemp -t axsend-tests.XXXXXX)"; trap 'rm -f "$out"' EXIT
swiftc -O -parse-as-library "$here/send-resolution.swift" "$here/send-resolution-tests.swift" -o "$out"
"$out"
echo
bash "$here/wrapper-test.sh"
