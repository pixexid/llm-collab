#!/usr/bin/env python3
"""touch_watcher_marker.py — the ONE writing shape for watcher liveness markers.

Watcher templates call this once per cycle:

    python3.11 bin/touch_watcher_marker.py --project <project-id> \
        --name <worker-lifecycle|pr-artifacts|heartbeat> --session <session-id>

The session id is supplied explicitly by the template: the session that
launched the watcher knows its own Claude session id and passes it down, so
ownership is recorded, never inferred (GH-726 I3). Freshness is the marker's
mtime; `started_at` is preserved across rewrites by the same session. The
reading side (and the format definition) lives in bin/_watcher_liveness.py.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _python_runtime import require_python

require_python()

import argparse  # noqa: E402

from _watcher_liveness import WATCHER_NAMES, write_marker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--name", required=True, choices=WATCHER_NAMES)
    parser.add_argument("--session", required=True)
    args = parser.parse_args()
    try:
        marker = write_marker(args.project, args.name, args.session)
    except ValueError as error:
        # Clean pre-write refusal: nothing was written.
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1
    print(f"[watcher-marker] {args.name} -> {marker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
