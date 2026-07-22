"""Tests for the trusted Runtime Adapter manifest resolver."""

from __future__ import annotations

import ast
import copy
import inspect
import unittest
from pathlib import Path

from llm_collab.runtime_adapter_manifest import (
    ManifestResolutionError,
    ResolvedAdapter,
    UNTRUSTED_MANIFEST_INPUT,
    TrustedManifestRegistry,
    validate_initialized_identity,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_collab" / "runtime_adapter_manifest.py"
TEST_PATH = Path(__file__)


def manifest() -> dict:
    return {
        "adapter_a": {
            "adapter_id": "adapter_a",
            "adapter_revision": "rev_1",
            "manifest_id": "manifest_a",
            "manifest_revision": "manifest_rev_1",
            "endpoint": {
                "endpoint_id": "endpoint_a",
                "adapter_name": "adapter_a",
                "adapter_revision": "rev_1",
            },
            "executable": "/trusted/bin/adapter-a",
            "argv": ["adapter-a", "--stdio"],
            "working_directory": "/trusted/work",
            "environment": {"SAFE": "1"},
            "environment_allowlist": ["SAFE"],
        }
    }


class RuntimeAdapterManifestTests(unittest.TestCase):
    def assert_untrusted(self, operation) -> None:
        with self.assertRaises(ManifestResolutionError) as caught:
            operation()
        self.assertEqual(caught.exception.code, UNTRUSTED_MANIFEST_INPUT)

    def test_resolves_exact_manifest_to_opaque_adapter(self) -> None:
        resolved = TrustedManifestRegistry(manifest()).resolve("adapter_a")
        self.assertIsInstance(resolved, ResolvedAdapter)
        self.assertEqual(resolved.adapter_id, "adapter_a")
        self.assertEqual(resolved.adapter_revision, "rev_1")
        self.assertEqual(resolved.manifest_id, "manifest_a")
        self.assertEqual(resolved.manifest_revision, "manifest_rev_1")
        self.assertEqual(resolved.argv, ("adapter-a", "--stdio"))
        self.assertEqual(dict(resolved.environment), {"SAFE": "1"})
        with self.assertRaises(TypeError):
            resolved.environment["OTHER"] = "2"  # type: ignore[index]

    def test_public_resolver_signature_cannot_accept_execution_inputs(self) -> None:
        params = set(inspect.signature(TrustedManifestRegistry.resolve).parameters)
        self.assertEqual(params, {"self", "adapter_id"})
        self.assertFalse(
            params
            & {
                "executable",
                "path",
                "argv",
                "env",
                "environment",
                "working_directory",
                "workdir",
                "shell",
                "manifest_path",
                "adapter_alias",
            }
        )

    def test_six_identity_bindings_are_enforced_by_one_predicate_path(self) -> None:
        cases = {
            "trusted-key": lambda data: data.__setitem__("adapter_b", data.pop("adapter_a")),
            "adapter-id": lambda data: data["adapter_a"].__setitem__("adapter_id", "alias"),
            "endpoint-name": lambda data: data["adapter_a"]["endpoint"].__setitem__("adapter_name", "alias"),
            "adapter-revision": lambda data: data["adapter_a"].__setitem__("adapter_revision", "rev_2"),
            "endpoint-revision": lambda data: data["adapter_a"]["endpoint"].__setitem__(
                "adapter_revision", "rev_2"
            ),
        }
        for name, mutate in cases.items():
            data = manifest()
            mutate(data)
            with self.subTest(name=name):
                before = copy.deepcopy(data)
                self.assert_untrusted(lambda: TrustedManifestRegistry(data).resolve("adapter_a"))
                self.assertEqual(data, before)

    def test_initialize_identity_mismatch_is_untrusted_and_state_free(self) -> None:
        resolved = TrustedManifestRegistry(manifest()).resolve("adapter_a")
        initialized = {
            "adapter_id": "adapter_a",
            "adapter_revision": "rev_1",
            "manifest_id": "manifest_a",
            "manifest_revision": "manifest_rev_1",
            "endpoint": {
                "endpoint_id": "endpoint_a",
                "adapter_name": "adapter_a",
                "adapter_revision": "rev_1",
            },
        }
        validate_initialized_identity(resolved, initialized)
        for field, value in (
            ("adapter_id", "alias"),
            ("adapter_revision", "rev_2"),
            ("manifest_id", "manifest_b"),
            ("manifest_revision", "manifest_rev_2"),
        ):
            changed = copy.deepcopy(initialized)
            changed[field] = value
            with self.subTest(field=field):
                self.assert_untrusted(lambda: validate_initialized_identity(resolved, changed))
        changed = copy.deepcopy(initialized)
        changed["endpoint"]["adapter_name"] = "alias"
        self.assert_untrusted(lambda: validate_initialized_identity(resolved, changed))
        changed = copy.deepcopy(initialized)
        changed["endpoint"]["adapter_revision"] = "rev_2"
        self.assert_untrusted(lambda: validate_initialized_identity(resolved, changed))

    def test_unknown_environment_key_rejects_instead_of_dropping(self) -> None:
        data = manifest()
        data["adapter_a"]["environment"]["SECRET"] = "do-not-drop"
        before = copy.deepcopy(data)
        self.assert_untrusted(lambda: TrustedManifestRegistry(data).resolve("adapter_a"))
        self.assertEqual(data, before)

    def test_unknown_manifest_fields_reject_instead_of_dropping(self) -> None:
        for field in ("shell", "manifest_path", "adapter_alias", "argv_extra"):
            data = manifest()
            data["adapter_a"][field] = "do-not-drop"
            with self.subTest(field=field):
                before = copy.deepcopy(data)
                self.assert_untrusted(lambda: TrustedManifestRegistry(data).resolve("adapter_a"))
                self.assertEqual(data, before)

    def test_registry_freezes_source_manifest_snapshot(self) -> None:
        data = manifest()
        registry = TrustedManifestRegistry(data)
        data["adapter_a"]["endpoint"]["adapter_name"] = "alias"
        resolved = registry.resolve("adapter_a")
        self.assertEqual(resolved.adapter_id, "adapter_a")
        self.assertEqual(resolved.endpoint["adapter_name"], "adapter_a")

    def test_malformed_execution_facts_are_rejected_without_mutation(self) -> None:
        cases = {
            "executable": lambda data: data["adapter_a"].__setitem__("executable", ""),
            "argv": lambda data: data["adapter_a"].__setitem__("argv", []),
            "working-directory": lambda data: data["adapter_a"].__setitem__("working_directory", ""),
            "environment": lambda data: data["adapter_a"].__setitem__("environment", {"SAFE": 1}),
            "allowlist": lambda data: data["adapter_a"].__setitem__("environment_allowlist", []),
        }
        for name, mutate in cases.items():
            data = manifest()
            mutate(data)
            with self.subTest(name=name):
                before = copy.deepcopy(data)
                self.assert_untrusted(lambda: TrustedManifestRegistry(data).resolve("adapter_a"))
                self.assertEqual(data, before)

    def test_no_spawn_imports_or_calls(self) -> None:
        forbidden_imports = {"subprocess", "pty", "multiprocessing"}
        forbidden_os_imports = {"system", "popen"}
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse({alias.name.split(".", 1)[0] for alias in node.names} & forbidden_imports)
                self.assertFalse(any(alias.name == "asyncio.subprocess" for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".", 1)[0], forbidden_imports)
                self.assertNotEqual(node.module, "asyncio.subprocess")
                if node.module == "asyncio":
                    self.assertFalse(any(alias.name == "subprocess" for alias in node.names))
                if node.module == "os":
                    self.assertFalse(
                        any(
                            alias.name.startswith(("exec", "spawn")) or alias.name in forbidden_os_imports
                            for alias in node.names
                        )
                    )
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    self.assertFalse(func.value.id == "os" and func.attr.startswith(("exec", "spawn")))
                    self.assertFalse(func.value.id == "os" and func.attr in {"system", "popen"})
                if isinstance(func, ast.Name):
                    self.assertFalse(func.id.startswith(("exec", "spawn")))
                    self.assertNotIn(func.id, forbidden_os_imports)

    def test_no_forbidden_runtime_or_gate_imports(self) -> None:
        forbidden = {
            "canonical",
            "ledger",
            "compatibility",
            "daemon",
            "registry",
            "project_issue_queue",
            "inbox",
        }
        for path in (MODULE_PATH, TEST_PATH):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        if parts[0] == "llm_collab":
                            self.assertFalse(set(parts) & forbidden)
                if isinstance(node, ast.ImportFrom):
                    parts = (node.module or "").split(".")
                    if parts and parts[0] == "llm_collab":
                        self.assertFalse(set(parts) & forbidden)
                        for alias in node.names:
                            self.assertNotIn(alias.name.split(".", 1)[0], forbidden)

    def test_no_bin_consumer_imports_manifest_module(self) -> None:
        for path in (ROOT / "bin").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(alias.name == "llm_collab.runtime_adapter_manifest" for alias in node.names),
                        path,
                    )
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "llm_collab.runtime_adapter_manifest", path)
                    if node.module == "llm_collab":
                        self.assertFalse(
                            any(alias.name == "runtime_adapter_manifest" for alias in node.names),
                            path,
                        )


if __name__ == "__main__":
    unittest.main()
