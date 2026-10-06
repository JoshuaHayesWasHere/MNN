"""LAN server for the Morning Paper.

Serves the editions in its data directory:

    GET  /opds              OPDS 1.2 catalog of the available EPUBs
    GET  /paper/<file>.epub one EPUB
    GET  /paper/latest.txt  file name of the newest EPUB
    GET  /api/display       TRMNL-style JSON pointing at the newest front page
    GET  /frontpage.png     the newest front page PNG
    GET  /api/status        how the last print went, section by section
    GET  /card.jpg          that status as a small card for a Muse display board
    GET  /card.rgb565       the same card as raw pixels
    GET  /api/health        whether the Press is fine and, if not, what is wrong
    GET  /kindle/install.sh the Kindle installer, with this server's address filled in
                            (add ?download to save it as a file for the Kindle)
    GET  /kindle/manifest   plain text: what the Kindle should hold, each file with
                            its SHA-256, and when to wake next
    GET  /kindle/<name>     any file the manifest names, and the bootstrap
    POST /api/rebuild       print today's edition again (needs PRESS_TOKEN)
    POST /api/log           append device log lines to server.log

With MUSE_MESSAGES set it also tells a paired Muse gadget how the morning
went, and with MUSE_CARD_URL asks it to show the card (see muse.py). Both
are off by default and never hold up the paper.

Papers arrive in the data directory from press_fetch.py; nothing can be
uploaded to this server. When the Kindle logs that it has downloaded an
edition, the Press records it (press/kindle.json), adds it to the receipt and,
if STAGE_URL and STAGE_TOKEN are set, sends that on to staging.

Reading and POST /api/log need no authentication, and there is no TLS: this
is for a trusted home LAN. The one request that makes the Press print,
POST /api/rebuild, needs "Authorization: Bearer <PRESS_TOKEN>" and is off
while PRESS_TOKEN is unset.

Usage:
    uv run mnn-press server [--host 0.0.0.0] [--port 8484] [--data-dir DIR]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hmac
import io
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from mnn import REPO, card, muse, staging

EPUB_RE = re.compile(r"morning-paper-(\d{4}-\d{2}-\d{2})\.epub")
PNG_RE = re.compile(r"frontpage-(\d{4}-\d{2}-\d{2})\.png")
HOST_RE = re.compile(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9A-Fa-f:]+\](:\d{1,5})?")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# What kindle/client.sh logs once an edition is safely on the device.
DELIVERED_RE = re.compile(
    r"\bkindle: (?:downloaded|replaced) morning-paper-(\d{4}-\d{2}-\d{2})\.epub\b")
# Any line from the Kindle scripts: proof the device checked in.
KINDLE_LINE_RE = re.compile(r"\bkindle(?:-bootstrap)?: ")
# How the Kindle scripts mark a line that reports something going wrong.
WARNING_RE = re.compile(r"\bkindle(?:-bootstrap)?: warning: (.+)")
# The scripts the Kindle runs, served from the repository's kindle/ folder.
KINDLE_DIR = REPO / "kindle"
KINDLE_SCRIPTS = ("install.sh", "bootstrap.sh", "client.sh")
# How often the Kindle looks again while today's paper is late, or after a
# download that failed. Matches the fetcher's own retry interval, so a paper
# printed late is picked up promptly. The Kindle bounds how long it keeps to
# it: fourteen tries in a row, then one every three hours (kindle/client.sh).
LATE_RETRY = 600
# Stands for this server's address in install.sh until it is served.
PRESS_PLACEHOLDER = "@PRESS@"
# What a browser saves install.sh as when asked to download it.
INSTALLER_NAME = "Install MNN.sh"
# The address ends up inside a double-quoted string in a shell script.
SHELL_SAFE_URL_RE = re.compile(r"https?://[A-Za-z0-9.\-_:\[\]/]+")
MAX_LOG_BYTES = 64 * 1024
# What the Kindle's panel and `eips` need: Paperwhite 11 portrait, 8-bit gray.
FRONT_PAGE_SIZE = (1236, 1648)
DATA_DIR_ENV = "PAPER_DATA_DIR"
# The two things a Muse board's display.draw_url can draw (see card.py).
CARD_FORMATS = {"/card.jpg": (card.jpeg, "image/jpeg"),
                "/card.rgb565": (card.rgb565, "application/octet-stream")}
# The token POST /api/rebuild asks for. Unset or empty, rebuild is off.
TOKEN_ENV = "PRESS_TOKEN"
# What /api/status passes on about a section. Its headlines stay in
# press/report.json: status says how the print went, not what the paper says.
SECTION_KEYS = ("title", "source", "status", "stories", "error", "notes", "seconds")
# How long after the edition time a paper may still be on its way, and how
# long the fetcher may go unseen, before health calls it a problem.
EDITION_GRACE = dt.timedelta(minutes=15)
FETCHER_GRACE = dt.timedelta(minutes=15)
LOW_DISK_BYTES = 200 * 1024 * 1024
# A rebuild is the fetcher's scheduled run, made to print regardless.
REBUILD_ARGS = ["--force", "--fetch-weather"]
REBUILD_TIMEOUT = 300


def default_data_dir() -> Path:
    """Outside the repository on purpose: printed papers are data, not code."""
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "morning-paper"


class EditionRejected(Exception):
    """A built paper the Kindle could not use, so it must not be stored."""


def check_edition(parts: dict[str, tuple[str, bytes]]) -> str:
    """Refuse anything the Kindle could not use. Takes {"epub": (file name,
    content), "frontpage": (file name, content)} and returns the edition date."""
    if set(parts) != {"epub", "frontpage"}:
        raise EditionRejected("an edition is exactly two files: epub and frontpage")
    (epub_name, epub), (png_name, png) = parts["epub"], parts["frontpage"]
    epub_match, png_match = EPUB_RE.fullmatch(epub_name), PNG_RE.fullmatch(png_name)
    if not epub_match:
        raise EditionRejected("epub must be named morning-paper-YYYY-MM-DD.epub")
    if not png_match:
        raise EditionRejected("frontpage must be named frontpage-YYYY-MM-DD.png")
    date = epub_match.group(1)
    if png_match.group(1) != date:
        raise EditionRejected("epub and frontpage are for different dates")
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        raise EditionRejected(f"{date} is not a real date") from None

    try:
        with zipfile.ZipFile(io.BytesIO(epub)) as archive:
            if archive.read("mimetype").strip() != b"application/epub+zip":
                raise EditionRejected("epub has the wrong mimetype entry")
    except (zipfile.BadZipFile, KeyError):
        raise EditionRejected("epub is not a valid EPUB archive") from None

    # PNG signature, then the IHDR chunk: width, height, bit depth, colour
    # type, compression, filter, interlace.
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR" or len(png) < 29:
        raise EditionRejected("frontpage is not a PNG")
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", png[16:29])
    if (width, height) != FRONT_PAGE_SIZE or (depth, colour, interlace) != (8, 0, 0):
        raise EditionRejected("frontpage must be 1236x1648, 8-bit grayscale, "
                                f"not interlaced (got {width}x{height}, depth {depth}, "
                                f"colour type {colour}, interlace {interlace})")
    return date


def store_edition(data_dir: Path, parts: dict[str, tuple[str, bytes]]) -> None:
    """Write both files under temporary names, then rename them into place, so
    a reader sees either the whole previous edition or the whole new one. The
    EPUB goes first: /api/display follows the front page, and must never point
    at a paper that is not there yet."""
    data_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for field in ("epub", "frontpage"):
            name, content = parts[field]
            with tempfile.NamedTemporaryFile(dir=data_dir, prefix=f".{name}.",
                                             suffix=".part", delete=False) as handle:
                staged.append((Path(handle.name), data_dir / name))
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, final in staged:
            temporary.chmod(0o644)
            temporary.replace(final)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def prune_editions(data_dir: Path, keep_days: int) -> list[str]:
    """Drop editions more than keep_days older than the newest one. Measured
    from the newest edition, not the clock, so a server that sits unused for a
    month does not come back and delete everything it has."""
    dated = editions(data_dir, EPUB_RE) + editions(data_dir, PNG_RE)
    if not dated:
        return []
    newest = dt.date.fromisoformat(max(date for date, _ in dated))
    cutoff = (newest - dt.timedelta(days=keep_days)).isoformat()
    removed = []
    for date, path in dated:
        if date < cutoff:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return sorted(removed)

ATOM = "http://www.w3.org/2005/Atom"
DCTERMS = "http://purl.org/dc/terms/"
OPDS_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
ET.register_namespace("", ATOM)
ET.register_namespace("dc", DCTERMS)


def editions(data_dir: Path, pattern: re.Pattern[str]) -> list[tuple[str, Path]]:
    """(date, path) for every file in data_dir matching pattern, newest first."""
    if not data_dir.is_dir():
        return []
    found = []
    for path in data_dir.iterdir():
        match = pattern.fullmatch(path.name)
        if match and path.is_file():
            found.append((match.group(1), path))
    return sorted(found, reverse=True)


def long_date(iso: str) -> str:
    try:
        date = dt.date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{date:%A, %B} {date.day}, {date.year}"


def atom_time(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds_until_next_poll(now: dt.datetime, newest: str | None,
                            edition_time: dt.time, retry: int) -> int:
    """How long the Kindle should sleep, given the newest edition date on disk.

    With today's paper already out there is nothing new until tomorrow's
    edition time, so sleep until then. Before today's edition time, sleep until
    it. Past it with no paper, the build is late: look again in `retry`
    seconds."""
    # A few minutes of slack so the wake lands after the build, not during it.
    slack = dt.timedelta(minutes=5)
    due = dt.datetime.combine(now.date(), edition_time) + slack
    if newest is not None and newest >= now.date().isoformat():
        due += dt.timedelta(days=1)
    elif now >= due:
        return retry
    return max(60, int((due - now).total_seconds()))


def press_status(data_dir: Path, now: dt.datetime | None = None, *,
                 headlines: bool = False) -> dict:
    """How the last print went. When the Press writes its own paper this
    carries the outcome of every section; with staging it is the receipt.
    Headlines are left out unless asked for, which only the card does."""
    receipt = staging.load_receipt(data_dir)
    kindle = staging.load_kindle(data_dir)
    papers = editions(data_dir, EPUB_RE)
    status = {
        "mode": "staging" if staging.configured() else "sources",
        "today": (now or dt.datetime.now()).date().isoformat(),
        "edition_date": papers[0][0] if papers else None,
        "last_print": {key: receipt.get(key) for key in
                       ("edition_date", "status", "error", "built_at", "kindle_downloaded_at")},
        # The Kindle's last successful download, whichever edition it was.
        "kindle": {key: kindle.get(key) for key in
                   ("edition_date", "downloaded_at", "last_seen_at", "last_warning")},
    }
    if status["mode"] == "sources":
        report = staging.load_report(data_dir)
        keys = SECTION_KEYS + ("headlines",) if headlines else SECTION_KEYS
        sections = [{key: section.get(key) for key in keys}
                    for section in report.get("sections") or [] if isinstance(section, dict)]
        status.update(
            last_run={key: report.get(key) for key in
                      ("edition_date", "status", "error", "ran_at")},
            sections=sections,
            failed_sections=[{"title": section.get("title"), "error": section.get("error")}
                             for section in sections if section.get("status") == "failed"])
    return status


def kindle_manifest(data_dir: Path, now: dt.datetime, edition_time: dt.time | None,
                    refresh_rate: int) -> str:
    """What the Kindle should hold and when it should wake next, as plain text
    with one record per line so BusyBox sh can read it with `while read`:

        mnn 1
        edition 2026-10-05 fresh              fresh | late | missing
        next 2026-10-06T06:45 in 86100 retry 600
        client client.sh <sha256> <size>
        paper morning-paper-2026-10-05.epub <sha256> <size>
        screen ready - full frontpage-2026-10-05.png <sha256>
        end

    Every file is pinned by its SHA-256, and `GET /kindle/<name>` serves it.
    `next` is given both as a local time and as seconds from now, so a Kindle
    with the wrong clock or time zone still wakes at the right moment. `end`
    lets the reader tell a whole manifest from one cut short."""
    papers = dict(editions(data_dir, EPUB_RE))
    pages = dict(editions(data_dir, PNG_RE))
    # An edition is the pair; the EPUB lands first (see store_edition).
    complete = sorted(set(papers) & set(pages), reverse=True)
    newest = complete[0] if complete else None
    today = now.date()

    if edition_time is None:
        # No edition time to wake for: the next wake is at the plain rate.
        # While the newest paper is not today's the Kindle retries sooner,
        # at LATE_RETRY, for as many tries as it allows itself.
        wait = refresh_rate
        late = newest is not None and newest < today.isoformat()
    else:
        wait = seconds_until_next_poll(now, newest, edition_time, LATE_RETRY)
        due = dt.datetime.combine(today, edition_time) + dt.timedelta(minutes=5)
        # Before this morning's edition time yesterday's paper is still current.
        current = today if now >= due else today - dt.timedelta(days=1)
        late = newest is not None and newest < current.isoformat()
    state = "missing" if newest is None else "late" if late else "fresh"
    # To the nearest minute: the wait is in whole seconds, the clock is not.
    wake = (now + dt.timedelta(seconds=wait + 30)).replace(second=0, microsecond=0)

    lines = ["mnn 1",
             f"edition {newest or '-'} {state}",
             f"next {wake:%Y-%m-%dT%H:%M} in {wait} retry {LATE_RETRY}"]
    client = (KINDLE_DIR / "client.sh").read_bytes()
    lines.append(f"client client.sh {staging.sha256(client)} {len(client)}")
    if newest:
        paper, page = papers[newest].read_bytes(), pages[newest].read_bytes()
        lines.append(f"paper {papers[newest].name} {staging.sha256(paper)} {len(paper)}")
        lines.append(f"screen ready - full {pages[newest].name} {staging.sha256(page)}")
    lines.append("end")
    return "\n".join(lines) + "\n"


def fetcher_problem(data_dir: Path, now: dt.datetime,
                    edition_time: dt.time | None) -> tuple[str | None, str | None]:
    """(when the fetcher was last seen, what is wrong with that or None).
    A fetcher running with --loop signs in every few minutes; one started by a
    timer signs in once per run, so it is only due at each edition time."""
    heartbeat = staging.load_heartbeat(data_dir)
    try:
        seen = dt.datetime.fromisoformat(heartbeat.get("at") or "")
    except (TypeError, ValueError):
        return None, "the fetcher has never run, so no paper will be printed"
    seen_at = seen.isoformat(timespec="seconds")
    if heartbeat.get("loop"):
        if now - seen > FETCHER_GRACE:
            return seen_at, f"the fetcher is not running (last seen {seen:%Y-%m-%d %H:%M})"
    elif edition_time is None:
        if now - seen > dt.timedelta(hours=25):
            return seen_at, f"the fetcher has not run since {seen:%Y-%m-%d %H:%M}"
    else:
        due = dt.datetime.combine(now.date(), edition_time)
        if now < due:
            due -= dt.timedelta(days=1)
        if seen < due and now - due > FETCHER_GRACE:
            return seen_at, (f"the fetcher did not run at {edition_time:%H:%M} "
                             f"(last seen {seen:%Y-%m-%d %H:%M})")
    return seen_at, None


def press_health(data_dir: Path, edition_time: dt.time | None = None, *,
                 rebuild_enabled: bool = False, now: dt.datetime | None = None) -> dict:
    """Whether the Press is fine. A problem means the paper is not arriving,
    or soon will not; a warning is worth a look but the paper printed."""
    now = now or dt.datetime.now()
    today = now.date()
    problems, warnings = [], []

    def note(found: list, code: str, message: str) -> None:
        found.append({"code": code, "message": message})

    papers = editions(data_dir, EPUB_RE)
    newest = papers[0][0] if papers else None
    late = (edition_time is not None
            and now >= dt.datetime.combine(today, edition_time) + EDITION_GRACE)
    if newest is None:
        note(problems, "no_edition", "no edition has been printed yet")
    elif late and newest < today.isoformat():
        note(problems, "edition_missing",
             f"today's edition has not been printed (it was due at {edition_time:%H:%M}); "
             f"the newest is {newest}")
    elif newest < (today - dt.timedelta(days=1)).isoformat():
        note(problems, "edition_missing", f"no edition has been printed since {newest}")

    receipt = staging.load_receipt(data_dir)
    if receipt.get("status") == "failed":
        note(problems, "print_failed", f"the last print failed: {receipt.get('error')}")

    fetcher_seen_at, stopped = fetcher_problem(data_dir, now, edition_time)
    if stopped:
        note(problems, "fetcher_stopped", stopped)

    try:
        free = shutil.disk_usage(data_dir).free
    except OSError:
        free = None
    if free is not None and free < LOW_DISK_BYTES:
        note(problems, "disk_low",
             f"the disk is nearly full: {free // (1024 * 1024)} MB free where papers are kept")

    status = press_status(data_dir, now)
    for section in status.get("failed_sections") or []:
        note(warnings, "section_left_out",
             f"section {section['title']!r} was left out of the last paper: {section['error']}")
    if receipt.get("unsent"):
        note(warnings, "receipt_unsent", "a receipt is waiting to be sent to staging")

    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "checked_at": now.astimezone().isoformat(timespec="seconds"),
        "mode": status["mode"],
        "today": today.isoformat(),
        "edition_date": newest,
        "edition_time": f"{edition_time:%H:%M}" if edition_time else None,
        "fetcher_seen_at": fetcher_seen_at,
        "free_mb": free // (1024 * 1024) if free is not None else None,
        "rebuild_enabled": rebuild_enabled,
    }


def run_rebuild(data_dir: Path) -> dict:
    """Print today's edition again. Returns {"ok", "detail"}: detail is what
    the fetcher said."""
    command = [sys.executable, "-m", "mnn", "fetch",
               *REBUILD_ARGS, "--data-dir", str(data_dir)]
    # The fetcher runs source modules, which have no use for this token.
    env = {key: value for key, value in os.environ.items() if key != TOKEN_ENV}
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env,
                                timeout=REBUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"gave up after {REBUILD_TIMEOUT} seconds"}
    except OSError as exc:
        return {"ok": False, "detail": f"could not start the fetcher: {exc}"}
    return {"ok": result.returncode == 0,
            "detail": (result.stdout + result.stderr).strip()[-2000:]}


def opds_feed(papers: list[tuple[str, Path]], base: str) -> bytes:
    feed = ET.Element(f"{{{ATOM}}}feed")

    def add(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
        child = ET.SubElement(parent, tag, attrs)
        child.text = text
        return child

    mtimes = [path.stat().st_mtime for _, path in papers]
    add(feed, f"{{{ATOM}}}id", f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'morning-paper/catalog')}")
    add(feed, f"{{{ATOM}}}title", "The Morning Paper")
    add(feed, f"{{{ATOM}}}updated", atom_time(max(mtimes, default=dt.datetime.now().timestamp())))
    add(add(feed, f"{{{ATOM}}}author"), f"{{{ATOM}}}name", "Morning Paper")
    for rel in ("self", "start"):
        add(feed, f"{{{ATOM}}}link", rel=rel, href=f"{base}/opds", type=OPDS_TYPE)

    for (date, path), mtime in zip(papers, mtimes):
        entry = add(feed, f"{{{ATOM}}}entry")
        add(entry, f"{{{ATOM}}}title", f"The Morning Paper: {long_date(date)}")
        # Same identifier build_paper.py writes into the EPUB.
        add(entry, f"{{{ATOM}}}id", f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'morning-paper/{date}')}")
        add(entry, f"{{{ATOM}}}updated", atom_time(mtime))
        add(add(entry, f"{{{ATOM}}}author"), f"{{{ATOM}}}name", "Morning Paper")
        add(entry, f"{{{DCTERMS}}}issued", date)
        add(entry, f"{{{DCTERMS}}}language", "en")
        add(entry, f"{{{ATOM}}}summary", f"Morning Paper edition for {long_date(date)}.", type="text")
        add(entry, f"{{{ATOM}}}link", rel="http://opds-spec.org/acquisition",
            href=f"{base}/paper/{path.name}", type="application/epub+zip")

    ET.indent(feed)
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True) + b"\n"


class PaperServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, data_dir: Path, log_path: Path,
                 refresh_rate: int, base_url: str | None,
                 edition_time: dt.time | None = None,
                 muse_wake: threading.Event | None = None,
                 rebuild_token: str | None = None) -> None:
        super().__init__(address, PaperHandler)
        self.muse_wake = muse_wake
        self.data_dir = data_dir
        self.log_path = log_path
        self.refresh_rate = refresh_rate
        self.edition_time = edition_time
        self.base_url = base_url.rstrip("/") if base_url else None
        self.log_lock = threading.Lock()
        self.rebuild_token = (rebuild_token or "").strip()
        self.rebuild_lock = threading.Lock()


class PaperHandler(BaseHTTPRequestHandler):
    server: PaperServer
    server_version = "MorningPaper/0.1"
    # Applied to the socket, so a stalled client cannot hold a thread forever.
    timeout = 15

    # --- Routing ---

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        if len(path) > 1:
            path = path.rstrip("/")
        data_dir = self.server.data_dir

        if path == "/":
            self.send_text(HTTPStatus.OK, __doc__.split("Usage:")[0].strip() + "\n")
        elif path == "/opds":
            body = opds_feed(editions(data_dir, EPUB_RE), self.base_url())
            self.send_bytes(HTTPStatus.OK, OPDS_TYPE, body)
        elif path == "/paper/latest.txt":
            papers = editions(data_dir, EPUB_RE)
            if papers:
                self.send_text(HTTPStatus.OK, papers[0][1].name + "\n")
            else:
                self.send_text(HTTPStatus.NOT_FOUND, "no edition has been built yet\n")
        elif path.startswith("/paper/"):
            # fullmatch against the edition pattern is what keeps this from
            # ever reaching outside the data directory.
            name = path.removeprefix("/paper/")
            if EPUB_RE.fullmatch(name):
                self.send_file(data_dir / name, "application/epub+zip")
            else:
                self.send_text(HTTPStatus.NOT_FOUND, "no such paper\n")
        elif path == "/api/display":
            self.send_display()
        elif path == "/api/status":
            self.send_json(HTTPStatus.OK, press_status(data_dir))
        elif path in CARD_FORMATS:
            self.send_card(path, parse_qs(url.query).get("size", [""])[-1])
        elif path == "/api/health":
            health = press_health(data_dir, self.server.edition_time,
                                  rebuild_enabled=bool(self.server.rebuild_token))
            # 503 when something is wrong, so a monitor that only looks at the
            # status code gets the same answer as one that reads "ok".
            self.send_json(HTTPStatus.OK if health["ok"] else HTTPStatus.SERVICE_UNAVAILABLE,
                           health)
        elif path == "/frontpage.png":
            pages = editions(data_dir, PNG_RE)
            if pages:
                self.send_file(pages[0][1], "image/png")
            else:
                self.send_text(HTTPStatus.NOT_FOUND, "no front page has been built yet\n")
        elif path.startswith("/kindle/"):
            self.send_kindle_file(path.removeprefix("/kindle/"),
                                  download="download" in parse_qs(url.query, keep_blank_values=True))
        else:
            self.send_text(HTTPStatus.NOT_FOUND, "not found\n")

    do_HEAD = do_GET

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/api/log":
            self.receive_log()
        elif path == "/api/rebuild":
            self.rebuild()
        else:
            self.send_text(HTTPStatus.NOT_FOUND, "not found\n")

    def read_body(self, limit: int) -> bytes | None:
        """The request body, or None once an error reply has been sent."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_text(HTTPStatus.LENGTH_REQUIRED, "Content-Length required\n")
            return None
        if length < 0:
            self.send_text(HTTPStatus.BAD_REQUEST, "bad Content-Length\n")
            return None
        if length > limit:
            self.send_text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           f"body limited to {limit} bytes\n")
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_text(HTTPStatus.BAD_REQUEST, "body shorter than Content-Length\n")
            return None
        return body

    def receive_log(self) -> None:
        body = self.read_body(MAX_LOG_BYTES)
        if body is None:
            return
        text = body.decode("utf-8", errors="replace")
        # One record per device line, with control characters blanked so a
        # device cannot forge extra records or write terminal escapes.
        lines = [CONTROL_RE.sub(" ", line).strip() for line in text.splitlines()]
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        records = [f"{stamp} {self.client_address[0]} {line}\n" for line in lines if line]
        with self.server.log_lock, self.server.log_path.open("a", encoding="utf-8") as log:
            log.writelines(records)
            delivered = [match.group(1) for line in lines
                         if (match := DELIVERED_RE.search(line))]
            warnings = [match.group(1) for line in lines
                        if (match := WARNING_RE.search(line))]
            if any(KINDLE_LINE_RE.search(line) for line in lines):
                staging.record_kindle(self.server.data_dir, stamp, self.client_address[0],
                                      delivered, warnings)
            changed = any([staging.mark_kindle_download(
                self.server.data_dir, date, stamp, self.client_address[0])
                for date in delivered])
        if changed:
            # Tell staging the paper reached the Kindle, without making the
            # Kindle wait on a machine outside the house.
            threading.Thread(target=staging.send_receipt, args=(self.server.data_dir,),
                             daemon=True).start()
            # The morning line was waiting for this.
            if self.server.muse_wake is not None:
                self.server.muse_wake.set()
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def rebuild(self) -> None:
        token = self.server.rebuild_token
        if not token:
            self.send_json(HTTPStatus.FORBIDDEN, {
                "ok": False, "error": f"rebuild is off: the Press has no {TOKEN_ENV} set"})
            return
        offered = self.headers.get("Authorization", "")
        if not hmac.compare_digest(offered.encode("utf-8", "replace"),
                                   f"Bearer {token}".encode("utf-8")):
            self.send_json(HTTPStatus.UNAUTHORIZED,
                           {"ok": False, "error": "rebuild needs the Press token"},
                           {"WWW-Authenticate": "Bearer"})
            return
        # A body is neither needed nor used, but one that was sent is read so
        # the connection ends cleanly.
        if self.headers.get("Content-Length") and self.read_body(1024) is None:
            return
        if not self.server.rebuild_lock.acquire(blocking=False):
            self.send_json(HTTPStatus.CONFLICT,
                           {"ok": False, "error": "a rebuild is already running"})
            return
        try:
            result = run_rebuild(self.server.data_dir)
        finally:
            self.server.rebuild_lock.release()
        status = press_status(self.server.data_dir)
        self.send_json(
            HTTPStatus.OK if result["ok"] else HTTPStatus.INTERNAL_SERVER_ERROR,
            {"ok": result["ok"], "today": status["today"],
             "edition_date": status["edition_date"], "detail": result["detail"]})

    # --- Logging ---

    def log_message(self, format: str, *args: object) -> None:
        """The stock log, with every string cut at its first `?`: no address
        here takes a query string, so none is kept."""
        super().log_message(format, *(arg.partition("?")[0] if isinstance(arg, str) else arg
                                      for arg in args))

    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        """The stock error reply, under the standard phrase for its code
        rather than a message that quotes the request line."""
        super().send_error(code, explain=explain)

    # --- Responses ---

    def base_url(self) -> str:
        """Address the client should use to call back, as an absolute URL."""
        if self.server.base_url:
            return self.server.base_url
        host = self.headers.get("Host", "")
        if not HOST_RE.fullmatch(host):
            # No usable Host header: fall back to the local address the client reached.
            address, port = self.connection.getsockname()[:2]
            host = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        return f"http://{host}"

    def send_kindle_file(self, name: str, *, download: bool = False) -> None:
        """The manifest, one of the Kindle's scripts, or an edition file the
        manifest names. Matching fixed names and the edition patterns is what
        keeps this from reaching any other file."""
        data_dir = self.server.data_dir
        if name == "manifest":
            try:
                manifest = kindle_manifest(data_dir, dt.datetime.now(), self.server.edition_time,
                                           self.server.refresh_rate)
            except OSError:
                self.send_text(HTTPStatus.SERVICE_UNAVAILABLE, "the manifest could not be built\n")
                return
            self.send_text(HTTPStatus.OK, manifest)
        elif EPUB_RE.fullmatch(name):
            self.send_file(data_dir / name, "application/epub+zip")
        elif PNG_RE.fullmatch(name):
            self.send_file(data_dir / name, "image/png")
        elif name in KINDLE_SCRIPTS:
            self.send_kindle_script(name, download=download and name == "install.sh")
        else:
            self.send_text(HTTPStatus.NOT_FOUND, "no such file\n")

    def send_kindle_script(self, name: str, *, download: bool = False) -> None:
        """With `download`, a browser saves the installer under the name it
        has on the Kindle, to be copied over USB and run from KOReader's file
        browser with nothing typed."""
        try:
            script = (KINDLE_DIR / name).read_text(encoding="utf-8")
        except OSError:
            self.send_text(HTTPStatus.NOT_FOUND, "this Press was installed without kindle/\n")
            return
        if PRESS_PLACEHOLDER in script:
            base = self.base_url()
            if not SHELL_SAFE_URL_RE.fullmatch(base):
                self.send_text(HTTPStatus.INTERNAL_SERVER_ERROR,
                               "this server's address cannot be written into a script\n")
                return
            script = script.replace(PRESS_PLACEHOLDER, base)
        headers = ({"Content-Disposition": f'attachment; filename="{INSTALLER_NAME}"'}
                   if download else None)
        self.send_bytes(HTTPStatus.OK, "text/x-shellscript; charset=utf-8",
                        script.encode("utf-8"), headers)

    def send_display(self) -> None:
        pages = editions(self.server.data_dir, PNG_RE)
        if not pages:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE,
                           {"status": 503, "error": "no front page has been built yet"})
            return
        date, page = pages[0]
        base = self.base_url()
        papers = dict(editions(self.server.data_dir, EPUB_RE))
        refresh_rate = self.server.refresh_rate
        if self.server.edition_time is not None:
            refresh_rate = seconds_until_next_poll(
                dt.datetime.now(), max(papers, default=None),
                self.server.edition_time, refresh_rate)
        self.send_json(HTTPStatus.OK, {
            "status": 0,
            "image_url": f"{base}/frontpage.png",
            "filename": page.name,
            "refresh_rate": refresh_rate,
            "edition_date": date,
            "paper_url": f"{base}/paper/{papers[date].name}" if date in papers else None,
        })

    def send_card(self, path: str, size: str) -> None:
        """Drawn afresh for every request, so the address never changes and
        the card is never behind the status it is made from."""
        pixels = card.parse_size(size) if size else card.DEFAULT_SIZE
        if pixels is None:
            self.send_text(HTTPStatus.BAD_REQUEST,
                           f"size must be WIDTHxHEIGHT, each side {card.MIN_SIDE} to "
                           f"{card.MAX_SIDE}, such as 800x480\n")
            return
        encode, content_type = CARD_FORMATS[path]
        status = press_status(self.server.data_dir, headlines=True)
        image = card.render(card.summarise(status, dt.date.today()), pixels)
        self.send_bytes(HTTPStatus.OK, content_type, encode(image))

    def send_json(self, status: HTTPStatus, payload: dict,
                  headers: dict[str, str] | None = None) -> None:
        # Compact separators matter: sed-based clients pull fields out with
        # patterns like "image_url":"...", which a space would break.
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_bytes(status, "application/json", body, headers)

    def send_text(self, status: HTTPStatus, text: str) -> None:
        self.send_bytes(status, "text/plain; charset=utf-8", text.encode("utf-8"))

    def send_bytes(self, status: HTTPStatus, content_type: str, body: bytes,
                   headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            handle = path.open("rb")
        except OSError:
            self.send_text(HTTPStatus.NOT_FOUND, "not found\n")
            return
        with handle:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(os.fstat(handle.fileno()).st_size))
            self.end_headers()
            if self.command != "HEAD":
                shutil.copyfileobj(handle, self.wfile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LAN server for the Morning Paper.")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8484, help="port (default: 8484)")
    parser.add_argument("--data-dir", type=Path,
                        default=os.environ.get(DATA_DIR_ENV) or default_data_dir(),
                        help=f"where printed papers are kept (default: ${DATA_DIR_ENV}, "
                             "else ~/.local/share/morning-paper)")
    parser.add_argument("--log-file", type=Path,
                        help="where POST /api/log appends (default: server.log in the "
                             "data directory)")
    parser.add_argument("--refresh-rate", type=int, default=3600,
                        help="seconds the Kindle should sleep between polls (default: 3600)")
    parser.add_argument("--edition-time", type=dt.time.fromisoformat, metavar="HH:MM",
                        help="local time the daily paper is built; when set, the Kindle is "
                             "told to sleep until the next edition instead of polling at "
                             "--refresh-rate")
    parser.add_argument("--base-url", default=None,
                        help="public base URL, if the request's Host header is not the right one")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with PaperServer((args.host, args.port), data_dir=data_dir,
                     log_path=args.log_file or data_dir / "server.log",
                     refresh_rate=args.refresh_rate, base_url=args.base_url,
                     edition_time=args.edition_time,
                     muse_wake=muse.start(data_dir, muse.Settings.from_env()),
                     rebuild_token=os.environ.get(TOKEN_ENV)) as server:
        print(f"serving {data_dir} on http://{args.host}:{args.port} "
              "(LAN only, no TLS; rebuild is "
              f"{'on, behind the token' if server.rebuild_token else 'off'})", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
