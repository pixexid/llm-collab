#!/usr/bin/env python3
"""Alias for refine_task.py with planning/authorship-oriented naming."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from refine_task import main


if __name__ == "__main__":
    main()
