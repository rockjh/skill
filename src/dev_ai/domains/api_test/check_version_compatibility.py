#!/usr/bin/env python3
"""Check and optionally advance the business-code version used by Bruno tests."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from .manifest_io import load_data


DEFAULT_API_PATTERNS = [
    "**/controller/**",
    "*Controller.java",
    "**/application/**",
    "**/service/**",
    "*Service.java",
    "**/dto/**",
    "**/domain/**",
    "**/repository/**",
    "**/integration/**",
    "**/infrastructure/**",
    "**/adapter/**",
    "**/validation/**",
    "**/security/**",
    "**/error/**",
    "**/exception/**",
    "*Exception.java",
    "*ErrorCode.java",
    "**/*ErrorCode.*",
    "**/config/**",
    "**/resources/application*.yml",
    "**/resources/application*.yaml",
    "**/resources/application*.properties",
    "**/swagger*",
    "**/openapi*",
    "**/db/migration/**",
    "**/flyway/**",
    "**/*.sql",
    "*.sql",
    "**/pom.xml",
    "pom.xml",
    # Conservative cross-language defaults. Projects can narrow these with
    # impact-rules.yaml when their repository separates API and non-API code.
    "**/*.go",
    "*.go",
    "go.mod",
    "go.sum",
    "**/*.py",
    "*.py",
    "pyproject.toml",
    "requirements*.txt",
    "**/*.js",
    "*.js",
    "**/*.jsx",
    "*.jsx",
    "**/*.ts",
    "*.ts",
    "**/*.tsx",
    "*.tsx",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "**/*.cs",
    "*.cs",
    "**/*.fs",
    "*.fs",
    "*.csproj",
    "*.fsproj",
    "*.sln",
    "**/*.rs",
    "*.rs",
    "Cargo.toml",
    "Cargo.lock",
    "**/*.php",
    "*.php",
    "composer.json",
    "**/*.kt",
    "*.kt",
    "**/*.kts",
    "*.kts",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "**/*.rb",
    "*.rb",
    "Gemfile",
    "Gemfile.lock",
    "**/*.scala",
    "*.scala",
    "build.sbt",
    "**/*.swift",
    "*.swift",
    "Package.swift",
    "**/*.ex",
    "*.ex",
    "**/*.exs",
    "*.exs",
    "mix.exs",
    "**/*.dart",
    "*.dart",
    "pubspec.yaml",
]
DEFAULT_IGNORE_PATTERNS = ["qa/**", "**/qa/**"]
DEFAULT_NON_API_PATTERNS = [
    "**/test/**", "**/tests/**", "test/**", "tests/**",
    "**/*.md", "*.md", "docs/**", "**/docs/**",
]
STRICT_COMPLETION_FIELDS = (
    "scenario_matrix_checked",
    "constraint_obligations_checked",
    "exact_assertions_checked",
    "variables_checked",
    "source_mapping_checked",
    "qa_lock_checked",
    "business_version_checked",
)


def is_qa_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized == "qa" or normalized.startswith("qa/")


def source_digest(repo: Path, *, exclude_root: Path | None = None) -> str:
    """Hash current business files without reading version-control history."""

    repo = repo.resolve()
    candidate = exclude_root.resolve() if exclude_root is not None else None
    excluded = candidate if candidate is not None and candidate != repo and candidate.is_relative_to(repo) else None
    values = [
        str(path.relative_to(repo))
        for path in repo.rglob("*")
        if path.is_file()
        and not any(part in {".git", "qa", "node_modules", "target", "build", ".venv", "venv"} for part in path.parts)
        and (excluded is None or path.resolve() != excluded and excluded not in path.resolve().parents)
    ]
    digest = hashlib.sha256()
    for value in sorted(values):
        if value == "qa" or value.startswith("qa/"):
            continue
        path = repo / value
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except (OSError, PermissionError):
            continue
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def classify(paths: list[str], rules: dict[str, Any]) -> str:
    api_patterns = rules.get("api_patterns", DEFAULT_API_PATTERNS)
    ignored = [*DEFAULT_IGNORE_PATTERNS, *rules.get("ignore_patterns", [])]
    non_api = rules.get("non_api_patterns", DEFAULT_NON_API_PATTERNS)
    for path in paths:
        normalized = path.replace("\\", "/").lower()
        if any(fnmatch.fnmatch(normalized, str(pattern).lower()) for pattern in ignored):
            continue
        if any(fnmatch.fnmatch(normalized, str(pattern).lower()) for pattern in api_patterns):
            return "api-impact"
        if not any(fnmatch.fnmatch(normalized, str(pattern).lower()) for pattern in non_api):
            return "api-impact"
    return "non-api"


def change_classes(paths: list[str], rules: dict[str, Any]) -> dict[str, list[str]]:
    ignored = [*DEFAULT_IGNORE_PATTERNS, *rules.get("ignore_patterns", [])]
    api_patterns = rules.get("api_patterns", DEFAULT_API_PATTERNS)
    non_api = rules.get("non_api_patterns", DEFAULT_NON_API_PATTERNS)
    groups = {"business_code": [], "qa_assets": [], "unrelated": []}
    for path in paths:
        normalized = path.replace("\\", "/").lower()
        if is_qa_path(path):
            groups["qa_assets"].append(path)
        elif any(fnmatch.fnmatch(normalized, str(pattern).lower()) for pattern in ignored):
            groups["unrelated"].append(path)
        elif any(fnmatch.fnmatch(normalized, str(pattern).lower()) for pattern in api_patterns) or not any(
            fnmatch.fnmatch(normalized, str(pattern).lower()) for pattern in non_api
        ):
            groups["business_code"].append(path)
        else:
            groups["unrelated"].append(path)
    return groups


def dump_lock(path: Path, lock: dict[str, Any]) -> None:
    try:
        import yaml  # type: ignore[import-not-found]

        rendered = yaml.safe_dump(lock, allow_unicode=True, sort_keys=False)
    except ModuleNotFoundError:
        rendered = json.dumps(lock, ensure_ascii=True, indent=2) + "\n"
    path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("business_repo", type=Path)
    parser.add_argument("contracts_root", type=Path)
    parser.add_argument("--rules", type=Path, help="impact-rules.yaml override")
    parser.add_argument("--write", action="store_true", help="advance version-lock.yaml")
    parser.add_argument("--init", action="store_true", help="create a draft version-lock baseline without execution evidence")
    parser.add_argument("--tests-adapted", action="store_true", help="confirm affected Bruno tests were adapted and run")
    parser.add_argument("--allow-draft", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--phase",
        choices=("before-generate", "before-execute", "complete"),
        help="phase gate; complete requires a passed coverage report",
    )
    parser.add_argument(
        "--completion-report",
        type=Path,
        help="coverage checker JSON report required for --phase complete or --write",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    if args.init and (args.write or args.phase or args.completion_report or args.tests_adapted or args.allow_draft):
        parser.error("--init cannot be combined with phase, completion, or update options")
    if args.allow_draft and args.phase != "before-execute":
        parser.error("--allow-draft is only valid for the first orchestrated before-execute gate")
    if not args.business_repo.is_dir():
        parser.error(f"business repository does not exist: {args.business_repo}")

    lock_path = args.contracts_root / "version-lock.yaml"
    if args.init:
        if lock_path.exists():
            print(f"ERROR: version lock already exists: {lock_path}")
            return 3
        digest = source_digest(args.business_repo, exclude_root=args.contracts_root.parent)
        current_sha = f"filesystem:{digest[:16]}"
        current_ref = "filesystem"
        initialized_at = datetime.now(timezone.utc).isoformat()
        lock = {
            "version": 1,
            "status": "draft",
            "business": {
                "repo": str(args.business_repo),
                "commit": current_sha,
                "ref": current_ref,
                "source_digest": digest,
                "initialized_at": initialized_at,
                "baseline_status": "draft",
            },
        }
        dump_lock(lock_path, lock)
        report = {"status": "initialized", "baseline_status": "draft", "current_sha": current_sha, "current_source_digest": digest}
        print(json.dumps(report, ensure_ascii=True, indent=2) if args.as_json else f"initialized draft version lock: {lock_path}")
        return 0

    completion_report: dict[str, Any] | None = None
    if args.completion_report:
        try:
            loaded = json.loads(args.completion_report.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read completion report {args.completion_report}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SystemExit("completion report must contain an object")
        completion_report = loaded
    strict_completion = bool(
        completion_report
        and completion_report.get("report_version") == 2
        and completion_report.get("check_profile") == "full-matrix-strict"
        and all(completion_report.get(field) is True for field in STRICT_COMPLETION_FIELDS)
        and completion_report.get("execution_scope") == "all"
        and completion_report.get("completion_ok") is True
        and completion_report.get("status") == "verified"
        and not completion_report.get("errors")
    )
    if (args.phase == "complete" or args.write) and not strict_completion:
        print("ERROR: completion phase requires a successful full-matrix-strict v2 all-scope coverage report")
        return 3
    if args.phase in {"before-generate", "before-execute"} and args.write:
        print("ERROR: version lock cannot be written during a pre-generation or pre-execution phase")
        return 3

    if not lock_path.is_file():
        raise SystemExit(f"missing version lock: {lock_path}")

    lock = load_data(lock_path)
    if not isinstance(lock, dict):
        raise SystemExit("version-lock.yaml must contain an object")
    lock_status = str(lock.get("status", "")).strip().lower()
    draft_bootstrap = args.allow_draft and args.phase == "before-execute" and lock_status == "draft"
    draft_completion = args.phase == "complete" and args.write and lock_status == "draft"
    if args.phase in {"before-execute", "complete"} and lock_status != "current" and not (draft_bootstrap or draft_completion):
        print(f"ERROR: business version lock status is {lock_status or '<missing>'}; expected current")
        return 3
    business = lock.get("business", {})
    locked_sha = business.get("commit") if isinstance(business, dict) else None
    current_digest = source_digest(args.business_repo, exclude_root=args.contracts_root.parent)
    current_sha = f"filesystem:{current_digest[:16]}"
    current_ref = "filesystem"
    locked_digest = business.get("source_digest") if isinstance(business, dict) else None

    report: dict[str, Any] = {
        "locked_sha": locked_sha,
        "current_sha": current_sha,
        "current_ref": current_ref,
        "version_control": "filesystem",
        "status": "current",
        "impact": "none",
        "changed_files": [],
        "dirty_files": [],
        "locked_source_digest": locked_digest,
        "current_source_digest": current_digest,
    }
    if locked_digest and locked_digest == current_digest:
        report["status"] = "current"
        report["source_digest_match"] = True
        if args.write:
            updated = dict(lock)
            updated_business = dict(business) if isinstance(business, dict) else {}
            updated_business.update(
                {
                    "repo": str(args.business_repo),
                    "commit": current_sha,
                    "ref": current_ref,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "impact_review": "none",
                    "changed_files": [],
                    "source_digest": current_digest,
                    "baseline_status": "current",
                }
            )
            updated["version"] = updated.get("version", 1)
            updated["status"] = "current"
            updated["business"] = updated_business
            dump_lock(lock_path, updated)
            report["status"] = "updated"
        if args.as_json:
            print(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            action = "updated" if args.write else "is current"
            print(f"version lock {action}: {current_digest}")
        return 0
    if not locked_sha:
        report.update(
            {
                "status": "unlocked-filesystem",
                "impact": "api-impact",
                "changed_files": ["<filesystem baseline missing>"],
                "error": "business repository has no filesystem baseline; review the current source digest before execution",
            }
        )
        if args.as_json:
            print(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            print("ERROR: business repository has no filesystem baseline; initialize version-lock.yaml after review")
        return 2

    paths = ["<filesystem source digest changed>"]
    rules = load_data(args.rules) if args.rules else {}
    if not isinstance(rules, dict):
        raise SystemExit("impact rules must contain an object")
    # A digest-only checkout cannot identify individual changed files. Treat a
    # changed source tree as API-impacting until a human reviews and adapts the
    # collection; classifying the placeholder as non-api would make the lock
    # advance without knowing what changed.
    impact = "api-impact"
    report.update({
        "status": "stale",
        "impact": impact,
        "changed_files": paths,
        "change_classes": change_classes(paths, rules),
    })

    if args.write:
        if impact == "api-impact" and not args.tests_adapted:
            report["error"] = "refusing to advance an API-impacting lock without --tests-adapted"
        else:
            updated = dict(lock)
            updated_business = dict(business) if isinstance(business, dict) else {}
            updated_business.update(
                {
                    "repo": str(args.business_repo),
                    "commit": current_sha,
                    "ref": current_ref,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "impact_review": impact,
                    "changed_files": paths,
                    "source_digest": current_digest,
                }
            )
            updated["version"] = updated.get("version", 1)
            updated["status"] = "current"
            updated_business["baseline_status"] = "current"
            updated["business"] = updated_business
            dump_lock(lock_path, updated)
            report["status"] = "updated"
    if args.as_json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(f"business SHA: {locked_sha} -> {current_sha}")
        print(f"impact: {impact}")
        for path in paths:
            print(f"changed: {path}")
        if report["status"] == "updated":
            print(f"updated {lock_path}")
        elif impact == "api-impact":
            print("ERROR: adapt and execute affected module Bruno tests before updating the lock")
        else:
            print("INFO: no API-impacting files detected; update the lock to current HEAD")
    if report.get("error"):
        return 3
    if report["status"] == "stale":
        return 3 if impact == "api-impact" else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
