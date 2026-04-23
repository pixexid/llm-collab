#!/usr/bin/env python3
"""
task_contract.py — Project/task contract helpers with Amiga UI/UX and DB enforcement.

Commands:
  python3 bin/task_contract.py sync --task TASK-ABC123 --write
  python3 bin/task_contract.py validate --task TASK-ABC123 --stage review --json
  python3 bin/task_contract.py record-db-evidence --task TASK-ABC123 --db-impact shared-supabase-required --db-migration-files db/migrations/20260417_add_staff_unavailability.sql --db-project-ref wbqjeasgxakubqcutgjt --db-apply-result "pass: supabase db push --linked" --db-schema-assertion "pass: execute_sql confirmed table staff_unavailability exists" --db-advisors-result "pass: get_advisors returned no blocking notices" --db-runtime-validation "pass: admin booking review + staff detail against shared Supabase"
  python3 bin/task_contract.py record-ui-evidence --task TASK-ABC123 --design-docs-read /path/to/DESIGN.md --design-skills-used impeccable --impeccable-commands-used /impeccable\\ craft,/polish --impeccable-detect-result pass --browser-validation-desktop "pass: /app/bookings desktop" --browser-validation-mobile "pass: 393px no overflow" --operator-visual-feedback-requested true --design-doc-update-decision "reviewed; no DESIGN.md diff required"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import dump_frontmatter, find_task_by_id, parse_frontmatter, write_file

AMIGA_DESIGN_DOC = "/Users/pixexid/Projects/amiga/docs/ui_ux/DESIGN.md"
AMIGA_SHARED_SUPABASE_PROJECT_REF = "wbqjeasgxakubqcutgjt"
DB_IMPACT_VALUES = {"none", "local-schema-only", "shared-supabase-required"}
DEFAULT_DESIGN_SKILLS = ["impeccable"]
IMPECCABLE_SETUP_COMMAND = "/impeccable teach"
IMPECCABLE_STEERING_COMMANDS = [
    "/impeccable craft",
    "/impeccable extract",
    "/audit",
    "/critique",
    "/polish",
    "/distill",
    "/clarify",
    "/optimize",
    "/harden",
    "/animate",
    "/colorize",
    "/bolder",
    "/quieter",
    "/delight",
    "/adapt",
    "/typeset",
    "/layout",
    "/overdrive",
]
IMPECCABLE_ALLOWED_COMMANDS = {IMPECCABLE_SETUP_COMMAND, *IMPECCABLE_STEERING_COMMANDS}
IMPECCABLE_COMMAND_ALIASES = {
    "/normalize": "/polish",
    "normalize": "/polish",
    "/arrange": "/layout",
    "arrange": "/layout",
    "prompts:audit": "/audit",
    "prompts:critique": "/critique",
    "prompts:polish": "/polish",
    "prompts:distill": "/distill",
    "prompts:clarify": "/clarify",
    "prompts:optimize": "/optimize",
    "prompts:harden": "/harden",
    "prompts:animate": "/animate",
    "prompts:colorize": "/colorize",
    "prompts:bolder": "/bolder",
    "prompts:quieter": "/quieter",
    "prompts:delight": "/delight",
    "prompts:adapt": "/adapt",
    "prompts:typeset": "/typeset",
    "prompts:layout": "/layout",
    "prompts:overdrive": "/overdrive",
}

RUNTIME_UI_MARKERS = (
    "src/",
    "public/",
    "package.json",
    "vite.config.",
    "tsconfig",
    "wrangler",
    "tailwind.config",
    "index.css",
    "app/",
)
UI_DOC_MARKERS = (
    "ui_ux/",
    "/ui_ux/",
    "DESIGN.md",
)
UI_TITLE_MARKERS = (
    "ui",
    "ux",
    "frontend",
    "design",
    "responsive",
    "layout",
    "modal",
    "drawer",
    "sheet",
    "toast",
    "icon",
    "badge",
)
DB_PATH_MARKERS = (
    "db/",
    "/db/",
    "schema.sql",
    "migration",
    "supabase",
)
DB_BODY_MARKERS = (
    "db impact",
    "database",
    "supabase",
    "migration",
    "schema",
    "rls",
    "policy",
    "column",
    "table",
    "sql",
)
DDL_BODY_MARKERS = (
    "migration",
    "table",
    "column",
    "index",
    "policy",
    "function",
    "rls",
    "schema",
)
DDL_PATH_MARKERS = (
    "db/migrations/",
    "/db/migrations/",
    "schema.sql",
)


def _normalize_list(value) -> list[str]:
    if value in (None, "", "<none>"):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value).strip()]


def _normalize_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in {"true", "yes", "1"}:
        return True
    if raw in {"false", "no", "0"}:
        return False
    return None


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _has_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _has_tokenized_marker(text: str, markers: tuple[str, ...]) -> bool:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if not tokens:
        return False
    joined = " ".join(tokens)
    return any((marker in joined) if " " in marker else (marker in tokens) for marker in markers)


def _normalize_impeccable_command(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    normalized = raw.replace("\\ ", " ").strip().lower()
    if normalized.startswith("/prompts:"):
        normalized = normalized.removeprefix("/prompts:")
        normalized = f"/{normalized}"
    elif normalized.startswith("prompts:"):
        normalized = normalized.removeprefix("prompts:")
        normalized = f"/{normalized}"
    elif normalized.startswith("impeccable "):
        normalized = f"/{normalized}"
    elif not normalized.startswith("/"):
        normalized = f"/{normalized}"
    normalized = IMPECCABLE_COMMAND_ALIASES.get(normalized, normalized)
    return normalized


def _normalize_impeccable_commands(value) -> list[str]:
    commands = []
    for item in _normalize_list(value):
        normalized = _normalize_impeccable_command(item)
        if normalized:
            commands.append(normalized)
    return list(dict.fromkeys(commands))


def _default_impeccable_commands(ui_mode: str) -> list[str]:
    if ui_mode == "docs_only":
        return ["/critique", "/polish"]
    return ["/impeccable craft", "/polish"]


def _task_path(task_id: str) -> Path:
    path = find_task_by_id(task_id)
    if path is None:
        raise FileNotFoundError(f"Task not found: {task_id}")
    return path


def detect_ui_ux_lane(frontmatter: dict, body: str = "") -> tuple[bool, str, list[str], str]:
    explicit = _normalize_bool(frontmatter.get("ui_ux_lane"))
    existing_detection = _normalize_text(frontmatter.get("ui_ux_detection"))
    related_paths = _normalize_list(frontmatter.get("related_paths"))
    title = _normalize_text(frontmatter.get("title")).lower()
    body_lower = body.lower()
    reasons: list[str] = []

    runtime_hits = [path for path in related_paths if any(marker in path for marker in RUNTIME_UI_MARKERS)]
    doc_hits = [path for path in related_paths if any(marker in path for marker in UI_DOC_MARKERS)]

    if runtime_hits:
        reasons.extend([f"runtime path: {path}" for path in runtime_hits])
    if doc_hits:
        reasons.extend([f"ui doc path: {path}" for path in doc_hits])

    if _has_tokenized_marker(title, UI_TITLE_MARKERS):
        reasons.append("title keyword match")
    if "ui/ux" in body_lower or "frontend" in body_lower or "design" in body_lower:
        reasons.append("body keyword match")

    auto_ui = bool(reasons)
    auto_mode = "implementation" if runtime_hits else ("docs_only" if auto_ui else "none")

    if explicit is True and existing_detection == "auto":
        return True, "auto", reasons or ["auto detection persisted"], auto_mode if auto_mode != "none" else "implementation"
    if explicit is True:
        return True, "manual_true", reasons or ["manual override"], auto_mode if auto_mode != "none" else "implementation"
    if explicit is False and existing_detection == "auto":
        return False, "auto", reasons or ["auto detection persisted"], "none"
    if explicit is False:
        return False, "manual_false", reasons or ["manual override"], "none"
    return auto_ui, "auto", reasons, auto_mode


def detect_db_contract(frontmatter: dict, body: str = "") -> tuple[str, str, list[str], bool]:
    explicit_impact = _normalize_text(frontmatter.get("db_impact"))
    related_paths = _normalize_list(frontmatter.get("related_paths"))
    title = _normalize_text(frontmatter.get("title")).lower()
    body_lower = body.lower()
    reasons: list[str] = []

    path_hits = [path for path in related_paths if any(marker in path.lower() for marker in DB_PATH_MARKERS)]
    if path_hits:
        reasons.extend([f"db path: {path}" for path in path_hits])

    if _has_any_marker(title, DB_BODY_MARKERS):
        reasons.append("title db keyword match")
    if _has_any_marker(body_lower, DB_BODY_MARKERS):
        reasons.append("body db keyword match")

    schema_path_hits = [
        path for path in related_paths if any(marker in path.lower() for marker in DDL_PATH_MARKERS)
    ]
    schema_change_detected = bool(schema_path_hits) or _has_any_marker(body_lower, DDL_BODY_MARKERS)

    if explicit_impact in DB_IMPACT_VALUES:
        return explicit_impact, "manual", reasons or ["manual override"], schema_change_detected

    auto_impact = "shared-supabase-required" if reasons else "none"
    return auto_impact, "auto", reasons, schema_change_detected


def sync_ui_ux_contract(frontmatter: dict, body: str) -> tuple[dict, list[str]]:
    updated = dict(frontmatter)
    changed: list[str] = []

    ui_lane, detection_mode, reasons, auto_mode = detect_ui_ux_lane(updated, body)
    if updated.get("ui_ux_lane") != ui_lane:
        updated["ui_ux_lane"] = ui_lane
        changed.append("ui_ux_lane")
    if updated.get("ui_ux_detection") != detection_mode:
        updated["ui_ux_detection"] = detection_mode
        changed.append("ui_ux_detection")
    if updated.get("ui_ux_detection_reasons") != reasons:
        updated["ui_ux_detection_reasons"] = reasons
        changed.append("ui_ux_detection_reasons")

    existing_mode = _normalize_text(updated.get("ui_ux_mode"))
    if ui_lane:
        next_mode = existing_mode if existing_mode in {"implementation", "docs_only"} else auto_mode
        if next_mode not in {"implementation", "docs_only"}:
            next_mode = "implementation"
        if updated.get("ui_ux_mode") != next_mode:
            updated["ui_ux_mode"] = next_mode
            changed.append("ui_ux_mode")

        required_docs = _normalize_list(updated.get("required_design_docs"))
        if AMIGA_DESIGN_DOC not in required_docs:
            required_docs = [AMIGA_DESIGN_DOC, *required_docs]
            updated["required_design_docs"] = required_docs
            changed.append("required_design_docs")
        elif updated.get("required_design_docs") != required_docs:
            updated["required_design_docs"] = required_docs
            changed.append("required_design_docs")

        required_skills = _normalize_list(updated.get("required_design_skills"))
        normalized_required_skills = [skill for skill in required_skills if skill == "impeccable"]
        if normalized_required_skills != list(DEFAULT_DESIGN_SKILLS):
            updated["required_design_skills"] = list(DEFAULT_DESIGN_SKILLS)
            changed.append("required_design_skills")

        if _normalize_bool(updated.get("impeccable_required")) is not True:
            updated["impeccable_required"] = True
            changed.append("impeccable_required")
        if _normalize_bool(updated.get("impeccable_antipatterns_enforced")) is not True:
            updated["impeccable_antipatterns_enforced"] = True
            changed.append("impeccable_antipatterns_enforced")
        if _normalize_bool(updated.get("design_doc_update_review_required")) is not True:
            updated["design_doc_update_review_required"] = True
            changed.append("design_doc_update_review_required")

        required_commands = _normalize_impeccable_commands(updated.get("impeccable_commands_required"))
        if not required_commands:
            required_commands = _default_impeccable_commands(updated["ui_ux_mode"])
        if updated.get("impeccable_commands_required") != required_commands:
            updated["impeccable_commands_required"] = required_commands
            changed.append("impeccable_commands_required")

        evidence_defaults: dict[str, object] = {
            "design_docs_read": _normalize_list(updated.get("design_docs_read")),
            "design_skills_used": _normalize_list(updated.get("design_skills_used")),
            "impeccable_commands_used": _normalize_impeccable_commands(updated.get("impeccable_commands_used")),
            "impeccable_detect_result": updated.get("impeccable_detect_result"),
            "browser_validation_desktop": updated.get("browser_validation_desktop"),
            "browser_validation_mobile": updated.get("browser_validation_mobile"),
            "operator_visual_feedback_requested": _normalize_bool(updated.get("operator_visual_feedback_requested"))
            or False,
            "design_doc_update_decision": updated.get("design_doc_update_decision"),
        }
        for key, default in evidence_defaults.items():
            if key not in updated:
                updated[key] = default
                changed.append(key)

        if updated["ui_ux_mode"] == "docs_only":
            for field in ("browser_validation_desktop", "browser_validation_mobile"):
                if _normalize_text(updated.get(field)) == "":
                    updated[field] = "skipped (docs-only)"
                    changed.append(field)
    else:
        if existing_mode not in {"", "none"}:
            updated["ui_ux_mode"] = "none"
            changed.append("ui_ux_mode")

    return updated, changed


def sync_db_contract(frontmatter: dict, body: str) -> tuple[dict, list[str]]:
    updated = dict(frontmatter)
    changed: list[str] = []

    db_impact, detection_mode, reasons, schema_change_detected = detect_db_contract(updated, body)
    if updated.get("db_impact") != db_impact:
        updated["db_impact"] = db_impact
        changed.append("db_impact")
    if updated.get("db_impact_detection") != detection_mode:
        updated["db_impact_detection"] = detection_mode
        changed.append("db_impact_detection")
    if updated.get("db_impact_detection_reasons") != reasons:
        updated["db_impact_detection_reasons"] = reasons
        changed.append("db_impact_detection_reasons")
    if updated.get("db_schema_change_detected") != schema_change_detected:
        updated["db_schema_change_detected"] = schema_change_detected
        changed.append("db_schema_change_detected")

    if db_impact == "shared-supabase-required":
        defaults: dict[str, object] = {
            "db_project_ref": updated.get("db_project_ref") or AMIGA_SHARED_SUPABASE_PROJECT_REF,
            "db_required_surfaces": _normalize_list(updated.get("db_required_surfaces"))
            or [
                "supabase_amiga.get_project",
                "supabase_amiga.execute_sql",
                "supabase_amiga.get_advisors",
                "supabase CLI",
            ],
            "db_migration_files": _normalize_list(updated.get("db_migration_files")),
            "db_apply_result": updated.get("db_apply_result"),
            "db_schema_assertion": updated.get("db_schema_assertion"),
            "db_advisors_result": updated.get("db_advisors_result"),
            "db_runtime_validation": updated.get("db_runtime_validation"),
        }
        for key, default in defaults.items():
            if key not in updated:
                updated[key] = default
                changed.append(key)
            elif updated.get(key) != default and key in {"db_required_surfaces", "db_migration_files"}:
                updated[key] = default
                changed.append(key)

    return updated, changed


def sync_task_contract(frontmatter: dict, body: str) -> tuple[dict, list[str]]:
    synced_ui, ui_changed = sync_ui_ux_contract(frontmatter, body)
    synced_db, db_changed = sync_db_contract(synced_ui, body)
    return synced_db, ui_changed + db_changed


def validate_ui_ux_contract(frontmatter: dict, body: str, *, stage: str) -> tuple[list[str], dict]:
    errors: list[str] = []
    fm, _ = sync_ui_ux_contract(frontmatter, body)
    ui_lane = bool(fm.get("ui_ux_lane"))
    summary = {
        "ui_ux_lane": ui_lane,
        "ui_ux_mode": fm.get("ui_ux_mode", "none"),
        "ui_ux_detection": fm.get("ui_ux_detection", "auto"),
        "required_design_docs": _normalize_list(fm.get("required_design_docs")),
        "required_design_skills": _normalize_list(fm.get("required_design_skills")),
    }

    if not ui_lane:
        return errors, summary

    required_docs = _normalize_list(fm.get("required_design_docs"))
    required_skills = _normalize_list(fm.get("required_design_skills"))
    required_commands = _normalize_impeccable_commands(fm.get("impeccable_commands_required"))
    if AMIGA_DESIGN_DOC not in required_docs:
        errors.append("UI/UX lane must require DESIGN.md in `required_design_docs`.")
    if required_skills != list(DEFAULT_DESIGN_SKILLS):
        errors.append("UI/UX lane must set `required_design_skills: [impeccable]`.")
    if _normalize_bool(fm.get("impeccable_required")) is not True:
        errors.append("UI/UX lane must set `impeccable_required: true`.")
    if _normalize_bool(fm.get("impeccable_antipatterns_enforced")) is not True:
        errors.append("UI/UX lane must set `impeccable_antipatterns_enforced: true`.")
    if _normalize_bool(fm.get("design_doc_update_review_required")) is not True:
        errors.append("UI/UX lane must set `design_doc_update_review_required: true`.")
    if _normalize_text(fm.get("ui_ux_mode")) not in {"implementation", "docs_only"}:
        errors.append("UI/UX lane must set `ui_ux_mode` to `implementation` or `docs_only`.")
    if not required_commands:
        errors.append("UI/UX lane must record `impeccable_commands_required`.")
    invalid_required_commands = [
        command for command in required_commands if command not in IMPECCABLE_ALLOWED_COMMANDS
    ]
    if invalid_required_commands:
        errors.append(
            "UI/UX lane has invalid `impeccable_commands_required`: " + ", ".join(invalid_required_commands)
        )
    if required_commands and not any(command in IMPECCABLE_STEERING_COMMANDS for command in required_commands):
        errors.append("UI/UX lane must plan at least one Impeccable steering command.")

    if stage in {"review", "pr"}:
        design_docs_read = set(_normalize_list(fm.get("design_docs_read")))
        missing_docs = [doc for doc in required_docs if doc not in design_docs_read]
        if missing_docs:
            errors.append(
                "UI/UX review evidence is missing required design docs in `design_docs_read`: "
                + ", ".join(missing_docs)
            )

        design_skills_used = _normalize_list(fm.get("design_skills_used"))
        if design_skills_used != list(DEFAULT_DESIGN_SKILLS):
            errors.append("UI/UX review evidence must record `design_skills_used: [impeccable]`.")

        commands_used = _normalize_impeccable_commands(fm.get("impeccable_commands_used"))
        if not commands_used:
            errors.append("UI/UX review evidence must include `impeccable_commands_used`.")
        invalid_used_commands = [command for command in commands_used if command not in IMPECCABLE_ALLOWED_COMMANDS]
        if invalid_used_commands:
            errors.append(
                "UI/UX review evidence has invalid `impeccable_commands_used`: "
                + ", ".join(invalid_used_commands)
            )
        if commands_used and not any(command in IMPECCABLE_STEERING_COMMANDS for command in commands_used):
            errors.append("UI/UX review evidence must include at least one Impeccable steering command.")
        missing_required_commands = [command for command in required_commands if command not in commands_used]
        if missing_required_commands:
            errors.append(
                "UI/UX review evidence is missing planned `impeccable_commands_required`: "
                + ", ".join(missing_required_commands)
            )

        if not _normalize_text(fm.get("impeccable_detect_result")):
            errors.append("UI/UX review evidence must include `impeccable_detect_result`.")

        mode = _normalize_text(fm.get("ui_ux_mode"))
        desktop = _normalize_text(fm.get("browser_validation_desktop"))
        mobile = _normalize_text(fm.get("browser_validation_mobile"))
        if mode == "implementation":
            if not desktop:
                errors.append("UI/UX implementation review evidence must include `browser_validation_desktop`.")
            if not mobile:
                errors.append("UI/UX implementation review evidence must include `browser_validation_mobile`.")
            if _normalize_bool(fm.get("operator_visual_feedback_requested")) is not True:
                errors.append(
                    "UI/UX implementation review evidence must set `operator_visual_feedback_requested: true`."
                )
        elif mode == "docs_only":
            if desktop != "skipped (docs-only)":
                errors.append("Docs-only UI/UX lanes must record `browser_validation_desktop: skipped (docs-only)`.")
            if mobile != "skipped (docs-only)":
                errors.append("Docs-only UI/UX lanes must record `browser_validation_mobile: skipped (docs-only)`.")

        if _normalize_bool(fm.get("design_doc_update_review_required")) is True and not _normalize_text(
            fm.get("design_doc_update_decision")
        ):
            errors.append("UI/UX review evidence must include `design_doc_update_decision`.")

    return errors, summary


def validate_db_contract(frontmatter: dict, body: str, *, stage: str) -> tuple[list[str], dict]:
    errors: list[str] = []
    fm, _ = sync_db_contract(frontmatter, body)
    db_impact = _normalize_text(fm.get("db_impact"))
    summary = {
        "db_impact": db_impact,
        "db_impact_detection": fm.get("db_impact_detection", "auto"),
        "db_impact_detection_reasons": _normalize_list(fm.get("db_impact_detection_reasons")),
        "db_project_ref": _normalize_text(fm.get("db_project_ref")),
        "db_schema_change_detected": _normalize_bool(fm.get("db_schema_change_detected")) is True,
    }

    if db_impact not in DB_IMPACT_VALUES:
        errors.append("Task must classify `db_impact` as none, local-schema-only, or shared-supabase-required.")
        return errors, summary

    if db_impact != "shared-supabase-required":
        return errors, summary

    if _normalize_text(fm.get("db_project_ref")) != AMIGA_SHARED_SUPABASE_PROJECT_REF:
        errors.append(
            f"Shared Supabase lanes must set `db_project_ref: {AMIGA_SHARED_SUPABASE_PROJECT_REF}`."
        )

    required_surfaces = set(_normalize_list(fm.get("db_required_surfaces")))
    required_surface_set = {
        "supabase CLI",
        "supabase_amiga.execute_sql",
        "supabase_amiga.get_advisors",
    }
    if not required_surface_set.issubset(required_surfaces):
        errors.append(
            "Shared Supabase lanes must require `supabase CLI`, `supabase_amiga.execute_sql`, and `supabase_amiga.get_advisors`."
        )

    if stage in {"review", "pr"}:
        if _normalize_bool(fm.get("db_schema_change_detected")) is True and not _normalize_list(
            fm.get("db_migration_files")
        ):
            errors.append("Shared Supabase schema-change lanes must record `db_migration_files`.")
        if not _normalize_text(fm.get("db_apply_result")):
            errors.append("Shared Supabase review evidence must include `db_apply_result`.")
        if not _normalize_text(fm.get("db_schema_assertion")):
            errors.append("Shared Supabase review evidence must include `db_schema_assertion`.")
        if not _normalize_text(fm.get("db_runtime_validation")):
            errors.append("Shared Supabase review evidence must include `db_runtime_validation`.")
        if _normalize_bool(fm.get("db_schema_change_detected")) is True and not _normalize_text(
            fm.get("db_advisors_result")
        ):
            errors.append("Shared Supabase schema-change lanes must include `db_advisors_result`.")

    return errors, summary


def validate_task_contract(frontmatter: dict, body: str, *, stage: str) -> tuple[list[str], dict]:
    ui_errors, ui_summary = validate_ui_ux_contract(frontmatter, body, stage=stage)
    db_errors, db_summary = validate_db_contract(frontmatter, body, stage=stage)
    return ui_errors + db_errors, {"ui_ux": ui_summary, "db": db_summary}


def write_synced_task(task_path: Path, frontmatter: dict, body: str) -> dict:
    synced, changed = sync_task_contract(frontmatter, body)
    if changed:
        write_file(task_path, dump_frontmatter(synced, body))
    return {"changed_fields": changed, "frontmatter": synced}


def command_sync(args: argparse.Namespace) -> None:
    task_path = _task_path(args.task)
    fm, body = parse_frontmatter(task_path.read_text())
    if args.ui_ux_lane != "auto":
        fm["ui_ux_lane"] = args.ui_ux_lane == "true"
        fm["ui_ux_detection"] = "manual_true" if args.ui_ux_lane == "true" else "manual_false"
    synced, changed = sync_task_contract(fm, body)
    if args.write and changed:
        write_file(task_path, dump_frontmatter(synced, body))
    payload = {
        "task": args.task,
        "path": str(task_path),
        "changed_fields": changed,
        "ui_ux_lane": synced.get("ui_ux_lane"),
        "ui_ux_mode": synced.get("ui_ux_mode"),
        "db_impact": synced.get("db_impact"),
    }
    print(json.dumps(payload, indent=2))


def command_validate(args: argparse.Namespace) -> None:
    task_path = _task_path(args.task)
    fm, body = parse_frontmatter(task_path.read_text())
    errors, summary = validate_task_contract(fm, body, stage=args.stage)
    payload = {
        "task": args.task,
        "path": str(task_path),
        "stage": args.stage,
        "ok": len(errors) == 0,
        "summary": summary,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(json.dumps(payload, indent=2))
    if errors:
        sys.exit(1)


def command_record_ui_evidence(args: argparse.Namespace) -> None:
    task_path = _task_path(args.task)
    fm, body = parse_frontmatter(task_path.read_text())
    synced, _ = sync_task_contract(fm, body)

    if args.design_docs_read is not None:
        synced["design_docs_read"] = _normalize_list(args.design_docs_read)
    if args.design_skills_used is not None:
        synced["design_skills_used"] = _normalize_list(args.design_skills_used)
    if args.impeccable_commands_used is not None:
        synced["impeccable_commands_used"] = _normalize_impeccable_commands(args.impeccable_commands_used)
    if args.impeccable_detect_result is not None:
        synced["impeccable_detect_result"] = args.impeccable_detect_result
    if args.browser_validation_desktop is not None:
        synced["browser_validation_desktop"] = args.browser_validation_desktop
    if args.browser_validation_mobile is not None:
        synced["browser_validation_mobile"] = args.browser_validation_mobile
    if args.operator_visual_feedback_requested is not None:
        synced["operator_visual_feedback_requested"] = _normalize_bool(args.operator_visual_feedback_requested)
    if args.design_doc_update_decision is not None:
        synced["design_doc_update_decision"] = args.design_doc_update_decision

    write_file(task_path, dump_frontmatter(synced, body))
    print(
        json.dumps(
            {
                "task": args.task,
                "path": str(task_path),
                "ui_ux_lane": synced.get("ui_ux_lane"),
                "recorded": True,
            },
            indent=2,
        )
    )


def command_record_db_evidence(args: argparse.Namespace) -> None:
    task_path = _task_path(args.task)
    fm, body = parse_frontmatter(task_path.read_text())
    synced, _ = sync_task_contract(fm, body)

    if args.db_impact is not None:
        synced["db_impact"] = args.db_impact
        synced["db_impact_detection"] = "manual"
    if args.db_project_ref is not None:
        synced["db_project_ref"] = args.db_project_ref
    if args.db_migration_files is not None:
        synced["db_migration_files"] = _normalize_list(args.db_migration_files)
    if args.db_apply_result is not None:
        synced["db_apply_result"] = args.db_apply_result
    if args.db_schema_assertion is not None:
        synced["db_schema_assertion"] = args.db_schema_assertion
    if args.db_advisors_result is not None:
        synced["db_advisors_result"] = args.db_advisors_result
    if args.db_runtime_validation is not None:
        synced["db_runtime_validation"] = args.db_runtime_validation

    write_file(task_path, dump_frontmatter(synced, body))
    print(
        json.dumps(
            {
                "task": args.task,
                "path": str(task_path),
                "db_impact": synced.get("db_impact"),
                "recorded": True,
            },
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Task contract helpers with Amiga UI/UX and DB enforcement.")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Sync UI/UX defaults onto a task.")
    sync.add_argument("--task", required=True, help="TASK-id")
    sync.add_argument("--ui-ux-lane", default="auto", choices=["auto", "true", "false"])
    sync.add_argument("--write", action="store_true", help="Persist synced changes.")
    sync.set_defaults(func=command_sync)

    validate = sub.add_parser("validate", help="Validate task contract/evidence.")
    validate.add_argument("--task", required=True, help="TASK-id")
    validate.add_argument("--stage", required=True, choices=["assignment", "review", "pr"])
    validate.add_argument("--json", action="store_true", help="Emit JSON result.")
    validate.set_defaults(func=command_validate)

    evidence = sub.add_parser("record-ui-evidence", help="Record UI/UX evidence on a task.")
    evidence.add_argument("--task", required=True, help="TASK-id")
    evidence.add_argument("--design-docs-read", default=None, help="Comma-separated design docs read.")
    evidence.add_argument("--design-skills-used", default=None, help="Comma-separated design skill list used.")
    evidence.add_argument(
        "--impeccable-commands-used",
        default=None,
        help="Comma-separated Impeccable commands used (for example: /impeccable craft,/polish).",
    )
    evidence.add_argument("--impeccable-detect-result", default=None, help="Result or evidence summary.")
    evidence.add_argument("--browser-validation-desktop", default=None, help="Desktop browser validation summary.")
    evidence.add_argument("--browser-validation-mobile", default=None, help="Mobile browser validation summary.")
    evidence.add_argument(
        "--operator-visual-feedback-requested",
        default=None,
        help="true|false: whether the operator was explicitly asked for visual feedback.",
    )
    evidence.add_argument(
        "--design-doc-update-decision",
        default=None,
        help="How DESIGN.md or linked UI docs were reviewed/updated for this lane.",
    )
    evidence.set_defaults(func=command_record_ui_evidence)

    db_evidence = sub.add_parser("record-db-evidence", help="Record DB evidence on a task.")
    db_evidence.add_argument("--task", required=True, help="TASK-id")
    db_evidence.add_argument("--db-impact", default=None, choices=sorted(DB_IMPACT_VALUES))
    db_evidence.add_argument("--db-project-ref", default=None)
    db_evidence.add_argument("--db-migration-files", default=None, help="Comma-separated migration files.")
    db_evidence.add_argument("--db-apply-result", default=None)
    db_evidence.add_argument("--db-schema-assertion", default=None)
    db_evidence.add_argument("--db-advisors-result", default=None)
    db_evidence.add_argument("--db-runtime-validation", default=None)
    db_evidence.set_defaults(func=command_record_db_evidence)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
