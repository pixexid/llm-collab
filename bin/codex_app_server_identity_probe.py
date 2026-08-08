#!/usr/bin/env python3.11
"""Authenticate one App Server initialize and verify its runtime-home identity."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _python_runtime import require_python

require_python()

import argparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _session_autobridge import _codex_app_server_token
from llm_collab.codex_app_server_live_probe import (
    CodexAppServerLiveProbeError,
    probe_runtime_home_identity,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--expected-runtime-home", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=float)
    args = parser.parse_args()

    token = _codex_app_server_token(args.token_file)
    if token is None:
        print("Codex app-server token is not usable", file=sys.stderr)
        return 1
    try:
        probe_runtime_home_identity(
            args.expected_runtime_home,
            endpoint_url=args.endpoint,
            timeout_seconds=args.timeout_seconds,
            token=token,
        )
    except CodexAppServerLiveProbeError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
