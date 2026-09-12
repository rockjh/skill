#!/usr/bin/env python3
"""Check and optionally advance the business-code SHA used by Bruno tests."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from manifest_io import load_data


DEFAULT_API_PATTERNS = [
    "**/controller/**",
    "*Controller.java",
    "**/service/**",
    "*Service.java",
    "**/dto/**",
    "**/domain/**",
    "**/exception/**",
    "*Exception.java",
    "**/config/**",
    "**/resources/application*.yml",
    "**/resources/application*.yaml",
    "**/resources/application*.properties",
    "**/swagger*",
    "**/openapi*",
    "**/pom.xml",
    "pom.xml",
]


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: {exc.output.strip()}") from exc


def changed_files(repo: Path, old_sha: str, current_sha: str) -> list[str]:
    raw = git(repo, "diff", "--name-status", f"{old_sha}..{current_sha}")
    paths: list[str] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            paths.append(parts[-1])
    return paths


def dirty_files(repo: Path) -> list[str]:
    """Return tracked worktree paths that are not represented by HEAD."""

    try:
        # Do not use git(), whose strip() would remove the leading porcelain
        # status column and shift the path by one character.
        raw = subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
            text=True,
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
        paths.append(value)
    return paths


def classify(paths: list[str], rules: dict[str, Any]) -> str:
    api_patterns = rules.get("api_patterns", DEFAULT_API_PATTERNS)
    ignored = rules.get("ignore_patterns", [])
    for path in paths:
        if any(fnmatch.fnmatch(path, pattern) for pattern in ignored):
            continue
        if any(fnmatch.fnmatch(path, pattern) for pattern in api_patterns):
            return "api-impact"
    return "non-api"


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
    parser.add_argument("--tests-adapted", action="store_true", help="confirm affected Bruno tests were adapted and run")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    lock_path = args.contracts_root / "version-lock.yaml"
    if not lock_path.is_file():
        raise SystemExit(f"missing version lock: {lock_path}")
    if not args.business_repo.is_dir():
        parser.error(f"business repository does not exist: {args.business_repo}")

    lock = load_data(lock_path)
    if not isinstance(lock, dict):
        raise SystemExit("version-lock.yaml must contain an object")
    business = lock.get("business", {})
    locked_sha = business.get("commit") if isinstance(business, dict) else None
    if not locked_sha:
        locked_sha = lock.get("business_git_sha")
    current_sha = git(args.business_repo, "rev-parse", "HEAD")
    current_ref = git(args.business_repo, "symbolic-ref", "--short", "-q", "HEAD") or "detached"
    dirty = dirty_files(args.business_repo)

    report: dict[str, Any] = {
        "locked_sha": locked_sha,
        "current_sha": current_sha,
        "current_ref": current_ref,
        "status": "current",
        "impact": "none",
        "changed_files": [],
        "dirty_files": dirty,
    }
    if locked_sha == current_sha:
        if dirty:
            impact = classify(dirty, load_data(args.rules) if args.rules else {})
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
    if not locked_sha:
        report["status"] = "missing-locked-sha"
        message = "version-lock.yaml has no business commit; initialize it after a reviewed baseline"
        if args.as_json:
            print(json.dumps({**report, "error": message}, ensure_ascii=True, indent=2))
        else:
            print(f"ERROR: {message}")
        return 3

    paths = changed_files(args.business_repo, str(locked_sha), current_sha)
    rules = load_data(args.rules) if args.rules else {}
    if not isinstance(rules, dict):
        raise SystemExit("impact rules must contain an object")
    impact = classify(paths, rules)
    report.update({"status": "stale", "impact": impact, "changed_files": paths})

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
