"""GH-563 Slice 1A: bb lifecycle provider identity contract."""

from __future__ import annotations

import json
import unittest

from llm_collab.bb_lifecycle_provider import (
    BB_SUPPORTED_OPERATIONS_JSON,
    BbLifecycleProvider,
)
from llm_collab.session_lifecycle import (
    DEFAULT_SUPPORTED_OPERATIONS_JSON,
    CodexLifecycleProvider,
    LifecycleSubject,
    SessionLifecycleError,
)


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


class FailClosedTest(unittest.TestCase):
    def _subject(self) -> LifecycleSubject:
        return LifecycleSubject(
            workspace_id="ws_1",
            scope_kind="project",
            scope_identity="llm-collab",
            conversation_id="CHAT-TEST",
            participant_id="claude",
            agent_id="claude",
            endpoint_id="endpoint_1",
            native_session_id="thr_test",
            runtime_instance_id="runtime_1",
        )

    def test_open_ui_fails_closed(self):
        """bb owns thread presentation; open_ui is not an advertised operation."""
        with self.assertRaises(SessionLifecycleError):
            BbLifecycleProvider().open_ui(self._subject())

    def test_attest_refuses_without_a_trusted_project_root(self):
        """AC6: refuse rather than widen.

        A thread attested against an absent or wrong project root would bind a
        worker to someone else's repository.
        """
        with self.assertRaises(SessionLifecycleError):
            BbLifecycleProvider().attest(
                self._subject(),
                runtime_home=None,  # type: ignore[arg-type]
                observed_at_utc="2026-08-06T00:00:00+00:00",
                correlation_id="corr_1",
                trusted_project_root=None,
            )


if __name__ == "__main__":
    unittest.main()
