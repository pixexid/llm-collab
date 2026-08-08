"""Bounded file reads with no workspace or CWD initialization side effects."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class UnreadableFile(RuntimeError):
    """An oversized, non-regular, or I/O-failed path. Distinct from absent, deliberately."""


# An optional budget charged by EVERY bounded read while a lookup is active, including the binding
# reads that happen inside resolve_exact_dispatch_pair() where the caller has no reach. Set and
# cleared by the caller around one lookup; None means no cumulative accounting, which is the
# historical behaviour for every other caller.
_ACTIVE_READ_BUDGET: list = []


class ReadBudget:
    def __init__(self, limit: int, label: str = "exact-session read") -> None:
        self.limit = limit
        self.label = label
        self.spent = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.spent

    def charge(self, count: int, path: Path) -> None:
        self.spent += count
        if self.spent > self.limit:
            raise UnreadableFile(
                f"{self.label} exceeds {self.limit} bytes at {path}"
            )


class active_read_budget:
    """Charge every bounded read in this block to one cumulative budget."""

    def __init__(self, budget) -> None:
        self.budget = budget

    def __enter__(self):
        _ACTIVE_READ_BUDGET.append(self.budget)
        return self.budget

    def __exit__(self, *exc) -> bool:
        _ACTIVE_READ_BUDGET.pop()
        return False


def read_regular_file_bounded_with_identity(path: Path, limit: int) -> tuple[bytes, float | None]:
    """Bytes plus the mtime from the SAME descriptor that produced them (GH-539).

    read()-then-stat() is two operations on two possibly-different objects: a rewrite
    landing between them yields metadata describing bytes the caller never parsed.
    Anything that records "this content had this identity" must take both from one
    descriptor, which is what the fstat below already does.
    """
    identity: dict = {}
    payload = read_regular_file_bounded(path, limit, _identity_out=identity)
    return payload, identity.get("mtime")


def read_regular_file_bounded(path: Path, limit: int, *, _identity_out: dict | None = None) -> bytes:
    """Read at most `limit` bytes from a REGULAR file, without ever blocking on open().

    Every untrusted read in this codebase needs the same four things and gets them wrong
    individually: a non-blocking open (a writer-less FIFO blocks forever INSIDE open(), before any
    byte cap or deadline can apply), an fstat on that SAME descriptor (stat-then-reopen can resolve
    two different objects), a regular-file requirement, and a read LOOP (one os.read may return
    short and silently truncate). Each was fixed separately for the token file and then not applied
    to its siblings, so this exists to make the next call site correct by construction.

    Raises FileNotFoundError when absent, UnreadableFile for every other refusal.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise UnreadableFile(f"cannot open {path}: {error}") from error
    try:
        info = os.fstat(descriptor)
        if _identity_out is not None:
            # Same descriptor, same object: this mtime describes exactly the bytes
            # returned below.
            _identity_out["mtime"] = info.st_mtime
        if not stat.S_ISREG(info.st_mode):
            raise UnreadableFile(f"{path} is not a regular file; refusing to read it")
        if info.st_size > limit:
            raise UnreadableFile(f"{path} exceeds the {limit} byte limit; refusing to parse it")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as error:
        raise UnreadableFile(f"cannot read {path}: {error}") from error
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if len(raw) > limit:
        raise UnreadableFile(f"{path} exceeds the {limit} byte limit; refusing to parse it")
    if _ACTIVE_READ_BUDGET:
        _ACTIVE_READ_BUDGET[-1].charge(len(raw), path)
    return raw
