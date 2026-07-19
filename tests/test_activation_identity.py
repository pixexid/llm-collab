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

    def test_sender_canonicalization_resolves_symlinks_and_dotdot(self):
        """Canonicalization is a SENDER-only, once-only step: every existing
        spelling of one directory resolves to one canonical path."""
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-worktree"
            real.mkdir()
            link = Path(tmp) / "wt-link"
            link.symlink_to(real)
            dotted = str(Path(tmp) / "sub" / ".." / "real-worktree")
            Path(tmp, "sub").mkdir()

            canon = {
                ident.canonical_worktree(spelling)
                for spelling in (str(real), str(link), dotted)
            }
            self.assertEqual(1, len(canon), "one canonical sender path")

    def test_sender_refuses_nonexistent_or_nondirectory_worktree(self):
        """A2 closure 1 (sender): a worktree that does not exist (or is a
        file) is refused BEFORE serialization — a not-yet-existing path could
        be created or symlink-swapped after send."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "never-created")
            with self.assertRaises(ValueError) as ctx:
                ident.canonical_worktree(missing)
            self.assertIn("existing directory", str(ctx.exception))
            a_file = Path(tmp) / "a-file"
            a_file.write_text("x")
            with self.assertRaises(ValueError) as ctx:
                ident.canonical_worktree(str(a_file))
            self.assertIn("not a directory", str(ctx.exception))

    def test_receiver_identity_is_filesystem_invariant(self):
        """A2 closure 1 (receiver): lease_identity performs NO resolution —
        the key derives from the serialized bytes only, so post-send
        creation or symlink-swap of the path cannot change it."""
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "lane-target")
            identity = self.base_identity(target)

            key_before = ident.lease_key(ident.lease_identity(identity))
            Path(target).mkdir()
            key_created = ident.lease_key(ident.lease_identity(identity))
            Path(target).rmdir()
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            Path(target).symlink_to(elsewhere)
            key_symlinked = ident.lease_key(ident.lease_identity(identity))

            self.assertEqual(key_before, key_created)
            self.assertEqual(key_before, key_symlinked,
                             "symlink swap must not move the identity")

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
        plain = {"project_id": "amiga", "chat_id": "CHAT-TEST0001", "related_task": "TASK-1", "to": "claude"}
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
        plain = {"project_id": "amiga", "chat_id": "CHAT-TEST0001", "related_task": "TASK-1", "to": "claude"}
        for marker in (
            {"activation": False},
            {"activation": None},
            {"worktree": ""},
            {"branch": None},
        ):
            verdict, _ = ident.classify_activation({**plain, **marker}, target_agent="claude")
            self.assertEqual("malformed", verdict, f"marker {marker} must not downgrade")

    def test_activation_value_must_be_exactly_true_even_with_full_identity(self):
        """Round-1 P2: activation:false/null alongside a COMPLETE valid
        identity is still malformed — only true marks a writer packet."""
        full = {
            "project_id": "amiga", "chat_id": "CHAT-TEST0001", "to": "claude",
            "related_task": "TASK-1", "worktree": "/tmp/wt", "branch": "b",
        }
        for value in (False, None, "true", 1):
            verdict, detail = ident.classify_activation(
                {**full, "activation": value}, target_agent="claude"
            )
            self.assertEqual("malformed", verdict, f"activation={value!r}")
            self.assertIn("exactly true", detail["detail"])
        verdict, _ = ident.classify_activation(
            {**full, "activation": True}, target_agent="claude"
        )
        self.assertEqual("activation", verdict)

    def test_identity_fields_refuse_control_characters(self):
        base = {
            "project": "amiga", "chat": "CHAT-TEST0001", "task": "TASK-1",
            "worktree": "/tmp/wt", "branch": "b", "target_agent": "claude",
        }
        for field, bad in (
            ("branch", "main\nproject_id: other"),
            ("task", "TASK\r1"),
            ("project", "am\tiga"),
            ("chat", "CHAT\x00X"),
            ("target_agent", "cla\x7fude"),
        ):
            with self.assertRaises(ValueError, msg=f"{field}={bad!r}") as ctx:
                ident.lease_identity({**base, field: bad})
            self.assertIn("control or line-breaking", str(ctx.exception))

    def test_unicode_line_separators_refused(self):
        """Addendum: str.splitlines() also breaks on NEL/U+2028/U+2029, so
        each is an injectable new frontmatter line. Exact repros pinned; the
        dump+parse proof shows what acceptance WOULD have injected."""
        sys.path.insert(0, str(REPO_ROOT / "bin"))
        from _helpers import dump_frontmatter, parse_frontmatter

        for sep in ("\x85", "\u2028", "\u2029"):
            payload = f"main{sep}project_id: other"
            with self.assertRaises(ValueError, msg=hex(ord(sep))) as ctx:
                ident.normalized_identity_field("branch", payload)
            self.assertIn("line-breaking", str(ctx.exception))
            # Precondition proof: had the validator accepted it, the parser
            # WOULD have materialized the injected field.
            parsed, _ = parse_frontmatter(
                dump_frontmatter({"activation": True, "branch": payload}, "x")
            )
            self.assertEqual("other", parsed.get("project_id"))

    def test_ordinary_non_ascii_printable_text_accepted(self):
        self.assertEqual(
            "naïve-brançh–日本",
            ident.normalized_identity_field("branch", "naïve-brançh–日本"),
        )

    def test_home_relative_packet_worktree_malformed_under_any_home(self):
        """PR113 round-2 P2.1: a raw packet worktree of `~/lane` must classify
        malformed identically regardless of the consumer's HOME — it can
        never produce a valid (let alone divergent) lease key."""
        fm = {
            "project_id": "amiga", "chat_id": "CHAT-TEST0001", "to": "claude",
            "related_task": "TASK-1", "activation": True,
            "worktree": "~/lane", "branch": "b",
        }
        verdicts = []
        old_home = os.environ.get("HOME")
        try:
            for fake_home in ("/tmp/home-a", "/tmp/home-b"):
                os.environ["HOME"] = fake_home
                verdicts.append(ident.classify_activation(fm, target_agent="claude"))
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        for verdict, detail in verdicts:
            self.assertEqual("malformed", verdict)
            self.assertIn("absolute", detail["detail"])
        self.assertEqual(verdicts[0], verdicts[1], "HOME must not influence the verdict")

        dotted = {**fm, "worktree": "../lane"}
        verdict, detail = ident.classify_activation(dotted, target_agent="claude")
        self.assertEqual("malformed", verdict)
        self.assertIn("absolute", detail["detail"])

    def test_sender_cli_still_expands_home_worktree(self):
        """Sender-side expanduser convenience is preserved: canonical_worktree
        (the delivery path) expands ~ against the SENDER before serialization."""
        old_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = "/tmp/sender-home"
            Path("/tmp/sender-home/lane").mkdir(parents=True, exist_ok=True)
            self.assertEqual(
                str(Path("/tmp/sender-home/lane").resolve()),
                ident.canonical_worktree("~/lane"),
            )
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home

    def test_edge_controls_refused_before_trimming(self):
        """Edge-control BLOCK: leading/trailing controls and line breakers
        must fail closed on the ORIGINAL value — never silently stripped —
        while plain outer spaces still trim."""
        for bad in ("b\n", "\x85b", "b\u2028", "b\u2029", "\tb", "b\r", "TASK-1\r"):
            with self.assertRaises(ValueError, msg=repr(bad)) as ctx:
                ident.normalized_identity_field("branch", bad)
            self.assertIn("control or line-breaking", str(ctx.exception))
        self.assertEqual("b", ident.normalized_identity_field("branch", "  b  "))

    def test_noncanonical_absolute_spellings_cannot_split_keys(self):
        """PR114 P1: the receiver requires the serialized worktree to be its
        own lexical normal form (pure string predicate — no filesystem), so
        dotted/duplicate/trailing spellings of one directory classify
        malformed instead of deriving a second key."""
        base = {
            "project_id": "amiga", "chat_id": "CHAT-TEST0001", "to": "claude",
            "related_task": "TASK-1", "activation": True, "branch": "b",
        }
        canonical = "/work/lane"
        verdict, identity = ident.classify_activation(
            {**base, "worktree": canonical}, target_agent="claude"
        )
        self.assertEqual("activation", verdict)
        canonical_key = ident.lease_key(identity)

        for spelling in (
            "/work/lane/../lane",
            "/work/./lane",
            "/work//lane",
            "//work/lane",
            "/work/lane/",
        ):
            verdict, detail = ident.classify_activation(
                {**base, "worktree": spelling}, target_agent="claude"
            )
            self.assertEqual("malformed", verdict, spelling)
            self.assertIn("canonical lexical form", detail["detail"])
        # The invariant the finding demanded: no spelling of the same
        # directory can ever produce a DIFFERENT valid key — every
        # noncanonical spelling is malformed, the canonical one has one key.
        self.assertTrue(canonical_key)

    def test_symlink_loop_worktree_is_controlled_validation_error(self):
        """PR114 P2: a symlink-loop worktree must land on the same controlled
        pre-write refusal (ValueError family), whether the platform raises
        OSError or RuntimeError from strict resolve."""
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            b = Path(tmp) / "b"
            a.symlink_to(b)
            b.symlink_to(a)
            with self.assertRaises(ValueError) as ctx:
                ident.canonical_worktree(str(a))
            self.assertIn("existing directory", str(ctx.exception))

    def test_missing_activation_marker_with_full_identity_is_malformed(self):
        """A2 closure 2: worktree/branch markers with a complete identity but
        NO activation field must not classify as a writer grant."""
        fm = {
            "project_id": "amiga", "chat_id": "CHAT-TEST0001", "to": "claude",
            "related_task": "TASK-1", "worktree": "/tmp/wt", "branch": "b",
        }
        verdict, detail = ident.classify_activation(fm, target_agent="claude")
        self.assertEqual("malformed", verdict)
        self.assertIn("present and exactly true", detail["detail"])

    def test_recipient_binding_matrix(self):
        """A2 closure 3: serialized `to` must be a string exactly equal to
        the claiming target agent."""
        base = {
            "project_id": "amiga", "chat_id": "CHAT-TEST0001",
            "related_task": "TASK-1", "activation": True,
            "worktree": "/tmp/wt", "branch": "b",
        }
        for to_case in ({}, {"to": "codex"}, {"to": True}, {"to": None}, {"to": 7}):
            fm = {**base, **to_case}
            verdict, detail = ident.classify_activation(fm, target_agent="claude")
            self.assertEqual("malformed", verdict, f"to case {to_case!r}")
            self.assertIn("target agent", detail["detail"])
        verdict, identity = ident.classify_activation(
            {**base, "to": "claude"}, target_agent="claude"
        )
        self.assertEqual("activation", verdict)
        self.assertEqual("claude", identity["target_agent"])

    def test_classifier_marks_relative_worktree_malformed_cwd_independent(self):
        """BLOCK P1.2: a relative worktree in frontmatter must classify
        malformed identically from any CWD — never resolve against the
        consumer's CWD."""
        fm = {
            "project_id": "amiga", "chat_id": "CHAT-TEST0001", "to": "claude",
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
        command = ident.build_activation_consume_command(
            "claude", "amiga", "CHAT-TEST0001", "p.md"
        )
        self.assertTrue(command.startswith("/"), "absolute launcher")
        self.assertIn("--chat CHAT-TEST0001", command, "chat-qualified exact scope")
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
                command = ident.build_activation_consume_command(
                    "claude", "amiga", "CHAT-TEST0001", "p one.md"
                )
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

    # Internal test seam: flips the module CONSTANT in-process before calling
    # main(). The production CLI entrypoint (running deliver.py) can never do
    # this — no env var or flag reaches ACTIVATION_RUNTIME_INTEGRATED.
    WRAPPER = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import deliver; deliver.ACTIVATION_RUNTIME_INTEGRATED = True; "
        "{extra_patch}"
        "sys.argv = ['deliver.py'] + sys.argv[2:]; deliver.main()"
    )

    def run_deliver(
        self,
        root: Path,
        *extra: str,
        cwd: Path | None = None,
        runtime_ready: bool = True,
        fixed_ts: str | None = None,
    ) -> subprocess.CompletedProcess:
        body = root / "b.md"
        write(body, "work")
        env = {**os.environ, "LLM_COLLAB_UI_REFRESH": "0"}
        if runtime_ready:
            extra_patch = (
                f"deliver.ts = lambda: {fixed_ts!r}; " if fixed_ts else ""
            )
            argv = [
                sys.executable, "-c", self.WRAPPER.format(extra_patch=extra_patch),
                str(REPO_ROOT / "bin"),
                *self.BASE, "--body-file", str(body), *extra,
            ]
        else:
            argv = [sys.executable, str(DELIVER), *self.BASE, "--body-file", str(body), *extra]
        return subprocess.run(
            argv, cwd=cwd or root, text=True, capture_output=True, env=env, check=False,
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

    def test_control_characters_refused_with_zero_mutations(self):
        """Round-1 P1: a newline in --branch is a frontmatter-injection
        channel (could rewrite project_id in the emitted packet). Refused
        pre-write; workspace byte-identical."""
        root = self.make_workspace()
        before = self.workspace_snapshot(root)
        result = self.run_deliver(
            root, "--activation", "--related-task", "TASK-TEST01",
            "--worktree", self.worktree,
            "--branch", "main\nproject_id: other-project",
        )
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("control or line-breaking", result.stderr)
        self.assertEqual(before, self.workspace_snapshot(root), "zero mutations")
        for p in (root / "Chats").rglob("*.md"):
            self.assertNotIn("other-project", p.read_text())

    def deliver_same_second(self, root: Path, *, forced_nonce: bool) -> None:
        extra_patch = "deliver.ts = lambda: '2026-01-01T00-00-00'; "
        if forced_nonce:
            # Adversarial randomness: os.urandom always returns the same
            # bytes, so nonce-only naming WOULD collide — the O_EXCL +
            # attempt-counter allocation must still produce distinct names.
            extra_patch += "import os as _os; _os.urandom = lambda n: b'\\x00' * n; "
        body = root / "b.md"
        write(body, "work")
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable, "-c", self.WRAPPER.format(extra_patch=extra_patch),
                    str(REPO_ROOT / "bin"),
                    *self.BASE, "--body-file", str(body),
                    "--activation", "--related-task", "TASK-TEST01",
                    "--worktree", self.worktree, "--branch", "b",
                ],
                cwd=root, text=True, capture_output=True,
                env={**os.environ, "LLM_COLLAB_UI_REFRESH": "0"}, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)

    def assert_two_distinct_deliveries(self, root: Path) -> None:
        packets = sorted(self.chat_dir.glob("2026-01-01T00-00-00_to-claude_*.md"))
        self.assertEqual(2, len(packets), "no overwrite on same-second collision")
        sender_copies = sorted(self.chat_dir.glob("2026-01-01T00-00-00_from-codex_*.md"))
        self.assertEqual(2, len(sender_copies), "both sender copies survive")
        contents = {p.read_text() for p in packets}
        self.assertEqual(2, len(contents), "two distinct immutable packet contents")
        inbox = json.loads((root / "agents" / "claude" / "inbox.json").read_text())
        self.assertEqual(2, len(inbox["unread"]), "two distinct inbox entries")
        for p in packets:
            self.assertIn(
                f"--packet {p.name}", p.read_text(),
                "each banner command selects its own immutable packet",
            )

    def test_same_second_activations_get_distinct_packets(self):
        root = self.make_workspace()
        self.deliver_same_second(root, forced_nonce=False)
        self.assert_two_distinct_deliveries(root)

    def test_forced_repeating_nonce_still_cannot_overwrite(self):
        """Round-2 P2: with os.urandom patched to repeat, allocation must
        still produce two distinct packet pairs — randomness quality is not
        the collision defense; O_EXCL + the attempt counter is."""
        root = self.make_workspace()
        self.deliver_same_second(root, forced_nonce=True)
        self.assert_two_distinct_deliveries(root)

    def test_coercible_scalar_identity_values_refused_at_delivery(self):
        """Round-2 P1: values the YAML-lite parser would coerce (true/false/
        null/integers/brackets) cannot round-trip byte-exact; delivery must
        refuse them before mutation."""
        root = self.make_workspace()
        before = self.workspace_snapshot(root)
        for field_args in (
            ("--branch", "true"),
            ("--branch", "False"),
            ("--branch", "null"),
            ("--branch", "123"),
            ("--branch", "[main]"),
            ("--related-task", "42"),
        ):
            result = self.run_deliver(
                root, "--activation", "--related-task", "TASK-TEST01",
                "--worktree", self.worktree, "--branch", "b",
                *field_args,
            )
            self.assertEqual(2, result.returncode, f"{field_args}: {result.stdout}")
            self.assertIn("coerced", result.stderr)
        self.assertEqual(before, self.workspace_snapshot(root), "zero mutations")

    def test_lease_key_round_trips_through_serialization(self):
        """Round-2 P1: sender-side identity and the identity a receiver
        derives from the PARSED packet must produce the same lease key."""
        sys.path.insert(0, str(REPO_ROOT / "bin"))
        from _helpers import dump_frontmatter, parse_frontmatter

        sender_identity = ident.lease_identity(
            {
                "project": "amiga", "chat": "CHAT-TEST0001", "task": "TASK-TEST01",
                "worktree": "/tmp/wt-roundtrip", "branch": "claude/gh-0000-test",
                "target_agent": "claude",
            }
        )
        fm = {
            "chat_id": "CHAT-TEST0001", "project_id": "amiga", "to": "claude",
            "related_task": sender_identity["task"], "activation": True,
            "worktree": sender_identity["worktree"], "branch": sender_identity["branch"],
        }
        parsed, _ = parse_frontmatter(dump_frontmatter(fm, "body"))
        verdict, receiver_identity = ident.classify_activation(parsed, target_agent="claude")
        self.assertEqual("activation", verdict)
        self.assertEqual(
            ident.lease_key(sender_identity), ident.lease_key(receiver_identity)
        )

    def test_classifier_rejects_coerced_nonstring_identity_values(self):
        """Round-2 P1 receiver side: a hand-written packet whose identity
        field parsed into a non-str (e.g. `branch: true`) is malformed."""
        sys.path.insert(0, str(REPO_ROOT / "bin"))
        from _helpers import parse_frontmatter

        raw = "\n".join(
            [
                "---", "chat_id: CHAT-TEST0001", "project_id: amiga", "to: claude",
                "related_task: TASK-1", "activation: true",
                "worktree: /tmp/wt", "branch: true", "---", "", "x",
            ]
        )
        parsed, _ = parse_frontmatter(raw)
        self.assertIs(True, parsed["branch"], "precondition: parser coerces")
        verdict, detail = ident.classify_activation(parsed, target_agent="claude")
        self.assertEqual("malformed", verdict)
        self.assertIn("round-trip", detail["detail"])

    def test_edge_control_branches_refused_at_delivery_with_zero_mutations(self):
        """Edge-control BLOCK delivery probes: values with leading/trailing
        controls or Unicode line breakers must exit 2 and write nothing —
        never be silently trimmed into an accepted delivery."""
        root = self.make_workspace()
        before = self.workspace_snapshot(root)
        for bad_branch in ("b\n", "\x85b", "b\u2028", "b\u2029", "\tb", "b\u2029x", "\u2028b"):
            result = self.run_deliver(
                root, "--activation", "--related-task", "TASK-TEST01",
                "--worktree", self.worktree, "--branch", bad_branch,
            )
            self.assertEqual(2, result.returncode, f"{bad_branch!r}: {result.stdout}")
            self.assertIn("control or line-breaking", result.stderr)
        self.assertEqual(before, self.workspace_snapshot(root), "zero mutations")

    def test_symlink_loop_worktree_delivery_refused_without_traceback(self):
        """PR114 P2 delivery path: a real a->b->a loop refuses pre-write with
        the documented diagnostic, exit 2, zero mutations, and no traceback."""
        root = self.make_workspace()
        loop_a = Path(root) / "loop-a"
        loop_b = Path(root) / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        before = self.workspace_snapshot(root)
        result = self.run_deliver(
            root, "--activation", "--related-task", "TASK-TEST01",
            "--worktree", str(loop_a), "--branch", "b",
        )
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("existing directory", result.stderr)
        self.assertNotIn("Traceback", result.stderr, "controlled refusal, not a crash")
        self.assertEqual(before, self.workspace_snapshot(root), "zero mutations")

    def test_nonexistent_sender_worktree_refused_with_zero_mutations(self):
        """A2 closure 1 delivery path: a worktree that does not exist at
        delivery time refuses pre-write."""
        root = self.make_workspace()
        before = self.workspace_snapshot(root)
        result = self.run_deliver(
            root, "--activation", "--related-task", "TASK-TEST01",
            "--worktree", str(Path(root) / "never-created"), "--branch", "b",
        )
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("existing directory", result.stderr)
        self.assertEqual(before, self.workspace_snapshot(root), "zero mutations")

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
                    sys.executable, "-c", self.WRAPPER.format(extra_patch=""),
                    str(REPO_ROOT / "bin"),
                    "--chat", "CHAT-TEST0001", "--from", "codex", "--to", "claude",
                    "--project", "amiga", "--title", f"canon {i}",
                    "--body-file", str(root / "b.md"),
                    "--activation", "--related-task", "TASK-TEST01",
                    "--worktree", str(link), "--branch", "b",
                ],
                cwd=cwd, text=True, capture_output=True,
                env={**os.environ, "LLM_COLLAB_UI_REFRESH": "0"},
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
        worktrees = set()
        for p in sorted(self.chat_dir.glob("*_to-claude_canon-*.md")):
            for line in p.read_text().splitlines():
                if line.startswith("worktree:"):
                    worktrees.add(line.split(":", 1)[1].strip())
        self.assertEqual({self.worktree}, worktrees, "canonical resolved path, symlink gone")

    def test_cross_chat_same_basename_commands_stay_exact(self):
        """PR113 round-2 P2.2: two chats can allocate the same basename
        (forced nonce + pinned second), so the claim command must be
        chat-qualified — each packet's command differs by --chat, stays
        shell-safe, placeholder-free, and within the AX budget."""
        import shlex

        root = self.make_workspace()
        chat2 = root / "Chats" / "2026-01-02_other__CHAT-TEST0002"
        write_json(chat2 / "meta.json", {"chat_id": "CHAT-TEST0002", "project_id": "amiga"})
        body = root / "b.md"
        write(body, "work")
        extra_patch = (
            "deliver.ts = lambda: '2026-01-01T00-00-00'; "
            "import os as _os; _os.urandom = lambda n: b'\\x00' * n; "
        )
        for chat in ("CHAT-TEST0001", "CHAT-TEST0002"):
            result = subprocess.run(
                [
                    sys.executable, "-c", self.WRAPPER.format(extra_patch=extra_patch),
                    str(REPO_ROOT / "bin"),
                    "--chat", chat, "--from", "codex", "--to", "claude",
                    "--project", "amiga", "--title", "same title",
                    "--body-file", str(body),
                    "--activation", "--related-task", "TASK-TEST01",
                    "--worktree", self.worktree, "--branch", "b",
                ],
                cwd=root, text=True, capture_output=True,
                env={**os.environ, "LLM_COLLAB_UI_REFRESH": "0"}, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)

        packet1 = next(self.chat_dir.glob("2026-01-01T00-00-00_to-claude_*.md"))
        packet2 = next(chat2.glob("2026-01-01T00-00-00_to-claude_*.md"))
        self.assertEqual(packet1.name, packet2.name, "precondition: identical basenames")

        commands = []
        for packet, chat in ((packet1, "CHAT-TEST0001"), (packet2, "CHAT-TEST0002")):
            line = next(l for l in packet.read_text().splitlines() if "inbox.py" in l)
            command = line.split("`")[1]
            self.assertIn(f"--chat {chat}", command)
            self.assertIn(f"--packet {packet.name}", command)
            self.assertNotIn("<", command)
            argv = shlex.split(command)
            self.assertEqual(chat, argv[argv.index("--chat") + 1])
            prompt = ident.build_activation_ring_prompt("codex", "TASK-TEST01", command)
            self.assertLessEqual(len(prompt), ident.AX_DOORBELL_MAX_CHARS)
            commands.append(command)
        self.assertNotEqual(commands[0], commands[1], "commands differ by chat scope")

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
