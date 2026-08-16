#!/usr/bin/env python3
"""Validate the visibility field of a native BB provisioning receipt."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_collab.bb_client import BbClient  # noqa: E402


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as error:
        print(f"REFUSED: malformed spawn receipt: {error}", file=sys.stderr)
        return 1
    refusal = BbClient.validate_spawn_visibility(payload)
    if refusal is not None:
        print(f"REFUSED: {refusal.reason}: {refusal.detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
