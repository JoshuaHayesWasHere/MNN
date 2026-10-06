"""Short messages from the Press to a paired Muse gadget.

After a print the Press can say how the morning went: one line once the
Kindle has the paper ("Printed at 06:41, four sections, 14 stories. On the
Kindle at 06:46."), and one message for each thing that went wrong. Off by
default; MUSE_MESSAGES picks `every` (the line each morning, and problems),
`problems` (silent on good mornings) or `off`.

With MUSE_CARD_URL set it also asks Muse, once per print, to draw the Press's
card (see card.py) on a gadget with a screen. The Press cannot reach that
gadget itself: only Muse can run `display.draw_url` on it.

It speaks the gadget service's local socket directly: one JSON line in,
{"message": "..."} with an optional "session_id", and one JSON line back,
{"ok": true} or {"ok": false, "error": "..."}. That needs no credentials and
no SDK here, only permission to open the socket. Muse receives the text as a
message written by the person, so every message ends by saying it is
automatic and needs no reply.

Nothing depends on this. It runs in the server, after the paper is printed
and served, reading the records the Press already keeps; a send that fails is
logged and dropped, never retried.

The ask source (sources/ask.py) uses `send` as well, to put its question to
Muse at print time. That is set in sources.toml and does not need
MUSE_MESSAGES.

Usage (to try the socket by hand):
    uv run mnn-press muse "Test from the Press."
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import socket
import sys
import threading
from pathlib import Path

from mnn import staging

MODE_ENV = "MUSE_MESSAGES"
DETAIL_ENV = "MUSE_DETAIL"
WAIT_ENV = "MUSE_KINDLE_WAIT"
SOCKET_ENV = "MUSE_SOCKET"
SESSION_ENV = "MUSE_SESSION_ID"
CARD_ENV = "MUSE_CARD_URL"

MODES = ("off", "problems", "every")
DETAILS = ("counts", "headlines")
DEFAULT_SOCKET = "/run/musegadget/musegadget.sock"
DEFAULT_WAIT_MINUTES = 60
# The receipt only ever holds the latest edition, so the wait has to end
# before the next morning's paper takes its place.
MAX_WAIT_MINUTES = 12 * 60
# The gadget service's own rule for a side chat's name.
SESSION_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
# The address goes into a message Muse reads, so it is held to plain URL
# characters; the board itself takes http:// and https:// only.
CARD_URL_RE = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~/?&=%+\-]*)?")
# As long as the gadget's own client waits: the service answers only once
# Muse has, and its reply is the one worth logging.
SEND_TIMEOUT = 90
# The service refuses requests over 64 KiB; nothing here should come near it.
MAX_MESSAGE = 1500
# An outcome older than this is history, not news: nothing is said about it
# when the messages are first turned on or the server comes back after a break.
MAX_AGE = dt.timedelta(hours=12)
LOW_DISK_BYTES = 200 * 1024 * 1024
DISK_RECOVERED_BYTES = 300 * 1024 * 1024
MAX_HEADLINES = 5
MAX_SECTIONS_NAMED = 5
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
# Muse reads this as written by the person, and may otherwise answer it.
SIGN_OFF = ("(An automatic status note from the Press, the morning paper on this "
            "network. No reply or action is needed.)")
# The card is the one message that does ask for something.
CARD_SIGN_OFF = ("(An automatic request from the Press, the morning paper on this network. "
                 "No reply is needed. If no gadget has a screen, do nothing.)")
SMALL_NUMBERS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


class MuseError(Exception):
    """The message did not reach Muse."""


@dataclasses.dataclass(frozen=True)
class Settings:
    mode: str = "off"
    detail: str = "counts"
    # How long after a print to wait for the Kindle; None never mentions it.
    kindle_wait: dt.timedelta | None = dt.timedelta(minutes=DEFAULT_WAIT_MINUTES)
    socket_path: str = DEFAULT_SOCKET
    session_id: str | None = None
    # Where a display board fetches the card; None never asks for it.
    card_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        """A value that cannot be read falls back to the quieter choice, with
        a warning: a typo must not stop the server or send more than asked.
        A wait longer than twelve hours is cut to twelve hours."""
        mode = _choice(MODE_ENV, MODES)
        detail = _choice(DETAIL_ENV, DETAILS)
        raw = os.environ.get(WAIT_ENV, "").strip()
        try:
            minutes = int(raw) if raw else DEFAULT_WAIT_MINUTES
            if minutes < 0:
                raise ValueError
        except ValueError:
            _log(f"{WAIT_ENV}={raw!r} is not a number of minutes; "
                 f"using {DEFAULT_WAIT_MINUTES}")
            minutes = DEFAULT_WAIT_MINUTES
        if minutes > MAX_WAIT_MINUTES:
            _log(f"{WAIT_ENV}={raw!r} is more than twelve hours; using {MAX_WAIT_MINUTES}")
            minutes = MAX_WAIT_MINUTES
        session_id = os.environ.get(SESSION_ENV, "").strip() or None
        if session_id and not SESSION_RE.fullmatch(session_id):
            _log(f"{SESSION_ENV} must be letters, digits and dashes, at most 64; "
                 "using the main chat")
            session_id = None
        card_url = os.environ.get(CARD_ENV, "").strip() or None
        if card_url and not CARD_URL_RE.fullmatch(card_url):
            _log(f"{CARD_ENV} must be the card's http:// or https:// address, with no "
                 "spaces; not asking for the card")
            card_url = None
        return cls(mode=mode, detail=detail,
                   kindle_wait=dt.timedelta(minutes=minutes) if minutes else None,
                   socket_path=os.environ.get(SOCKET_ENV, "").strip() or DEFAULT_SOCKET,
                   session_id=session_id, card_url=card_url)


def _choice(name: str, allowed: tuple[str, ...]) -> str:
    value = os.environ.get(name, "").strip().lower() or allowed[0]
    if value not in allowed:
        _log(f"{name}={value!r} is not one of {', '.join(allowed)}; using {allowed[0]}")
        return allowed[0]
    return value


def _log(text: str) -> None:
    print(f"muse: {text}", file=sys.stderr, flush=True)


# --- The socket ---------------------------------------------------------------


def send(text: str, settings: Settings, timeout: float | None = None) -> None:
    """Hand one message to the gadget service, or raise MuseError. `timeout`
    is how long to wait for the service, SEND_TIMEOUT unless given."""
    request = {"message": text}
    if settings.session_id:
        request["session_id"] = settings.session_id
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(SEND_TIMEOUT if timeout is None else timeout)
            sock.connect(settings.socket_path)
            sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            with sock.makefile("rb") as replies:
                reply = json.loads(replies.readline())
    except (OSError, ValueError) as exc:
        raise MuseError(f"could not reach the gadget service at "
                        f"{settings.socket_path}: {exc}") from exc
    if not isinstance(reply, dict) or not reply.get("ok"):
        error = reply.get("error") if isinstance(reply, dict) else None
        raise MuseError(f"not delivered: {error or reply}")


# --- What to say --------------------------------------------------------------


def clean(text: object, limit: int = 200) -> str:
    """One line of plain text, cut to length. What is quoted here (an error,
    a headline) can come from a feed, and ends up in front of Muse."""
    line = " ".join(CONTROL_RE.sub(" ", str(text)).split())
    return line if len(line) <= limit else line[:limit - 3].rstrip() + "..."


def count(number: int, noun: str) -> str:
    word = SMALL_NUMBERS[number] if 0 <= number < len(SMALL_NUMBERS) else str(number)
    return f"{word} {noun}" + ("" if number == 1 else "s")


def _when(value: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value)).astimezone()
    except ValueError:
        return None


def _fresh(value: object, now: dt.datetime,
           wait: dt.timedelta | None = None) -> dt.datetime | None:
    """The time recorded, if it is recent enough to be worth a message. News
    that is held back for `wait` only starts to age when the wait ends."""
    when = _when(value)
    if when is None or now - (when + (wait or dt.timedelta(0))) > MAX_AGE:
        return None
    return when


def morning_line(receipt: dict, report: dict, settings: Settings,
                 built: dt.datetime, now: dt.datetime) -> str:
    line = f"Printed at {built:%H:%M}"
    # Only the Press's own run knows the sections; a staged edition's receipt
    # carries no counts.
    printed = [section for section in report.get("sections") or []
               if isinstance(section, dict) and section.get("status") == "ok"]
    if printed and not staging.configured() and report.get("edition_date") == receipt.get(
            "edition_date"):
        stories = sum(int(section.get("stories") or 0) for section in printed)
        line += f", {count(len(printed), 'section')}, {stories} " + (
            "story" if stories == 1 else "stories")
    else:
        printed = []
    line += "."
    if settings.kindle_wait is not None:
        kindle = _when(receipt.get("kindle_downloaded_at"))
        if kindle is not None:
            line += f" On the Kindle at {kindle:%H:%M}."
        else:
            line += (f" The Kindle had not fetched it by {now:%H:%M}; it stays on the Press "
                     "for the Kindle's next wake.")
    if settings.detail == "headlines":
        leads = [clean(section["headlines"][0], 120) for section in printed
                 if section.get("headlines")][:MAX_HEADLINES]
        if leads:
            line += " Leading: " + "; ".join(f'"{lead}"' for lead in leads) + "."
    return line


def due(data_dir: Path, settings: Settings, now: dt.datetime, handled: dict,
        free_bytes: int | None) -> list[tuple[str, object, str]]:
    """Every message owed right now, as (kind, key, text). `handled` maps a
    kind to the key it was last sent for, so each is said once: the key is
    the edition date, the day when staging could not be read (there is no
    edition then), or for the disk simply True until it has recovered."""
    if settings.mode == "off":
        return []
    receipt = staging.load_receipt(data_dir)
    report = staging.load_report(data_dir)
    date = receipt.get("edition_date")
    staged = staging.configured()
    owed: list[tuple[str, object, str]] = []

    if receipt.get("status") == "failed" and _fresh(receipt.get("reported_at"), now):
        owed.append(("print", date, (
            f"The Press could not print the {date} paper: "
            f"{clean(receipt.get('error')).rstrip('.')}. The previous paper is still being "
            "served. " + (
                "A download that failed is tried again at the next check; an edition that "
                "will not build waits until it is staged again." if staged else
                "The Press will try again at its next check."))))
    elif receipt.get("status") == "ok" and (
            built := _fresh(receipt.get("built_at"), now, settings.kindle_wait)):
        wait = settings.kindle_wait
        missing = wait is not None and not receipt.get("kindle_downloaded_at")
        if missing and now < built + wait:
            pass  # still inside the Kindle's time to fetch
        elif settings.mode == "every":
            owed.append(("delivery", date, morning_line(receipt, report, settings, built, now)))
        elif missing:
            owed.append(("delivery", date, (
                f"The {date} paper was printed at {built:%H:%M}, but the Kindle had not "
                f"fetched it by {now:%H:%M}. It stays on the Press, and the Kindle collects "
                "it the next time it wakes.")))

    if not staged and report.get("status") == "partial" and _fresh(report.get("ran_at"), now):
        missed = [section for section in report.get("sections") or []
                  if isinstance(section, dict) and section.get("status") == "failed"]
        if missed:
            named = [f"{clean(section.get('title'), 60)} ({clean(section.get('error'), 120)})"
                     for section in missed[:MAX_SECTIONS_NAMED]]
            if len(missed) > MAX_SECTIONS_NAMED:
                named.append(f"and {len(missed) - MAX_SECTIONS_NAMED} more")
            owed.append(("sections", report.get("edition_date"), (
                f"The {report.get('edition_date')} paper was printed without "
                f"{count(len(missed), 'section')}: {'; '.join(named)}. The rest of the "
                "paper is out as usual, and those sources are tried again at the next "
                "edition.")))
    if staged and report.get("mode") == "staging" and report.get("status") == "failed" and (
            ran := _fresh(report.get("ran_at"), now)):
        day = ran.date().isoformat()
        already_printed = receipt.get("status") == "ok" and date == day
        if not already_printed:
            owed.append(("staging", day, (
                f"The Press could not read staging: {clean(report.get('error')).rstrip('.')}. "
                "No new paper has been printed and the previous one is still being served. "
                "The Press will try again at its next check.")))

    if free_bytes is not None and free_bytes < LOW_DISK_BYTES:
        owed.append(("disk", True, (
            f"The Press has only {free_bytes // (1024 * 1024)} MB free where it keeps its "
            "papers. It keeps printing while there is room, but it only ever deletes its "
            "own old editions, so the space has to be freed by hand. If the disk fills, "
            "printing stops and the last paper stays up.")))
    return [item for item in owed if handled.get(item[0]) != item[1]]


def card_due(data_dir: Path, settings: Settings, now: dt.datetime,
             handled: dict) -> list[tuple[str, object, str]]:
    """The request to draw the card, owed once for each print: every paper
    printed (a reprint is a new card), and the first failure of an edition
    (the Press tries a failed print again, and the board need not flash each
    time). It does not wait for the Kindle and does not depend on MUSE_MESSAGES."""
    if not settings.card_url:
        return []
    receipt = staging.load_receipt(data_dir)
    date = receipt.get("edition_date")
    if receipt.get("status") == "ok" and _fresh(receipt.get("built_at"), now):
        key = f"{date} printed {receipt.get('built_at')}"
    elif receipt.get("status") == "failed" and _fresh(receipt.get("reported_at"), now):
        key = f"{date} failed"
    else:
        return []
    if handled.get("card") == key:
        return []
    return [("card", key, (
        "Please show the Press's card on my gadget with a screen: run display.draw_url "
        f"on it with the url {settings.card_url} . The card is a small picture that says "
        "whether today's paper is ready."))]


# --- Sending what is due ------------------------------------------------------


def state_path(data_dir: Path) -> Path:
    return staging.press_dir(data_dir) / "muse.json"


def tick(data_dir: Path, settings: Settings, now: dt.datetime | None = None,
         free_bytes: int | None = None) -> list[str]:
    """Send whatever is owed and remember it, sent or not: a message that
    could not be delivered is dropped. Returns the kinds that were attempted."""
    now = (now or dt.datetime.now()).astimezone()
    if free_bytes is None and data_dir.is_dir():
        free_bytes = shutil.disk_usage(data_dir).free
    state = staging.load_json(state_path(data_dir))
    handled = state.get("handled") if isinstance(state.get("handled"), dict) else {}
    before = dict(handled)
    # The disk alert is said once per shortage, then again only after a clear
    # recovery: free space hovering at the line is still the same shortage.
    if free_bytes is not None and free_bytes >= DISK_RECOVERED_BYTES:
        handled.pop("disk", None)
    # The card request goes last: each send waits for Muse to answer, and an
    # alert must not wait behind a request Muse may not know what to do with.
    owed = (due(data_dir, settings, now, handled, free_bytes)
            + card_due(data_dir, settings, now, handled))
    for kind, key, text in owed:
        handled[kind] = key
    if handled != before:
        # Written before sending, so a crash mid-send cannot repeat a message.
        staging.write_atomic(state_path(data_dir), staging.dump({"handled": handled}))
    for kind, key, text in owed:
        try:
            sign_off = CARD_SIGN_OFF if kind == "card" else SIGN_OFF
            send(f"{text[:MAX_MESSAGE]} {sign_off}", settings)
            _log(f"sent ({kind}): {text}")
        except MuseError as exc:
            _log(f"dropped ({kind}): {exc}")
    return [kind for kind, _, _ in owed]


def watch(data_dir: Path, settings: Settings, wake: threading.Event,
          interval: float = 30) -> None:
    """Look for something to say every `interval` seconds, and at once when
    `wake` is set (the Kindle has just reported a download). Never raises:
    the server it runs beside must not notice it."""
    while True:
        wake.wait(interval)
        wake.clear()
        try:
            tick(data_dir, settings)
        except Exception as exc:  # whatever goes wrong here, the paper comes first
            _log(f"skipped a check: {type(exc).__name__}: {exc}")


def start(data_dir: Path, settings: Settings) -> threading.Event | None:
    """Start the watcher beside the server. Returns the event that wakes it,
    or None when there is nothing it would ever send."""
    if settings.mode == "off" and not settings.card_url:
        return None
    wake = threading.Event()
    wake.set()
    threading.Thread(target=watch, args=(data_dir, settings, wake), daemon=True,
                     name="muse").start()
    _log(f"messages {settings.mode} ({settings.detail}), card "
         f"{'on' if settings.card_url else 'off'}, through {settings.socket_path}")
    return wake


def main(argv: list[str] | None = None) -> int:
    text = " ".join(sys.argv[1:] if argv is None else argv).strip()
    if not text:
        print(__doc__.split("Usage")[1].strip(": \n"), file=sys.stderr)
        return 2
    try:
        send(f"{text} {SIGN_OFF}", Settings.from_env())
    except MuseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("Sent to Muse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
