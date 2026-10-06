"""Stage an assembled edition for the Press to pull.

Uploads the edition JSON, then moves the `latest` pointer to it. The pointer
goes last and is read back, so the Press never sees a pointer to a file that
is not there.

Usage:
    uv run mnn-press stage-put edition/2026-10-05.json

Needs STAGE_URL and STAGE_TOKEN. Exit status is non-zero on any failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from mnn.staging import Stage, StageError, describe, now


def edition_date(raw: bytes, path: Path) -> str:
    try:
        date = json.loads(raw)["date"]
        dt.date.fromisoformat(date)
    except (ValueError, KeyError, TypeError) as exc:
        raise StageError(f"{path}: not an edition file with a YYYY-MM-DD 'date'") from exc
    return date


def stage_edition(stage: Stage, edition: Path) -> dict:
    try:
        edition_bytes = edition.read_bytes()
    except OSError as exc:
        raise StageError(str(exc)) from exc
    date = edition_date(edition_bytes, edition)

    pointer = {
        "date": date,
        "edition": describe(f"edition-{date}.json", edition_bytes),
        "staged_at": now(),
    }
    stage.put(pointer["edition"]["name"], edition_bytes, "application/json")

    # The file the pointer names is in place and intact before it moves.
    stage.fetch_verified(pointer["edition"])
    stage.put_latest(pointer)
    if stage.latest() != pointer:
        raise StageError("the latest pointer read back differently from what was sent")
    return pointer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage an edition for the Press to pull.")
    parser.add_argument("edition", type=Path, help="assembled edition JSON")
    parser.add_argument("--url", help="staging address (default: $STAGE_URL)")
    parser.add_argument("--token", help="staging token (default: $STAGE_TOKEN)")
    args = parser.parse_args(argv)

    try:
        pointer = stage_edition(Stage.from_env(args.url, args.token), args.edition)
    except StageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"staged {pointer['date']}: {pointer['edition']['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
