#!/usr/bin/env python3
"""Check and optionally advance the business-code version used by Bruno tests."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from manifest_io import load_data


DEFAULT_API_PATTERNS = [
    "**/controller/**",
    "*Controller.java",
    "**/service/**",
    "*Service.java",
    "**/dto/**",
    "**/domain/**",
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


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            text=True,
            encoding="utf-8",
            errors="strict",
            stderr=subprocess.STDOUT,
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: {exc.output.strip()}") from exc


def current_git_commit(repo: Path) -> str:
    commit = git(repo, "rev-parse", "HEAD")
    if not commit:
        raise SystemExit(f"git rev-parse HEAD returned an empty commit in {repo}")
    return commit


def is_qa_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized == "qa" or normalized.startswith("qa/")


def git_available(repo: Path) -> bool:
    try:
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-dir"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def changed_files(repo: Path, old_sha: str, current_sha: str) -> list[str]:
    raw = git(repo, "diff", "--name-status", f"{old_sha}..{current_sha}")
    paths: list[str] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            paths.append(parts[-1])
    return paths


def dirty_files(repo: Path) -> list[str]:
    """Return business worktree paths that are not represented by HEAD."""

    try:
        # Do not use git(), whose strip() would remove the leading porcelain
        # status column and shift the path by one character.
        raw = subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            text=True,
            encoding="utf-8",
            errors="strict",
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git status failed in {repo}: {exc.output.strip()}") from exc
    paths: list[str] = []
    for line in raw.splitlines():
        if len(line) < 4:
            continue
        value = line[3:]
        # Rename/copy status is rendered as "old -> new"; the new path is the
        # observable file that needs impact classification.
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[-1]
        if not is_qa_path(value):
            paths.append(value)
    return paths


def source_digest(repo: Path) -> str:
    """Hash tracked business files while ignoring the repository's qa tree."""

    try:
        raw = subprocess.check_output(
            ["git", "-C", str(repo), "ls-files", "-z"],
            encoding="utf-8",
            errors="strict",
            stderr=subprocess.DEVNULL,
        )
        values = [item for item in raw.split("\0") if item]
    except (OSError, subprocess.CalledProcessError):
        values = [
            str(path.relative_to(repo))
            for path in repo.rglob("*")
            if path.is_file()
            and not any(part in {".git", "qa", "node_modules", "target", "build", ".venv", "venv"} for part in path.parts)
        ]
    digest = hashlib.sha256()
    for value in sorted(values):
        if value == "qa" or value.startswith("qa/"):
            continue
        path = repo / value
        if not path.is_file():
            continue
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def classify(paths: list[str], rules: dict[str, Any]) -> str:
    api_patterns = rules.get("api_patterns", DEFAULT_API_PATTERNS)
    ignored = [*DEFAULT_IGNORE_PATTERNS, *rules.get("ignore_patterns", [])]
    for path in paths:
        if any(fnmatch.fnmatch(path, pattern) for pattern in ignored):
            continue
        if any(fnmatch.fnmatch(path, pattern) for pattern in api_patterns):
            return "api-impact"
    return "non-api"


