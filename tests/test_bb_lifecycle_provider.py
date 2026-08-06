"""GH-563 Slice 1A: bb lifecycle provider identity contract."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from llm_collab.codex_runtime_home import bind_runtime_home
from llm_collab.codex_session_ref import SessionRefError
from llm_collab.session_lifecycle import (
    BB_SUPPORTED_OPERATIONS_JSON,
    DEFAULT_SUPPORTED_OPERATIONS_JSON,
    BbLifecycleProvider,
    CodexLifecycleProvider,
    LifecycleSubject,
    SessionLifecycleError,
    TrustedProjectRoot,
)

PROJECT = "llm-collab"


class DescriptorTest(unittest.TestCase):
    def test_descriptor_advertises_start(self):
        """The store refuses a provider without `start`.

        `reserve_managed_start` and `complete_managed_start` both validate the
        descriptor against frozenset({"start"}), so this is not a stylistic
        choice: without it, Slice 1B's reservation fails before any native call.
        """
        operations = json.loads(
            BbLifecycleProvider().descriptor()["supported_operations_json"]
        )
        self.assertIn("start", operations)

    def test_descriptor_advertises_nothing_beyond_start(self):
        """Advertising an unproven operation is a claim the store may act on.

        No caller in this slice or in TASK-A1B97C drives this provider through
        `reserve` or `inspect`, so advertising them asserted a capability that
        nothing exercises and nothing proves.
        """
        self.assertEqual(["start"], json.loads(BB_SUPPORTED_OPERATIONS_JSON))

    def test_descriptor_is_not_the_codex_attached_shape(self):
        """Codex is `["reserve","attach"]` — identity-only, cannot drive a start.

        Copying it would have produced a provider that fails at the reservation
        the whole bb lane depends on, which is why this assertion names the
        precedent rather than just checking a string.
        """
        # Read the dataclass field default rather than instantiating: the Codex
        # provider requires an exact_thread_probe, and this assertion is about
        # its declared operation set, not a live instance.
        codex_operations = set(json.loads(CodexLifecycleProvider.supported_operations_json))
        bb_operations = set(json.loads(BB_SUPPORTED_OPERATIONS_JSON))
        self.assertNotIn("start", codex_operations)
        self.assertIn("start", bb_operations)

    def test_descriptor_is_not_the_broad_fake_default(self):
        """Advertising an operation this provider does not implement is a lie.

        FakeLifecycleProvider's default set includes heartbeat/retire/open_ui;
        inheriting it by accident would claim capabilities bb does not have here.
        """
        default_operations = set(json.loads(DEFAULT_SUPPORTED_OPERATIONS_JSON))
        bb_operations = set(json.loads(BB_SUPPORTED_OPERATIONS_JSON))
        self.assertNotEqual(default_operations, bb_operations)
        self.assertEqual(set(), bb_operations - default_operations)
        for absent in ("heartbeat", "retire", "open_ui"):
            self.assertNotIn(absent, bb_operations)

    def test_descriptor_carries_every_required_field(self):
        descriptor = BbLifecycleProvider().descriptor()
        for field in (
            "provider_id",
            "provider_revision",
            "trust_class",
            "supported_operations_json",
            "challenge_algorithm",
            "challenge_ttl_seconds",
        ):
            self.assertIn(field, descriptor)
        self.assertEqual("managed", descriptor["trust_class"])

    def test_authority_is_a_native_runtime_authority(self):
        authority = BbLifecycleProvider().authority()
        self.assertEqual("native_runtime", authority.authority_kind)
        self.assertEqual("bb_managed_provider", authority.identity)


class AttestTest(unittest.TestCase):
    """AC6: each guard is proven with the OTHER input valid.

    A combined-None case cannot distinguish which guard fired, so it would still
    pass with the project-root guard deleted. Every case below varies exactly one
    input away from a pair that is proven to attest successfully.
    """

    def setUp(self) -> None:
        tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.runtime_home_dir = root / "runtime-home"
        self.runtime_home_dir.mkdir()
        self.repo = root / "repo"
        self.repo.mkdir()
        self.cwd = self.repo / "work"
        self.cwd.mkdir()
        self.runtime_home = bind_runtime_home(self.runtime_home_dir)
        self.trusted_root = TrustedProjectRoot(
            PROJECT, "repo_app", str(self.repo), str(self.cwd)
        )

    def _subject(self) -> LifecycleSubject:
        return LifecycleSubject(
            workspace_id="ws_alpha",
            scope_kind="project",
            scope_identity=PROJECT,
            conversation_id="CHAT-TEST",
            participant_id="claude",
            agent_id="claude",
            endpoint_id="endpoint_bb_one",
            native_session_id="thr_ru3nj2r8ur",
            runtime_instance_id="runtime_bb",
        )

    def _attest(self, *, runtime_home, trusted_project_root):
        return BbLifecycleProvider().attest(
            self._subject(),
            runtime_home=runtime_home,
            observed_at_utc="2026-08-06T00:00:00+00:00",
            correlation_id="corr_bb_one",
            trusted_project_root=trusted_project_root,
        )

    def test_a_valid_pair_attests(self):
        """The baseline. Without it the refusal cases below prove nothing."""
        session_ref = self._attest(
            runtime_home=self.runtime_home, trusted_project_root=self.trusted_root
        )
        self.assertEqual("thr_ru3nj2r8ur", session_ref["native_session_id"])

    def test_missing_project_root_refuses_with_a_valid_runtime_home(self):
        """A thread attested against no project root would bind to any repository."""
        with self.assertRaises(SessionLifecycleError):
            self._attest(runtime_home=self.runtime_home, trusted_project_root=None)

    def test_wrong_project_root_refuses_with_a_valid_runtime_home(self):
        """Refuse rather than widen: another project's root is not this subject's."""
        other_root = TrustedProjectRoot(
            "some-other-project", "repo_app", str(self.repo), str(self.cwd)
        )
        with self.assertRaises(SessionLifecycleError):
            self._attest(runtime_home=self.runtime_home, trusted_project_root=other_root)

    def test_missing_runtime_home_refuses_with_a_valid_project_root(self):
        """The exact typed refusal, not any exception.

        `assertRaises(Exception)` passed on an incidental AttributeError, so it
        held with the typed guard deleted — it proved that something went wrong,
        not that this guard fired.
        """
        with self.assertRaises(SessionRefError):
            self._attest(
                runtime_home=None,  # type: ignore[arg-type]
                trusted_project_root=self.trusted_root,
            )


class FailClosedTest(unittest.TestCase):
    def test_open_ui_fails_closed(self):
        """bb owns thread presentation; open_ui is not an advertised operation."""
        subject = LifecycleSubject(
            workspace_id="ws_alpha",
            scope_kind="project",
            scope_identity=PROJECT,
            conversation_id="CHAT-TEST",
            participant_id="claude",
            agent_id="claude",
            endpoint_id="endpoint_bb_one",
            native_session_id="thr_ru3nj2r8ur",
            runtime_instance_id="runtime_bb",
        )
        with self.assertRaises(SessionLifecycleError):
            BbLifecycleProvider().open_ui(subject)


if __name__ == "__main__":
    unittest.main()
