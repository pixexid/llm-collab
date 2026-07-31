"""Profile-reader proof for worker start-pi (#271).

Fixture-based: proves the greatest-binding-generation reduction, the Pi Web
endpoint scoping, wake forced to runtime_trigger, the preserved display alias,
and the fail-closed `pi_profile_required` cases (zero eligible, conflicting
home, greatest-generation tie, incomplete-only, partial override).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import worker_rotate_pi as wr  # noqa: E402


def _rec(tmp, sid, *, agent="relay", project="llm-collab", endpoint=wr.PI_WEB_ENDPOINT,
         home="/pi", gen=1, provider="openai-codex", model="gpt-5.6-luna", thinking="high", family="pi"):
    (tmp / f"{sid}.json").write_text(json.dumps({
        "session_id": sid, "agent_id": agent, "project_id": project, "endpoint_id": endpoint,
        "binding_generation": gen, "runtime": {"family": family, "home": home},
        "pi_fingerprint": {"provider": provider, "model_id": model, "thinking_level": thinking},
    }))


class ResolvePiProfileTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())

    def test_greatest_generation_wins_and_scopes_to_pi_web(self):
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-01", gen=7, model="gpt-5.4")
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-02", gen=8, model="gpt-5.6-luna")
        # desktop endpoint + wrong family are ignored even at higher gen
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-03", gen=9, endpoint="endpoint_com_pi_gui_desktop", model="other")
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-04", gen=9, family="codex_app", model="other")
        p = wr.resolve_pi_profile("relay", "llm-collab", sessions_dir=self.tmp)
        self.assertEqual((p["provider"], p["model"], p["thinking"]), ("openai-codex", "gpt-5.6-luna", "high"))
        self.assertEqual(p["wake_strategy"], "runtime_trigger")
        self.assertEqual(p["endpoint_id"], wr.PI_WEB_ENDPOINT)
        self.assertEqual(p["display"], "RELAY")

    def test_zero_eligible_fails_closed(self):
        with self.assertRaisesRegex(wr.RotateError, "pi_profile_required"):
            wr.resolve_pi_profile("ghost", "llm-collab", sessions_dir=self.tmp)

    def test_conflicting_home_fails_closed(self):
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-01", gen=8, home="/pi-a")
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-02", gen=8, home="/pi-b")
        with self.assertRaisesRegex(wr.RotateError, "conflicting runtime homes"):
            wr.resolve_pi_profile("relay", "llm-collab", sessions_dir=self.tmp)

    def test_greatest_generation_tie_conflict_fails_closed(self):
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-01", gen=8, model="gpt-5.6-luna")
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-02", gen=8, model="gpt-5.7")
        with self.assertRaisesRegex(wr.RotateError, "conflicting tuples"):
            wr.resolve_pi_profile("relay", "llm-collab", sessions_dir=self.tmp)

    def test_override_disambiguates_but_must_be_complete(self):
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-01", gen=8, model="gpt-5.6-luna")
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-02", gen=8, model="gpt-5.7")
        p = wr.resolve_pi_profile("relay", "llm-collab", sessions_dir=self.tmp, override=("openai-codex", "gpt-5.7", "high"))
        self.assertEqual(p["model"], "gpt-5.7")
        with self.assertRaisesRegex(wr.RotateError, "override needs all"):
            wr.resolve_pi_profile("relay", "llm-collab", sessions_dir=self.tmp, override=("openai-codex", "", "high"))

    def test_other_project_higher_generation_ignored(self):
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-01", gen=8, project="llm-collab", model="gpt-5.6-luna")
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-02", gen=99, project="other", model="leaked")
        p = wr.resolve_pi_profile("relay", "llm-collab", sessions_dir=self.tmp)
        self.assertEqual(p["model"], "gpt-5.6-luna")  # the other project's gen-99 must not win

    def test_corrupt_candidate_fails_closed(self):
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-01", gen=8)
        (self.tmp / "SESSION-PIWEB-RELAY-A-02.json").write_text("{ not json")
        with self.assertRaisesRegex(wr.RotateError, "corrupt candidate"):
            wr.resolve_pi_profile("relay", "llm-collab", sessions_dir=self.tmp)

    def test_non_integer_generation_excluded(self):
        _rec(self.tmp, "SESSION-PIWEB-RELAY-A-01", gen=8, model="gpt-5.6-luna")
        (self.tmp / "SESSION-PIWEB-RELAY-A-02.json").write_text(json.dumps({
            "session_id": "SESSION-PIWEB-RELAY-A-02", "agent_id": "relay", "project_id": "llm-collab",
            "endpoint_id": wr.PI_WEB_ENDPOINT, "binding_generation": "99", "runtime": {"family": "pi", "home": "/pi"},
            "pi_fingerprint": {"provider": "openai-codex", "model_id": "stringgen", "thinking_level": "high"}}))
        p = wr.resolve_pi_profile("relay", "llm-collab", sessions_dir=self.tmp)
        self.assertEqual(p["model"], "gpt-5.6-luna")  # "99" (string) is not a candidate


import argparse  # noqa: E402
import re  # noqa: E402

NATIVE = "019fc0de11223344556677889900aabb"
PROFILE = {"endpoint_id": "endpoint_pi_web_local", "runtime_home": "/pi",
           "wake_strategy": "runtime_trigger", "provider": "zai", "model": "glm-5.2",
           "thinking": "max", "display": "GLIM"}


class _Transport:
    def __init__(self):
        self.paths = []
        self._marker = None

    def __call__(self, method, path, body):
        base = path.split("?")[0]
        self.paths.append(f"{method} {base}")
        if method == "POST" and base == "/api/tabs":
            return 201, {"ok": True, "data": {"tab": {"id": "tab-9", "cwd": "/repo", "sessionFile": "/s.jsonl"}}}
        if method == "POST" and base in ("/api/model", "/api/thinking"):
            return 200, {"success": True, "data": {"level": "max"}}
        if method == "GET" and base == "/api/state":
            return 200, {"success": True, "data": {"sessionId": NATIVE, "sessionFile": "/s.jsonl",
                         "model": {"provider": "zai", "id": "glm-5.2"}, "thinkingLevel": "max"}}
        if method == "POST" and base == "/api/prompt":
            self._marker = re.search(r"BOOTSTRAP_READY_\S+", body["message"]).group(0)
            return 200, {"success": True}
        if method == "GET" and base == "/api/last-assistant-text":
            return 200, {"success": True, "data": {"text": self._marker}}
        if method == "DELETE" and base.startswith("/api/tabs/"):
            return 200, {"ok": True, "data": {}}
        raise AssertionError(f"unexpected {method} {path}")


class _Autobridge:
    def __init__(self, register_rc=0):
        self.calls, self.register_rc, self.registered = [], register_rc, None

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "register":
            self.registered = args[args.index("--session") + 1]
            return self.register_rc, json.dumps({"ok": self.register_rc == 0})
        if args[0] == "show-binding":
            return 0, json.dumps({"session_id": self.registered, "status": "active", "binding_generation": 3})
        return 1, ""


def _cfg(**over):
    d = dict(pi_web_url="http://127.0.0.1:31415", agent="glmpi", project="llm-collab",
             chat="CHAT-NEWPROJ", repo_target="app", provider=None, model=None, thinking=None,
             bootstrap_timeout=5.0, poll_interval=0.0, json_output=True)
    d.update(over)
    return argparse.Namespace(**d)


class StartPiFlowTest(unittest.TestCase):
    def _run(self, cfg, transport, run):
        return wr.start_pi(cfg, piweb=wr.PiWeb("http://x", request=transport), run_autobridge=run,
                           event_path_for=lambda l: f"/e/{l}.jsonl", resolve_cwd=lambda p, r: "/repo",
                           resolve_profile=lambda agent, project, override=None: dict(PROFILE), prepare_event=lambda p: None,
                           sleep=lambda _s: None, clock=(lambda c=[0.0]: (c.__setitem__(0, c[0] + 1), c[0])[1]))

    def test_first_start_registers_without_supersede_and_verifies(self):
        t, run = _Transport(), _Autobridge()
        result = self._run(_cfg(), t, run)
        self.assertEqual(result["session"], f"SESSION-PIWEB-GLIM-NEWPROJ-{NATIVE[:8].upper()}")
        self.assertTrue(result["verified"])
        reg = next(a for a in run.calls if a[0] == "register")
        self.assertNotIn("--supersedes-session", reg)          # the first-start difference
        self.assertEqual(reg[reg.index("--status") + 1], "active")
        self.assertEqual(reg[reg.index("--expect-pi-model") + 1], "glm-5.2")
        self.assertEqual(reg[reg.index("--wake-strategy") + 1], "runtime_trigger")

    def test_register_failure_reports_and_leaves_no_false_success(self):
        t, run = _Transport(), _Autobridge(register_rc=1)
        with self.assertRaises(wr.RotateError):
            self._run(_cfg(), t, run)

    def test_bootstrap_prompt_watches_path_and_omits_removed_acknowledge_flag(self):
        prompt = wr.BOOTSTRAP_TEMPLATE.format(
            agent="glmpi", native="n", logical="L", event_path="/e.jsonl",
            py="py", inbox="ib", project="p", chat="c", repo="r", marker="M")
        self.assertIn("monitor_watch_path", prompt)
        self.assertNotIn("--acknowledge", prompt)  # inbox.py read already marks read

    def test_autobridge_subprocess_runs_from_canonical_workspace_cwd(self):
        # Proves every autobridge subprocess gets the canonical cwd, so an
        # isolated lane writes canonical State/ledger, not worktree-local ones.
        captured = {}

        class _Result:
            returncode, stdout = 0, "{}"

        def fake_run(cmd, **kw):
            captured["cwd"] = kw.get("cwd")
            return _Result()

        orig = wr.subprocess.run
        wr.subprocess.run = fake_run
        try:
            wr._default_run_autobridge(["show", "--session", "x", "--json"])
        finally:
            wr.subprocess.run = orig
        self.assertEqual(captured["cwd"], str(wr._workspace_root()))


if __name__ == "__main__":
    unittest.main()
