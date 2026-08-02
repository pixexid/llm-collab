"""Fake-HTTP proof for worker_rotate_pi.

Fakes the Pi Web transport (exercising the real schema parsing) and the
session_autobridge shell-out, sharing one chronology so ordering is provable.
Covers the contract invariants:
  * register is never called before the bootstrap-ready marker is observed;
  * a native/state mismatch or malformed state closes the new tab, no register;
  * show and show-binding must describe the same active binding tuple;
  * a superseded predecessor is refused before any tab is created;
  * success reports a verified higher-generation, predecessor-superseded rebind.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import worker_rotate_pi as wr  # noqa: E402
import _helpers  # noqa: E402

PRED = "SESSION-PIWEB-RELAY-01285DE6-019FAED3"
NATIVE = "019fb3ac73ad79400db1cfd667d9b7f"
SUCCESSOR = f"SESSION-PIWEB-RELAY-01285DE6-{NATIVE[:8].upper()}"


class FakeTransport:
    def __init__(self, chronology, *, provider="zai", model_id="glm-5.2",
                 thinking="max", marker_after=1, bad_state=False, drift=False):
        self.chronology = chronology
        self.paths: list[str] = []
        self.provider, self.model_id, self.thinking = provider, model_id, thinking
        self.marker_after, self._polls, self._marker = marker_after, 0, None
        self.bad_state, self.drift, self._state_calls = bad_state, drift, 0

    def __call__(self, method, path, body):
        base = path.split("?")[0]
        self.paths.append(f"{method} {base}")
        self.chronology.append(("http", f"{method} {base}"))
        if method == "POST" and base == "/api/sessions":
            return 200, {"id": NATIVE, "cwd": "/repo", "path": "/s.jsonl"}
        if method == "POST" and base == f"/api/sessions/{NATIVE}/model":
            return 200, {"sessionId": NATIVE, "model": {"provider": self.provider, "id": self.model_id}, "thinkingLevel": self.thinking}
        if method == "POST" and base == f"/api/sessions/{NATIVE}/thinking-level":
            return 200, {"sessionId": NATIVE, "model": {"provider": self.provider, "id": self.model_id}, "thinkingLevel": self.thinking}
        if method == "GET" and base == f"/api/sessions/{NATIVE}/status":
            self._state_calls += 1
            if self.bad_state:
                return 200, {}  # missing sessionId -> KeyError
            provider = "openai" if (self.drift and self._state_calls >= 2) else self.provider
            return 200, {"sessionId": NATIVE, "model": {"provider": provider, "id": self.model_id}, "thinkingLevel": self.thinking, "isStreaming": False}
        if method == "POST" and base == f"/api/sessions/{NATIVE}/prompt":
            self._marker = re.search(r"BOOTSTRAP_READY_\S+", body["text"]).group(0)
            return 200, {"accepted": True}
        if method == "GET" and base == f"/api/sessions/{NATIVE}/messages":
            self._polls += 1
            if self.marker_after is not None and self._polls >= self.marker_after:
                self.chronology.append(("marker_ready", self._marker))
                return 200, {
                    "messages": [{"role": "assistant", "content": [{"type": "text", "text": self._marker}]}],
                    "start": 0,
                    "total": 1,
                }
            return 200, {"messages": [], "start": 0, "total": 0}
        if method == "POST" and base == f"/api/sessions/{NATIVE}/stop":
            return 200, {"stopped": True}
        raise AssertionError(f"unexpected request {method} {path}")


class FakeAutobridge:
    def __init__(self, chronology, *, pred_active=True, repo="app", register_rc=0, binding_overrides=None):
        self.chronology = chronology
        self.calls: list[list[str]] = []
        self.pred_active, self.repo, self.register_rc = pred_active, repo, register_rc
        self.binding_overrides = binding_overrides or {}
        self.registered = False
        self.successor = None

    def _show(self, status):
        return {"session_id": PRED, "agent_id": "relay", "project_id": "llm-collab", "chat_id": "CHAT-1", "repo_targets": [self.repo], "endpoint_id": "ep-1", "binding_id": "bind-1", "binding_generation": 7, "status": status, "mode": "manual", "wake_strategy": "none", "runtime": {"home": "/home", "family": "pi"}}

    def _binding(self, session=PRED, status="active", gen=7):
        return {"session_id": session, "status": status, "endpoint_id": "ep-1", "binding_id": "bind-1", "binding_generation": gen, "runtime_home": "/home", "project_id": "llm-collab", "chat_id": "CHAT-1", "agent_id": "relay", "repo_targets": [self.repo]}

    def __call__(self, args):
        self.calls.append(args)
        self.chronology.append(("autobridge", args[0]))
        if args[0] == "show":
            sid = args[args.index("--session") + 1]
            if sid != PRED:
                return 1, ""
            active = self.pred_active and not self.registered
            return 0, json.dumps(self._show("active" if active else "superseded"))
        if args[0] == "show-binding":
            if self.registered:
                return 0, json.dumps(self._binding(session=self.successor, status="active", gen=8))
            rec = self._binding()
            rec.update(self.binding_overrides)
            return 0, json.dumps(rec)
        if args[0] == "register":
            self.registered = True
            self.successor = args[args.index("--session") + 1]
            return self.register_rc, json.dumps({"ok": self.register_rc == 0})
        return 1, ""


def make_cfg():
    return argparse.Namespace(
        pi_web_url=wr.DEFAULT_PI_WEB_URL, agent="relay", project="llm-collab", chat="CHAT-1",
        repo_target="app", provider="zai", model="glm-5.2", thinking="max",
        supersedes_session=PRED, bootstrap_timeout=5.0, poll_interval=0.0, json_output=True,
    )


def _fake_clock(step=1.0):
    t = [0.0]

    def clock():
        v = t[0]
        t[0] += step
        return v

    return clock


def _run(cfg, transport, autobridge):
    return wr.rotate(
        cfg,
        piweb=wr.PiWeb(wr.DEFAULT_PI_WEB_URL, request=transport),
        run_autobridge=autobridge,
        event_path_for=lambda logical: f"/State/events/{logical}.jsonl",
        resolve_cwd=lambda project, repo: "/repo",
        sleep=lambda _s: None,
        clock=_fake_clock(),
        prepare_event=lambda p: None,
    )


class WorkerRotatePiTest(unittest.TestCase):
    def test_monitor_uses_runtime_inbox_not_state_workspace(self):
        with patch.object(_helpers, "RUNTIME_ROOT", Path("/deployed/runtime")):
            self.assertEqual(
                "/deployed/runtime/bin/inbox.py",
                wr._monitor_inbox(),
            )

    def setUp(self):
        # rotate() resolves the workspace via _helpers.config_get(); without an
        # isolated config it reads the gitignored collab.config.json (absent in a
        # clean checkout / CI) and load_config() exits. Point it at a temp config
        # so these tests are self-contained.
        import tempfile
        import _helpers
        self._cfg_tmp = tempfile.TemporaryDirectory()
        cfg_root = Path(self._cfg_tmp.name)
        (cfg_root / "collab.config.json").write_text(json.dumps(
            {"workspace_name": "ws", "projects_root": str(cfg_root), "schema_version": 2}))
        self._orig_config_file = _helpers.CONFIG_FILE
        _helpers.CONFIG_FILE = cfg_root / "collab.config.json"
        _helpers._config_cache = None
        self.addCleanup(self._cfg_tmp.cleanup)
        self.addCleanup(self._restore_config)

    def _restore_config(self):
        import _helpers
        _helpers.CONFIG_FILE = self._orig_config_file
        _helpers._config_cache = None

    def test_happy_path_registers_after_marker_and_verifies(self):
        chron: list = []
        transport, run = FakeTransport(chron), FakeAutobridge(chron)
        result = _run(make_cfg(), transport, run)

        self.assertEqual(result["successor_session"], SUCCESSOR)
        self.assertTrue(result["verified"])
        self.assertEqual(result["successor_generation"], 8)
        self.assertEqual(result["predecessor_status"], "superseded")
        self.assertIn("register", [c[0] for c in run.calls])
        self.assertFalse(any(p.startswith("DELETE") for p in transport.paths))
        marker_at = next(i for i, e in enumerate(chron) if e[0] == "marker_ready")
        register_at = next(i for i, e in enumerate(chron) if e == ("autobridge", "register"))
        self.assertLess(marker_at, register_at)

    def test_register_argv_carries_status_supersede_and_expected_fingerprint(self):
        chron: list = []
        transport, run = FakeTransport(chron), FakeAutobridge(chron)
        _run(make_cfg(), transport, run)
        reg = next(a for a in run.calls if a[0] == "register")
        self.assertEqual(reg[reg.index("--status") + 1], "active")
        self.assertEqual(reg[reg.index("--supersedes-session") + 1], PRED)
        self.assertEqual(reg[reg.index("--expect-pi-provider") + 1], "zai")
        self.assertEqual(reg[reg.index("--expect-pi-model") + 1], "glm-5.2")
        self.assertEqual(reg[reg.index("--expect-pi-thinking") + 1], "max")
        self.assertEqual(reg[reg.index("--mode") + 1], "auto-read")

    def test_model_drift_before_register_closes_tab_and_skips_register(self):
        chron: list = []
        transport, run = FakeTransport(chron, drift=True), FakeAutobridge(chron)
        with self.assertRaises(wr.RotateError):
            _run(make_cfg(), transport, run)
        self.assertNotIn("register", [c[0] for c in run.calls])
        self.assertIn(f"POST /api/sessions/{NATIVE}/stop", transport.paths)

    def test_bootstrap_timeout_closes_tab_and_skips_register(self):
        chron: list = []
        transport, run = FakeTransport(chron, marker_after=None), FakeAutobridge(chron)
        with self.assertRaises(wr.RotateError):
            _run(make_cfg(), transport, run)
        self.assertNotIn("register", [c[0] for c in run.calls])
        self.assertIn(f"POST /api/sessions/{NATIVE}/stop", transport.paths)

    def test_state_mismatch_closes_tab_and_skips_register(self):
        chron: list = []
        transport, run = FakeTransport(chron, provider="openai"), FakeAutobridge(chron)
        with self.assertRaises(wr.RotateError):
            _run(make_cfg(), transport, run)
        self.assertNotIn("register", [c[0] for c in run.calls])
        self.assertIn(f"POST /api/sessions/{NATIVE}/stop", transport.paths)

    def test_malformed_state_closes_tab_and_skips_register(self):
        chron: list = []
        transport, run = FakeTransport(chron, bad_state=True), FakeAutobridge(chron)
        with self.assertRaises(wr.RotateError):
            _run(make_cfg(), transport, run)
        self.assertNotIn("register", [c[0] for c in run.calls])
        self.assertIn(f"POST /api/sessions/{NATIVE}/stop", transport.paths)

    def test_superseded_predecessor_refused_before_any_tab(self):
        chron: list = []
        transport, run = FakeTransport(chron), FakeAutobridge(chron, pred_active=False)
        with self.assertRaises(wr.RotateError):
            _run(make_cfg(), transport, run)
        self.assertNotIn("POST /api/sessions", transport.paths)

    def test_show_and_binding_authority_divergence_refused_before_any_tab(self):
        chron: list = []
        transport = FakeTransport(chron)
        run = FakeAutobridge(chron, binding_overrides={"binding_id": "bind-2"})
        with self.assertRaises(wr.RotateError):
            _run(make_cfg(), transport, run)
        self.assertNotIn("POST /api/sessions", transport.paths)

    def test_non_loopback_url_refused(self):
        with self.assertRaises(wr.RotateError):
            wr.require_loopback("http://example.com:31415")
        self.assertEqual(wr.require_loopback("http://127.0.0.1:31415"), "http://127.0.0.1:31415")

    def test_operator_pi_web_is_the_default(self):
        self.assertEqual(wr.DEFAULT_PI_WEB_URL, "http://127.0.0.1:8504")

    def test_marker_search_pages_past_non_assistant_tail(self):
        paths = []

        def request(method, path, body):
            paths.append(path)
            if "before=12" in path:
                return 200, {
                    "messages": [{"role": "assistant", "content": "READY"}],
                    "start": 0,
                    "total": 22,
                }
            return 200, {
                "messages": [{"role": "toolResult"}] * 10,
                "start": 12,
                "total": 22,
            }

        client = wr.PiWeb(wr.DEFAULT_PI_WEB_URL, request=request)
        client._sessions[NATIVE] = {"cwd": "/repo"}
        self.assertEqual(client.last_assistant_text(NATIVE), "READY")
        self.assertIn("before=12", paths[1])


if __name__ == "__main__":
    unittest.main()
