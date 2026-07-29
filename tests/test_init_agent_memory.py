from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import init_agent_memory
from _helpers import build_handoff_prompt


class InitAgentMemoryPointerTest(unittest.TestCase):
    def test_existing_claude_md_section_is_replaced_and_following_user_section_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            claude_md = project_root / "CLAUDE.md"
            suffix = "## User Notes\nKeep this block byte-preserved.\n"
            claude_md.write_text(
                "# Project\n\n"
                "## Collaboration System\n\n"
                "# Check inbox\n"
                "python /path/to/_collab/bin/inbox.py --me worker\n\n"
                "# Send message\n"
                "python /path/to/_collab/bin/deliver.py --chat last --from worker --to <agent> --project <project_id> --title \"...\"\n\n"
                f"{suffix}",
                encoding="utf-8",
            )
            init_agent_memory.write_claude_md(
                "codex",
                "Primary join-skill pointer: `/tmp/skill`\nBootstrap every session: `/tmp/bootstrap`",
                str(project_root),
                True,
            )
            updated = claude_md.read_text(encoding="utf-8")
            self.assertIn("Primary join-skill pointer", updated)
            self.assertNotIn("--chat last", updated)
            self.assertNotIn("deliver.py", updated)
            self.assertTrue(updated.endswith(suffix))

    def test_generated_pointers_and_primary_getting_started_path_drop_stale_onboarding_copies(self) -> None:
        join_skill = str(init_agent_memory.collab_join_skill_path())
        bootstrap = init_agent_memory.collab_bootstrap_command("codex")
        agent = {"id": "codex", "display_name": "Codex"}

        with (
            patch.object(init_agent_memory, "get_agent", return_value=agent),
            patch.object(init_agent_memory, "config_get", return_value="demo-workspace"),
        ):
            for skill_capable in (True, False):
                with self.subTest(skill_capable=skill_capable):
                    snippet = init_agent_memory.build_snippet(
                        "codex", skill_capable=skill_capable
                    )
                    self.assertIn(join_skill, snippet)
                    self.assertIn(bootstrap, snippet)
                    self.assertNotIn("--chat last", snippet)
                    self.assertNotIn("deliver.py", snippet)
                    self.assertNotIn("agents/{id}/inbox.json", snippet)

        handoff = build_handoff_prompt(
            {"id": "codex", "display_name": "Codex", "activation": {}},
            first_time=True,
        )
        self.assertIn(join_skill, handoff)
        self.assertIn(bootstrap, handoff)
        self.assertNotIn("--chat last", handoff)
        self.assertNotIn("deliver.py", handoff)
        self.assertNotIn("agents/{id}/inbox.json", handoff)

        onboarding_docs = (
            REPO_ROOT / "docs" / "getting-started.md",
            REPO_ROOT / "docs" / "identity-system.md",
            REPO_ROOT / "docs" / "multi-project.md",
        )
        for path in onboarding_docs:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("skills/llm-collab-join/SKILL.md", text)
                self.assertNotIn("--chat last", text)

        getting_started = (REPO_ROOT / "docs" / "getting-started.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("agents/{id}/inbox.json", getting_started)


if __name__ == "__main__":
    unittest.main()
