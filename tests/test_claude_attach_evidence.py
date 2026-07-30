from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from llm_collab.claude_attach_evidence import (
    ClaudeAttachEvidenceError,
    ClaudeAttachEvidenceResult,
    ClaudeAttachEvidenceValidator,
)
from llm_collab.codex_runtime_home import RuntimeHomeIdentity
from llm_collab.ledger import LedgerPaths, LedgerStore
import llm_collab.ledger.store as store_module
from llm_collab.session_lifecycle import (
    ClaudeLifecycleProvider,
    LifecycleSubject,
    SessionLifecycleCore,
    SessionLifecycleError,
    TrustedProjectRoot,
)


WORKSPACE = "ws_alpha"
PROJECT = "llm-collab"
NOW = "2026-07-30T00:00:00+00:00"
EXPIRY = "2026-07-30T00:01:00+00:00"
REGISTRY_REVISION = "sha256:" + hashlib.sha256(b"claude-provider-test").hexdigest()


class FakeSessionStartHook:
    def __init__(self, evidence):
        self.evidence = evidence
        self.reads = 0

    def read(self):
        self.reads += 1
        return dict(self.evidence)


class FakeClaudeChannel:
    def __init__(self, evidence):
        self.evidence = evidence
        self.reads = 0

    def read(self):
        self.reads += 1
        return dict(self.evidence)


def subject(**changes) -> LifecycleSubject:
    values = {
        "workspace_id": WORKSPACE,
        "scope_kind": "project",
        "scope_identity": PROJECT,
        "conversation_id": "CHAT-CLAUDE",
        "participant_id": "participant_claude",
        "agent_id": "agent_claude",
        "endpoint_id": "endpoint_claude",
        "native_session_id": "native_claude_one",
        "runtime_instance_id": "runtime_claude_one",
    }
    values.update(changes)
    return LifecycleSubject(**values)


class ClaudeAttachEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.cwd = self.repo / "work"
        self.cwd.mkdir(parents=True)
        self.other_cwd = root / "other-repo"
        self.other_cwd.mkdir()
        self.claude_home = root / "claude-home"
        self.transcript = (
            self.claude_home / "projects" / "-tmp-repo-work" / "session.jsonl"
        )
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text('{"type":"session"}\n', encoding="utf-8")
        home = str(self.claude_home.resolve())
        self.runtime_home = RuntimeHomeIdentity(
            home, hashlib.sha256(home.encode("utf-8")).hexdigest()
        )
        self.trusted_root = TrustedProjectRoot(
            PROJECT, "repo_app", str(self.repo), str(self.cwd)
        )
        self.paths = LedgerPaths.derive(root / "state", WORKSPACE)
        patcher = patch.object(
            store_module, "_linked_sqlite_version_info", return_value=(3, 51, 3)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        with LedgerStore.open_writer(self.paths) as store:
            store.record_registry_snapshot(
                workspace_id=WORKSPACE,
                registry_revision=REGISTRY_REVISION,
                registry_source_sha256=REGISTRY_REVISION.split(":", 1)[1],
                captured_at_utc=NOW,
                workspace_snapshot_json=json.dumps(
                    {"workspace_id": WORKSPACE, "projects": [PROJECT]}
                ),
                project_snapshots={PROJECT: json.dumps({"id": PROJECT})},
                source_snapshots={},
            )

    def hook(self, **changes):
        values = {
            "native_session_id": "native_claude_one",
            "cwd": str(self.cwd),
            "transcript_path": str(self.transcript),
            "transcript_sha256": hashlib.sha256(
                self.transcript.read_bytes()
            ).hexdigest(),
            "source": "startup",
            "proven_at_utc": NOW,
            "provider_revision": "revision_1",
        }
        values.update(changes)
        return FakeSessionStartHook(values)

    def channel(self, **changes):
        values = {
            "endpoint_id": "endpoint_claude",
            "runtime_instance_id": "runtime_claude_one",
        }
        values.update(changes)
        return FakeClaudeChannel(values)

    def provider(self, pairs) -> ClaudeLifecycleProvider:
        remaining = iter(pairs)

        def attach(active_subject, trusted_root, runtime_home):
            hook, channel, with_origin = next(remaining)
            origin = object()
            if with_origin:
                source = lambda: (origin, hook.read(), channel.read())
            else:
                source = lambda: (hook.read(), channel.read())
            return ClaudeAttachEvidenceValidator(
                source,
                verifier_origin=origin,
                provider_revision="revision_1",
            )(active_subject, trusted_root, runtime_home)

        return ClaudeLifecycleProvider(attach_evidence=attach)

    def provision(
        self, store: LedgerStore, provider: ClaudeLifecycleProvider
    ) -> SessionLifecycleCore:
        active_subject = subject()
        core = SessionLifecycleCore(provider, token_factory=lambda: "token-claude")
        core.register_participant(
            store,
            active_subject,
            created_at_utc=NOW,
            registry_revision=REGISTRY_REVISION,
        )
        store.register_lifecycle_provider(
            workspace_id=WORKSPACE,
            provider_descriptor=provider.descriptor(),
            created_at_utc=NOW,
        )
        return core

    def test_matching_injected_evidence_creates_one_claude_binding(self) -> None:
        first_hook, first_channel = self.hook(), self.channel()
        second_hook, second_channel = self.hook(source="resume"), self.channel()
        provider = self.provider(
            [
                (first_hook, first_channel, True),
                (second_hook, second_channel, True),
            ]
        )
        with LedgerStore.open_writer(self.paths) as store:
            core = self.provision(store, provider)
            challenge = core.reserve(
                store,
                subject(),
                runtime_home=self.runtime_home,
                created_at_utc=NOW,
                expires_at_utc=EXPIRY,
                correlation_id="corr-reserve",
                trusted_project_root=self.trusted_root,
            )
            binding = core.consume(
                store,
                subject(),
                challenge,
                runtime_home=self.runtime_home,
                consumed_at_utc=NOW,
                correlation_id="corr-consume",
                trusted_project_root=self.trusted_root,
            )
            count = store._connection.execute(
                "SELECT count(*) FROM conversation_bindings"
            ).fetchone()[0]

        self.assertEqual(1, count)
        self.assertEqual("provider_claude", binding["provider_id"])
        self.assertEqual("claude_session_start_provider", provider.authority().identity)
        self.assertEqual(1, first_hook.reads)
        self.assertEqual(1, first_channel.reads)
        self.assertEqual(1, second_hook.reads)
        self.assertEqual(1, second_channel.reads)

    def test_each_mismatch_fails_before_any_binding_row(self) -> None:
        outside = Path(self.tmp.name) / "outside.jsonl"
        outside.write_text("outside\n", encoding="utf-8")
        cases = {
            "native session": (
                self.hook(native_session_id="other-session"),
                self.channel(),
                True,
            ),
            "cwd": (self.hook(cwd=str(self.other_cwd)), self.channel(), True),
            "digest": (
                self.hook(transcript_sha256="0" * 64),
                self.channel(),
                True,
            ),
            "transcript path": (
                self.hook(
                    transcript_path=str(outside),
                    transcript_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
                ),
                self.channel(),
                True,
            ),
            "hook source": (
                self.hook(source="compact"),
                self.channel(),
                True,
            ),
            "endpoint": (
                self.hook(),
                self.channel(endpoint_id="other-endpoint"),
                True,
            ),
            "runtime instance": (
                self.hook(),
                self.channel(runtime_instance_id="other-runtime"),
                True,
            ),
            "verifier origin": (self.hook(), self.channel(), False),
        }

        with LedgerStore.open_writer(self.paths) as store:
            for label, evidence in cases.items():
                with self.subTest(label=label):
                    provider = self.provider([evidence])
                    core = self.provision(store, provider)
                    with self.assertRaises(ClaudeAttachEvidenceError):
                        core.reserve(
                            store,
                            subject(),
                            runtime_home=self.runtime_home,
                            created_at_utc=NOW,
                            expires_at_utc=EXPIRY,
                            correlation_id="corr-fail",
                            trusted_project_root=self.trusted_root,
                        )
                    self.assertEqual(
                        0,
                        store._connection.execute(
                            "SELECT count(*) FROM conversation_bindings"
                        ).fetchone()[0],
                    )

    def test_cwd_mismatch_cannot_leak_a_binding(self) -> None:
        provider = self.provider(
            [
                (self.hook(cwd=str(self.other_cwd)), self.channel(), True),
                (self.hook(cwd=str(self.other_cwd)), self.channel(), True),
            ]
        )
        with LedgerStore.open_writer(self.paths) as store:
            core = self.provision(store, provider)
            try:
                challenge = core.reserve(
                    store,
                    subject(),
                    runtime_home=self.runtime_home,
                    created_at_utc=NOW,
                    expires_at_utc=EXPIRY,
                    correlation_id="corr-reserve",
                    trusted_project_root=self.trusted_root,
                )
                core.consume(
                    store,
                    subject(),
                    challenge,
                    runtime_home=self.runtime_home,
                    consumed_at_utc=NOW,
                    correlation_id="corr-consume",
                    trusted_project_root=self.trusted_root,
                )
            except (ClaudeAttachEvidenceError, SessionLifecycleError):
                pass
            count = store._connection.execute(
                "SELECT count(*) FROM conversation_bindings"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_provider_propagates_source_failure_and_refuses_open_ui(self) -> None:
        class SourceFailure(RuntimeError):
            pass

        def fail(*_args):
            raise SourceFailure("hook unavailable")

        provider = ClaudeLifecycleProvider(attach_evidence=fail)
        with self.assertRaises(SourceFailure):
            provider.attest(
                subject(),
                runtime_home=self.runtime_home,
                observed_at_utc=NOW,
                correlation_id="corr-fail",
                trusted_project_root=self.trusted_root,
            )
        self.assertEqual('["reserve","attach"]', provider.supported_operations_json)
        with self.assertRaises(SessionLifecycleError):
            provider.open_ui(subject())

    def test_provider_normalizes_a_returned_identity_mismatch(self) -> None:
        provider = ClaudeLifecycleProvider(
            attach_evidence=lambda *_args: ClaudeAttachEvidenceResult(
                native_session_id="other-session",
                validated_cwd=str(self.cwd),
                transcript_path=str(self.transcript),
                transcript_sha256=hashlib.sha256(
                    self.transcript.read_bytes()
                ).hexdigest(),
                hook_source="startup",
                channel_endpoint_id="endpoint_claude",
                channel_runtime_instance_id="runtime_claude_one",
                proven_at_utc=NOW,
                provider_revision="revision_1",
            )
        )
        with self.assertRaises(SessionLifecycleError):
            provider.attest(
                subject(),
                runtime_home=self.runtime_home,
                observed_at_utc=NOW,
                correlation_id="corr-mismatch",
                trusted_project_root=self.trusted_root,
            )

    def test_validator_is_single_use(self) -> None:
        hook, channel, origin = self.hook(), self.channel(), object()
        validator = ClaudeAttachEvidenceValidator(
            lambda: (origin, hook.read(), channel.read()),
            verifier_origin=origin,
            provider_revision="revision_1",
        )
        validator(subject(), self.trusted_root, self.runtime_home)
        with self.assertRaisesRegex(ClaudeAttachEvidenceError, "single-use"):
            validator(subject(), self.trusted_root, self.runtime_home)


if __name__ == "__main__":
    unittest.main()
