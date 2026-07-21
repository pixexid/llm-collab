#!/usr/bin/env python3
"""Entrypoint for the inert workspace daemon."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_collab.daemon.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
