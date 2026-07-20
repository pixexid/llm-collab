"""Release-closure collection regressions for fresh project initialization."""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import deploy_release_watch as release_watch
import init as init_script


class InjectedAnswers:
    """Callable prompt input that fails if a test did not supply every answer."""

    def __init__(self, answers: list[str], *, before_read=None):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.before_read = before_read

    def __call__(self, prompt_text: str) -> str:
        if self.before_read is not None:
            self.before_read(prompt_text)
        self.prompts.append(prompt_text)
        if not self.answers:
            raise AssertionError(f"unexpected prompt after injected answers ran out: {prompt_text}")
        return self.answers.pop(0)

    def assert_exhausted(self) -> None:
        if self.answers:
            raise AssertionError(f"unused injected answers: {self.answers!r}")


def production_project_answers(
    *,
    project_id: str,
    display_name: str,
    repo: str,
    branch: str,
    release_gate_agent: str,
    workflow: str,
    event: str,
    required_jobs: list[str],
    smoke_job: str,
    smoke_steps: list[str],
    add_another: bool,
) -> list[str]:
    return [
        project_id,
        display_name,
        f"app:{project_id}",
        "n",  # no preflight command
        "y",  # GitHub enabled
        "production",
        repo,
        "",  # no GitHub Project number
        branch,
        release_gate_agent,
        workflow,
        event,
        ",".join(required_jobs),
        smoke_job,
        ",".join(smoke_steps),
        "y" if add_another else "n",
    ]


class InitReleaseClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        stdin_guard = patch(
            "builtins.input",
            side_effect=AssertionError("tests must use the injected input seam"),
        )
        stdin_guard.start()
        self.addCleanup(stdin_guard.stop)

    def collect_with_injected_answers(
        self,
        answers: list[str],
        projects_path: Path,
        *,
        before_read=None,
    ) -> tuple[list[dict], str, InjectedAnswers]:
        injected = InjectedAnswers(answers, before_read=before_read)
        output = StringIO()
        with redirect_stdout(output):
            projects = init_script.collect_projects(
                ["codex", "claude"],
                input_fn=injected,
                projects_path=projects_path,
            )
        injected.assert_exhausted()
        return projects, output.getvalue(), injected

    def test_github_disabled_omits_closure_and_prints_exact_repair_path(self) -> None:
        with TemporaryDirectory() as tmp:
            projects_path = Path(tmp) / "projects.json"
            projects_path.write_text(
                json.dumps({"projects": [{"id": "operator-owned-existing"}]})
            )
            before = projects_path.read_text()
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("project collection must not read any registry"),
            ):
                with patch.object(
                    Path,
                    "write_text",
                    side_effect=AssertionError("project collection must not write any registry"),
                ):
                    projects, output, _ = self.collect_with_injected_answers(
                        [
                            "y",
                            "docs",
                            "Docs",
                            "site:docs",
                            "n",
                            "n",
                            "main",
                            "codex",
                            "n",
                        ],
                        projects_path,
                    )

            self.assertNotIn("release_closure", projects[0])
            self.assertIn("exact-SHA `success` is unavailable", output)
            self.assertIn(str(projects_path.resolve()), output)
            self.assertEqual(projects_path.read_text(), before)

    def test_non_production_guidance_lists_all_keys_and_stays_fail_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            projects_path = Path(tmp) / "projects.json"
            projects, output, _ = self.collect_with_injected_answers(
                [
                    "y",
                    "stage",
                    "Stage",
                    "app:stage",
                    "n",
                    "y",
                    "",
                    "production-ish",
                    "non-production/local",
                    "",
                    "",
                    "",
                    "codex",
                    "n",
                ],
                projects_path,
            )

            self.assertNotIn("release_closure", projects[0])
            self.assertEqual(projects[0]["default_branch_base"], "main")
            self.assertIn("exact-SHA `success` stays fail-closed", output)
            self.assertIn(str(projects_path.resolve()), output)
            for key in init_script.RELEASE_CLOSURE_REQUIRED_KEYS:
                self.assertIn(f"- {key}", output)

    def run_ambiguous_main_fixture(self, *, config_exists: bool) -> tuple[list[str], str]:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "collab.config.json"
            projects_path = root / "projects.json"
            if config_exists:
                config_path.write_text("{}")
            sentinel = b'{"projects":[{"id":"existing","operator":"owned"}]}\n'
            projects_path.write_bytes(sentinel)
            answers = []
            if config_exists:
                answers.append("y")
            answers.extend(
                [
                    "fixture",
                    str(root / "repos"),
                    str(root / "state"),
                    "15",
                    "n",
                    "y",  # add projects
                    "existing",
                    "Existing",
                    "app:existing",
                    "n",
                    "y",  # GitHub enabled
                    "fixture/existing",
                    "",
                    "main",
                    "codex",
                    "n",
                ]
            )
            injected = InjectedAnswers(answers)
            captured_projects = {}
            registry_was_intact_before_write = False
            output = StringIO()
            original_write_json = init_script.write_json
            original_read_text = Path.read_text

            def guarded_read_text(path: Path, *args, **kwargs):
                if path.resolve() == projects_path.resolve():
                    raise AssertionError("reinitialize must not read projects.json")
                return original_read_text(path, *args, **kwargs)

            def guarded_write_json(path: Path, data: dict | list) -> None:
                nonlocal registry_was_intact_before_write
                if path.resolve() == projects_path.resolve():
                    self.assertEqual(projects_path.read_bytes(), sentinel)
                    registry_was_intact_before_write = True
                    captured_projects.update(data)
                original_write_json(path, data)

            agents = [
                {
                    "id": "codex",
                    "display_name": "Codex",
                    "role": "orchestrator",
                    "activation": {"type": "human"},
                }
            ]
            with patch.object(init_script, "ROOT", root):
                with patch.object(init_script, "_local_config", {}):
                    with patch.object(init_script, "collect_agents", return_value=agents):
                        with patch.object(Path, "read_text", new=guarded_read_text):
                            with patch.object(
                                init_script,
                                "write_json",
                                side_effect=guarded_write_json,
                            ):
                                with redirect_stdout(output):
                                    init_script.main(input_fn=injected)
            injected.assert_exhausted()

            generated = captured_projects["projects"][0]
            self.assertEqual(generated["id"], "existing")
            self.assertNotIn("release_closure", generated)
            self.assertTrue(registry_was_intact_before_write)
            self.assertNotEqual(projects_path.read_bytes(), sentinel)
            self.assertFalse(
                any("Release environment" in prompt for prompt in injected.prompts)
            )
            self.assertIn("existing/ambiguous reinitialize entry", output.getvalue())
            self.assertIn(str(projects_path.resolve()), output.getvalue())
            self.assertIn("exact-SHA `success` stays fail-closed", output.getvalue())
            for key in init_script.RELEASE_CLOSURE_REQUIRED_KEYS:
                self.assertIn(f"- {key}", output.getvalue())
            return injected.prompts, output.getvalue()

    def test_mutation_reinitialize_guard_blocks_automatic_closure(self) -> None:
        prompts, _ = self.run_ambiguous_main_fixture(config_exists=True)
        self.assertTrue(any("Reinitialize?" in prompt for prompt in prompts))

    def test_mutation_projects_only_guard_blocks_automatic_closure(self) -> None:
        prompts, _ = self.run_ambiguous_main_fixture(config_exists=False)
        self.assertFalse(any("Reinitialize?" in prompt for prompt in prompts))

    def test_paired_distinct_projects_resolve_through_shipped_exact_sha_evaluator(self) -> None:
        fixtures = {
            "amiga": {
                "repo": "fixture/amiga",
                "branch": "release-amiga",
                "gate": "codex",
                "workflow": "ship-amiga",
                "event": "push",
                "jobs": ["amiga-build", "amiga-smoke"],
                "smoke_job": "amiga-smoke",
                "steps": ["Amiga production host", "Amiga production auth"],
                "sha": "a" * 40,
                "run_id": 101,
            },
            "nuvyr": {
                "repo": "fixture/nuvyr",
                "branch": "release-nuvyr",
                "gate": "claude",
                "workflow": "publish-nuvyr",
                "event": "workflow_run",
                "jobs": ["nuvyr-check", "nuvyr-publish"],
                "smoke_job": "nuvyr-publish",
                "steps": ["Nuvyr web health", "Nuvyr API health"],
                "sha": "b" * 40,
                "run_id": 202,
            },
        }
        answers = ["y"]
        for index, (project_id, fixture) in enumerate(fixtures.items()):
            answers.extend(
                production_project_answers(
                    project_id=project_id,
                    display_name=project_id.title(),
                    repo=fixture["repo"],
                    branch=fixture["branch"],
                    release_gate_agent=fixture["gate"],
                    workflow=fixture["workflow"],
                    event=fixture["event"],
                    required_jobs=fixture["jobs"],
                    smoke_job=fixture["smoke_job"],
                    smoke_steps=fixture["steps"],
                    add_another=index == 0,
                )
            )

        with TemporaryDirectory() as tmp:
            projects, _, _ = self.collect_with_injected_answers(
                answers,
                Path(tmp) / "projects.json",
            )

        by_id = {project["id"]: project for project in projects}
        self.assertEqual(len(projects), 2)
        self.assertIsNot(by_id["amiga"], by_id["nuvyr"])
        self.assertIsNot(
            by_id["amiga"]["release_closure"],
            by_id["nuvyr"]["release_closure"],
        )
        self.assertNotEqual(by_id["amiga"]["github"]["repo"], by_id["nuvyr"]["github"]["repo"])
        self.assertNotEqual(
            by_id["amiga"]["default_branch_base"],
            by_id["nuvyr"]["default_branch_base"],
        )
        self.assertNotEqual(
            by_id["amiga"]["release_gate_agent"],
            by_id["nuvyr"]["release_gate_agent"],
        )
        for key in (
            "workflow",
            "trigger_event",
            "required_jobs",
            "smoke_job",
            "required_smoke_steps",
        ):
            self.assertNotEqual(
                by_id["amiga"]["release_closure"][key],
                by_id["nuvyr"]["release_closure"][key],
            )

        calls: list[str] = []

        def runner(argv: list[str]) -> str:
            endpoint = argv[-1]
            calls.append(endpoint)
            fixture = next(
                value for value in fixtures.values() if f"repos/{value['repo']}/" in endpoint
            )
            if "/jobs?" in endpoint:
                jobs = []
                for job_name in fixture["jobs"]:
                    steps = (
                        [
                            {"name": step, "conclusion": "success"}
                            for step in fixture["steps"]
                        ]
                        if job_name == fixture["smoke_job"]
                        else []
                    )
                    jobs.append(
                        {"name": job_name, "conclusion": "success", "steps": steps}
                    )
                return json.dumps({"jobs": jobs})
            return json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": fixture["run_id"],
                            "head_sha": fixture["sha"],
                            "status": "completed",
                            "conclusion": "success",
                            "run_attempt": 1,
                            "event": fixture["event"],
                            "head_branch": fixture["branch"],
                            "name": fixture["workflow"],
                        }
                    ]
                }
            )

        for project_id, fixture in fixtures.items():
            evaluation = release_watch.evaluate_project_release(
                project_id,
                fixture["sha"],
                project=by_id[project_id],
                runner=runner,
            )
            self.assertEqual(evaluation.verdict.state, "SUCCESS")
            self.assertEqual(evaluation.repository, fixture["repo"])
            self.assertEqual(evaluation.workflow, fixture["workflow"])
            self.assertEqual(evaluation.verdict.merge_sha, fixture["sha"])
            self.assertEqual(evaluation.verdict.run_id, fixture["run_id"])
        self.assertTrue(
            all(
                any(f"head_sha={fixture['sha']}" in call for call in calls)
                for fixture in fixtures.values()
            )
        )

    def test_mutation_defaulted_or_inherited_closure_values_reprompt(self) -> None:
        path = Path("/tmp/fixture-projects.json")
        output = StringIO()
        first = InjectedAnswers(
            [
                "",
                "ship-amiga",
                "",
                "push",
                "",
                "amiga-build,amiga-deploy",
                "",
                "amiga-deploy",
                "",
                "Amiga health",
            ]
        )
        second = InjectedAnswers(
            [
                "",
                "publish-nuvyr",
                "",
                "workflow_run",
                "",
                "nuvyr-test,nuvyr-publish",
                "",
                "nuvyr-publish",
                "",
                "Nuvyr health",
            ]
        )
        with redirect_stdout(output):
            amiga = init_script.collect_release_closure("amiga", path, input_fn=first)
            nuvyr = init_script.collect_release_closure("nuvyr", path, input_fn=second)
        first.assert_exhausted()
        second.assert_exhausted()

        self.assertEqual(amiga["workflow"], "ship-amiga")
        self.assertEqual(nuvyr["workflow"], "publish-nuvyr")
        self.assertNotEqual(amiga, nuvyr)
        self.assertGreaterEqual(output.getvalue().count("no default or inherited value"), 6)

    def test_mutation_smoke_job_outside_required_jobs_reprompts(self) -> None:
        path = Path("/tmp/fixture-projects.json")
        answers = InjectedAnswers(
            ["ship", "push", "build,deploy", "smoke", "deploy", "host health"]
        )
        output = StringIO()
        with redirect_stdout(output):
            closure = init_script.collect_release_closure(
                "amiga",
                path,
                input_fn=answers,
            )
        answers.assert_exhausted()

        self.assertEqual(closure["smoke_job"], "deploy")
        self.assertIn("'smoke' must appear in release_closure.required_jobs", output.getvalue())
        self.assertIn("release_closure.smoke_job", output.getvalue())

    def test_mutation_empty_list_item_reprompts(self) -> None:
        path = Path("/tmp/fixture-projects.json")
        answers = InjectedAnswers(
            [
                "ship",
                "push",
                "build,,deploy",
                "build,deploy",
                "deploy",
                "host health, ,auth health",
                "host health,auth health",
            ]
        )
        output = StringIO()
        with redirect_stdout(output):
            closure = init_script.collect_release_closure(
                "amiga",
                path,
                input_fn=answers,
            )
        answers.assert_exhausted()

        self.assertEqual(closure["required_jobs"], ["build", "deploy"])
        self.assertEqual(
            closure["required_smoke_steps"],
            ["host health", "auth health"],
        )
        self.assertEqual(
            output.getvalue().count("a non-empty list of non-empty strings is required"),
            2,
        )

    def test_mutation_write_before_validation_never_occurs(self) -> None:
        write_json = Mock()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "projects.json"
            with patch.object(init_script, "write_json", write_json):
                projects, _, _ = self.collect_with_injected_answers(
                    [
                        "y",
                        "amiga",
                        "Amiga",
                        "app:amiga",
                        "n",
                        "y",
                        "",
                        "production",
                        "",
                        "fixture/amiga",
                        "",
                        "",
                        "main",
                        "",
                        "codex",
                        "ship",
                        "push",
                        "build,,deploy",
                        "build,deploy",
                        "smoke",
                        "deploy",
                        "health",
                        "n",
                    ],
                    path,
                    before_read=lambda _prompt: write_json.assert_not_called(),
                )

        self.assertEqual(projects[0]["release_closure"]["required_jobs"], ["build", "deploy"])
        write_json.assert_not_called()

    def test_mutation_append_before_validation_is_observable(self) -> None:
        sink: list[dict] = []

        def observe_append_boundary(prompt_text: str) -> None:
            if "Add another project?" in prompt_text:
                self.assertEqual(len(sink), 1)
            else:
                self.assertEqual(sink, [])

        answers = InjectedAnswers(
            [
                "y",
                "amiga",
                "Amiga",
                "app:amiga",
                "n",
                "y",
                "production",
                "fixture/amiga",
                "",
                "main",
                "codex",
                "ship",
                "push",
                "build,,deploy",
                "build,deploy",
                "smoke",
                "deploy",
                "health",
                "n",
            ],
            before_read=observe_append_boundary,
        )
        with redirect_stdout(StringIO()):
            projects = init_script.collect_projects(
                ["codex"],
                input_fn=answers,
                projects_path=Path("/tmp/observable-projects.json"),
                project_sink=sink,
            )
        answers.assert_exhausted()

        self.assertIs(projects, sink)
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0]["release_closure"]["required_jobs"], ["build", "deploy"])

    def test_mutation_production_choice_has_no_default_or_inference(self) -> None:
        path = Path("/tmp/fixture-projects.json")
        answers = InjectedAnswers(["", "from-path-name", "production"])
        output = StringIO()
        with redirect_stdout(output):
            selected = init_script.select_release_mode(
                "amiga-production-looking-name",
                path,
                input_fn=answers,
            )
        answers.assert_exhausted()

        self.assertEqual(selected, "production")
        self.assertEqual(len(answers.prompts), 3)
        self.assertEqual(output.getvalue().count("no default or inference is allowed"), 2)

    def test_mutation_every_error_names_project_exact_key_and_registry_path(self) -> None:
        path = Path("/tmp/exact-fixture-projects.json").resolve()
        output = StringIO()
        with redirect_stdout(output):
            init_script.select_release_mode(
                "amiga",
                path,
                input_fn=InjectedAnswers(["", "production"]),
            )
            init_script._require_project_value(
                "amiga",
                "github.repo",
                "Repo",
                path,
                input_fn=InjectedAnswers(["", "fixture/amiga"]),
            )
            init_script._require_project_value(
                "amiga",
                "default_branch_base",
                "Branch",
                path,
                input_fn=InjectedAnswers(["", "main"]),
            )
            init_script.select_release_gate_agent(
                "amiga",
                ["codex"],
                projects_path=path,
                input_fn=InjectedAnswers(["ghost", "codex"]),
            )
            init_script.collect_release_closure(
                "amiga",
                path,
                input_fn=InjectedAnswers(
                    [
                        "",
                        "ship",
                        "",
                        "push",
                        "build,,deploy",
                        "build,deploy",
                        "",
                        "smoke",
                        "deploy",
                        "health,,auth",
                        "health,auth",
                    ]
                ),
            )

        error_lines = [
            line for line in output.getvalue().splitlines() if line.startswith("[error]")
        ]
        expected_key_counts = {
            "release_closure": 1,
            "github.repo": 1,
            "default_branch_base": 1,
            "release_gate_agent": 1,
            "release_closure.workflow": 1,
            "release_closure.trigger_event": 1,
            "release_closure.required_jobs": 1,
            "release_closure.smoke_job": 2,
            "release_closure.required_smoke_steps": 1,
        }
        self.assertEqual(len(error_lines), sum(expected_key_counts.values()))
        for line in error_lines:
            self.assertIn("project 'amiga'", line)
            self.assertIn(str(path), line)
        for key, expected_count in expected_key_counts.items():
            self.assertEqual(
                sum(f"projects.json key {key!r}" in line for line in error_lines),
                expected_count,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
