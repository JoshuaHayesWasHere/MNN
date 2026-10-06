"""The three things you can ask the Press, each answered as one JSON object.

    paper_status    is today's edition printed and on the Kindle, and which
                    sections made it?
    paper_rebuild   print today's edition again
    paper_health    is the Press itself in working order?

These are plain commands. They work from a shell today, and they are what the
Muse gadget commands in musegadget_commands.py run. Nothing here talks to
Muse or holds an SDK token.

Usage:
    uv run mnn-press paper_status
    docker compose exec press-server python -m mnn paper_status
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mnn import REPO, muse, server, staging
from mnn.staging import Stage, StageError

SERVER_URL_ENV = "PAPER_SERVER"
DEFAULT_SERVER_URL = "http://127.0.0.1:8484"


def data_dir() -> Path:
    return Path(os.environ.get(server.DATA_DIR_ENV) or server.default_data_dir()).expanduser()


def paper_status() -> dict:
    today = dt.date.today().isoformat()
    papers = server.editions(data_dir(), server.EPUB_RE)
    printed = papers[0][0] if papers else None
    receipt = staging.load_receipt(data_dir())

    # Shared with GET /api/status: the mode and, when the Press writes its
    # own paper, what became of each section.
    press = server.press_status(data_dir())
    staged, staging_error = None, None
    if press["mode"] == "staging":
        try:
            staged = Stage.from_env().latest().get("date")
        except StageError as exc:
            staging_error = str(exc)

    kindle_at = receipt.get("kindle_downloaded_at") if receipt.get("edition_date") == printed else None
    return {
        "today": today,
        "mode": press["mode"],
        "staged_date": staged,
        "staging_error": staging_error,
        "printed_date": printed,
        "printed_today": printed == today,
        "press_has_staged_edition": staged is not None and staged == printed,
        "kindle_downloaded_at": kindle_at,
        # The Kindle's last successful download, whichever edition it was.
        "kindle": press["kindle"],
        "last_receipt": {key: receipt.get(key) for key in
                         ("edition_date", "status", "error", "built_at", "unsent")},
        "last_run": press.get("last_run"),
        "sections": press.get("sections"),
        "failed_sections": press.get("failed_sections"),
    }


def paper_rebuild() -> dict:
    """Runs the fetcher the same way the schedule does, but with --force."""
    result = subprocess.run(
        [sys.executable, "-m", "mnn", "fetch", "--force", "--fetch-weather"],
        cwd=REPO, capture_output=True, text=True, timeout=300)
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "output": (result.stdout + result.stderr).strip()[-2000:],
        "status": paper_status(),
    }


def paper_health() -> dict:
    directory = data_dir()
    usage = shutil.disk_usage(directory) if directory.is_dir() else None
    url = os.environ.get(SERVER_URL_ENV, DEFAULT_SERVER_URL).rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/api/display", timeout=10) as response:
            serving = json.load(response)
        serving_error = None
    except urllib.error.HTTPError as exc:
        serving, serving_error = None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        serving, serving_error = None, str(exc)

    # Only meaningful on a bare-metal install; a container has no systemd.
    units = {}
    for unit in (("morning-paper-server.service", "morning-paper-fetch.timer")
                 if shutil.which("systemctl") else ()):
        try:
            units[unit] = subprocess.run(["systemctl", "--user", "is-active", unit],
                                         capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            units[unit] = f"unknown ({exc})"

    receipt = staging.load_receipt(directory)
    press = server.press_status(directory)
    problems = []
    if serving is None:
        problems.append(f"server not answering at {url}: {serving_error}")
    if usage and usage.free < muse.LOW_DISK_BYTES:
        problems.append("less than 200 MB free in the data directory")
    if receipt.get("status") == "failed":
        problems.append(f"last print failed: {receipt.get('error')}")
    if receipt.get("unsent"):
        problems.append("a receipt is waiting to be sent to staging")
    for section in press.get("failed_sections") or []:
        problems.append(f"section {section['title']!r} was left out of the last paper: "
                        f"{section['error']}")
    return {
        "healthy": not problems,
        "problems": problems,
        "mode": press["mode"],
        "server_url": url,
        "serving_edition": serving.get("edition_date") if serving else None,
        "data_dir": str(directory),
        "free_mb": usage.free // (1024 * 1024) if usage else None,
        "editions_kept": len(server.editions(directory, server.EPUB_RE)),
        "staging_configured": bool(os.environ.get(staging.URL_ENV)
                                   and os.environ.get(staging.TOKEN_ENV)),
        "units": units,
    }


COMMANDS = {"paper_status": paper_status, "paper_rebuild": paper_rebuild,
            "paper_health": paper_health}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask the Press about the paper.")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    result = COMMANDS[args.command]()
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) and result.get("healthy", True) else 1


if __name__ == "__main__":
    sys.exit(main())
