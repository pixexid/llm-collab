"""Contract checks for the inert standalone V1 feature declaration."""

from __future__ import annotations

import json
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
THIS_TEST = "tests/test_standalone_feature_declarations.py"


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def load_declaration() -> dict[str, object]:
    return json.loads(
        DECLARATION_PATH.read_text(encoding="utf-8"),
        parse_constant=reject_json_constant,
    )


class StandaloneFeatureDeclarationTests(unittest.TestCase):
    def test_declaration_parses_as_strict_json(self) -> None:
        self.assertIsInstance(load_declaration(), dict)

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

    def test_tracked_runtime_paths_have_zero_consumers(self) -> None:
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        needles = {
            "declaration path": str(DECLARATION_PATH.relative_to(ROOT)),
            "declaration id": DECLARATION_ID,
            **{f"feature {feature}": feature for feature in FEATURES},
        }
        matches: list[str] = []

        for raw_path in tracked:
            if not raw_path:
                continue
            relative_path = raw_path.decode("utf-8")
            if relative_path == THIS_TEST or relative_path.startswith(
                EXCLUDED_PREFIXES
            ):
                continue
            path = ROOT / relative_path
            if not path.is_file():
                continue
            content = path.read_bytes()
            for label, needle in needles.items():
                if needle.encode("utf-8") in content:
                    matches.append(f"{relative_path}: {label}")

        self.assertEqual(matches, [], "standalone declaration gained a consumer")


if __name__ == "__main__":
    unittest.main()
