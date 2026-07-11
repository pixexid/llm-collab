#!/bin/bash
# Build axsend (the AX doorbell bridge). Idempotent: rebuilds only when the
# source is newer than the binary. Safe to call from any session/agent.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
bin="$here/axsend"
# Non-test Swift sources (axsend.swift + the pure send-resolution module).
srcs=("$here/axsend.swift" "$here/send-resolution.swift")
newest=$(ls -t "${srcs[@]}" | head -1)
if [ ! -f "$bin" ] || [ "$newest" -nt "$bin" ]; then
  swiftc -O -parse-as-library "${srcs[@]}" -o "$bin"
  echo "built $bin"
else
  echo "up-to-date $bin"
fi
# Ensure the bin/ symlink exists for the canonical invocation path.
ln -sf ../tools/axbridge/axsend "$here/../../bin/axsend" 2>/dev/null || true
