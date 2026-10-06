"""Sources: how the Press writes its own paper.

A source is a Python module with one function:

    def produce(date, config):
        return {"title": "World", "articles": [{"title": ..., "body": [...]}, ...]}

`date` is the edition's datetime.date. `config` is the source's `[[section]]`
table from sources.toml, as a dict. The return value is one section in the
edition format build_paper.py reads; an article may also carry `deck`,
`source` (a byline), `url`, `quote`, `quote_by` and `why`. A section may add
`notes`, a list of strings recorded beside its outcome (the built-in RSS
source lists unreachable feeds there). A source with nothing to say today
returns no articles and `empty`, a string saying why; its section is left out
of the paper without counting as a failure (the built-in inbox source does
this when no fresh file is waiting).

sources.toml lists the sections in the order they print:

    title = "The Morning Paper"          # optional masthead

    [[section]]
    title = "World"
    feeds = ["https://example.com/rss"]
    stories = 5

    [[section]]
    title = "Tides"
    source = "tides"                     # tides.py, beside this sources.toml
    timeout = 30                         # seconds; default 60

`source` defaults to "rss", the built-in feed reader (sources/rss.py);
"inbox" (sources/inbox.py) prints the files left in a folder. A name is
looked for as <name>.py in the config directory, then among the built-in
sources. The config directory is $PRESS_CONFIG_DIR, else
~/.config/morning-paper; a sources.toml there replaces the default one beside
this file.

Every source runs in a process of its own, all at once, each against its own
clock. One that raises, runs out of time, or returns nothing loses its own
section and nothing else.

"ask" (sources/ask.py) puts a question to a Muse and prints the answer. It is
the one source that runs after the others, so that it can be told what they
brought in: its config carries `sections_so_far`, a list of
{"title", "headlines"} for each section already in. Its answer is not matched
against their stories: a headline it repeats costs neither section anything.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import multiprocessing
import multiprocessing.connection
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from mnn import REPO

CONFIG_DIR_ENV = "PRESS_CONFIG_DIR"
CONFIG_NAME = "sources.toml"
DEFAULT_CONFIG = REPO / CONFIG_NAME
BUILTIN_DIR = Path(__file__).resolve().parent / "sources"
DEFAULT_SOURCE = "rss"
# Sources that run once the others are in, and the key their config then
# carries: what the paper holds so far.
LATE_SOURCES = ("ask",)
SO_FAR = "sections_so_far"
DEFAULT_TIMEOUT = 60
# No section is given longer than the day between two papers.
MAX_TIMEOUT = 24 * 60 * 60
SOURCE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Characters XML has no way to write, so an EPUB cannot hold them: control
# characters, lone surrogates and the two non-characters.
UNPRINTABLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


class ConfigError(Exception):
    """sources.toml is missing, unreadable, or does not describe a paper."""


class SourceNotFound(Exception):
    """A section names a source that is not there."""


class NothingToday(Exception):
    """A source ran and had, by its own account, nothing to print today."""

    def __init__(self, reason: str, notes: list[str]) -> None:
        super().__init__(reason)
        self.notes = notes


@dataclass(frozen=True)
class SectionSpec:
    title: str
    source: str
    timeout: float
    # The whole [[section]] table, handed to the source as it was written.
    config: dict


@dataclass(frozen=True)
class Paper:
    sections: tuple[SectionSpec, ...]
    path: Path
    title: str = ""
    note: str = ""
    weather: dict = field(default_factory=dict)


# --- sources.toml ----------------------------------------------------------


def default_config_dir() -> Path:
    if os.environ.get(CONFIG_DIR_ENV):
        return Path(os.environ[CONFIG_DIR_ENV]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "morning-paper"


def config_path(config_dir: Path | None) -> Path:
    """The reader's own sources.toml when there is one, else the default."""
    if config_dir is not None and (config_dir / CONFIG_NAME).is_file():
        return config_dir / CONFIG_NAME
    return DEFAULT_CONFIG


