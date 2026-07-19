from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DELIVER = REPO_ROOT / "bin" / "deliver.py"
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _activation_identity as ident


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_json(path: Path, payload: dict) -> None:
    write(path, json.dumps(payload, indent=2))


class IdentityUnitTest(unittest.TestCase):
    def base_identity(self, worktree: str) -> dict[str, str]:
        return {
            "project": "amiga",
            "chat": "CHAT-TEST0001",
            "task": "TASK-TEST01",
            "worktree": worktree,
            "branch": "claude/gh-0000-test",
            "target_agent": "claude",
        }

    def test_canonicalization_resolves_symlinks_and_dotdot(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-worktree"
            real.mkdir()
            link = Path(tmp) / "wt-link"
            link.symlink_to(real)
            dotted = str(Path(tmp) / "sub" / ".." / "real-worktree")
            Path(tmp, "sub").mkdir()

            keys = {
                ident.lease_key(ident.lease_identity(self.base_identity(spelling)))
                for spelling in (str(real), str(link), dotted)
            }
            self.assertEqual(1, len(keys), "every spelling must derive one identity")

    def test_missing_field_raises_with_flag_name(self):
        broken = self.base_identity("/tmp/wt")
        broken["task"] = ""
        with self.assertRaises(ValueError) as ctx:
            ident.lease_identity(broken)
        self.assertIn("--task", str(ctx.exception))

    def test_lease_key_is_stable_and_field_sensitive(self):
        a = ident.lease_identity(self.base_identity("/tmp/wt"))
        self.assertEqual(ident.lease_key(a), ident.lease_key(dict(a)))
        b = dict(a)
        b["branch"] = "other"
        self.assertNotEqual(ident.lease_key(a), ident.lease_key(b))

    def test_classifier_none_activation_malformed(self):
        plain = {"project_id": "amiga", "chat_id": "CHAT-TEST0001", "related_task": "TASK-1"}
        self.assertEqual(("none", None), ident.classify_activation(plain, target_agent="claude"))

        complete = {
            **plain,
            "activation": True,
            "worktree": "/tmp/wt",
            "branch": "b",
        }
        verdict, identity = ident.classify_activation(complete, target_agent="claude")
        self.assertEqual("activation", verdict)
        self.assertEqual("claude", identity["target_agent"])

        partial = {**plain, "activation": True}
        verdict, detail = ident.classify_activation(partial, target_agent="claude")
        self.assertEqual("malformed", verdict)
        self.assertIn("--worktree", detail["detail"])

        marker_only = {**plain, "branch": "b"}
        verdict, _ = ident.classify_activation(marker_only, target_agent="claude")
        self.assertEqual("malformed", verdict, "any marker without full identity is malformed")

    def test_classifier_uses_marker_presence_not_truthiness(self):
        """BLOCK P1.1: falsy marker VALUES are activation-shaped packets with
        a broken identity — malformed, never (none)."""
        plain = {"project_id": "amiga", "chat_id": "CHAT-TEST0001", "related_task": "TASK-1"}
        for marker in (
            {"activation": False},
            {"activation": None},
            {"worktree": ""},
            {"branch": None},
        ):
            verdict, _ = ident.classify_activation({**plain, **marker}, target_agent="claude")
            self.assertEqual("malformed", verdict, f"marker {marker} must not downgrade")

    def test_classifier_marks_relative_worktree_malformed_cwd_independent(self):
        """BLOCK P1.2: a relative worktree in frontmatter must classify
        malformed identically from any CWD — never resolve against the
        consumer's CWD."""
        fm = {
            "project_id": "amiga", "chat_id": "CHAT-TEST0001",
            "related_task": "TASK-1", "activation": True,
            "worktree": "rel/path", "branch": "b",
        }
        verdicts = []
        old_cwd = os.getcwd()
        try:
            for cwd in ("/tmp", "/"):
                os.chdir(cwd)
                verdicts.append(ident.classify_activation(fm, target_agent="claude"))
        finally:
            os.chdir(old_cwd)
        for verdict, detail in verdicts:
            self.assertEqual("malformed", verdict)
            self.assertIn("absolute", detail["detail"])
        self.assertEqual(verdicts[0], verdicts[1], "CWD must not influence the verdict")


class PromptBuilderTest(unittest.TestCase):
    def test_command_is_absolute_exact_and_placeholder_free(self):
        command = ident.build_activation_consume_command("claude", "amiga", "p.md")
        self.assertTrue(command.startswith("/"), "absolute launcher")
        self.assertIn("--packet p.md", command)
        self.assertNotIn("<", command)
        self.assertNotIn("--session", command)

    def test_ring_prompt_tiers_stay_bounded_with_exact_command(self):
        packet = "2026-07-19T03-34-03_to-claude_gh-1563-attended-probe-activation-one.md"
        long_root = "/Users/pixexid/Projects/llm-collab-worktrees/claude/t-6a1eae-gh-1563-activation-lease"
        command = f"{long_root}/bin/llm-collab inbox.py --me claude --project amiga --packet {packet}"
        first_tier = (
            f"[from codex] ACTIVATION TASK-PROBE63: claim via `{command}` — do not Read the packet file."
        )
        self.assertGreater(len(first_tier), ident.AX_DOORBELL_MAX_CHARS)

        prompt = ident.build_activation_ring_prompt("codex", "TASK-PROBE63", command)
        self.assertLessEqual(len(prompt), ident.AX_DOORBELL_MAX_CHARS)
        self.assertIn(f"`{command}`", prompt)
        self.assertIn(packet, prompt)

    def test_command_is_shell_safe_for_spaced_workspace_roots(self):
        """BLOCK P2: a workspace root containing spaces must serialize to a
        runnable, shell-safe command with the exact packet selector intact."""
        import shlex
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            spaced_root = Path(tmp) / "collab root"
            spaced_root.mkdir()
            with mock.patch.object(ident, "ROOT", spaced_root):
                command = ident.build_activation_consume_command("claude", "amiga", "p one.md")
        argv = shlex.split(command)
        self.assertEqual(str(spaced_root / "bin" / "llm-collab"), argv[0])
        self.assertEqual("p one.md", argv[argv.index("--packet") + 1])
        prompt = ident.build_activation_ring_prompt("codex", "TASK-X", command)
        self.assertLessEqual(len(prompt), ident.AX_DOORBELL_MAX_CHARS)
        self.assertIn(f"`{command}`", prompt)

    def test_ring_prompt_raises_when_command_cannot_fit(self):
        oversized = "/very/long" * 30 + "/bin/llm-collab inbox.py --packet x.md"
        with self.assertRaises(ValueError) as ctx:
            ident.build_activation_ring_prompt("codex", "TASK-X", oversized)
        self.assertIn("cannot fit", str(ctx.exception))

    def test_banner_carries_command_without_claiming_enforcement(self):
        banner = ident.activation_body_banner("/abs/cmd --packet x.md")
        self.assertIn("`/abs/cmd --packet x.md`", banner)
        self.assertNotIn("REFUSED", banner)
        self.assertNotIn("exit 75", banner, "enforcement is a later lane; do not promise it")


class DeliverFoundationTest(unittest.TestCase):
    def make_workspace(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="llm-collab-laneA-"))
        write_json(
            root / "collab.config.json",
            {
                "workspace_name": "test-collab",
                "schema_version": 2,
                "projects_root": str(root),
                "poll_interval_seconds": 15,
                "notifications_enabled": False,
            },
        )
        write_json(
            root / "projects.json",
            {"projects": [{"id": "amiga", "display_name": "Amiga", "repos": {"app": "."}}]},
        )
        write_json(root / "agents.json", {"agents": []})
        for agent in ("codex", "claude"):
            payload = json.loads((root / "agents.json").read_text())
            payload["agents"].append(
                {"id": agent, "display_name": agent,
                 "activation": {"type": "cli_session", "watcher_enabled": False}}
            )
            write_json(root / "agents.json", payload)
            write(root / "agents" / agent / "identity.md", f"# Identity: {agent}\n")
            write_json(
                root / "agents" / agent / "inbox.json",
                {"agent": agent, "unread": [], "read": []},
            )
        chat_dir = root / "Chats" / "2026-01-01_test__CHAT-TEST0001"
        write_json(chat_dir / "meta.json", {"chat_id": "CHAT-TEST0001", "project_id": "amiga"})
        self.chat_dir = chat_dir
        worktree = root / "worktrees" / "t-test"
        worktree.mkdir(parents=True)
        self.worktree = str(worktree.resolve())
        return root

    BASE = [
        "--chat", "CHAT-TEST0001", "--from", "codex", "--to", "claude",
        "--project", "amiga", "--title", "lane a check",
    ]

    def run_deliver(
        self,
        root: Path,
        *extra: str,
        cwd: Path | None = None,
        runtime_ready: bool = True,
    ) -> subprocess.CompletedProcess:
        body = root / "b.md"
        write(body, "work")
        env = {**os.environ, "LLM_COLLAB_UI_REFRESH": "0"}
        env.pop("LLM_COLLAB_ACTIVATION_RUNTIME_READY", None)
        if runtime_ready:
            # Lane A contract testing: the delivery guard stays fail-closed
            # in production until GH-1572; tests exercise the serialization
            # contract behind the explicit readiness override.
            env["LLM_COLLAB_ACTIVATION_RUNTIME_READY"] = "1"
        return subprocess.run(
            [sys.executable, str(DELIVER), *self.BASE, "--body-file", str(body), *extra],
            cwd=cwd or root, text=True, capture_output=True, env=env, check=False,
        )

    def workspace_snapshot(self, root: Path) -> dict[str, str]:
        state = {}
        for p in sorted((root / "Chats").rglob("*")):
            if p.is_file():
                state[str(p)] = p.read_text()
        state["inbox"] = (root / "agents" / "claude" / "inbox.json").read_text()
        return state

    def test_activation_delivery_fails_closed_until_runtime_integration(self):
        """BLOCK P2: the required claim command is not runnable until GH-1572;
        the public path must refuse pre-write with an explicit diagnostic."""
        root = self.make_workspace()
        before = self.workspace_snapshot(root)
        result = self.run_deliver(
            root, "--activation", "--related-task", "TASK-TEST01",
            "--worktree", self.worktree, "--branch", "b",
            runtime_ready=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("runtime integration", result.stderr)
        self.assertIn("GH-1572", result.stderr)
        self.assertEqual(before, self.workspace_snapshot(root), "zero mutations on refusal")

    def test_whitespace_only_fields_refused_with_zero_mutations(self):
        """BLOCK P1.3: whitespace-only task/branch must exit 2 and leave the
        chat dir and inbox byte-identical."""
        root = self.make_workspace()
        before = self.workspace_snapshot(root)
        for extra in (
            ("--related-task", "   ", "--worktree", self.worktree, "--branch", "b"),
            ("--related-task", "TASK-TEST01", "--worktree", self.worktree, "--branch", "   "),
        ):
            result = self.run_deliver(root, "--activation", *extra)
            self.assertEqual(2, result.returncode, result.stdout)
            self.assertIn("activation identity requires", result.stderr)
        self.assertEqual(before, self.workspace_snapshot(root), "zero mutations on refusal")

    def test_activation_requires_full_identity(self):
        root = self.make_workspace()
        result = self.run_deliver(root, "--activation", "--related-task", "TASK-TEST01")
        self.assertEqual(2, result.returncode)
        self.assertIn("activation identity requires", result.stderr)

        result = self.run_deliver(
            root, "--activation", "--related-task", "TASK-TEST01",
            "--worktree", self.worktree,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--branch", result.stderr)

    def test_partial_identity_without_activation_flag_is_rejected(self):
        root = self.make_workspace()
        result = self.run_deliver(root, "--worktree", "/tmp/x")
        self.assertEqual(2, result.returncode)
        self.assertIn("--activation", result.stderr)

    def test_relative_worktree_refused_from_any_cwd(self):
        root = self.make_workspace()
        for cwd_name in ("cwd-a", "cwd-b"):
            (root / cwd_name).mkdir()
            result = self.run_deliver(
                root, "--activation", "--related-task", "TASK-TEST01",
                "--worktree", "worktrees/rel", "--branch", "b",
                cwd=root / cwd_name,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("absolute", result.stderr)

    def test_symlinked_worktree_serializes_one_canonical_identity(self):
        root = self.make_workspace()
        link = root / "wt-link"
        link.symlink_to(self.worktree)
        write(root / "b.md", "work")
        for i, cwd in enumerate((root, root / "worktrees")):
            result = subprocess.run(
                [
                    sys.executable, str(DELIVER),
                    "--chat", "CHAT-TEST0001", "--from", "codex", "--to", "claude",
                    "--project", "amiga", "--title", f"canon {i}",
                    "--body-file", str(root / "b.md"),
                    "--activation", "--related-task", "TASK-TEST01",
                    "--worktree", str(link), "--branch", "b",
                ],
                cwd=cwd, text=True, capture_output=True,
                env={
                    **os.environ,
                    "LLM_COLLAB_UI_REFRESH": "0",
                    "LLM_COLLAB_ACTIVATION_RUNTIME_READY": "1",
                },
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
        worktrees = set()
        for p in sorted(self.chat_dir.glob("*_to-claude_canon-*.md")):
            for line in p.read_text().splitlines():
                if line.startswith("worktree:"):
                    worktrees.add(line.split(":", 1)[1].strip())
        self.assertEqual({self.worktree}, worktrees, "canonical resolved path, symlink gone")

    def test_activation_packet_carries_banner_with_exact_command(self):
        root = self.make_workspace()
        result = self.run_deliver(
            root, "--activation", "--related-task", "TASK-TEST01",
            "--worktree", self.worktree, "--branch", "b",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        packets = sorted(self.chat_dir.glob("*_to-claude_*.md"))
        self.assertEqual(1, len(packets))
        body = packets[0].read_text()
        self.assertIn("activation: true", body)
        self.assertIn(f"worktree: {self.worktree}", body)
        command_lines = [l for l in body.splitlines() if "inbox.py" in l]
        self.assertTrue(command_lines)
        self.assertIn(f"--packet {packets[0].name}", command_lines[0])
        self.assertNotIn("<", command_lines[0].split("`")[1] if "`" in command_lines[0] else command_lines[0])
        self.assertIn(str(root / "bin" / "llm-collab"), command_lines[0])

    def test_non_activation_messages_unchanged(self):
        root = self.make_workspace()
        result = self.run_deliver(root)
        self.assertEqual(0, result.returncode, result.stderr)
        packets = sorted(self.chat_dir.glob("*_to-claude_*.md"))
        body = packets[0].read_text()
        self.assertNotIn("activation", body.split("---")[1], "no activation frontmatter")
        self.assertNotIn("ACTIVATION PACKET", body)


if __name__ == "__main__":
    unittest.main()
