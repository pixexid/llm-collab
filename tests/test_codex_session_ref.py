import ast
import copy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from llm_collab.codex_runtime_home import bind_runtime_home
from llm_collab.codex_session_ref import (
    RUNTIME_HOME_ID_FIELD,
    RUNTIME_HOME_REALPATH_FIELD,
    RepositoryBinding,
    SessionAuthority,
    SessionRefError,
    build_session_ref,
    validate_session_ref,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas" / "standalone" / "v1"
MODULE = Path("llm_collab/codex_session_ref.py")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def no_network(uri: str):
    raise LookupError(f"offline standalone schema registry has no resource for {uri}")


def standalone_validator():
    catalog = load_json(SCHEMA_DIR / "index.json")
    schemas = [load_json(path) for path in SCHEMA_DIR.glob("*.schema.json")]
    resources = [(schema["$id"], Resource.from_contents(schema)) for schema in schemas]
    resources.append((catalog["catalog_id"], Resource.from_contents(catalog, default_specification=DRAFT202012)))
    registry = Registry(retrieve=no_network).with_resources(resources)
    return Draft202012Validator(
        registry.contents("https://llm-collab.dev/schemas/standalone/v1/session-ref.schema.json"),
        registry=registry,
        format_checker=FormatChecker(),
    )


def canonical_digest_without_integrity(value: dict) -> str:
    material = copy.deepcopy(value)
    material.pop("integrity", None)
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class CodexSessionRefTests(unittest.TestCase):
    def setUp(self):
        self.validator = standalone_validator()

    def authority(self):
        return SessionAuthority(
            authority_kind="native_runtime",
            identity="native_adapter",
            implementation_revision="r1",
            capability_profile_id="native_session_binding",
            capability_profile_revision="r1",
        )

    def build(self):
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        codex_home = root / "codex-home"
        codex_home.mkdir()
        repo = root / "repo"
        repo.mkdir()
        cwd = repo / "work"
        cwd.mkdir()
        identity = bind_runtime_home(codex_home)
        binding = RepositoryBinding(
            project_id="proj",
            repo_id="app",
            repo_root=repo,
            cwd=cwd,
        )
        session_ref = build_session_ref(
            workspace_id="ws_alpha",
            scope={"kind": "project", "project_id": "proj"},
            endpoint_id="endpoint_alpha",
            native_session_id="native-session-alpha",
            runtime_home=identity,
            authority=self.authority(),
            observed_at_utc="2026-07-23T00:00:00Z",
            correlation_id="corr_session_alpha",
            repository_binding=binding,
        )
        return dict(session_ref), identity, repo, cwd, binding

    def test_builds_schema_valid_session_ref_from_real_runtime_home_identity(self):
        session_ref, identity, repo, cwd, _binding = self.build()

        self.assertEqual([], list(self.validator.iter_errors(session_ref)))
        self.assertEqual(identity.runtime_home_realpath, session_ref["extensions"][RUNTIME_HOME_REALPATH_FIELD])
        self.assertEqual(identity.runtime_home_id, session_ref["extensions"][RUNTIME_HOME_ID_FIELD])
        self.assertEqual(os.path.realpath(cwd), session_ref["repository_binding"]["canonical_cwd"])
        self.assertEqual(
            {
                "endpoint_id": session_ref["endpoint_id"],
                "session_ref_id": session_ref["session_ref_id"],
                "native_session_id": session_ref["native_session_id"],
                "repository_binding": session_ref["repository_binding"],
            },
            session_ref["evidence"]["subject"],
        )
        self.assertEqual(canonical_digest_without_integrity(session_ref["evidence"]), session_ref["evidence"]["integrity"])
        self.assertTrue(session_ref["session_ref_id"].startswith("session_"))
        self.assertEqual(os.path.commonpath([os.path.realpath(repo), session_ref["repository_binding"]["canonical_cwd"]]), os.path.realpath(repo))

    def test_session_ref_id_and_integrity_are_checks_not_authority(self):
        session_ref, identity, _repo, _cwd, _binding = self.build()

        with self.assertRaisesRegex(SessionRefError, "session_ref_id mismatch"):
            build_session_ref(
                workspace_id="ws_alpha",
                scope={"kind": "project", "project_id": "proj"},
                endpoint_id="endpoint_alpha",
                native_session_id="native-session-alpha",
                runtime_home=identity,
                authority=self.authority(),
                observed_at_utc="2026-07-23T00:00:00Z",
                correlation_id="corr_session_alpha",
                expected_session_ref_id="session_wrong",
            )
        with self.assertRaisesRegex(SessionRefError, "evidence integrity mismatch"):
            build_session_ref(
                workspace_id="ws_alpha",
                scope={"kind": "workspace"},
                endpoint_id="endpoint_beta",
                native_session_id="native-session-beta",
                runtime_home=identity,
                authority=self.authority(),
                observed_at_utc="2026-07-23T00:00:00Z",
                correlation_id="corr_session_beta",
                expected_evidence_integrity=session_ref["evidence"]["integrity"],
            )

    def test_schema_and_semantic_drift_fail_closed(self):
        session_ref, identity, _repo, _cwd, binding = self.build()

        missing_required = copy.deepcopy(session_ref)
        missing_required.pop("native_session_id")
        with self.assertRaisesRegex(SessionRefError, "schema validation failed"):
            validate_session_ref(missing_required, runtime_home=identity)

        bad_subject = copy.deepcopy(session_ref)
        bad_subject["evidence"]["subject"]["native_session_id"] = "native-other"
        bad_subject["evidence"]["integrity"] = canonical_digest_without_integrity(bad_subject["evidence"])
        with self.assertRaisesRegex(SessionRefError, "subject mismatch"):
            validate_session_ref(bad_subject, runtime_home=identity)

        bad_integrity = copy.deepcopy(session_ref)
        bad_integrity["evidence"]["integrity"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(SessionRefError, "integrity mismatch"):
            validate_session_ref(bad_integrity, runtime_home=identity)

        bad_authority = copy.deepcopy(session_ref)
        bad_authority["evidence"]["authority"]["capability_profile_revision"] = "r2"
        bad_authority["evidence"]["integrity"] = canonical_digest_without_integrity(bad_authority["evidence"])
        with self.assertRaisesRegex(SessionRefError, "authority mismatch"):
            validate_session_ref(bad_authority, runtime_home=identity, authority=self.authority())

        bad_repo = copy.deepcopy(session_ref)
        bad_repo["repository_binding"]["repo_id"] = "other"
        bad_repo["evidence"]["subject"]["repository_binding"]["repo_id"] = "other"
        bad_repo["evidence"]["integrity"] = canonical_digest_without_integrity(bad_repo["evidence"])
        with self.assertRaisesRegex(SessionRefError, "repository binding mismatch"):
            validate_session_ref(bad_repo, runtime_home=identity, repository_binding=binding)

    def test_runtime_home_drift_fails_closed(self):
        session_ref, identity, _repo, _cwd, _binding = self.build()
        drifted = copy.deepcopy(session_ref)
        drifted["extensions"][RUNTIME_HOME_ID_FIELD] = "0" * 64
        with self.assertRaisesRegex(SessionRefError, "runtime_home_id mismatch"):
            validate_session_ref(drifted, runtime_home=identity)

        other_home = Path(self.tmp.name) / "other-home"
        other_home.mkdir()
        other_identity = bind_runtime_home(other_home)
        with self.assertRaisesRegex(SessionRefError, "runtime_home_realpath mismatch"):
            validate_session_ref(session_ref, runtime_home=other_identity)

    def test_repository_binding_rejects_outside_cwd_and_cross_project_alias(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            repo = root / "repo"
            repo.mkdir()
            outside = root / "outside"
            outside.mkdir()
            identity = bind_runtime_home(codex_home)
            kwargs = dict(
                workspace_id="ws_alpha",
                scope={"kind": "project", "project_id": "proj"},
                endpoint_id="endpoint_alpha",
                native_session_id="native-session-alpha",
                runtime_home=identity,
                authority=self.authority(),
                observed_at_utc="2026-07-23T00:00:00Z",
                correlation_id="corr_session_alpha",
            )
            with self.assertRaisesRegex(SessionRefError, "under repository root"):
                build_session_ref(
                    **kwargs,
                    repository_binding=RepositoryBinding("proj", "app", repo, outside),
                )
            with self.assertRaisesRegex(SessionRefError, "project_id mismatch"):
                build_session_ref(
                    **kwargs,
                    repository_binding=RepositoryBinding("other", "app", repo, repo),
                )

    def test_schema_resource_is_load_bearing(self):
        session_ref, _identity, _repo, _cwd, _binding = self.build()
        invalid = copy.deepcopy(session_ref)
        invalid["extensions"]["x_bad_field"] = "schema drift"
        errors = list(self.validator.iter_errors(invalid))
        self.assertTrue(errors)
        with self.assertRaisesRegex(SessionRefError, "schema validation failed"):
            validate_session_ref(invalid, runtime_home=_identity)

    def test_builder_schema_validation_is_production_load_bearing(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            identity = bind_runtime_home(codex_home)
            with patch("llm_collab.codex_session_ref._session_id_for", return_value="bad-session-id"):
                with self.assertRaisesRegex(SessionRefError, "schema validation failed"):
                    build_session_ref(
                        workspace_id="ws_alpha",
                        scope={"kind": "workspace"},
                        endpoint_id="endpoint_alpha",
                        native_session_id="native-session-alpha",
                        runtime_home=identity,
                        authority=self.authority(),
                        observed_at_utc="2026-07-23T00:00:00Z",
                        correlation_id="corr_session_alpha",
                    )

    def test_module_has_no_live_connection_process_or_delivery_surface(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        banned_imports = {"socket", "subprocess", "urllib", "requests", "websocket"}
        banned_literals = {
            "initialize",
            "initialized",
            "model/list",
            "thread/resume",
            "turn/send",
            "turn/steer",
            "delivery",
            "receipt",
            "autobridge",
            "_session_autobridge",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], banned_imports)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(node.value, banned_literals)


if __name__ == "__main__":
    unittest.main()
