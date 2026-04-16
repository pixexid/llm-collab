#!/usr/bin/env python3
"""
task_contract.py — Project/task contract helpers with Amiga UI/UX enforcement.

Commands:
  python3 bin/task_contract.py sync --task TASK-ABC123 --write
  python3 bin/task_contract.py validate --task TASK-ABC123 --stage review --json
  python3 bin/task_contract.py record-ui-evidence --task TASK-ABC123 --design-docs-read /path/to/DESIGN.md --design-skills-used impeccable,design-taste-frontend --impeccable-detect-result pass --browser-validation-desktop "pass: /app/bookings desktop" --browser-validation-mobile "pass: 393px no overflow" --operator-visual-feedback-requested true --design-doc-update-decision "reviewed; no DESIGN.md diff required"
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
DEFAULT_DESIGN_SKILLS = [
    "design-taste-frontend",
    "stitch-design-taste",
    "impeccable",
]
REQUIRED_TASTE_SKILLS = {"design-taste-frontend", "stitch-design-taste", "impeccable"}

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
    title_tokens = {token for token in re.split(r"[^a-z0-9]+", title) if token}
    body_lower = body.lower()
    reasons: list[str] = []

    runtime_hits = [path for path in related_paths if any(marker in path for marker in RUNTIME_UI_MARKERS)]
    doc_hits = [path for path in related_paths if any(marker in path for marker in UI_DOC_MARKERS)]

    if runtime_hits:
        reasons.extend([f"runtime path: {path}" for path in runtime_hits])
    if doc_hits:
        reasons.extend([f"ui doc path: {path}" for path in doc_hits])

    if any(marker in title_tokens for marker in UI_TITLE_MARKERS):
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
        if not required_skills:
            required_skills = list(DEFAULT_DESIGN_SKILLS)
            updated["required_design_skills"] = required_skills
            changed.append("required_design_skills")
        elif updated.get("required_design_skills") != required_skills:
            updated["required_design_skills"] = required_skills
            changed.append("required_design_skills")

        if _normalize_bool(updated.get("impeccable_required")) is not True:
            updated["impeccable_required"] = True
            changed.append("impeccable_required")
        if _normalize_bool(updated.get("design_doc_update_review_required")) is not True:
            updated["design_doc_update_review_required"] = True
            changed.append("design_doc_update_review_required")

        evidence_defaults: dict[str, object] = {
            "design_docs_read": _normalize_list(updated.get("design_docs_read")),
            "design_skills_used": _normalize_list(updated.get("design_skills_used")),
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
    if AMIGA_DESIGN_DOC not in required_docs:
        errors.append("UI/UX lane must require DESIGN.md in `required_design_docs`.")
    if "impeccable" not in required_skills:
        errors.append("UI/UX lane must require `impeccable` in `required_design_skills`.")
    if not REQUIRED_TASTE_SKILLS.intersection(required_skills):
        errors.append("UI/UX lane must require at least one shared design/taste skill.")
    if _normalize_bool(fm.get("impeccable_required")) is not True:
        errors.append("UI/UX lane must set `impeccable_required: true`.")
    if _normalize_bool(fm.get("design_doc_update_review_required")) is not True:
        errors.append("UI/UX lane must set `design_doc_update_review_required: true`.")
    if _normalize_text(fm.get("ui_ux_mode")) not in {"implementation", "docs_only"}:
        errors.append("UI/UX lane must set `ui_ux_mode` to `implementation` or `docs_only`.")

    if stage in {"review", "pr"}:
        design_docs_read = set(_normalize_list(fm.get("design_docs_read")))
        missing_docs = [doc for doc in required_docs if doc not in design_docs_read]
        if missing_docs:
            errors.append(
                "UI/UX review evidence is missing required design docs in `design_docs_read`: "
                + ", ".join(missing_docs)
            )

        design_skills_used = set(_normalize_list(fm.get("design_skills_used")))
        if "impeccable" not in design_skills_used:
            errors.append("UI/UX review evidence must record `impeccable` in `design_skills_used`.")
        if not REQUIRED_TASTE_SKILLS.intersection(design_skills_used):
            errors.append("UI/UX review evidence must record at least one shared design/taste skill.")

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


def write_synced_task(task_path: Path, frontmatter: dict, body: str) -> dict:
    synced, changed = sync_ui_ux_contract(frontmatter, body)
    if changed:
        write_file(task_path, dump_frontmatter(synced, body))
    return {"changed_fields": changed, "frontmatter": synced}


def command_sync(args: argparse.Namespace) -> None:
    task_path = _task_path(args.task)
    fm, body = parse_frontmatter(task_path.read_text())
    if args.ui_ux_lane != "auto":
        fm["ui_ux_lane"] = args.ui_ux_lane == "true"
        fm["ui_ux_detection"] = "manual_true" if args.ui_ux_lane == "true" else "manual_false"
    synced, changed = sync_ui_ux_contract(fm, body)
    if args.write and changed:
        write_file(task_path, dump_frontmatter(synced, body))
    payload = {
        "task": args.task,
        "path": str(task_path),
        "changed_fields": changed,
        "ui_ux_lane": synced.get("ui_ux_lane"),
        "ui_ux_mode": synced.get("ui_ux_mode"),
    }
    print(json.dumps(payload, indent=2))


def command_validate(args: argparse.Namespace) -> None:
    task_path = _task_path(args.task)
    fm, body = parse_frontmatter(task_path.read_text())
    errors, summary = validate_ui_ux_contract(fm, body, stage=args.stage)
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
    synced, _ = sync_ui_ux_contract(fm, body)

    if args.design_docs_read is not None:
        synced["design_docs_read"] = _normalize_list(args.design_docs_read)
    if args.design_skills_used is not None:
        synced["design_skills_used"] = _normalize_list(args.design_skills_used)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Task contract helpers with Amiga UI/UX enforcement.")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Sync UI/UX defaults onto a task.")
    sync.add_argument("--task", required=True, help="TASK-id")
    sync.add_argument("--ui-ux-lane", default="auto", choices=["auto", "true", "false"])
    sync.add_argument("--write", action="store_true", help="Persist synced changes.")
    sync.set_defaults(func=command_sync)

    validate = sub.add_parser("validate", help="Validate UI/UX task contract/evidence.")
    validate.add_argument("--task", required=True, help="TASK-id")
    validate.add_argument("--stage", required=True, choices=["assignment", "review", "pr"])
    validate.add_argument("--json", action="store_true", help="Emit JSON result.")
    validate.set_defaults(func=command_validate)

    evidence = sub.add_parser("record-ui-evidence", help="Record UI/UX evidence on a task.")
    evidence.add_argument("--task", required=True, help="TASK-id")
    evidence.add_argument("--design-docs-read", default=None, help="Comma-separated design docs read.")
    evidence.add_argument("--design-skills-used", default=None, help="Comma-separated skill/CLI list used.")
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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
