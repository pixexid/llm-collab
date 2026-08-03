from __future__ import annotations

import os
os.environ.setdefault("LLM_COLLAB_RUNTIME_GATE_TEST_BYPASS", "1")  # GH-503: focused-run gate bypass (test-only)
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import deliver
import _helpers


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

    def test_codex_identity_with_opaque_app_is_not_routine_ax(self) -> None:
        codex = {
            "id": "codex",
            "activation": {"type": "cli_session", "ax_app": "ZCode"},
        }
        self.assertIsNone(deliver.ax_doorbell_app(codex))
        self.assertFalse(
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

    def test_activation_runtime_integration_is_enabled(self) -> None:
        self.assertTrue(deliver.ACTIVATION_RUNTIME_INTEGRATED)




class AxAttendedRecoveryRoutingTest(unittest.TestCase):
    """GH-1547: an AXValue-opaque cli_session AX target (activation.ax_attended_only)
    must never receive a routine AX doorbell — routing emits an explicit
    Codex-attended-recovery requirement instead, and readable targets keep the
    normal doorbell flow unchanged."""

    ZCODE = {
        "id": "zcode",
        "activation": {
            "type": "cli_session",
            "watcher_enabled": False,
            "ax_app": "ZCode",
            "ax_attended_only": True,
        },
    }

    def test_claude_is_never_a_routine_doorbell_target(self) -> None:
        # The watcher-only exclusion remains authoritative even for a malformed
        # registry entry that tries to give Claude an AX app.
        claude = {
            "id": "claude",
            "activation": {
                "type": "cli_session",
                "watcher_enabled": True,
                "ax_app": "Claude",
            },
        }
        self.assertFalse(deliver.ax_attended_only(claude))
        self.assertFalse(
            deliver.is_ax_doorbell_target(claude, "claude", sender_id="codex")
        )
        self.assertFalse(
            deliver.is_ax_attended_recovery_target(claude, "claude", sender_id="codex")
        )

    def test_custom_identity_cannot_route_to_the_claude_app(self) -> None:
        agent = {
            "id": "custom",
            "activation": {
                "type": "cli_session",
                "watcher_enabled": True,
                "ax_app": "Claude",
            },
        }
        self.assertIsNone(deliver.ax_doorbell_app(agent))
        self.assertFalse(
            deliver.is_ax_doorbell_target(agent, "custom", sender_id="codex")
        )

    def test_binary_profile_precedence_is_preserved(self) -> None:
        for app, profile in (
            ("Codex Claude", "codex"),
            ("ZCode Claude", "zcode"),
        ):
            agent = {
                "id": "custom",
                "activation": {
                    "type": "cli_session",
                    "watcher_enabled": True,
                    "ax_app": app,
                },
            }
            self.assertIsNone(deliver.ax_doorbell_app(agent))
            self.assertEqual(deliver.ax_app_profile(app), profile)

    def test_only_the_native_codex_profile_is_routine_capable(self) -> None:
        for app, expected in (
            ("Codex", True),
            ("ChatGPT", True),
            ("ZCode", False),
            ("Claude", False),
            ("Unknown Electron App", False),
        ):
            self.assertEqual(
                deliver.ax_app_supports_routine_doorbell(app),
                expected,
            )

    def test_claude_app_cannot_route_to_attended_recovery(self) -> None:
        agent = {
            "id": "custom",
            "activation": {
                "type": "cli_session",
                "watcher_enabled": True,
                "ax_app": "Claude",
                "ax_attended_only": True,
            },
        }
        self.assertFalse(
            deliver.is_ax_attended_recovery_target(
                agent, "custom", sender_id="codex"
            )
        )

    def test_unsupported_app_cannot_route_to_routine_or_attended_ax(self) -> None:
        for ax_app in ("Unknown Electron App", "", 7):
            with self.subTest(ax_app=ax_app):
                agent = {
                    "id": "custom",
                    "activation": {
                        "type": "cli_session",
                        "watcher_enabled": True,
                        "ax_app": ax_app,
                        "ax_attended_only": True,
                    },
                }
                self.assertFalse(
                    deliver.is_ax_doorbell_target(
                        agent, "custom", sender_id="codex"
                    )
                )
                self.assertFalse(
                    deliver.is_ax_attended_recovery_target(
                        agent, "custom", sender_id="codex"
                    )
                )

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

    def test_only_codex_identity_keeps_the_routine_doorbell(self) -> None:
        codex = {
            "id": "codex",
            "activation": {
                "type": "cli_session",
                "watcher_enabled": True,
                "ax_app": "Codex",
            },
        }
        relay = {
            "id": "relay",
            "activation": {
                "type": "cli_session",
                "watcher_enabled": True,
                "ax_app": "Codex",
            },
        }
        self.assertTrue(deliver.is_ax_doorbell_target(codex, "codex", sender_id="zcode"))
        self.assertFalse(deliver.is_ax_doorbell_target(relay, "relay", sender_id="zcode"))
        self.assertTrue(deliver.is_watcher_only_target(relay, "relay"))

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
        # zcode is now legacy_disabled_implementation (no AX route): not an
        # attended-recovery target.
        self.assertFalse(
            deliver.is_ax_attended_recovery_target(
                agents["zcode"], "zcode", sender_id="codex"
            )
        )
        self.assertFalse(
            deliver.is_ax_attended_recovery_target(
                agents["kimi"], "kimi", sender_id="codex"
            )
        )

    def test_live_registry_marks_antigravity_attended_and_zcode_disabled(self) -> None:
        import json as _json

        agents = {
            a["id"]: a
            for a in _json.loads((REPO_ROOT / "agents.json").read_text())["agents"]
        }
        # zcode is now legacy_disabled_implementation (no AX route).
        self.assertFalse(deliver.ax_attended_only(agents["zcode"]))
        self.assertTrue(deliver.ax_attended_only(agents["antigravity"]))
        self.assertFalse(deliver.ax_attended_only(agents["codex"]))
        self.assertFalse(deliver.ax_attended_only(agents["claude"]))
        # Primary acceptance: zcode accepts no new work; glmpi/relay/kimi stay enabled.
        self.assertTrue(_helpers.is_agent_disabled(agents["zcode"]))
        self.assertFalse(_helpers.is_agent_disabled(agents["glmpi"]))
        self.assertFalse(_helpers.is_agent_disabled(agents["relay"]))
        self.assertFalse(_helpers.is_agent_disabled(agents["kimi"]))


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
        self.assertEqual(
            [agent["id"] for agent in agents if agent.get("activation", {}).get("ax_app")],
            ["codex"],
        )
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
        self.assertEqual(checked, 1)


class AxRecoveryWordingPinTest(unittest.TestCase):
    """GH-1547 round-1 P2 pin: after fail-closed draft protection, recovery
    guidance must be CONDITIONAL (re-ring only for a proven readable+empty
    composer) — the old unconditional "the ring clears the stuck draft, re-ring"
    instruction is no longer executable (routine ring refuses with exit 11)."""

    AXBRIDGE = REPO_ROOT / "tools" / "axbridge"

    def test_canonical_docs_do_not_hand_author_ax_commands(self) -> None:
        import re

        docs = [REPO_ROOT / "AGENTS.md", REPO_ROOT / "README.md"]
        docs.extend((REPO_ROOT / "docs").rglob("*.md"))
        command = re.compile(r"(?:bin/)?axsend(?:-ensure)?\s+ring|\$AX\s+ring")
        for path in docs:
            self.assertIsNone(
                command.search(path.read_text()),
                f"{path.relative_to(REPO_ROOT)} hand-authors an AX command; "
                "run only the exact command printed by deliver.py",
            )

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

    def test_schema_reference_doorbell_claim_is_conditional(self) -> None:
        # The schema-reference routing note must not claim unconditionally that
        # ax_app => ax_doorbell_required: an unresolvable/undriveable target
        # (ax_attended_only) emits attended recovery. GH-470: it must NOT claim a
        # value-opaque/non-empty composer holds, and must state Codex is the only
        # routine target (others fail closed).
        schema = (REPO_ROOT / "docs" / "schema-reference.md").read_text()
        self.assertIn("ATTENDED RECOVERY REQUIRED", schema)
        self.assertIn("GH-470", schema)
        self.assertIn("Codex is the only routine doorbell target", schema)
        # An unrecognized ax_app must be documented as fail-closed, not attended.
        self.assertIn("does NOT get attended recovery", schema)
        # The old "value-opaque composer => hold" framing must be gone.
        self.assertNotIn("Unknown and Claude profiles fail closed", schema)

    AUTHORITATIVE_ROUTING_DOCS = (
        "README.md",
        "docs/getting-started.md",
        "docs/multi-project.md",
        "docs/schema-reference.md",
        "docs/workflows/task-intake-and-delegation.md",
        "bin/deliver.py",
    )

    def test_routing_family_docs_carry_attended_qualification(self) -> None:
        # GH-1547 final docs-sync amendment: any authoritative current-contract
        # surface that describes the ax_app -> doorbell routing must also carry
        # the ax_attended_only qualification, so the unconditional-claim family
        # cannot drift back in.
        import re

        for rel in self.AUTHORITATIVE_ROUTING_DOCS:
            text = (REPO_ROOT / rel).read_text()
            mentions_family = re.search(r"ax_app", text) and re.search(
                r"ax_doorbell|AX\s+doorbell|axsend-ensure ring", text
            )
            if not mentions_family:
                continue
            self.assertIn(
                "ax_attended",
                text,
                f"{rel} describes ax_app doorbell routing without the "
                "ax_attended_only qualification (GH-1547 family drift)",
            )

    def test_recovery_wording_is_gh470_not_empty_proof(self) -> None:
        # GH-470: the old "re-ring ONLY when the composer is proven readable and
        # empty" rule stranded the sender and must be gone from axsend.swift and
        # the README; recovery no longer requires a proven-empty composer.
        confirm_msg = (self.AXBRIDGE / "axsend.swift").read_text()
        self.assertNotIn("proven readable and empty", confirm_msg)
        self.assertIn("does not require a proven-empty composer", confirm_msg)
        readme = (self.AXBRIDGE / "README.md").read_text()
        self.assertNotIn("proven readable and empty", readme)

    def test_set_composer_text_uses_exact_readback_not_prefix_contains(self) -> None:
        # GH-470 P1: setComposerText success requires an EXACT readback
        # (current() == text), replacing the 20-char-prefix contains() check that
        # could false-positive on a stale same-prefix draft and submit the stale
        # pointer. The replacement is an element-targeted AXValue write (no
        # focus-dependent process-wide Cmd+A/Delete pre-clear that could clear the
        # wrong control); an ignored write fails the exact readback and falls to
        # the key-event path.
        src = (self.AXBRIDGE / "axsend.swift").read_text()
        self.assertIn("if current() == text { return true }", src)
        self.assertNotIn("current().contains(String(text.prefix(20)))", src)
        # No unconditional key-event pre-clear before the AXValue write in
        # setComposerText (avoids clearing an unverified/foreign focused control).
        body = src[src.index("func setComposerText"):]
        body = body[: body.index("\nfunc ")]
        write_at = body.index("kAXValueAttribute as CFString, text as CFString")
        first_clear = body.find("selectAllAndDelete(pid: pid)")
        self.assertTrue(first_clear == -1 or first_clear > write_at,
                        "setComposerText must not key-event-clear before the "
                        "element-targeted AXValue write (GH-470 P1 focus safety)")

    def test_attended_recovery_banner_is_target_resolution_not_value_opacity(self) -> None:
        # GH-470: the deliver.py attended-recovery banner must describe an
        # unresolvable/undriveable TARGET, not a value-opaque/non-empty composer
        # (which now proceeds). The old emptiness/value-opacity reason must be gone.
        deliver_src = (REPO_ROOT / "bin" / "deliver.py").read_text()
        self.assertNotIn("emptiness cannot be proven", deliver_src)
        self.assertNotIn("has an AXValue-opaque composer", deliver_src)
        # Contiguous source substrings (the banner is split across string lines).
        self.assertIn("resolved or verified as a safe send target", deliver_src)
        self.assertIn("target-resolution hold", deliver_src)


class AxAttendedRecoveryPrintPriorityTest(unittest.TestCase):
    """GH-1547 PR #110 P2 3609336511: the human-relay print branch must not
    shadow the attended-recovery banner. The print chain must branch on the
    computed operator_relay_required (which excludes attended-recovery targets),
    never on a raw is_human_relay() check."""

    def test_print_chain_uses_computed_relay_flag(self) -> None:
        src = (REPO_ROOT / "bin" / "deliver.py").read_text()
        self.assertIn("elif operator_relay_required:", src)
        self.assertNotIn("elif is_human_relay(recipient_agent)", src)

    def test_attended_recovery_excludes_relay_for_flagged_target(self) -> None:
        # The computed pair can never both be true for the same recipient:
        # a flagged human-relay target resolves attended-recovery, and the
        # relay flag computation excludes attended-recovery targets.
        antigravity = {
            "id": "antigravity",
            "activation": {
                "type": "human_relay",
                "watcher_enabled": False,
                "ax_attended_only": True,
            },
        }
        self.assertTrue(
            deliver.is_ax_attended_recovery_target(
                antigravity, "antigravity", sender_id="claude"
            )
        )
        self.assertTrue(deliver.is_human_relay(antigravity))


class AxBoundVsUnboundPrecedenceTest(unittest.TestCase):
    """TASK-4ECB66: prove the wake precedence the fresh-session incident hinged
    on — an exact, dispatchable Codex binding suppresses the AX doorbell (the
    runtime trigger IS the wake), while an unbound supported Codex target is
    classified for AX; scope refusal stays terminal. deliver.py computes this
    inline in main(), so the behavioural cases apply deliver's own importable
    classifier under the documented gate, and test_precedence_source_pin ties
    that gate to the real source so the truth table cannot silently drift.
    """

    CODEX = {
        "id": "codex",
        "activation": {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"},
    }

    @staticmethod
    def _ax_required(agent, agent_id, *, sender_id, autobridge_ready, dispatch_scope_refused):
        # Mirrors deliver.main(): pinned to source by test_precedence_source_pin.
        wake_fallback_allowed = not autobridge_ready and not dispatch_scope_refused
        return (
            agent_id != "operator"
            and wake_fallback_allowed
            and deliver.is_ax_doorbell_target(agent, agent_id, sender_id=sender_id)
        )

    def test_bound_dispatchable_codex_suppresses_ax(self) -> None:
        self.assertFalse(self._ax_required(
            self.CODEX, "codex", sender_id="claude",
            autobridge_ready=True, dispatch_scope_refused=False))

    def test_unbound_codex_requires_ax(self) -> None:
        self.assertTrue(self._ax_required(
            self.CODEX, "codex", sender_id="claude",
            autobridge_ready=False, dispatch_scope_refused=False))

    def test_scope_refused_is_terminal_even_when_unbound(self) -> None:
        self.assertFalse(self._ax_required(
            self.CODEX, "codex", sender_id="claude",
            autobridge_ready=False, dispatch_scope_refused=True))

    def test_precedence_source_pin(self) -> None:
        src = (REPO_ROOT / "bin" / "deliver.py").read_text()
        self.assertIn(
            "wake_fallback_allowed = not autobridge_ready and not dispatch_scope_refused",
            src,
            "wake precedence changed — update AxBoundVsUnboundPrecedenceTest to match",
        )
        # ax_doorbell_required must stay gated on wake_fallback_allowed.
        marker = "ax_doorbell_required = ("
        idx = src.index(marker)
        self.assertIn("wake_fallback_allowed", src[idx:idx + 300])


class Gh470CodexNotStrandedRoutingTest(unittest.TestCase):
    """GH-470: a Codex target with a value-opaque or non-empty composer must NOT
    be converted into an indefinite sender HOLD. deliver.py routes Codex to the
    routine AX doorbell (which the GH-470 binary proceeds through, clearing +
    overriding any composer content) and NEVER to attended recovery, because a
    Codex target is not `ax_attended_only`. Value-opacity of a resolvable Codex
    composer is not a routing hold; only an unresolvable/undriveable *target*
    (ax_attended_only) routes to attended recovery."""

    CODEX = {
        "id": "codex",
        "activation": {"type": "cli_session", "watcher_enabled": True, "ax_app": "Codex"},
    }

    def test_codex_routes_to_routine_doorbell_not_attended(self):
        self.assertFalse(deliver.ax_attended_only(self.CODEX))
        self.assertTrue(
            deliver.is_ax_doorbell_target(self.CODEX, "codex", sender_id="claude"))
        self.assertFalse(
            deliver.is_ax_attended_recovery_target(self.CODEX, "codex", sender_id="claude"))

    def test_only_unresolvable_target_flag_routes_to_attended(self):
        # A genuinely unresolvable/undriveable target (the flag's remaining
        # meaning) still routes to attended recovery — value-opacity does not.
        opaque_target = {
            "id": "antigravity",
            "activation": {"type": "human_relay", "ax_attended_only": True},
        }
        self.assertTrue(deliver.ax_attended_only(opaque_target))


if __name__ == "__main__":
    unittest.main()
