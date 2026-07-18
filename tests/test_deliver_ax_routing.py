from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import deliver


class AxDoorbellRoutingTest(unittest.TestCase):
    """PR78 R6: an unsupported cli_session recipient (no activation.ax_app) must
    NOT be treated as an AX-doorbell target, so deliver.py never emits an AX ring
    for a wake transport that fails closed (e.g. Gemini after R5 mapped it to the
    .unknown composer profile)."""

    def test_cli_session_without_ax_app_is_not_ax_doorbell(self) -> None:
        gemini = {
            "id": "gemini",
            "activation": {"type": "cli_session", "watcher_enabled": True},
        }
        self.assertIsNone(deliver.ax_doorbell_app(gemini))
        self.assertFalse(
            deliver.is_ax_doorbell_target(gemini, "gemini", sender_id="codex")
        )

    def test_cli_session_with_ax_app_is_ax_doorbell(self) -> None:
        codex = {
            "id": "codex",
            "activation": {
                "type": "cli_session",
                "watcher_enabled": True,
                "ax_app": "Codex",
            },
        }
        self.assertEqual(deliver.ax_doorbell_app(codex), "Codex")
        self.assertTrue(
            deliver.is_ax_doorbell_target(codex, "codex", sender_id="claude")
        )

    def test_codex_self_target_is_not_ax_doorbell(self) -> None:
        codex = {
            "id": "codex",
            "activation": {
                "type": "cli_session",
                "watcher_enabled": True,
                "ax_app": "Codex",
            },
        }
        self.assertTrue(deliver.is_codex_self_target("codex", "codex"))
        self.assertFalse(
            deliver.is_ax_doorbell_target(codex, "codex", sender_id="codex")
        )

    def test_human_relay_is_not_ax_doorbell(self) -> None:
        antigravity = {
            "id": "antigravity",
            "activation": {"type": "human_relay", "watcher_enabled": False},
        }
        self.assertFalse(
            deliver.is_ax_doorbell_target(
                antigravity,
                "antigravity",
                sender_id="codex",
            )
        )

    def test_blank_ax_app_is_not_ax_doorbell(self) -> None:
        agent = {
            "id": "x",
            "activation": {"type": "cli_session", "watcher_enabled": True, "ax_app": "  "},
        }
        self.assertIsNone(deliver.ax_doorbell_app(agent))
        self.assertFalse(
            deliver.is_ax_doorbell_target(agent, "x", sender_id="codex")
        )




class AxAttendedRecoveryRoutingTest(unittest.TestCase):
    """GH-1547: an AXValue-opaque cli_session AX target (activation.ax_attended_only)
    must never receive a routine AX doorbell — routing emits an explicit
    Codex-attended-recovery requirement instead, and readable targets keep the
    normal doorbell flow unchanged."""

    ZCODE = {
        "id": "zcode",
        "activation": {
            "type": "cli_session",
            "watcher_enabled": True,
            "ax_app": "ZCode",
            "ax_attended_only": True,
        },
    }

    def test_attended_only_target_is_not_routine_doorbell(self) -> None:
        self.assertTrue(deliver.ax_attended_only(self.ZCODE))
        self.assertFalse(
            deliver.is_ax_doorbell_target(self.ZCODE, "zcode", sender_id="codex")
        )

    def test_attended_only_target_routes_to_attended_recovery(self) -> None:
        self.assertTrue(
            deliver.is_ax_attended_recovery_target(
                self.ZCODE, "zcode", sender_id="codex"
            )
        )

    def test_readable_targets_keep_routine_doorbell(self) -> None:
        for agent_id, app in (("codex", "Codex"), ("claude", "Claude")):
            agent = {
                "id": agent_id,
                "activation": {
                    "type": "cli_session",
                    "watcher_enabled": True,
                    "ax_app": app,
                },
            }
            self.assertFalse(deliver.ax_attended_only(agent))
            self.assertTrue(
                deliver.is_ax_doorbell_target(agent, agent_id, sender_id="zcode")
            )
            self.assertFalse(
                deliver.is_ax_attended_recovery_target(
                    agent, agent_id, sender_id="zcode"
                )
            )

    def test_flagged_human_relay_routes_to_attended_recovery_not_operator(self) -> None:
        # GH-1547 cold-review P2: Antigravity (human_relay, ax_attended_only,
        # no ax_app) must route to Codex-attended recovery, NOT the operator
        # relay — the operator is never the routine relay for an agent Codex
        # can supervise. It still never gets a routine AX doorbell.
        antigravity = {
            "id": "antigravity",
            "activation": {
                "type": "human_relay",
                "watcher_enabled": False,
                "ax_attended_only": True,
            },
        }
        self.assertFalse(
            deliver.is_ax_doorbell_target(antigravity, "antigravity", sender_id="codex")
        )
        self.assertTrue(
            deliver.is_ax_attended_recovery_target(
                antigravity, "antigravity", sender_id="codex"
            )
        )

    def test_unflagged_human_relay_keeps_operator_relay(self) -> None:
        # An ordinary human_relay agent without the opacity flag (e.g. cdx2)
        # is untouched: no doorbell, no attended recovery — operator relay.
        cdx2 = {
            "id": "cdx2",
            "activation": {"type": "human_relay", "watcher_enabled": False},
        }
        self.assertFalse(
            deliver.is_ax_doorbell_target(cdx2, "cdx2", sender_id="codex")
        )
        self.assertFalse(
            deliver.is_ax_attended_recovery_target(cdx2, "cdx2", sender_id="codex")
        )

    def test_live_registry_antigravity_routes_attended_not_relay(self) -> None:
        import json as _json

        agents = {
            a["id"]: a
            for a in _json.loads((REPO_ROOT / "agents.json").read_text())["agents"]
        }
        self.assertTrue(
            deliver.is_ax_attended_recovery_target(
                agents["antigravity"], "antigravity", sender_id="codex"
            )
        )
        self.assertTrue(
            deliver.is_ax_attended_recovery_target(
                agents["zcode"], "zcode", sender_id="codex"
            )
        )

    def test_live_registry_marks_zcode_and_antigravity_attended_only(self) -> None:
        import json as _json

        agents = {
            a["id"]: a
            for a in _json.loads((REPO_ROOT / "agents.json").read_text())["agents"]
        }
        self.assertTrue(deliver.ax_attended_only(agents["zcode"]))
        self.assertTrue(deliver.ax_attended_only(agents["antigravity"]))
        self.assertFalse(deliver.ax_attended_only(agents["codex"]))
        self.assertFalse(deliver.ax_attended_only(agents["claude"]))