def parse_config(text: str, path: Path) -> Paper:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    def string(table: dict, key: str, where: str, default: str = "") -> str:
        value = table.get(key, default)
        if not isinstance(value, str):
            raise ConfigError(f"{where}: '{key}' must be a string")
        return value.strip()

    raw_sections = raw.get("section")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ConfigError(f"{path}: needs at least one [[section]]")
    sections = []
    for number, table in enumerate(raw_sections, start=1):
        where = f"{path}: section {number}"
        if not isinstance(table, dict):
            raise ConfigError(f"{where}: must be a [[section]] table")
        title = string(table, "title", where)
        if not title:
            raise ConfigError(f"{where}: missing 'title'")
        where = f"{where} ({title})"
        source = string(table, "source", where, DEFAULT_SOURCE)
        if not SOURCE_NAME_RE.fullmatch(source):
            raise ConfigError(f"{where}: 'source' must be a module name such as "
                              f"\"rss\", not {source!r}")
        timeout = table.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or not 0 < timeout <= MAX_TIMEOUT:
            raise ConfigError(f"{where}: 'timeout' must be a positive number of seconds, "
                              f"{MAX_TIMEOUT} (a day) at most")
        sections.append(SectionSpec(title=title, source=source, timeout=timeout,
                                    config=dict(table, title=title)))

    raw_weather = raw.get("weather", {})
    if not isinstance(raw_weather, dict):
        raise ConfigError(f"{path}: 'weather' must be a [weather] table")
    # The edition's weather object: a place, a forecast line and coordinates.
    where = f"{path}: weather"
    weather: dict = {}
    for key in ("location", "summary"):
        if value := string(raw_weather, key, where):
            weather[key] = value
    for key in ("latitude", "longitude"):
        if key not in raw_weather:
            continue
        value = raw_weather[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: '{key}' must be a number")
        weather[key] = value
    if ("latitude" in weather) != ("longitude" in weather):
        raise ConfigError(f"{where}: 'latitude' and 'longitude' must be given together")
    return Paper(sections=tuple(sections), path=path, title=string(raw, "title", str(path)),
                 note=string(raw, "note", str(path)), weather=weather)


def load_config(config_dir: Path | None) -> Paper:
    path = config_path(config_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_config(text, path)


# --- Finding and loading a source ------------------------------------------


def find_source(name: str, config_dir: Path | None) -> Path:
    """A reader's module wins over a built-in one of the same name."""
    places = [config_dir] if config_dir is not None else []
    places.append(BUILTIN_DIR)
    for place in places:
        if (place / f"{name}.py").is_file():
            return place / f"{name}.py"
    raise SourceNotFound(f"no source named {name!r}: looked for {name}.py in "
                         + " and ".join(str(place) for place in places))


def load_source(path: Path):
    """Import a source module from its file and check it has `produce`. Read
    fresh every time, so an edited module is picked up by the next run."""
    name = f"press_source_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} is not a Python module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    if not callable(getattr(module, "produce", None)):
        raise AttributeError(f"{path.name} has no produce(date, config) function")
    return module


# --- Running the sources ---------------------------------------------------


def _describe(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _produce(connection, path: str, date: str, config: dict, config_dir: str | None) -> None:
    """Body of a source's process: load it, run it, send back what it made."""
    try:
        # So a reader's source can import the modules beside it.
        if config_dir and config_dir not in sys.path:
            sys.path.insert(0, config_dir)
        module = load_source(Path(path))
        connection.send(("ok", module.produce(dt.date.fromisoformat(date), config)))
    except BaseException as exc:  # SystemExit and friends are failures here too
        try:
            connection.send(("error", _describe(exc)))
        except Exception:
            pass
    finally:
        connection.close()


def _printable(text: str) -> str:
    """The text with a replacement mark wherever an EPUB could not hold it."""
    return UNPRINTABLE_RE.sub("\N{REPLACEMENT CHARACTER}", text)


def _check(spec: SectionSpec, result: object) -> tuple[dict, list[str]]:
    """Turn what a source returned into a clean section, or raise ValueError
    saying why it cannot be printed, or NothingToday when the source says it
    has nothing for this paper."""
    # The builder's own rules decide what a printable section is.
    from mnn import build_paper

    if result is None:
        raise ValueError("returned nothing")
    if not isinstance(result, dict):
        raise ValueError(f"returned {type(result).__name__}, not a section")
    notes = result.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    notes = [" ".join(str(note).split()) for note in notes] if isinstance(notes, list) else []
    try:
        section = build_paper.parse_section(
            {"title": result.get("title") or spec.title, "articles": result.get("articles", [])},
            "section")
    except build_paper.EditionError as exc:
        raise ValueError(f"returned a section the builder cannot print: {exc}") from exc
    if not section.articles:
        empty = result.get("empty")
        if isinstance(empty, str) and empty.strip():
            raise NothingToday(" ".join(empty.split()), notes)
        raise ValueError("; ".join(["returned no stories", *notes]))
    articles = []
    for article in section.articles:
        entry = {"title": _printable(article.title),
                 "body": [_printable(paragraph) for paragraph in article.paragraphs]}
        entry.update({key: _printable(value)
                      for key in ("deck", "source", "url", "quote", "quote_by", "why")
                      if (value := getattr(article, key))})
        articles.append(entry)
    return {"title": _printable(section.title), "articles": articles}, notes


def run_sources(paper: Paper, date: dt.date,
                config_dir: Path | None) -> tuple[list[dict], list[dict]]:
    """Run every section's source and return (sections that can be printed,
    one outcome per configured section). Both keep the order of sources.toml.

    An outcome is {"title", "source", "status": "ok", "empty" or "failed",
    "stories", "headlines", "error", "notes", "seconds"}. "empty" is a source
    that had nothing to print today; why is the first of its notes."""
    context = multiprocessing.get_context("spawn")
    outcomes = [{"title": spec.title, "source": spec.source, "status": "failed", "stories": 0,
                 "headlines": [], "error": None, "notes": [], "seconds": 0.0}
                for spec in paper.sections]
    results: dict[int, dict] = {}
    running = {}  # receiving end of the pipe -> (index, process, deadline)
    started = time.monotonic()  # reset for each wave below

    def fail(index: int, reason: str) -> None:
        outcomes[index]["error"] = reason
        outcomes[index]["seconds"] = round(time.monotonic() - started, 1)

    def stop(process) -> None:
        if process.is_alive():
            process.terminate()
            process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)

    def so_far() -> list[dict]:
        return [{"title": results[index]["title"],
                 "headlines": [article["title"] for article in results[index]["articles"]]}
                for index in sorted(results)]

    waves = [[index for index, spec in enumerate(paper.sections)
              if (spec.source in LATE_SOURCES) == late] for late in (False, True)]
    for late, wave in enumerate(waves):
        started = time.monotonic()
        for index in wave:
            spec = paper.sections[index]
            config = dict(spec.config, **{SO_FAR: so_far()}) if late else spec.config
            try:
                path = find_source(spec.source, config_dir)
                receiver, sender = context.Pipe(duplex=False)
                process = context.Process(
                    target=_produce, daemon=True, name=f"source-{spec.source}-{index}",
                    args=(sender, str(path), date.isoformat(), config,
                          str(config_dir) if config_dir is not None else None))
                process.start()
                sender.close()
            except SourceNotFound as exc:
                fail(index, str(exc))
            except Exception as exc:  # could not even start it: still only this section
                fail(index, f"could not start: {_describe(exc)}")
            else:
                running[receiver] = (index, process, time.monotonic() + spec.timeout)

        while running:
            wait = min(deadline for _, _, deadline in running.values()) - time.monotonic()
            for receiver in multiprocessing.connection.wait(list(running), max(0.0, wait)):
                index, process, _ = running.pop(receiver)
                try:
                    kind, payload = receiver.recv()
                    reason = f"raised {payload}" if kind != "ok" else None
                except EOFError:  # the process died before it could answer
                    process.join(2)
                    reason = f"stopped without a result (exit code {process.exitcode})"
                except Exception as exc:
                    reason = f"sent a result that could not be read: {_describe(exc)}"
                receiver.close()
                # The answer is in; anything the source left running goes with it.
                stop(process)
                if reason is None:
                    try:
                        results[index], notes = _check(paper.sections[index], payload)
                    except NothingToday as exc:
                        outcomes[index]["status"] = "empty"
                        notes = [str(exc), *exc.notes]
                    except ValueError as exc:
                        reason = str(exc)
                if reason is not None:
                    fail(index, reason)
                else:
                    outcomes[index].update(notes=notes,
                                           seconds=round(time.monotonic() - started, 1))
            now = time.monotonic()
            for receiver, (index, process, deadline) in list(running.items()):
                if now >= deadline:
                    del running[receiver]
                    stop(process)
                    receiver.close()
                    fail(index, f"timed out after {paper.sections[index].timeout:g}s")

    # A story already printed in an earlier section is not printed again. A
    # late source is left out of that: told the headlines, it may head its own
    # words with one, and that is no reason to drop either.
    seen: set[str] = set()
    sections = []
    for index in sorted(results):
        section = results[index]
        if paper.sections[index].source in LATE_SOURCES:
            fresh = section["articles"]
        else:
            fresh = []
            for article in section["articles"]:
                keys = {article["title"].casefold(),
                        article.get("url") or article["title"].casefold()}
                if not keys & seen:
                    seen |= keys
                    fresh.append(article)
        if not fresh:
            outcomes[index]["error"] = "every story it returned is already in an earlier section"
            continue
        section["articles"] = fresh
        sections.append(section)
        outcomes[index].update(status="ok", stories=len(fresh),
                               headlines=[article["title"] for article in fresh])
    return sections, outcomes


def assemble(paper: Paper, date: dt.date, sections: list[dict]) -> dict:
    """The edition file for one day, in the shape build_paper.py reads."""
    edition: dict = {"date": date.isoformat(), "sections": sections}
    if paper.title:
        edition["title"] = paper.title
    if paper.note:
        edition["note"] = paper.note
    if paper.weather:
        edition["weather"] = paper.weather
    return edition
