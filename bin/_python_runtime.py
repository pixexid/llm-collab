"""
Shared Python runtime guard for llm-collab command entrypoints.

Keep this file parseable on Python 3.9 so it can fail before command modules
evaluate newer annotations at import time.
"""

from __future__ import annotations

import os
import sys

MIN_VERSION = (3, 10)


def require_python() -> None:
    if sys.version_info >= MIN_VERSION:
        return

    detected = ".".join(str(part) for part in sys.version_info[:3])
    minimum = ".".join(str(part) for part in MIN_VERSION)
    command = os.path.basename(sys.argv[0]) or "llm-collab command"
    message = (
        f"{command} requires Python {minimum}+; detected Python {detected} at {sys.executable}.\n"
        "Use /Users/pixexid/Projects/llm-collab/bin/llm-collab <script> ... "
        "or run with an explicit Python 3.10+ interpreter such as python3.13."
    )
    print(message, file=sys.stderr)
    raise SystemExit(1)
