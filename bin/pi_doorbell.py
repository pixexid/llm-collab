#!/usr/bin/env python3
"""Write one bounded collab packet pointer to a Pi event-monitor doorbell."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    pointer = os.environ.get("LLM_COLLAB_MESSAGE_PATH", "")
    if not pointer or "\n" in pointer or len(pointer.encode()) > 4096:
        return 2
    collab_root = Path.cwd().resolve()
    pointer_path = Path(pointer)
    pointer_path = (
        pointer_path.resolve()
        if pointer_path.is_absolute()
        else (collab_root / pointer_path).resolve()
    )
    try:
        pointer_path.relative_to(collab_root)
    except ValueError:
        return 2
    pointer = str(pointer_path)
    if len(pointer.encode()) > 4096:
        return 2
    doorbell = Path(sys.argv[1]).expanduser()
    if not doorbell.parent.is_dir():
        return 2
    fd, temporary = tempfile.mkstemp(
        dir=doorbell.parent.parent,
        prefix=f".{doorbell.parent.name}.{doorbell.name}.",
    )
    published = False
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(pointer + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, doorbell)
        published = True
    finally:
        if not published:
            Path(temporary).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
