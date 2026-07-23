#!/usr/bin/env python3
"""Entrypoint for the inert workspace daemon."""

from pathlib import Path
import sys

BIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN_DIR))

from _python_runtime import require_python

require_python()

sys.path.insert(0, str(BIN_DIR.parent))

from llm_collab.daemon.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
