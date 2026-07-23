"""Contract checks for the inert standalone V1 feature declaration."""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DECLARATION_PATH = (
    ROOT / "docs" / "protocols" / "standalone-v1-feature-declarations.json"
)
DECLARATION_ID = (
    "https://llm-collab.dev/declarations/standalone/v1/feature-declarations.json"
)
FEATURES = {
    "daemon_observation",
    "canonical_writes",
    "runtime_dispatch",
    "ax_v2",
    "remote_transport",
}
TOP_LEVEL_KEYS = {"declaration_version", "declaration_id", "features"}
EXCLUDED_PREFIXES = ("docs/protocols/", "docs/migration/", "Tasks/", "Chats/")
RUNTIME_ROOTS = ("bin/", "scripts/", "tools/", "pm2/", "llm_collab/", "tests/")
THIS_TEST = "tests/test_standalone_feature_declarations.py"
SANCTIONED_CONSUMERS = {
    "llm_collab/canonical/control.py",
    "llm_collab/daemon/gate.py",
    "tests/test_collabd_canonical.py",
    "tests/test_collabd_gate.py",
}
THREAD_EVENT_RUNNER_RFC = ROOT / "docs" / "workflows" / "thread-event-runner-rfc.md"


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def reject_duplicate_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_declaration() -> dict[str, object]:
    return json.loads(
        DECLARATION_PATH.read_text(encoding="utf-8"),
        parse_constant=reject_json_constant,
        object_pairs_hook=reject_duplicate_pairs,
    )


def feature_declaration_matches(
    tracked_paths: tuple[str, ...],
    content_by_path: dict[str, bytes],
) -> list[str]:
    needles = {
        "declaration path": str(DECLARATION_PATH.relative_to(ROOT)),
        "declaration id": DECLARATION_ID,
        **{f"feature {feature}": feature for feature in FEATURES},
    }
    matches: list[str] = []

    for relative_path in tracked_paths:
        if (
            not relative_path.startswith(RUNTIME_ROOTS)
            or relative_path == THIS_TEST
            or relative_path in SANCTIONED_CONSUMERS
            or relative_path.startswith(EXCLUDED_PREFIXES)
        ):
            continue
        path = ROOT / relative_path
        content = content_by_path.get(relative_path)
        if content is None:
            if not path.is_file():
                continue
            content = path.read_bytes()
        for label, needle in needles.items():
            if needle.encode("utf-8") in content:
                matches.append(f"{relative_path}: {label}")
    return matches


class StandaloneFeatureDeclarationTests(unittest.TestCase):
    def test_declaration_parses_as_strict_json(self) -> None:
        self.assertIsInstance(load_declaration(), dict)
        with self.assertRaisesRegex(
            ValueError,
            "duplicate JSON key 'runtime_dispatch'",
        ):
            json.loads(
                '{"features":{"runtime_dispatch":true,'
                '"runtime_dispatch":false}}',
                parse_constant=reject_json_constant,
                object_pairs_hook=reject_duplicate_pairs,
            )

    def test_top_level_key_set_is_exact(self) -> None:
        self.assertEqual(set(load_declaration()), TOP_LEVEL_KEYS)

    def test_version_and_identity_are_exact(self) -> None:
        declaration = load_declaration()
        self.assertIs(type(declaration["declaration_version"]), int)
        self.assertEqual(declaration["declaration_version"], 1)
        self.assertIs(type(declaration["declaration_id"]), str)
        self.assertEqual(declaration["declaration_id"], DECLARATION_ID)

    def test_feature_key_set_is_exact(self) -> None:
        features = load_declaration()["features"]
        self.assertIsInstance(features, dict)
        self.assertEqual(set(features), FEATURES)

    def test_every_committed_feature_is_boolean_false(self) -> None:
        features = load_declaration()["features"]
        self.assertTrue(all(value is False for value in features.values()))

    def test_runtime_dispatch_rollout_step_requires_both_gates(self) -> None:
        rfc = THREAD_EVENT_RUNNER_RFC.read_text(encoding="utf-8")
        step = re.search(
            r"(?ms)^4\. Return the test flag to off,.*?(?=^Before the isolated dispatch/fault matrix)",
            rfc,
        )
        self.assertIsNotNone(step)
        step_text = step.group(0)
        self.assertIn("runtime_dispatch", step_text)
        self.assertIn("THREAD_EVENT_RUNNER_DISPATCH_EXACT_THREAD=1", step_text)
        self.assertIn("explicitly approved project pilot", step_text)
        self.assertIn("one exact", step_text)

    def test_tracked_runtime_paths_have_no_unapproved_consumers(self) -> None:
        self.assertEqual(
            RUNTIME_ROOTS,
            ("bin/", "scripts/", "tools/", "pm2/", "llm_collab/", "tests/"),
        )
        self.assertEqual(
            SANCTIONED_CONSUMERS,
            {
                "llm_collab/canonical/control.py",
                "llm_collab/daemon/gate.py",
                "tests/test_collabd_canonical.py",
                "tests/test_collabd_gate.py",
            },
        )
        self.assertIn(
            DECLARATION_ID,
            (ROOT / "llm_collab" / "daemon" / "gate.py").read_text(),
        )
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        matches = feature_declaration_matches(
            tuple(raw_path.decode("utf-8") for raw_path in tracked if raw_path),
            {},
        )

        self.assertEqual(matches, [], "standalone declaration gained an unapproved consumer")

    def test_unsanctioned_feature_consumer_still_fails_guard(self) -> None:
        matches = feature_declaration_matches(
            (
                THIS_TEST,
                "llm_collab/canonical/control.py",
                "llm_collab/daemon/gate.py",
                "tests/test_collabd_canonical.py",
                "tests/test_collabd_gate.py",
                "llm_collab/unapproved_consumer.py",
            ),
            {"llm_collab/unapproved_consumer.py": b"canonical_writes = True"},
        )
        self.assertEqual(
            matches,
            ["llm_collab/unapproved_consumer.py: feature canonical_writes"],
        )

    def test_prose_feature_mentions_are_outside_runtime_scan(self) -> None:
        matches = feature_declaration_matches(
            (
                "README.md",
                "docs/schema-reference.md",
                "docs/standalone-agent-session-bus-architecture.md",
            ),
            {
                "README.md": b"canonical_writes",
                "docs/schema-reference.md": b"runtime_dispatch",
                "docs/standalone-agent-session-bus-architecture.md": DECLARATION_ID.encode("utf-8"),
            },
        )
        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
