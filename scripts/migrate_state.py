"""Move legacy local state to the dltk state directory once."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def migrate(source: Path, destination: Path) -> bool:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.exists():
        return False
    if destination.exists():
        raise RuntimeError(f"refusing to merge state into existing destination: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Perform the one-time local state migration to dltk.")
    legacy_state = "dev" + "-ai"
    parser.add_argument("--source", type=Path, default=Path.home() / ".local" / "state" / legacy_state)
    parser.add_argument("--destination", type=Path, default=Path.home() / ".local" / "state" / "dltk")
    args = parser.parse_args()
    moved = migrate(args.source, args.destination)
    print(f"migrated {args.source} -> {args.destination}" if moved else f"no legacy state at {args.source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
