#!/usr/bin/env python3
"""Write one bounded collab packet pointer to a Pi event-monitor doorbell."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    pointer = os.environ.get("LLM_COLLAB_MESSAGE_PATH", "")
    if not pointer or "\n" in pointer or len(pointer.encode()) > 4096:
        return 2
    doorbell = Path(sys.argv[1]).expanduser()
    doorbell.parent.mkdir(parents=True, exist_ok=True)
    doorbell.touch(mode=0o600, exist_ok=True)
    doorbell.chmod(0o600)
    doorbell.write_text(pointer + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
