#!/bin/bash
# Focused axbridge tests (issue #77 / PR78): pure send-resolution + window
# selection unit tests, plus the axsend-ensure wrapper argv-forwarding test.
# Set AXBRIDGE_FORCE_REBUILD=1 to recompile both cached Swift fixtures.
set -euo pipefail

cache_key() {
  local invocation="$1"
  shift
  {
    printf '%s\n' "$invocation"
    shasum -a 256 "$@" | awk '{print $1}'
  } | shasum -a 256 | awk '{print $1}'
}

if [[ "${1:-}" == "--cache-key" ]]; then
  [[ $# -ge 3 ]] || exit 64
  cache_key "$2" "${@:3}"
  exit
fi

here="$(cd "$(dirname "$0")" && pwd)"
cache_dir="${AXBRIDGE_CACHE_DIR:-${TMPDIR:-/tmp}/llm-collab-axbridge-swift-$UID}"
mkdir -p -m 700 "$cache_dir"

compile_cached() {
  local name="$1"
  shift
  local -a command=(swiftc -O -parse-as-library "$@")
  local invocation key cached building
  printf -v invocation '%q ' "${command[@]}" -o
  key="$(cache_key "$invocation" "$@")"
  cached="$cache_dir/$name-$key"
  if [[ "${AXBRIDGE_FORCE_REBUILD:-0}" == "1" || ! -x "$cached" ]]; then
    building="$(mktemp "$cache_dir/.$name.$key.XXXXXX")"
    if ! "${command[@]}" -o "$building"; then
      rm -f "$building"
      return 1
    fi
    mv -f "$building" "$cached"
  fi
  printf '%s\n' "$cached"
}

out="$(compile_cached send-resolution-tests "$here/send-resolution.swift" "$here/send-resolution-tests.swift")"
"$out"
cli="$(compile_cached axsend "$here/axsend.swift" "$here/send-resolution.swift")"
for command in ring type; do
  set +e
  refusal="$("$cli" "$command" --app Claude --text probe 2>&1)"
  status=$?
  set -e
  [[ $status -eq 11 ]]
  [[ "$refusal" == *"Claude receives durable mailbox packets"* ]]
  [[ "$refusal" == *"AX_OUTCOME=NOT_DELIVERED reason=claude_durable_only"* ]]
done
# GH-98: a malformed ring/type (missing --text) stays a usage error with NO
# delivery outcome line — claude_durable_only requires a well-formed attempt.
for command in ring type; do
  set +e
  usage="$("$cli" "$command" --app Claude 2>&1)"
  status=$?
  set -e
  [[ $status -eq 64 ]]
  [[ "$usage" != *"AX_OUTCOME"* ]]
done
echo
bash "$here/wrapper-test.sh"
