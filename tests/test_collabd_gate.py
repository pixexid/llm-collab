from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from llm_collab.daemon.gate import (
    DECLARATION_ID,
    DeclarationError,
    evaluate_observation_gate,
    parse_feature_declaration,
    read_exact_nofollow,
)


def declaration(*, enabled: object = True, extra: dict | None = None) -> bytes:
    payload = {
        "declaration_version": 1,
        "declaration_id": DECLARATION_ID,
        "features": {"daemon_observation": enabled},
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload).encode()


class ObservationGateTest(unittest.TestCase):
    def test_closed_declaration_defaults_omitted_features_false(self) -> None:
        features = parse_feature_declaration(declaration())
        self.assertTrue(features["daemon_observation"])
        self.assertEqual(
            {name for name, value in features.items() if not value},
            {"canonical_writes", "runtime_dispatch", "ax_v2", "remote_transport"},
        )

    def test_duplicate_unknown_mistyped_and_nonconstant_declarations_fail_closed(self) -> None:
        invalid = [
            b'{"declaration_version":1,"declaration_version":1,"declaration_id":"'
            + DECLARATION_ID.encode()
            + b'","features":{}}',
            b'{"declaration_version":1,"declaration\\u005fversion":1,"declaration_id":"'
            + DECLARATION_ID.encode()
            + b'","features":{}}',
            b'{"declaration_version":1,"declaration_id":"'
            + DECLARATION_ID.encode()
            + b'","features":{"daemon_observation":true,"daemon_observation":true}}',
            declaration(extra={"unknown": False}),
            declaration(enabled=1),
            declaration(enabled="1"),
            declaration(enabled=None),
            b'{"declaration_version":1,"declaration_id":"'
            + DECLARATION_ID.encode()
            + b'","features":{"daemon_observation":true},"weight":NaN}',
            json.dumps(
                {
                    "declaration_version": True,
                    "declaration_id": DECLARATION_ID,
                    "features": {},
                }
            ).encode(),
            json.dumps(
                {
                    "declaration_version": 1,
                    "declaration_id": "wrong",
                    "features": {},
                }
            ).encode(),
            json.dumps(
                {
                    "declaration_version": 1,
                    "declaration_id": DECLARATION_ID,
                    "features": {"unknown": False},
                }
            ).encode(),
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(DeclarationError):
                parse_feature_declaration(raw)

    def test_three_independent_exact_string_gates_and_invalid_all_false(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            path = Path(tmp) / "declaration.json"
            for feature, enabled, observe, expected in (
                (True, "1", "1", True),
                (False, "1", "1", False),
                (True, "0", "1", False),
                (True, "1", "0", False),
                (True, "true", "1", False),
                (True, "1", "true", False),
                (True, "01", "1", False),
            ):
                with self.subTest(feature=feature, enabled=enabled, observe=observe):
                    path.write_bytes(declaration(enabled=feature))
                    status = evaluate_observation_gate(
                        path,
                        environ={
                            "THREAD_EVENT_RUNNER_ENABLED": enabled,
                            "THREAD_EVENT_RUNNER_OBSERVE": observe,
                        },
                    )
                    self.assertEqual(status.effective, expected)
            path.write_bytes(b'{"features":')
            status = evaluate_observation_gate(
                path,
                environ={
                    "THREAD_EVENT_RUNNER_ENABLED": "1",
                    "THREAD_EVENT_RUNNER_OBSERVE": "1",
                },
            )
            self.assertFalse(status.declaration_valid)
            self.assertFalse(any(status.features.values()))
            self.assertFalse(status.effective)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_exact_reader_refuses_fifo_without_blocking(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            fifo = Path(tmp) / "declaration.json"
            os.mkfifo(fifo)
            started = time.monotonic()
            with self.assertRaises(OSError):
                read_exact_nofollow(fifo)
            self.assertLess(time.monotonic() - started, 1)

    def test_exact_reader_refuses_symlink_directory_and_mutation(self) -> None:
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            regular = root / "regular"
            regular.write_bytes(declaration())
            symlink = root / "symlink"
            symlink.symlink_to(regular)
            with self.assertRaises(OSError):
                read_exact_nofollow(symlink)
            with self.assertRaises(OSError):
                read_exact_nofollow(root)
            before = regular.stat()
            changed = SimpleNamespace(
                st_mode=before.st_mode,
                st_ino=before.st_ino,
                st_dev=before.st_dev,
                st_size=before.st_size,
                st_mtime_ns=before.st_mtime_ns + 1,
            )
            with patch(
                "llm_collab.daemon.gate.os.fstat",
                side_effect=[before, changed],
            ), self.assertRaisesRegex(OSError, "changed during read"):
                read_exact_nofollow(regular)

    def test_frozen_gate_names_are_literal(self) -> None:
        source = Path(__file__).parents[1] / "llm_collab" / "daemon" / "gate.py"
        text = source.read_text()
        self.assertIn('environment.get("THREAD_EVENT_RUNNER_ENABLED") == "1"', text)
        self.assertIn('environment.get("THREAD_EVENT_RUNNER_OBSERVE") == "1"', text)
        self.assertIn('features["daemon_observation"]', text)


if __name__ == "__main__":
    unittest.main()