def change_classes(paths: list[str], rules: dict[str, Any]) -> dict[str, list[str]]:
    ignored = [*DEFAULT_IGNORE_PATTERNS, *rules.get("ignore_patterns", [])]
    api_patterns = rules.get("api_patterns", DEFAULT_API_PATTERNS)
    groups = {"business_code": [], "qa_assets": [], "unrelated": []}
    for path in paths:
        if is_qa_path(path):
            groups["qa_assets"].append(path)
        elif any(fnmatch.fnmatch(path, pattern) for pattern in ignored):
            groups["unrelated"].append(path)
        elif any(fnmatch.fnmatch(path, pattern) for pattern in api_patterns):
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

    if args.init and (args.write or args.phase or args.completion_report or args.tests_adapted):
        parser.error("--init cannot be combined with phase, completion, or update options")
    if not args.business_repo.is_dir():
        parser.error(f"business repository does not exist: {args.business_repo}")

    lock_path = args.contracts_root / "version-lock.yaml"
    if args.init:
        if lock_path.exists():
            print(f"ERROR: version lock already exists: {lock_path}")
            return 3
        digest = source_digest(args.business_repo)
        has_git = git_available(args.business_repo)
        current_sha = current_git_commit(args.business_repo) if has_git else f"filesystem:{digest[:16]}"
        current_ref = git(args.business_repo, "symbolic-ref", "--short", "-q", "HEAD") or "detached" if has_git else "filesystem"
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
    if (args.phase == "complete" or args.write) and (
        not completion_report or completion_report.get("completion_ok") is not True or completion_report.get("status") != "verified"
    ):
        print("ERROR: completion phase requires a coverage report with completion_ok=true and status=verified")
        return 3
    if args.phase in {"before-generate", "before-execute"} and args.write:
        print("ERROR: version lock cannot be written during a pre-generation or pre-execution phase")
        return 3

    if not lock_path.is_file():
        raise SystemExit(f"missing version lock: {lock_path}")

    lock = load_data(lock_path)
    if not isinstance(lock, dict):
        raise SystemExit("version-lock.yaml must contain an object")
    business = lock.get("business", {})
    locked_sha = business.get("commit") if isinstance(business, dict) else None
    if not locked_sha:
        locked_sha = lock.get("business_git_sha")
    has_git = git_available(args.business_repo)
    current_digest = source_digest(args.business_repo)
    if has_git:
        current_sha = current_git_commit(args.business_repo)
        current_ref = git(args.business_repo, "symbolic-ref", "--short", "-q", "HEAD") or "detached"
        dirty = dirty_files(args.business_repo)
    else:
        current_sha = f"filesystem:{current_digest[:16]}"
        current_ref = "filesystem"
        dirty = []
    locked_digest = business.get("source_digest") if isinstance(business, dict) else None

    report: dict[str, Any] = {
        "locked_sha": locked_sha,
        "current_sha": current_sha,
        "current_ref": current_ref,
        "version_control": "git" if has_git else "filesystem",
        "status": "current",
        "impact": "none",
        "changed_files": [],
        "dirty_files": dirty,
        "locked_source_digest": locked_digest,
        "current_source_digest": current_digest,
    }
    if locked_digest and locked_digest == current_digest:
        if dirty:
            rules = load_data(args.rules) if args.rules else {}
            if not isinstance(rules, dict):
                raise SystemExit("impact rules must contain an object")
            ignore_patterns = [*DEFAULT_IGNORE_PATTERNS, *rules.get("ignore_patterns", [])]
            business_dirty = [
                path for path in dirty
                if not any(fnmatch.fnmatch(path, pattern) for pattern in ignore_patterns)
            ]
            if business_dirty:
                impact = classify(business_dirty, rules)
                report.update({
                    "status": "dirty",
                    "impact": impact,
                    "changed_files": business_dirty,
                    "change_classes": change_classes(dirty, rules),
                })
                message = "business repository has untracked or modified files not represented by the locked source digest"
                if args.as_json:
                    print(json.dumps({**report, "error": message}, ensure_ascii=True, indent=2))
                else:
                    print(f"ERROR: {message}")
                    print(f"impact: {impact}")
                    for path in business_dirty:
                        print(f"dirty: {path}")
                return 3 if impact == "api-impact" else 2
        report["status"] = "current"
        report["source_digest_match"] = True
        if dirty:
            report["qa_dirty_files"] = dirty
        if args.as_json:
            print(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            suffix = " (only ignored QA files are dirty)" if dirty else ""
            print(f"version lock source digest is current: {current_digest}{suffix}")
        return 0
    if locked_sha == current_sha:
        if dirty:
            rules = load_data(args.rules) if args.rules else {}
            if not isinstance(rules, dict):
                raise SystemExit("impact rules must contain an object")
            ignore_patterns = [*DEFAULT_IGNORE_PATTERNS, *rules.get("ignore_patterns", [])]
            business_dirty = [
                path for path in dirty
                if not any(fnmatch.fnmatch(path, pattern) for pattern in ignore_patterns)
            ]
            if not business_dirty:
                report.update({"status": "current", "impact": "non-api", "qa_dirty_files": dirty})
                if args.as_json:
                    print(json.dumps(report, ensure_ascii=True, indent=2))
                else:
                    print("version lock is current; only ignored QA files are dirty")
                return 0
            impact = classify(dirty, rules)
            report.update({"status": "dirty", "impact": impact, "changed_files": dirty})
            message = "business repository has tracked changes not represented by the locked commit"
            if args.as_json:
                print(json.dumps({**report, "error": message}, ensure_ascii=True, indent=2))
            else:
                print(f"ERROR: {message}")
                print(f"impact: {impact}")
                for path in dirty:
                    print(f"dirty: {path}")
            return 3 if impact == "api-impact" else 2
        print(json.dumps(report, ensure_ascii=True, indent=2) if args.as_json else f"version lock is current: {current_sha}")
        return 0
    if not locked_sha and has_git:
        report["status"] = "missing-locked-sha"
        message = "version-lock.yaml has no business commit; initialize it after a reviewed baseline"
        if args.as_json:
            print(json.dumps({**report, "error": message}, ensure_ascii=True, indent=2))
        else:
            print(f"ERROR: {message}")
        return 3
    if not locked_sha and not has_git:
        report.update(
            {
                "status": "unlocked-filesystem",
                "impact": "api-impact",
                "changed_files": ["<filesystem baseline missing>"],
                "error": "business repository has no Git baseline; review the filesystem digest before execution",
            }
        )
        if args.as_json:
            print(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            print("ERROR: business repository has no Git baseline; initialize version-lock.yaml after review")
        return 2

    if has_git and not str(locked_sha).startswith("filesystem:"):
        all_paths = changed_files(args.business_repo, str(locked_sha), current_sha)
        paths = [path for path in all_paths if not is_qa_path(path)]
    else:
        paths = ["<filesystem source digest changed>"]
    rules = load_data(args.rules) if args.rules else {}
    if not isinstance(rules, dict):
        raise SystemExit("impact rules must contain an object")
    # A digest-only checkout cannot identify individual changed files. Treat a
    # changed source tree as API-impacting until a human reviews and adapts the
    # collection; classifying the placeholder as non-api would make the lock
    # advance without knowing what changed.
    impact = "api-impact" if not has_git else classify(paths, rules)
    report.update({
        "status": "stale",
        "impact": impact,
        "changed_files": paths,
        "change_classes": change_classes(all_paths if has_git else paths, rules),
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
