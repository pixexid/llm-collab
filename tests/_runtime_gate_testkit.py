"""GH-503 test kit: authorize the runtime freshness-gate bypass for THIS run only.

Generates a per-run token, writes it to a sentinel tempfile, and exports the token
and sentinel path. In-process gate checks and subprocessed CLIs that inherit this
env (and can read the sentinel) then bypass the gate; production has no sentinel and
cannot forge the per-run token, so this is not a generic bypass switch. The sentinel
is removed at interpreter exit.

Subprocess tests that build a *custom* env dict (not inheriting os.environ) must add
these two vars: use gate_bypass_env() to get them.
"""
from __future__ import annotations

import atexit
import os
import secrets
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from current_runtime import TEST_TOKEN_ENV, TEST_SENTINEL_ENV


def _install() -> None:
    if os.environ.get(TEST_TOKEN_ENV) and os.environ.get(TEST_SENTINEL_ENV):
        return  # already installed (this process or an inherited parent run)
    token = secrets.token_hex(16)
    fd, path = tempfile.mkstemp(prefix="llmcollab-gate-sentinel-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    os.environ[TEST_TOKEN_ENV] = token
    os.environ[TEST_SENTINEL_ENV] = path
    atexit.register(lambda: Path(path).unlink(missing_ok=True))


def gate_bypass_env() -> dict[str, str]:
    """The two env vars a custom-env subprocess must carry to inherit the bypass."""
    return {
        TEST_TOKEN_ENV: os.environ[TEST_TOKEN_ENV],
        TEST_SENTINEL_ENV: os.environ[TEST_SENTINEL_ENV],
    }


_install()
