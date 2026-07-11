#!/bin/bash
# Focused tests for the pure send-button resolution (issue #77).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="$(mktemp -t axsend-tests.XXXXXX)"
trap 'rm -f "$out"' EXIT
swiftc -O -parse-as-library "$here/send-resolution.swift" "$here/send-resolution-tests.swift" -o "$out"
"$out"
