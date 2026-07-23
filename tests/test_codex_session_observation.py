import ast
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from llm_collab.codex_runtime_home import bind_runtime_home
from llm_collab.codex_session_observation import CodexSessionObservationError, build_observed_session_ref
from llm_collab.codex_session_ref import RepositoryBinding, SessionAuthority


MODULE = Path(__file__).resolve().parents[1] / "llm_collab" / "codex_session_observation.py"


def authority(revision="r1"):
    return SessionAuthority(
        authority_kind="native_runtime",
        identity="codex_app_server",
        implementation_revision=revision,
        capability_profile_id="codex_read_only",
        capability_profile_revision="cap_r1",
    )


class CodexSessionObservationTests(unittest.TestCase):
    def build(self, root: Path, *, project_id="proj", home_name="home", native_session_id="native-alpha", authority_revision="r1"):
        home = root / home_name
        home.mkdir()
        repo = root / f"repo-{project_id}"
        repo.mkdir(exist_ok=True)
        cwd = repo / "work"
        cwd.mkdir(exist_ok=True)
        return build_observed_session_ref(
            workspace_id="ws_alpha",
            scope={"kind": "project", "project_id": project_id},
            endpoint_id="endpoint_codex",
            native_session_id=native_session_id,
            runtime_home=bind_runtime_home(home),
            authority=authority(authority_revision),
            observed_at_utc="2026-07-23T00:00:00Z",
            correlation_id=f"corr_{project_id}",
            observation_state="idle",
            repository_binding=RepositoryBinding(project_id, "app", repo, cwd),
            observed={"host_session_id": native_session_id},
        )

    def test_builds_session_ref_from_normalized_observation(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            session_ref = self.build(Path(tmp))

        self.assertTrue(session_ref["session_ref_id"].startswith("session_"))
        self.assertEqual("native-alpha", session_ref["native_session_id"])
        self.assertEqual("proj", session_ref["scope"]["project_id"])
        self.assertEqual("exact_session_binding", session_ref["evidence"]["evidence_kind"])

    def test_observation_state_and_renderer_claims_fail_closed(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            with self.assertRaisesRegex(CodexSessionObservationError, "unknown observation state"):
                build_observed_session_ref(
                    workspace_id="ws_alpha",
                    scope={"kind": "workspace"},
                    endpoint_id="endpoint_codex",
                    native_session_id="native-beta",
                    runtime_home=bind_runtime_home(home),
                    authority=authority(),
                    observed_at_utc="2026-07-23T00:00:00Z",
                    correlation_id="corr_workspace",
                    observation_state="visible",
                )

        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            with self.assertRaisesRegex(CodexSessionObservationError, "renderer evidence"):
                build_observed_session_ref(
                    workspace_id="ws_alpha",
                    scope={"kind": "workspace"},
                    endpoint_id="endpoint_codex",
                    native_session_id="native-alpha",
                    runtime_home=bind_runtime_home(home),
                    authority=authority(),
                    observed_at_utc="2026-07-23T00:00:00Z",
                    correlation_id="corr_workspace",
                    observation_state="idle",
                    observed={"renderer_visible": True},
                )

            with self.assertRaisesRegex(CodexSessionObservationError, "unknown observed fact"):
                build_observed_session_ref(
                    workspace_id="ws_alpha",
                    scope={"kind": "workspace"},
                    endpoint_id="endpoint_codex",
                    native_session_id="native-alpha",
                    runtime_home=bind_runtime_home(home),
                    authority=authority(),
                    observed_at_utc="2026-07-23T00:00:00Z",
                    correlation_id="corr_workspace",
                    observation_state="idle",
                    observed={"untrusted": True},
                )

    def test_identity_is_not_rebound_across_two_homes_or_two_projects(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            alpha = self.build(root, project_id="alpha", home_name="home-a")
            beta_home = root / "home-b"
            beta_home.mkdir()

            with self.assertRaisesRegex(Exception, "session_ref_id mismatch"):
                repo = root / "repo-alpha"
                build_observed_session_ref(
                    workspace_id="ws_alpha",
                    scope={"kind": "project", "project_id": "alpha"},
                    endpoint_id="endpoint_codex",
                    native_session_id="native-alpha",
                    runtime_home=bind_runtime_home(beta_home),
                    authority=authority(),
                    observed_at_utc="2026-07-23T00:00:00Z",
                    correlation_id="corr_alpha",
                    observation_state="idle",
                    repository_binding=RepositoryBinding("alpha", "app", repo, repo / "work"),
                    expected_session_ref_id=alpha["session_ref_id"],
                )

        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            alpha = self.build(root, project_id="alpha", home_name="home")
            beta = self.build(root, project_id="beta", home_name="home-beta")
            self.assertNotEqual(alpha["session_ref_id"], beta["session_ref_id"])

            with self.assertRaisesRegex(Exception, "project_id mismatch"):
                repo = root / "repo-other"
                repo.mkdir()
                build_observed_session_ref(
                    workspace_id="ws_alpha",
                    scope={"kind": "project", "project_id": "alpha"},
                    endpoint_id="endpoint_codex",
                    native_session_id="native-alpha",
                    runtime_home=bind_runtime_home(root / "home"),
                    authority=authority(),
                    observed_at_utc="2026-07-23T00:00:00Z",
                    correlation_id="corr_alpha",
                    observation_state="idle",
                    repository_binding=RepositoryBinding("beta", "app", repo, repo),
                )

    def test_authority_drift_is_load_bearing(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            identity = bind_runtime_home(home)
            first = build_observed_session_ref(
                workspace_id="ws_alpha",
                scope={"kind": "workspace"},
                endpoint_id="endpoint_codex",
                native_session_id="native-alpha",
                runtime_home=identity,
                authority=authority("r1"),
                observed_at_utc="2026-07-23T00:00:00Z",
                correlation_id="corr_workspace",
                observation_state="idle",
                observed={"implementation_revision": "r1"},
            )
            second = build_observed_session_ref(
                workspace_id="ws_alpha",
                scope={"kind": "workspace"},
                endpoint_id="endpoint_codex",
                native_session_id="native-alpha",
                runtime_home=identity,
                authority=authority("r2"),
                observed_at_utc="2026-07-23T00:00:00Z",
                correlation_id="corr_workspace",
                observation_state="idle",
                observed={"implementation_revision": "r2"},
            )
            self.assertNotEqual(first["session_ref_id"], second["session_ref_id"])
            with self.assertRaisesRegex(CodexSessionObservationError, "implementation_revision mismatch"):
                build_observed_session_ref(
                    workspace_id="ws_alpha",
                    scope={"kind": "workspace"},
                    endpoint_id="endpoint_codex",
                    native_session_id="native-alpha",
                    runtime_home=identity,
                    authority=authority("r2"),
                    observed_at_utc="2026-07-23T00:00:00Z",
                    correlation_id="corr_workspace",
                    observation_state="idle",
                    observed={"implementation_revision": "r1"},
                )

    def test_native_session_project_and_cwd_drift_fail_closed(self):
        with TemporaryDirectory(dir="/tmp") as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repo = root / "repo-alpha"
            repo.mkdir()
            cwd = repo / "work"
            cwd.mkdir()
            kwargs = dict(
                workspace_id="ws_alpha",
                scope={"kind": "project", "project_id": "alpha"},
                endpoint_id="endpoint_codex",
                native_session_id="native-alpha",
                runtime_home=bind_runtime_home(home),
                authority=authority(),
                observed_at_utc="2026-07-23T00:00:00Z",
                correlation_id="corr_alpha",
                observation_state="idle",
                repository_binding=RepositoryBinding("alpha", "app", repo, cwd),
            )
            for observed, pattern in (
                ({"host_session_id": "native-other"}, "host_session_id mismatch"),
                ({"project_id": "beta"}, "project_id mismatch"),
                ({"canonical_cwd": os.path.realpath(repo)}, "canonical_cwd mismatch"),
            ):
                with self.subTest(pattern=pattern), self.assertRaisesRegex(CodexSessionObservationError, pattern):
                    build_observed_session_ref(**kwargs, observed=observed)

    def test_module_has_no_live_or_send_surface(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        banned_imports = {"socket", "subprocess", "urllib", "requests", "websocket"}
        banned_literals = {
            "turn/start",
            "thread/resume",
            "runtime" + "_" + "dispatch",
            "delivery",
            "inbox",
            "queue",
            "daemon",
            "registry",
            "initialize",
            "initialized",
            "model/list",
        }
        literals = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned_imports)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], banned_imports)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
        for literal in literals:
            for banned in banned_literals:
                self.assertNotIn(banned, literal)


if __name__ == "__main__":
    unittest.main()
