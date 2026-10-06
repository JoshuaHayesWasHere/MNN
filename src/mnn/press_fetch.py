"""The Press: get today's edition, print it, and record how it went.

One run does one thing: it gets an edition, renders the EPUB and front page
with build_paper.py, and moves them into the server's data directory. Where
the edition comes from depends on STAGE_URL.

Unset (the default), the Press writes the paper itself. It runs the sources
listed in sources.toml (see press_sources.py), each on its own, and prints
whatever sections came back. A section whose source failed is left out; the
outcome of every section is written to press/report.json in the data
directory. One paper is printed per edition time (--at).

Set, a remote author has taken over. The Press reads the `latest` pointer from
staging and, if that edition has not been printed here yet, downloads it,
prints it, and writes a receipt back to staging. Needs STAGE_TOKEN too.

    nothing new         exit 0, silently
    printed             exit 0, receipt says ok
    nothing to print,   exit 1, receipt (and report) say what failed;
    or the build failed yesterday's paper stays exactly where it was

By default it runs once and exits, for a timer to call. With --loop it stays
up and schedules itself: a first check at --at (06:40), then every
--retry-minutes until --until (09:00) in case the edition is staged late or
the feeds were unreachable, then nothing until the next morning.

Usage:
    uv run mnn-press fetch [--data-dir DIR] [--fetch-weather] [--force]
    uv run mnn-press fetch --loop [--at 06:40] [--until 09:00] [--retry-minutes 10]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

from mnn import press_sources, server, staging
from mnn.staging import Stage, StageError


class PressError(Exception):
    """No paper could be made: the edition was rejected or would not build,
    or no source returned a section to print."""


def render(edition_path: Path, expected_date: str, work: Path, fetch_weather: bool,
           config_dir: Path | None = None) -> dict[str, tuple[str, bytes]]:
    """Build the EPUB and front page into a scratch directory and return them
    in the shape server.check_edition takes."""
    # Imported where it is used, as press_sources does.
    from mnn import build_paper

    try:
        edition = build_paper.load_edition(edition_path)
    except (OSError, build_paper.EditionError) as exc:
        reason = str(exc).replace(f"{edition_path}: ", "")
        raise PressError(f"edition file rejected: {reason}") from exc
    if edition.date.isoformat() != expected_date:
        raise PressError(f"pointer says {expected_date} but the edition file is dated "
                         f"{edition.date.isoformat()}")
    if fetch_weather:
        edition = build_paper.with_weather(edition)
    # The front page portrait, if the config directory holds one.
    edition = build_paper.with_portrait(edition, config_dir)
    try:
        frontpage = build_paper.write_front_page(edition, work)
        epub = build_paper.write_epub(edition, work)
    except Exception as exc:  # the builder's failures are not ours to enumerate
        raise PressError(f"build failed: {type(exc).__name__}: {exc}") from exc
    return {"epub": (epub.name, epub.read_bytes()),
            "frontpage": (frontpage.name, frontpage.read_bytes())}


def note_staging(data_dir: Path, error: str | None) -> None:
    """Record that staging could not be read, so a morning with no edition to
    pin a receipt to still leaves something to report (see muse.py), and clear
    that record once staging answers again."""
    report = staging.load_report(data_dir)
    if error is None and (report.get("mode") != "staging" or report.get("status") == "ok"):
        return
    staging.save_report(data_dir, {
        "mode": "staging", "edition_date": None, "ran_at": staging.now(),
        "status": "failed" if error else "ok", "error": error, "sections": []})


def fetch(stage: Stage, data_dir: Path, *, fetch_weather: bool, force: bool,
          keep_days: int, config_dir: Path | None = None) -> int:
    press = staging.press_dir(data_dir)
    state_path = press / "state.json"
    state = staging.load_json(state_path)

    # A receipt that did not get through last time goes first.
    if staging.load_receipt(data_dir).get("unsent"):
        staging.send_receipt(data_dir, stage)

    try:
        pointer = stage.latest()
        date = pointer["date"]
        dt.date.fromisoformat(date)
        fingerprint = pointer["edition"]["sha256"]
    except StageError as exc:
        if exc.status == 404:
            note_staging(data_dir, None)
            return 0  # nothing has been staged yet
        # Staging unreachable: there is no edition to pin a receipt to.
        note_staging(data_dir, str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError) as exc:
        note_staging(data_dir, f"the latest pointer is malformed ({exc!r})")
        print(f"error: the latest pointer is malformed ({exc!r})", file=sys.stderr)
        return 1
    note_staging(data_dir, None)

    if not force and fingerprint in (state.get("printed"), state.get("failed")):
        return 0

    receipt = {
        "edition_date": date,
        "edition_sha256": fingerprint,
        "press": socket.gethostname(),
        "kindle_downloaded_at": None,
    }
    try:
        with tempfile.TemporaryDirectory(dir=press, prefix="work-") as scratch:
            work = Path(scratch)
            edition_path = work / pointer["edition"]["name"]
            staging.write_atomic(edition_path, stage.fetch_verified(pointer["edition"]))
            parts = render(edition_path, date, work, fetch_weather, config_dir)
            # Checked against the Kindle contract, then renamed into place.
            server.check_edition(parts)
            server.store_edition(data_dir, parts)
            pruned = server.prune_editions(data_dir, keep_days)
    except (StageError, PressError, server.EditionRejected, OSError) as exc:
        # Only a build that will fail the same way again is remembered. A
        # download that broke is worth another try on the next run.
        if not isinstance(exc, (StageError, OSError)):
            state["failed"] = fingerprint
            staging.write_atomic(state_path, staging.dump(state))
        receipt.update(status="failed", build_ok=False, error=str(exc), reported_at=staging.now())
        staging.save_receipt(data_dir, receipt)
        staging.send_receipt(data_dir, stage)
        print(f"error: {date}: {exc}; still serving the previous edition", file=sys.stderr)
        return 1

    state.update(printed=fingerprint, date=date)
    state.pop("failed", None)
    staging.write_atomic(state_path, staging.dump(state))
    receipt.update(
        status="ok", build_ok=True, error=None, built_at=staging.now(), pruned=pruned,
        files={field: {"name": name, "size": len(content)}
               for field, (name, content) in parts.items()})
    staging.save_receipt(data_dir, receipt)
    if not staging.send_receipt(data_dir, stage):
        print(f"warning: {date} is printed but the receipt could not be sent; "
              "it will be retried on the next run", file=sys.stderr)
    print(f"printed {date}: " + ", ".join(name for name, _ in parts.values()))
    return 0


def last_due(now: dt.datetime, at: dt.time) -> dt.datetime:
    """The most recent edition time: today's if it has passed, else yesterday's."""
    due = dt.datetime.combine(now.date(), at)
    return due if now >= due else due - dt.timedelta(days=1)


def write_paper(data_dir: Path, config_dir: Path | None, *, at: dt.time, fetch_weather: bool,
                force: bool, keep_days: int, now: dt.datetime | None = None) -> int:
    """Print today's paper from the Press's own sources, unless one has
    already been printed since the last edition time."""
    now = now or dt.datetime.now()
    press = staging.press_dir(data_dir)
    state_path = press / "state.json"
    state = staging.load_json(state_path)
    try:
        printed_at = dt.datetime.fromisoformat(state.get("sources_printed_at") or "")
    except ValueError:
        printed_at = None
    # A new install prints straight away; after that, once per edition time.
    if not force and printed_at is not None and printed_at >= last_due(now, at):
        return 0

    date = now.date()
    report = {"mode": "sources", "edition_date": date.isoformat(), "ran_at": staging.now(),
              "status": "failed", "error": None, "sections": []}
    receipt = {"edition_date": date.isoformat(), "press": socket.gethostname(),
               "kindle_downloaded_at": None}
    parts, pruned = {}, []
    try:
        paper = press_sources.load_config(config_dir)
        report["config"] = str(paper.path)
        sections, report["sections"] = press_sources.run_sources(paper, date, config_dir)
        if not sections:
            if any(outcome["status"] == "failed" for outcome in report["sections"]):
                raise PressError("every section failed, so there is nothing to print")
            raise PressError("no section had anything to print today")
        with tempfile.TemporaryDirectory(dir=press, prefix="work-") as scratch:
            work = Path(scratch)
            edition_path = work / f"edition-{date.isoformat()}.json"
            edition_path.write_text(
                json.dumps(press_sources.assemble(paper, date, sections)), encoding="utf-8")
            parts = render(edition_path, date.isoformat(), work, fetch_weather, config_dir)
            # Checked against the Kindle contract, then renamed into place.
            server.check_edition(parts)
            server.store_edition(data_dir, parts)
            pruned = server.prune_editions(data_dir, keep_days)
    except (press_sources.ConfigError, PressError, server.EditionRejected, OSError) as exc:
        report["error"] = str(exc)
        staging.save_report(data_dir, report)
        receipt.update(status="failed", build_ok=False, error=str(exc), reported_at=staging.now())
        staging.save_receipt(data_dir, receipt)
        print(f"error: {date}: {exc}; still serving the previous edition", file=sys.stderr)
        for outcome in report["sections"]:
            if outcome["status"] == "failed":
                print(f"  {outcome['title']}: {outcome['error']}", file=sys.stderr)
        return 1

    failed = [outcome for outcome in report["sections"] if outcome["status"] == "failed"]
    report["status"] = "partial" if failed else "ok"
    staging.save_report(data_dir, report)
    state["sources_printed_at"] = now.isoformat(timespec="seconds")
    staging.write_atomic(state_path, staging.dump(state))
    receipt.update(
        status="ok", build_ok=True, error=None, built_at=staging.now(), pruned=pruned,
        files={field: {"name": name, "size": len(content)}
               for field, (name, content) in parts.items()})
    staging.save_receipt(data_dir, receipt)
    print(f"printed {date} from {len(sections)} of {len(report['sections'])} sections: "
          + ", ".join(name for name, _ in parts.values()))
    for outcome in failed:
        print(f"warning: section {outcome['title']!r} left out: {outcome['error']}",
              file=sys.stderr)
    return 0


def next_check(now: dt.datetime, at: dt.time, until: dt.time, retry: dt.timedelta) -> dt.datetime:
    """The next moment to look for an edition: `at`, then every `retry` up to and
    including `until`, then `at` again tomorrow. `at` is always a check of its
    own day, even when it is later than `until`."""
    slot = dt.datetime.combine(now.date(), at)
    last = max(dt.datetime.combine(now.date(), until), slot)
    while slot <= last:
        if slot > now:
            return slot
        slot += retry
    return dt.datetime.combine(now.date() + dt.timedelta(days=1), at)


# How often a waiting fetcher signs in, so /api/health can tell it is running.
HEARTBEAT_SECONDS = 300


def run_forever(check, at: dt.time, until: dt.time, retry: dt.timedelta,
                beat=lambda: None) -> None:
    # One look straight away, so a restart during the morning does not skip
    # the day and a fresh install prints without waiting for tomorrow.
    beat()
    check()
    while True:
        due = next_check(dt.datetime.now(), at, until, retry)
        print(f"next check at {due:%Y-%m-%d %H:%M}", flush=True)
        # Slept in short steps so a clock change or a suspended host cannot
        # leave it waiting on a stale deadline.
        beaten = time.monotonic()
        while (remaining := (due - dt.datetime.now()).total_seconds()) > 0:
            if time.monotonic() - beaten >= HEARTBEAT_SECONDS:
                beat()
                beaten = time.monotonic()
            time.sleep(min(remaining, 60))
        beat()
        check()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print today's edition: from the Press's own sources, or from "
                    "staging when STAGE_URL is set.")
    parser.add_argument("--data-dir", type=Path,
                        default=os.environ.get(server.DATA_DIR_ENV) or server.default_data_dir(),
                        help="the server's data directory (default: as for server.py)")
    parser.add_argument("--fetch-weather", action="store_true",
                        help="fill in the forecast when the edition has none")
    parser.add_argument("--force", action="store_true",
                        help="print again even if this edition was already handled")
    parser.add_argument("--keep-days", type=int, default=14,
                        help="drop editions more than this many days older than the "
                             "newest (default: 14)")
    parser.add_argument("--loop", action="store_true",
                        help="stay running and check on a daily schedule")
    parser.add_argument("--at", type=dt.time.fromisoformat, default=dt.time(6, 40),
                        metavar="HH:MM",
                        help="the edition time: with --loop, the first check of the day; "
                             "without staging, one paper is printed per edition time "
                             "(default: 06:40)")
    parser.add_argument("--until", type=dt.time.fromisoformat, default=dt.time(9, 0),
                        metavar="HH:MM", help="with --loop: last retry of the day (default: 09:00)")
    parser.add_argument("--retry-minutes", type=int, default=10,
                        help="with --loop: minutes between checks from --at to --until (default: 10)")
    parser.add_argument("--config-dir", type=Path, default=press_sources.default_config_dir(),
                        help="where your own sources.toml, source modules and front page "
                             "portrait live "
                             f"(default: ${press_sources.CONFIG_DIR_ENV}, else "
                             "~/.config/morning-paper)")
    parser.add_argument("--url", help="staging address (default: $STAGE_URL); when there "
                                      "is none, the Press writes its own paper")
    parser.add_argument("--token", help="staging token (default: $STAGE_TOKEN)")
    args = parser.parse_args(argv)
    if args.retry_minutes < 1:
        parser.error("--retry-minutes must be at least 1")

    data_dir = Path(args.data_dir).expanduser()
    config_dir = Path(args.config_dir).expanduser()
    staging.press_dir(data_dir).mkdir(parents=True, exist_ok=True)
    if staging.configured(args.url):
        # Staging takes precedence: a remote author has taken over the paper.
        try:
            stage = Stage.from_env(args.url, args.token)
        except StageError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        def check(force: bool = False) -> int:
            return fetch(stage, data_dir, fetch_weather=args.fetch_weather, force=force,
                         keep_days=args.keep_days, config_dir=config_dir)
    else:
        def check(force: bool = False) -> int:
            return write_paper(data_dir, config_dir, at=args.at,
                               fetch_weather=args.fetch_weather, force=force,
                               keep_days=args.keep_days)

    if not args.loop:
        # A forced run is a reprint someone asked for, not the schedule.
        if not args.force:
            staging.save_heartbeat(data_dir, loop=False)
        return check(args.force)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        run_forever(check, args.at, args.until, dt.timedelta(minutes=args.retry_minutes),
                    beat=lambda: staging.save_heartbeat(data_dir, loop=True))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