class AxRegistryBinaryAgreementTest(unittest.TestCase):
    """GH-1547 agreement fixture: the agents.json ax_attended_only hints must
    agree with the axsend binary's composer opacity table. The Swift table keeps
    one case per line with an `// ax-readable` / `// ax-opaque` marker exactly so
    this fixture can parse it deterministically."""

    def _swift_opacity(self) -> dict[str, bool]:
        src = (REPO_ROOT / "tools" / "axbridge" / "send-resolution.swift").read_text()
        table: dict[str, bool] = {}
        for line in src.splitlines():
            stripped = line.strip()
            if not stripped.startswith("case ."):
                continue
            if "// ax-readable" in stripped:
                readable = True
            elif "// ax-opaque" in stripped:
                readable = False
            else:
                continue
            profile = stripped.split(".", 1)[1].split(":", 1)[0].strip()
            table[profile] = readable
        return table

    def test_swift_table_parses_and_covers_all_profiles(self) -> None:
        table = self._swift_opacity()
        self.assertEqual(
            table,
            {"claude": True, "codex": True, "zcode": False, "unknown": False},
        )

    def test_registry_agrees_with_binary_for_every_ax_app_agent(self) -> None:
        import json as _json

        table = self._swift_opacity()
        agents = _json.loads((REPO_ROOT / "agents.json").read_text())["agents"]
        checked = 0
        for agent in agents:
            activation = agent.get("activation", {})
            ax_app = activation.get("ax_app")
            if not ax_app:
                continue
            # Map the registry app name to the binary profile exactly the way
            # axsend does (profileFor is substring/lowercase-based).
            app = ax_app.lower()
            if "codex" in app or app == "chatgpt":
                profile = "codex"
            elif "zcode" in app:
                profile = "zcode"
            elif "claude" in app:
                profile = "claude"
            else:
                profile = "unknown"
            readable = table[profile]
            attended_only = bool(activation.get("ax_attended_only"))
            self.assertEqual(
                attended_only,
                not readable,
                f"registry/binary opacity disagreement for {agent['id']!r}: "
                f"binary says readable={readable}, registry ax_attended_only={attended_only}",
            )
            checked += 1
        self.assertGreaterEqual(checked, 3)  # codex, claude, zcode at minimum


class AxRecoveryWordingPinTest(unittest.TestCase):
    """GH-1547 round-1 P2 pin: after fail-closed draft protection, recovery
    guidance must be CONDITIONAL (re-ring only for a proven readable+empty
    composer) — the old unconditional "the ring clears the stuck draft, re-ring"
    instruction is no longer executable (routine ring refuses with exit 11)."""

    AXBRIDGE = REPO_ROOT / "tools" / "axbridge"

    def test_stale_unconditional_re_ring_wording_is_gone(self) -> None:
        for rel in ("axsend.swift", "README.md"):
            text = (self.AXBRIDGE / rel).read_text()
            for stale in (
                "reliably clears any stuck draft",
                "reliably clears any draft",
                "clears the old draft + retypes + resends",
                'reliably clears Electron drafts',
            ):
                self.assertNotIn(
                    stale, text,
                    f"{rel}: stale unconditional recovery wording {stale!r} must not return",
                )

    def test_conditional_recovery_wording_present(self) -> None:
        confirm_msg = (self.AXBRIDGE / "axsend.swift").read_text()
        self.assertIn("re-ring ONLY when the target composer is proven readable and empty", confirm_msg)
        readme = (self.AXBRIDGE / "README.md").read_text()
        self.assertIn("ONLY when the", readme)
        self.assertIn("proven readable and empty", readme)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
