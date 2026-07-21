"""Fail-closed activation gate for inert daemon observation."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DECLARATION_ID = (
    "https://llm-collab.dev/declarations/standalone/v1/feature-declarations.json"
)
FEATURES = frozenset(
    {
        "daemon_observation",
        "canonical_writes",
        "runtime_dispatch",
        "ax_v2",
        "remote_transport",
    }
)
_FALSE_FEATURES = {name: False for name in FEATURES}


class DeclarationError(ValueError):
    pass


@dataclass(frozen=True)
class GateStatus:
    declaration_valid: bool
    features: Mapping[str, bool]
    thread_event_runner_enabled: bool
    thread_event_runner_observe: bool
    effective: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "declaration_valid": self.declaration_valid,
            "features": dict(sorted(self.features.items())),
            "thread_event_runner_enabled": self.thread_event_runner_enabled,
            "thread_event_runner_observe": self.thread_event_runner_observe,
            "effective": self.effective,
        }


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DeclarationError("duplicate declaration member")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise DeclarationError(f"non-JSON numeric constant: {value}")


def parse_feature_declaration(raw: bytes) -> dict[str, bool]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DeclarationError) as exc:
        raise DeclarationError("invalid feature declaration JSON") from exc
    if not isinstance(value, dict) or set(value) != {
        "declaration_version",
        "declaration_id",
        "features",
    }:
        raise DeclarationError("feature declaration has an invalid top-level shape")
    if type(value["declaration_version"]) is not int or value["declaration_version"] != 1:
        raise DeclarationError("feature declaration version must be integer 1")
    if value["declaration_id"] != DECLARATION_ID:
        raise DeclarationError("feature declaration identity mismatch")
    declared = value["features"]
    if not isinstance(declared, dict) or not set(declared).issubset(FEATURES):
        raise DeclarationError("feature declaration has an unknown feature")
    if any(type(flag) is not bool for flag in declared.values()):
        raise DeclarationError("feature declaration values must be booleans")
    return {name: declared.get(name, False) for name in FEATURES}


def read_exact_nofollow(path: Path, *, maximum_bytes: int = 1024 * 1024) -> bytes:
    """Read one regular final component through a no-follow parent descriptor."""
    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be positive")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("O_NOFOLLOW is required")
    directory_flags = os.O_RDONLY | nofollow | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(path.parent, directory_flags)
    try:
        fd = os.open(
            path.name,
            os.O_RDONLY
            | nofollow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise OSError("declaration is not one bounded regular file")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if len(raw) > maximum_bytes:
            raise OSError("declaration exceeds the byte limit")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OSError("declaration changed during read")
        return raw
    finally:
        os.close(fd)


def evaluate_observation_gate(
    declaration_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> GateStatus:
    environment = os.environ if environ is None else environ
    enabled = environment.get("THREAD_EVENT_RUNNER_ENABLED") == "1"
    observe = environment.get("THREAD_EVENT_RUNNER_OBSERVE") == "1"
    try:
        features = parse_feature_declaration(read_exact_nofollow(declaration_path))
        valid = True
    except (OSError, DeclarationError, ValueError):
        features = dict(_FALSE_FEATURES)
        valid = False
    effective = valid and features["daemon_observation"] and enabled and observe
    return GateStatus(valid, features, enabled, observe, effective)
