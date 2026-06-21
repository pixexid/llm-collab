#!/bin/bash
# Build axsend (the AX doorbell bridge). Idempotent: rebuilds only when the
# source is newer than the binary. Safe to call from any session/agent.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/axsend.swift"
bin="$here/axsend"
if [ ! -f "$bin" ] || [ "$src" -nt "$bin" ]; then
  swiftc -O "$src" -o "$bin"
  echo "built $bin"
else
  echo "up-to-date $bin"
fi
# Ensure the bin/ symlink exists for the canonical invocation path.
ln -sf ../tools/axbridge/axsend "$here/../../bin/axsend" 2>/dev/null || true
