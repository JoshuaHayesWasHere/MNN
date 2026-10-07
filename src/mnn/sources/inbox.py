"""The inbox source: a section printed from files left in a folder.

Anything that can write a file can write part of the paper: an assistant, a
script, a cron job, you. A `[[section]]` in sources.toml asks for it:

    [[section]]
    title = "From the Inbox"
    source = "inbox"

A file is one section in the edition format, as JSON, named for the day it is
meant for, with a label after the date if you like (a label may only use a-z,
A-Z, 0-9, `.`, `_` and `-`):

    2026-10-05.json            or 2026-10-05-label.json

    {"title": "From the Inbox",
     "articles": [{"title": "Headline", "body": ["First paragraph."]}]}

`title` is optional (the section's title in sources.toml is used without it).
An article needs a `title` and a `body`, and may carry `deck`, `source`,
`url`, `quote`, `quote_by` and `why`, as in any edition.

A file is fresh when its name is dated the day of the paper or up to
`max_age_days` before it (default 1, so a file written the evening before, or
by a writer in another time zone, still prints). A file dated later waits for
its day. With several fresh files the newest comes first and their stories
share the section, up to `stories` of them (default 10).

A printed file is moved to `printed/<paper's date>/` inside the inbox, so it
is never printed in a later paper; printing the same day's paper again reads
it back from there, unless a file of the same name left since can be printed
in its place. Those folders are deleted `keep_days` (default 14) after their
paper. A file that cannot be printed (not JSON, not a section, larger than
256 KiB, not a regular file, not named as above) is left where it is,
reported, and skipped. With nothing fresh the section is simply not in the
paper.

The folder is `folder` in the section's table, else $PRESS_INBOX_DIR, else
`inbox` in $PRESS_CONFIG_DIR or ~/.config/morning-paper (--config-dir does
not move it). The Press must be able to write to it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

from mnn import build_paper
from mnn import press_sources

INBOX_DIR_ENV = "PRESS_INBOX_DIR"
PRINTED = "printed"
# A date, then anything a file name is usually made of.
NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[-_.][A-Za-z0-9._-]*)?\.json")
# A section of the paper is a few pages of text. Anything larger is not one.
MAX_FILE_BYTES = 256 * 1024
DEFAULT_MAX_AGE_DAYS = 1
LONGEST_MAX_AGE_DAYS = 7
DEFAULT_STORIES = 10
DEFAULT_KEEP_DAYS = 14
ARTICLE_KEYS = ("title", "body", "deck", "source", "url", "quote", "quote_by", "why", "grid")


class Unprintable(Exception):
    """A file in the inbox cannot be printed as it is."""


def inbox_dir(config: dict) -> Path:
    folder = config.get("folder")
    if folder is not None:
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("'folder' must be a path")
        return Path(folder.strip()).expanduser()
    if os.environ.get(INBOX_DIR_ENV):
        return Path(os.environ[INBOX_DIR_ENV]).expanduser()
    return press_sources.default_config_dir() / "inbox"


def _whole(config: dict, key: str, default: int, lowest: int, highest: int | None = None) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < lowest \
            or (highest is not None and value > highest):
        limit = f" from {lowest} to {highest}" if highest is not None else f", {lowest} or more"
        raise ValueError(f"'{key}' must be a whole number{limit}")
    return value


def _dated(name: str) -> dt.date | None:
    """The day a file is named for, or None when its name does not say."""
    match = NAME_RE.fullmatch(name)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def read_section(path: Path) -> dict:
    """The section a file holds, checked by the builder's own rules, or
    Unprintable saying why not."""
    try:
        # Not followed: a link in the inbox could point anywhere on the Press.
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise Unprintable("not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise Unprintable(f"larger than {MAX_FILE_BYTES // 1024} KiB")
        with path.open("rb") as file:
            data = file.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise Unprintable(f"could not be read: {exc.strerror or exc}") from exc
    if len(data) > MAX_FILE_BYTES:
        raise Unprintable(f"larger than {MAX_FILE_BYTES // 1024} KiB")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise Unprintable(f"not JSON: {' '.join(str(exc).split()) or type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise Unprintable("not a section: the top level must be an object")
    title = raw.get("title")
    try:
        section = build_paper.parse_section(
            {"title": title or "Inbox", "articles": raw.get("articles")}, "section")
    except build_paper.EditionError as exc:
        raise Unprintable(f"not a section: {exc}") from exc
    if not section.articles:
        raise Unprintable("not a section: it has no articles")
    return {"title": section.title if title else "",
            "articles": [{key: article[key] for key in ARTICLE_KEYS if article.get(key)}
                         for article in raw["articles"]]}


def _prune(printed: Path, oldest: dt.date) -> None:
    """Drop the printed files of papers older than `oldest`."""
    try:
        folders = [entry for entry in printed.iterdir() if entry.is_dir()]
    except OSError:
        return
    for folder in folders:
        try:
            if dt.date.fromisoformat(folder.name) < oldest:
                shutil.rmtree(folder, ignore_errors=True)
        except ValueError:
            pass  # not one of ours


def produce(date: dt.date, config: dict) -> dict:
    folder = inbox_dir(config)
    max_age = _whole(config, "max_age_days", DEFAULT_MAX_AGE_DAYS, 0, LONGEST_MAX_AGE_DAYS)
    stories = _whole(config, "stories", DEFAULT_STORIES, 1)
    keep_days = _whole(config, "keep_days", DEFAULT_KEEP_DAYS, 0)
    if not folder.is_dir():
        raise FileNotFoundError(f"the inbox folder {folder} does not exist")
    printed = folder / PRINTED / date.isoformat()
    _prune(folder / PRINTED, date - dt.timedelta(days=keep_days))

    notes: list[str] = []
    rejected = False

    def note(text: str) -> None:
        notes.append(text)
        print(f"inbox: {text}", file=sys.stderr)

    # What is waiting, then what this day's paper has already printed (so the
    # paper can be printed again).
    files: list[tuple[dt.date, str, bool, Path]] = []
    oldest = date - dt.timedelta(days=max_age)
    for path in sorted(folder.iterdir()):
        if path.suffix != ".json" or path.name.startswith("."):
            continue
        day = _dated(path.name)
        if day is None:
            rejected = True
            note(f"{path.name}: skipped, its name must be a date, as in 2026-10-05.json, "
                 "optionally followed by a label, as in 2026-10-05-label.json (a label "
                 "may only use a-z, A-Z, 0-9, '.', '_' and '-')")
        elif day < oldest:
            note(f"{path.name}: not printed, it is dated more than {max_age} day(s) "
                 "before this paper")
        elif day <= date:
            files.append((day, path.name, True, path))
    if printed.is_dir():
        files += [(day, path.name, False, path) for path in sorted(printed.iterdir())
                  if (day := _dated(path.name)) is not None]

    title, articles, left, done = "", [], 0, set()
    # Newest first, then by name. A waiting file comes before a printed one of
    # the same name and, once it prints, replaces it: it is the newer version.
    for _, name, waiting, path in sorted(
            files, key=lambda file: (-file[0].toordinal(), file[1], not file[2])):
        if name in done:
            continue
        if len(articles) >= stories:
            left += waiting
            continue
        try:
            section = read_section(path)
        except Unprintable as exc:
            rejected = True
            note(f"{name}: skipped, {exc}")
            continue
        except Exception as exc:  # one odd file must not take the others down
            rejected = True
            note(f"{name}: skipped, {type(exc).__name__}: {exc}")
            continue
        if waiting:
            try:
                printed.mkdir(parents=True, exist_ok=True)
                os.replace(path, printed / name)
            except OSError as exc:
                rejected = True
                note(f"{name}: not printed, it could not be moved to "
                     f"{PRINTED}/{date.isoformat()}/ ({exc.strerror or exc}), "
                     "so it would be printed again tomorrow")
                continue
        done.add(name)
        room = stories - len(articles)
        if len(section["articles"]) > room:
            note(f"{name}: only its first {room} of {len(section['articles'])} stories "
                 f"fit the section's {stories}")
        articles.extend(section["articles"][:room])
        title = title or section["title"]
    if left:
        note(f"{left} file(s) left waiting: the section already has its {stories} stories")

    result = {"title": title or config.get("title", ""), "articles": articles, "notes": notes}
    # An empty inbox is an ordinary day. One holding only files that could
    # not be printed is a failure someone should hear about.
    if not articles and not rejected:
        result["empty"] = "nothing fresh in the inbox"
    return result
