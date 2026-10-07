"""Build the Morning Paper from an edition file.

Reads an edition JSON (see docs/editions.md, "Writing an edition") and writes two
files into the output directory:

    morning-paper-YYYY-MM-DD.epub   EPUB 3 for KOReader
    frontpage-YYYY-MM-DD.png        1236x1648 8-bit grayscale front page

Usage:
    uv run mnn-press build edition/sample.json [--out out]
        [--fetch-weather] [--config-dir DIR] [--keep-days N]
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import html
import io
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from ebooklib import epub
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from mnn import REPO

BLACK, DARK, WHITE = 0, 64, 255

DEFAULT_TITLE = "The Morning Paper"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"
# WMO weather interpretation codes, as returned by Open-Meteo.
WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Thunderstorms with hail",
}
# Lead story plus up to three rows of two secondary headlines, or the same
# six in one column beside a portrait. Those that do not fit above the banner
# are left off.
FRONT_PAGE_HEADLINES = 7

# The optional front page portrait (see docs/sources.md, "A front page portrait"):
# a picture in the config directory, and a second one for rainy days.
PORTRAIT_NAME = "portrait"
RAIN_PORTRAIT_NAME = "portrait-rain"
PORTRAIT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
# The rainy-day portrait runs when the day's chance of rain is at least this.
RAIN_PORTRAIT_CHANCE = 30
PORTRAIT_BORDER = 4
# A long lead story can leave too little room for a portrait worth printing.
PORTRAIT_MIN_SIDE = 240

# Black and one dark gray only: nothing here depends on colour, and thin light
# gray strokes wash out on e-ink. No page margins either: the reader adds its
# own, and a second set on top makes every page look shrunk.
CSS = """\
body { font-family: serif; line-height: 1.45; margin: 0; color: #000; }
p { margin: 0; text-indent: 1.3em; text-align: justify; }
p.first { text-indent: 0; }
a { color: #000; }
div.cover { text-align: center; margin: 0; padding: 0; }
div.cover img { max-width: 100%; height: auto; }

p.kicker { text-indent: 0; text-align: left; font-size: 0.7em; font-weight: bold;
  letter-spacing: 0.1em; text-transform: uppercase; margin-top: 0.3em; }
p.kicker a { text-decoration: none; }
span.tab { background-color: #000; color: #fff; padding: 0.2em 0.7em; }
span.tab a { color: #fff; }
span.where { padding-left: 0.8em; }

h1 { font-size: 1.95em; line-height: 1.12; margin: 0.35em 0 0.3em 0; text-align: left; }
p.deck { text-indent: 0; text-align: left; font-style: italic; font-size: 1.08em;
  margin-bottom: 0.7em; }
p.byline { text-indent: 0; text-align: left; font-size: 0.7em; letter-spacing: 0.06em;
  text-transform: uppercase; border-top: 1px solid #000; border-bottom: 1px solid #000;
  padding: 0.4em 0; margin-bottom: 1em; }
span.dropcap { float: left; font-size: 3.5em; line-height: 0.8; font-weight: bold;
  margin: 0.06em 0.08em 0 0; }

blockquote.pull { page-break-inside: avoid; margin: 1em 0; padding: 0.6em 0;
  border-top: 3px double #000; border-bottom: 3px double #000; }
blockquote.pull p { text-indent: 0; text-align: left; font-style: italic;
  font-size: 1.3em; line-height: 1.3; }
blockquote.pull p.who { font-style: normal; font-weight: bold; font-size: 0.68em;
  letter-spacing: 0.08em; text-transform: uppercase; margin-top: 0.6em; }
div.box { page-break-inside: avoid; border: 2px solid #000;
  padding: 0.6em 0.9em 0.7em 0.9em; margin: 1em 0; }
div.box p { text-indent: 0; text-align: left; font-size: 0.93em; }
div.box p.label { font-weight: bold; font-size: 0.7em; letter-spacing: 0.1em;
  text-transform: uppercase; margin-bottom: 0.3em; }
p.source { text-indent: 0; text-align: left; font-size: 0.78em; margin-top: 1.2em;
  border-top: 1px solid #000; padding-top: 0.5em; }

table.grid { page-break-inside: avoid; clear: both; border-collapse: collapse;
  margin: 1.2em auto; border: 3px solid #000; }
table.grid td { width: 1.9em; height: 1.9em; padding: 0; text-align: center;
  vertical-align: middle; font-size: 1.25em; font-weight: bold; line-height: 1;
  border: 1px solid #000; }
table.grid td.r { border-right: 3px solid #000; }
table.grid td.b { border-bottom: 3px solid #000; }
table.grid td.r.b { border-right: 3px solid #000; border-bottom: 3px solid #000; }

div.wayout { page-break-inside: avoid; margin-top: 1.4em; }
p.next { text-indent: 0; text-align: left; font-size: 0.95em; }
p.next b { font-size: 0.74em; letter-spacing: 0.1em; text-transform: uppercase;
  padding-right: 0.6em; }
p.next a { font-weight: bold; text-decoration: none; }
p.end { text-indent: 0; text-align: center; font-style: italic; }
table.way { width: 100%; border-collapse: separate; border-spacing: 0;
  margin-top: 0.7em; border: 2px solid #000; }
table.way td { text-align: center; vertical-align: middle; padding: 1.25em 0.3em;
  font-size: 0.74em; font-weight: bold; letter-spacing: 0.08em; text-transform: uppercase;
  line-height: 1.2; border-right: 2px solid #000; }
table.way td.last { border-right: none; }
table.way a { text-decoration: none; }

h1.section { font-size: 2.7em; text-align: center; border-top: 3px double #000;
  border-bottom: 3px double #000; padding: 0.3em 0; margin: 0.7em 0 0.25em 0; }
p.count { text-indent: 0; text-align: center; font-style: italic; margin-bottom: 1.3em; }
ol.heads { margin: 0; padding-left: 1.5em; }
ol.heads li { margin-bottom: 0.95em; text-align: left; }
ol.heads a { font-weight: bold; font-size: 1.12em; text-decoration: none; line-height: 1.25; }
ol.heads span.deck { display: block; font-style: italic; font-size: 0.9em; }

h1.home { font-size: 1.6em; margin-bottom: 0.1em; }
p.homecount { text-indent: 0; text-align: left; font-style: italic; margin-bottom: 0.6em; }
h2.shelf { font-size: 0.82em; letter-spacing: 0.1em; text-transform: uppercase;
  text-align: left; margin: 0.9em 0 0.3em 0; padding-bottom: 0.2em;
  border-bottom: 2px solid #000; }
h2.shelf a { text-decoration: none; }
ul.shelf { margin: 0; padding-left: 1.1em; }
ul.shelf li { text-align: left; font-size: 0.88em; margin-bottom: 0.25em; line-height: 1.28; }
ul.shelf a { text-decoration: none; }
"""

# Roughly how fast people read a screen of news.
WORDS_PER_MINUTE = 230
HOME_FILE = "home.xhtml"



@dataclass(frozen=True)
class Canvas:
    """Pixel size of a front page drawing and the white space kept inside it."""
    width: int
    height: int
    margin_x: int
    margin_top: int
    margin_bottom: int


# Kindle Paperwhite 11 panel in portrait. The wake screen is drawn edge to
# edge, so it carries its own margins.
WAKE_SCREEN = Canvas(1236, 1648, margin_x=72, margin_top=56, margin_bottom=52)
# The space KOReader gives a page on that panel with its default margins and
# status bar. A cover of exactly this size is shown pixel for pixel; KOReader
# never enlarges an image, and shrinks a bigger one, so any other size leaves
# a white border or blurs the text. Its own page margins stand in for ours.
EPUB_COVER = Canvas(1194, 1536, margin_x=4, margin_top=4, margin_bottom=4)


class EditionError(Exception):
    """The edition file is missing something the builder needs."""


@dataclass(frozen=True)
class Article:
    title: str
    paragraphs: tuple[str, ...]
    deck: str = ""
    source: str = ""
    url: str = ""
    quote: str = ""
    quote_by: str = ""
    why: str = ""
    # A number puzzle: nine rows of nine cells, a digit or "." for an empty one.
    grid: tuple[str, ...] = ()

    @property
    def minutes(self) -> int:
        words = sum(len(p.split()) for p in (self.deck, *self.paragraphs, self.quote, self.why))
        return max(1, round(words / WORDS_PER_MINUTE))


@dataclass(frozen=True)
class Section:
    title: str
    articles: tuple[Article, ...]


@dataclass(frozen=True)
class Edition:
    title: str
    date: dt.date
    sections: tuple[Section, ...]
    note: str = ""
    weather_location: str = ""
    weather_summary: str = ""
    weather_coordinates: tuple[float, float] | None = None
    # Percent chance of rain from the fetched forecast, when there is one.
    rain_chance: float | None = None
    # The picture for the front page, as read from the config directory.
    portrait: Image.Image | None = None

    @property
    def story_count(self) -> int:
        return sum(len(section.articles) for section in self.sections)


# --- Edition loading -------------------------------------------------------


def _text(obj: dict, key: str, where: str, *, required: bool = False) -> str:
    value = obj.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise EditionError(f"{where}: {key!r} must be a string")
    value = " ".join(value.split())
    if required and not value:
        raise EditionError(f"{where}: missing {key!r}")
    return value


def _paragraphs(raw: object, where: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = re.split(r"\n\s*\n", raw)
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise EditionError(f"{where}: 'body' must be a string or a list of strings")
    paragraphs = tuple(" ".join(p.split()) for p in raw if p.strip())
    if not paragraphs:
        raise EditionError(f"{where}: 'body' is empty")
    return paragraphs


GRID_SIDE = 9
GRID_ROW_RE = re.compile(rf"[1-9.]{{{GRID_SIDE}}}")


def _grid(raw: object, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) != GRID_SIDE \
            or not all(isinstance(row, str) and GRID_ROW_RE.fullmatch(row) for row in raw):
        raise EditionError(f"{where}: 'grid' must be {GRID_SIDE} rows of {GRID_SIDE} cells, "
                           'each a digit from 1 to 9 or "." for an empty cell')
    return tuple(raw)


def _article(raw: object, where: str) -> Article:
    if not isinstance(raw, dict):
        raise EditionError(f"{where}: must be an object")
    url = _text(raw, "url", where)
    if url and not url.startswith(("http://", "https://")):
        raise EditionError(f"{where}: 'url' must be an http(s) link")
    return Article(
        title=_text(raw, "title", where, required=True),
        paragraphs=_paragraphs(raw.get("body"), where),
        deck=_text(raw, "deck", where),
        source=_text(raw, "source", where),
        url=url,
        quote=_text(raw, "quote", where),
        quote_by=_text(raw, "quote_by", where),
        why=_text(raw, "why", where),
        grid=_grid(raw.get("grid"), where),
    )


def parse_section(raw: object, where: str) -> Section:
    """One section of an edition, checked. It may come back with no articles."""
    if not isinstance(raw, dict):
        raise EditionError(f"{where}: must be an object")
    title = _text(raw, "title", where, required=True)
    raw_articles = raw.get("articles", [])
    if not isinstance(raw_articles, list):
        raise EditionError(f"{where}: 'articles' must be a list")
    return Section(title=title, articles=tuple(
        _article(raw_article, f"{where} ({title}), article {ai}")
        for ai, raw_article in enumerate(raw_articles, start=1)))


def load_edition(path: Path) -> Edition:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EditionError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise EditionError(f"{path}: top level must be an object")

    try:
        date = dt.date.fromisoformat(_text(raw, "date", str(path), required=True))
    except ValueError as exc:
        raise EditionError(f"{path}: 'date' must be YYYY-MM-DD") from exc

    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, list):
        raise EditionError(f"{path}: 'sections' must be a list")
    sections = []
    for si, raw_section in enumerate(raw_sections, start=1):
        section = parse_section(raw_section, f"{path}: section {si}")
        # A quiet news day can leave a section empty; drop it rather than
        # printing a section page with nothing under it.
        if section.articles:
            sections.append(section)
        else:
            print(f"note: section {section.title!r} has no articles, skipping it",
                  file=sys.stderr)
    if not sections:
        raise EditionError(f"{path}: no articles in any section")

    weather = raw.get("weather") or {}
    if not isinstance(weather, dict):
        raise EditionError(f"{path}: 'weather' must be an object")
    coordinates = None
    if "latitude" in weather or "longitude" in weather:
        try:
            coordinates = (float(weather["latitude"]), float(weather["longitude"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise EditionError(
                f"{path}: weather needs numeric 'latitude' and 'longitude' together") from exc
    return Edition(
        title=_text(raw, "title", str(path)) or DEFAULT_TITLE,
        date=date,
        sections=tuple(sections),
        note=_text(raw, "note", str(path)),
        weather_location=_text(weather, "location", f"{path}: weather"),
        weather_summary=_text(weather, "summary", f"{path}: weather"),
        weather_coordinates=coordinates,
    )


def fetch_weather(date: dt.date, coordinates: tuple[float, float]) -> tuple[str, float | None]:
    """One-line forecast for a day from Open-Meteo, e.g.
    'Partly cloudy, high 64°F, low 48°F, 20% chance of rain', and the chance
    of rain as a number (None when the service gives none)."""
    query = urllib.parse.urlencode({
        "latitude": coordinates[0],
        "longitude": coordinates[1],
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "start_date": date.isoformat(),
        "end_date": date.isoformat(),
    })
    with urllib.request.urlopen(f"{WEATHER_API}?{query}", timeout=15) as response:
        daily = json.load(response)["daily"]
    parts = [WEATHER_CODES.get(daily["weather_code"][0], "Mixed conditions"),
             f"high {round(daily['temperature_2m_max'][0])}°F",
             f"low {round(daily['temperature_2m_min'][0])}°F"]
    rain = daily["precipitation_probability_max"][0]
    if rain is not None:
        parts.append(f"{round(rain)}% chance of rain")
        rain = float(rain)
    return ", ".join(parts), rain


def with_weather(edition: Edition) -> Edition:
    """Fill in the forecast unless the edition already carries one. The paper
    still has to come out when the forecast service does not answer."""
    if edition.weather_summary:
        return edition
    if edition.weather_coordinates is None:
        print("note: the edition gives no weather coordinates, skipping the forecast",
              file=sys.stderr)
        return edition
    try:
        summary, rain_chance = fetch_weather(edition.date, edition.weather_coordinates)
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"warning: forecast unavailable ({exc}), building without it", file=sys.stderr)
        return edition
    return replace(edition, weather_summary=summary, rain_chance=rain_chance)


def find_portraits(config_dir: Path, rain_chance: float | None) -> list[Path]:
    """The portrait files to try for the day, first choice first: the
    rainy-day one when rain is likely and there is one, then the plain one.
    Capitals in a file name do not matter. A file that looks like a portrait
    but is not one the Press reads is logged, and so is a rainy-day picture
    left waiting for rain with no plain one beside it."""
    if not config_dir.is_dir():
        return []
    named: dict[str, list[Path]] = {PORTRAIT_NAME: [], RAIN_PORTRAIT_NAME: []}
    for path in sorted(config_dir.iterdir()):
        if not path.name.lower().startswith(PORTRAIT_NAME) or path.is_dir():
            continue
        name, suffix = path.stem.lower(), path.suffix.lower()
        if name in named and suffix in PORTRAIT_SUFFIXES:
            named[name].append(path)
        else:
            print(f"warning: {path} looks like a portrait, but only {PORTRAIT_NAME} and "
                  f"{RAIN_PORTRAIT_NAME} ending {', '.join(PORTRAIT_SUFFIXES)} are read, "
                  "ignoring it", file=sys.stderr)
    # A name kept in several formats is read in the first of PORTRAIT_SUFFIXES.
    plain, rain = (
        min(named[name], key=lambda path: PORTRAIT_SUFFIXES.index(path.suffix.lower()),
            default=None)
        for name in (PORTRAIT_NAME, RAIN_PORTRAIT_NAME))
    if rain_chance is None or rain_chance < RAIN_PORTRAIT_CHANCE:
        if rain is not None and plain is None:
            print(f"note: portrait {rain} waits for a rainy day and no plain portrait is "
                  "set, printing without one", file=sys.stderr)
        rain = None
    return [path for path in (rain, plain) if path is not None]


def read_portrait(path: Path) -> Image.Image:
    """A picture file as grayscale, the right way up, on white paper. A
    large one is brought down towards the size of a slot on the way."""
    # No slot is wider than half a page, and a camera's photo is many times
    # that. Shrinking it as early as its format allows, before any copy is
    # made, is what keeps a large one from filling memory.
    widest = max(WAKE_SCREEN.width, EPUB_COVER.width) // 2
    with Image.open(path) as source:
        # A JPEG can be decoded smaller to begin with.
        source.draft(None, (widest, widest))
        ImageOps.exif_transpose(source, in_place=True)
        picture = source
        if picture.mode.startswith("I;16"):
            # Sixteen-bit gray is scaled to eight bits; converted as it is,
            # nearly every shade would be clipped to white.
            picture = picture.point(lambda shade: shade / 256).convert("L")
        elif picture.mode not in ("L", "RGB", "RGBA"):
            # A palette or a 1-bit picture cannot be averaged as it stands.
            picture = picture.convert("RGBA")
        factor = min(picture.size) // widest
        if factor > 1:
            picture = picture.reduce(factor)
        picture = picture.convert("RGBA")
    # Transparency prints as paper.
    flat = Image.new("RGBA", picture.size, "white")
    flat.alpha_composite(picture)
    return flat.convert("L")


def with_portrait(edition: Edition, config_dir: Path | None) -> Edition:
    """Add the day's portrait from the config directory, if there is one. A
    rainy-day picture that cannot be read gives way to the plain one, and
    whatever goes wrong finding or reading a picture costs the portrait,
    never the paper."""
    if config_dir is None:
        return edition
    try:
        paths = find_portraits(config_dir, edition.rain_chance)
    except Exception as exc:
        print(f"warning: portrait in {config_dir} cannot be read "
              f"({type(exc).__name__}: {exc}), printing without it", file=sys.stderr)
        return edition
    for tried, path in enumerate(paths, start=1):
        try:
            return replace(edition, portrait=read_portrait(path))
        except Exception as exc:
            then = (f"using {paths[tried].name} instead" if tried < len(paths)
                    else "printing without it")
            print(f"warning: portrait {path} cannot be read ({type(exc).__name__}: {exc}), "
                  f"{then}", file=sys.stderr)
    return edition


def eink_portrait(picture: Image.Image, side: int) -> Image.Image:
    """Fit a picture to a square slot for e-ink: head and shoulders, pure
    black and white by Floyd-Steinberg dither, inside a thin black border. A
    picture that is already that size and already black and white is kept."""
    shades = {shade for _, shade in picture.getcolors()}
    if picture.size == (side, side) and shades <= {BLACK, WHITE}:
        return picture
    inner = side - 2 * PORTRAIT_BORDER
    # A face sits above the middle of most pictures, so the crop leans up.
    fitted = ImageOps.fit(picture.convert("L"), (inner, inner), Image.Resampling.LANCZOS,
                          centering=(0.5, 0.35))
    dithered = ImageEnhance.Contrast(fitted).enhance(1.25).convert("1").convert("L")
    return ImageOps.expand(dithered, PORTRAIT_BORDER, BLACK)


def prune_output(out_dir: Path, keep_days: int, today: dt.date, keep: dt.date) -> list[Path]:
    """Delete built papers and front pages dated more than keep_days ago."""
    cutoff = today - dt.timedelta(days=keep_days)
    removed = []
    for path in sorted(out_dir.iterdir()):
        match = re.fullmatch(r"(?:morning-paper|frontpage)-(\d{4}-\d{2}-\d{2})\.(?:epub|png)",
                             path.name)
        if not match:
            continue
        try:
            date = dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if date < cutoff and date != keep:
            path.unlink()
            removed.append(path)
    return removed


def long_date(date: dt.date) -> str:
    return f"{date:%A, %B} {date.day}, {date.year}"


def edition_urn(date: dt.date) -> str:
    """Stable identifier for one day's paper (server.py derives the same one)."""
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'morning-paper/{date.isoformat()}')}"


def top_headlines(edition: Edition, limit: int) -> list[tuple[Section, Article]]:
    """Front page stories first, then the lead story of every other section."""
    front, *rest = edition.sections
    picks = [(front, article) for article in front.articles]
    picks += [(section, section.articles[0]) for section in rest]
    return picks[:limit]


# --- Front page image ------------------------------------------------------


@functools.cache
def _font_file(pattern: str) -> str | None:
    """Resolve a fontconfig pattern such as 'serif:bold' to a font file."""
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", pattern],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _font(pattern: str, size: int) -> ImageFont.FreeTypeFont:
    path = _font_file(pattern)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    print(f"warning: no font for {pattern!r}, using Pillow's built-in face", file=sys.stderr)
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int) -> list[str]:
    """Greedy word wrap, ending the last line with an ellipsis if text is cut."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if not current or draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        last = lines[max_lines - 1]
        while " " in last and draw.textlength(f"{last}…", font=font) > max_width:
            last = last.rsplit(" ", 1)[0]
        lines = lines[: max_lines - 1] + [last.rstrip(" ,;:.") + "…"]
    return lines


def _tracked(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, font,
             fill: int, *, align: str = "left", tracking: int = 3) -> None:
    """Draw letter-spaced text, which Pillow has no built-in option for."""
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x, y = xy
    if align == "center":
        x -= total / 2
    elif align == "right":
        x -= total
    for ch, width in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += width + tracking


def render_front_page(edition: Edition, canvas: Canvas, *, ready_note: bool) -> Image.Image:
    """Draw the front page. With ready_note it is the Kindle wake screen;
    without it the same page serves as the EPUB cover."""
    img = Image.new("L", (canvas.width, canvas.height), WHITE)
    draw = ImageDraw.Draw(img)
    x0, x1, cx = canvas.margin_x, canvas.width - canvas.margin_x, canvas.width // 2
    width = x1 - x0

    label = _font("serif:bold", 24)
    small_label = _font("serif:bold", 21)

    # Top strip and masthead.
    y = canvas.margin_top
    _tracked(draw, (x0, y), "MORNING EDITION", label, BLACK)
    _tracked(draw, (x1, y), edition.date.isoformat(), label, BLACK, align="right")
    y += 44
    draw.rectangle((x0, y, x1, y + 1), fill=BLACK)
    y += 14

    masthead_size = 128
    while masthead_size > 48 and draw.textlength(
            edition.title, font=_font("serif:bold", masthead_size)) > width:
        masthead_size -= 4
    draw.text((cx, y), edition.title, font=_font("serif:bold", masthead_size),
              fill=BLACK, anchor="ma")
    y += int(masthead_size * 1.42)
    draw.rectangle((x0, y, x1, y + 5), fill=BLACK)
    draw.rectangle((x0, y + 10, x1, y + 11), fill=BLACK)
    y += 30

    # Dateline and weather.
    draw.text((cx, y), long_date(edition.date), font=_font("serif:bold", 34),
              fill=BLACK, anchor="ma")
    y += 50
    # An edition with no weather at all gets no weather line.
    weather = edition.weather_summary
    if edition.weather_location:
        weather = f"{edition.weather_location}: {weather or 'forecast unavailable'}"
    if weather:
        weather_font = _font("serif:italic", 28)
        weather_line = _wrap(draw, weather, weather_font, width, 1)[0]
        draw.text((cx, y), weather_line, font=weather_font, fill=BLACK, anchor="ma")
        y += 48
    draw.rectangle((x0, y, x1, y + 1), fill=BLACK)
    y += 30

    # Footer is laid out from the bottom so the headlines know where to stop.
    bottom = canvas.height - canvas.margin_bottom
    if edition.note:
        note_font = _font("serif:italic", 24)
        note_line = _wrap(draw, edition.note, note_font, width, 1)[0]
        draw.text((cx, bottom), note_line, font=note_font, fill=DARK, anchor="md")
        bottom -= 46
    box_top = bottom - 172
    sections_line = " · ".join(section.title for section in edition.sections)
    box_title_font = _font("serif:bold", 56)
    box_sub_font = _font("serif", 28)
    if ready_note:
        draw.rectangle((x0, box_top, x1, bottom), fill=BLACK)
        box_fill = WHITE
        box_title = "Today's Paper is ready"
        box_sub = (f"{edition.story_count} stories in {len(edition.sections)} sections"
                   " · open it in KOReader")
    else:
        draw.rectangle((x0, box_top, x1, bottom), outline=BLACK, width=4)
        box_fill = BLACK
        box_title = "Inside this edition"
        box_sub = sections_line
    draw.text((cx, box_top + 26), box_title, font=box_title_font, fill=box_fill, anchor="ma")
    box_sub = _wrap(draw, box_sub, box_sub_font, width - 60, 1)[0]
    draw.text((cx, box_top + 112), box_sub, font=box_sub_font, fill=box_fill, anchor="ma")
    limit = box_top - 28

    # Lead story.
    (lead_section, lead), *others = top_headlines(edition, FRONT_PAGE_HEADLINES)
    _tracked(draw, (x0, y), lead_section.title.upper(), label, DARK)
    y += 38
    lead_font = _font("serif:bold", 62)
    for line in _wrap(draw, lead.title, lead_font, width, 3):
        draw.text((x0, y), line, font=lead_font, fill=BLACK)
        y += 74
    y += 22
    lead_text = lead.deck or lead.paragraphs[0]
    deck_font = _font("serif", 31)
    for line in _wrap(draw, lead_text, deck_font, width, 3):
        draw.text((x0, y), line, font=deck_font, fill=BLACK)
        y += 43
    y += 26
    draw.rectangle((x0, y, x1, y + 1), fill=BLACK)
    y += 24

    gutter = 44
    col_width = (width - gutter) // 2
    head_font = _font("serif:bold", 35)
    kicker_h, line_h = 32, 44

    # With a portrait, it takes the right of the page beside one column of
    # secondary headlines, for as many as fit above the banner.
    side = min(col_width, limit - y)
    portrait = None
    if edition.portrait is not None and side >= PORTRAIT_MIN_SIDE:
        try:
            portrait = eink_portrait(edition.portrait, side)
        except Exception as exc:
            print(f"warning: portrait cannot be converted ({type(exc).__name__}: {exc}), "
                  "printing without it", file=sys.stderr)
    if portrait is not None:
        img.paste(portrait, (x1 - side, y))
        text_width = width - side - gutter
        if others:
            _tracked(draw, (x0, y), "ALSO IN TODAY'S PAPER", label, BLACK)
            y += 46
        for i, (section, article) in enumerate(others):
            lines = _wrap(draw, article.title, head_font, text_width, 3)
            if y + kicker_h + line_h * len(lines) > limit:
                break
            if i:
                draw.rectangle((x0, y - 16, x0 + text_width, y - 15), fill=DARK)
            _tracked(draw, (x0, y), section.title.upper(), small_label, DARK, tracking=2)
            for n, line in enumerate(lines):
                draw.text((x0, y + kicker_h + n * line_h), line, font=head_font, fill=BLACK)
            y += kicker_h + line_h * len(lines) + 30
        return img

    # Secondary headlines, two columns per row, for as many rows as fit.
    if others:
        _tracked(draw, (x0, y), "ALSO IN TODAY'S PAPER", label, BLACK)
        y += 46
    grid_top = y
    for row_start in range(0, len(others), 2):
        row = others[row_start:row_start + 2]
        wrapped = [_wrap(draw, article.title, head_font, col_width, 3) for _, article in row]
        row_height = kicker_h + line_h * max(len(lines) for lines in wrapped)
        if y + row_height > limit:
            break
        if row_start:
            draw.rectangle((x0, y - 16, x1, y - 15), fill=DARK)
        for col, ((section, _), lines) in enumerate(zip(row, wrapped)):
            x = x0 + col * (col_width + gutter)
            _tracked(draw, (x, y), section.title.upper(), small_label, DARK, tracking=2)
            for i, line in enumerate(lines):
                draw.text((x, y + kicker_h + i * line_h), line, font=head_font, fill=BLACK)
        y += row_height + 30
    if y > grid_top:
        # Column rule, drawn once the height of the grid is known.
        rule_x = x0 + col_width + gutter // 2
        draw.rectangle((rule_x, grid_top, rule_x + 1, y - 34), fill=DARK)

    return img


def write_front_page(edition: Edition, out_dir: Path) -> Path:
    final = out_dir / f"frontpage-{edition.date.isoformat()}.png"
    tmp = final.with_name(f".{final.name}.tmp")
    render_front_page(edition, WAKE_SCREEN, ready_note=True).save(
        tmp, format="PNG", optimize=True)
    tmp.replace(final)
    return final


# --- EPUB ------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _minutes(count: int) -> str:
    return f"about {count} minute{'' if count == 1 else 's'}"


def _kicker(label: str, href: str, where: str) -> str:
    """The line at the top of every page: a black tab that names where you
    are and, tapped, takes you one level up."""
    return (f'<p class="kicker"><span class="tab"><a href="{_esc(href)}">{_esc(label)}</a></span>'
            f'<span class="where">{_esc(where)}</span></p>')


def _way_bar(cells: list[tuple[str, str]]) -> str:
    """The row of large tap targets that ends every page, so there is always a
    way back to the contents without the reader's own menus."""
    tds = "".join(
        f'<td{" class=\"last\"" if i == len(cells) - 1 else ""}>'
        f'<a href="{_esc(href)}">{_esc(label)}</a></td>'
        for i, (label, href) in enumerate(cells))
    return f'<table class="way"><tr>{tds}</tr></table>'


def _first_paragraph(text: str) -> str:
    if text[0].isalnum():
        return (f'<p class="first"><span class="dropcap">{_esc(text[0])}</span>'
                f"{_esc(text[1:])}</p>")
    return f'<p class="first">{_esc(text)}</p>'


def _grid_table(grid: tuple[str, ...]) -> str:
    """The puzzle as a table: heavy rules round each box of three, and the
    empty cells left empty."""
    rows = []
    for r, row in enumerate(grid):
        cells = []
        for c, cell in enumerate(row):
            edges = " ".join(name for name, on in (("r", c % 3 == 2 and c < GRID_SIDE - 1),
                                                   ("b", r % 3 == 2 and r < GRID_SIDE - 1)) if on)
            cells.append(f'<td{f" class=\"{edges}\"" if edges else ""}>'
                         f'{cell if cell != "." else "&#160;"}</td>')
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return f'<table class="grid">{"".join(rows)}</table>'


def _article_body(section: Section, article: Article, *, number: int, section_href: str,
                  following: tuple[str, str, str] | None) -> str:
    """`following` is (label, title, href) for what comes after this story, or
    None when it is the last page of the paper."""
    parts = [
        _kicker(section.title, section_href, f"{number} of {len(section.articles)}"),
        f"<h1>{_esc(article.title)}</h1>",
    ]
    if article.deck:
        parts.append(f'<p class="deck">{_esc(article.deck)}</p>')
    # A puzzle is not read by the minute.
    length = "Puzzle" if article.grid else f"{article.minutes} minute read"
    byline = " \u00b7 ".join(filter(None, (article.source, length)))
    parts.append(f'<p class="byline">{_esc(byline)}</p>')

    # The pull quote sits after the second paragraph, or after the only one.
    quote_after = min(1, len(article.paragraphs) - 1)
    for i, paragraph in enumerate(article.paragraphs):
        if i == 0:
            parts.append(_first_paragraph(paragraph))
        else:
            # Text resumes flush left after the quote, as after any break.
            flush = article.quote and i == quote_after + 1
            parts.append(f'<p{" class=\"first\"" if flush else ""}>{_esc(paragraph)}</p>')
        if article.quote and i == quote_after:
            who = f'<p class="who">{_esc(article.quote_by)}</p>' if article.quote_by else ""
            parts.append(f'<blockquote class="pull"><p>\u201c{_esc(article.quote)}\u201d</p>'
                         f"{who}</blockquote>")
    if article.grid:
        parts.append(_grid_table(article.grid))
    if article.why:
        parts.append(f'<div class="box"><p class="label">Why it matters</p>'
                     f"<p>{_esc(article.why)}</p></div>")
    if article.url:
        parts.append(
            f'<p class="source">Source: <a href="{_esc(article.url)}">{_esc(article.url)}</a></p>')

    cells = [("\u2039 Contents", HOME_FILE), (section.title, section_href)]
    if following:
        label, title, href = following
        closing = f'<p class="next"><b>{_esc(label)}</b><a href="{_esc(href)}">{_esc(title)}</a></p>'
        cells.append(("Next \u203a", href))
    else:
        closing = '<p class="end">That is today\u2019s paper.</p>'
    parts.append(f'<div class="wayout">{closing}{_way_bar(cells)}</div>')
    return "\n".join(parts)


def _section_body(edition: Edition, section: Section, hrefs: list[str]) -> str:
    count = len(section.articles)
    minutes = sum(article.minutes for article in section.articles)
    parts = [
        _kicker(edition.title, HOME_FILE, long_date(edition.date)),
        f'<h1 class="section">{_esc(section.title)}</h1>',
        f'<p class="count">{count} {"story" if count == 1 else "stories"}'
        f" \u00b7 {_minutes(minutes)}</p>",
        '<ol class="heads">',
    ]
    for article, href in zip(section.articles, hrefs):
        deck = f'<span class="deck">{_esc(article.deck)}</span>' if article.deck else ""
        parts.append(f'<li><a href="{_esc(href)}">{_esc(article.title)}</a>{deck}</li>')
    parts.append("</ol>")
    parts.append(_way_bar([("\u2039 Contents", HOME_FILE), ("Start reading \u203a", hrefs[0])]))
    return "\n".join(parts)


def _home_body(edition: Edition, plan: list[tuple[Section, str, list[str]]]) -> str:
    """The page every other page links back to: each section and every
    headline in it, one tap away."""
    minutes = sum(a.minutes for section in edition.sections for a in section.articles)
    stories = edition.story_count
    parts = [
        f'<p class="kicker"><span class="tab">{_esc(edition.title)}</span>'
        f'<span class="where">{_esc(long_date(edition.date))}</span></p>',
        '<h1 class="home">Inside today</h1>',
        f'<p class="homecount">{stories} {"story" if stories == 1 else "stories"}'
        f" \u00b7 {_minutes(minutes)} \u00b7 tap anything to go there</p>",
    ]
    for section, section_href, hrefs in plan:
        parts.append(f'<h2 class="shelf"><a href="{_esc(section_href)}">{_esc(section.title)}</a></h2>')
        parts.append('<ul class="shelf">')
        parts += [f'<li><a href="{_esc(href)}">{_esc(article.title)}</a></li>'
                  for article, href in zip(section.articles, hrefs)]
        parts.append("</ul>")
    return "\n".join(parts)


def write_epub(edition: Edition, out_dir: Path) -> Path:
    book = epub.EpubBook()
    book.set_identifier(edition_urn(edition.date))
    book.set_title(f"{edition.title}: {long_date(edition.date)}")
    book.set_language("en")
    book.add_author(edition.title)
    book.add_metadata("DC", "date", edition.date.isoformat())
    book.add_metadata(
        "DC", "description",
        f"{edition.story_count} stories in {len(edition.sections)} sections.",
    )

    cover_png = io.BytesIO()
    render_front_page(edition, EPUB_COVER, ready_note=False).save(
        cover_png, format="PNG", optimize=True)
    book.set_cover("images/cover.png", cover_png.getvalue(), create_page=False)

    css = epub.EpubItem(uid="style", file_name="style/paper.css",
                        media_type="text/css", content=CSS.encode("utf-8"))
    book.add_item(css)

    def add_page(title: str, file_name: str, body: str) -> epub.EpubHtml:
        page = epub.EpubHtml(title=title, file_name=file_name, lang="en")
        page.content = body
        page.add_item(css)
        book.add_item(page)
        return page

    cover_alt = f"{edition.title}, {long_date(edition.date)}"
    cover = add_page(
        "Cover", "cover.xhtml",
        f'<div class="cover"><img src="images/cover.png" alt="{_esc(cover_alt)}"/></div>',
    )

    # Work out every file name first: each page links to its neighbours.
    plan = []
    for si, section in enumerate(edition.sections, start=1):
        hrefs = [f"s{si}-a{ai}.xhtml" for ai in range(1, len(section.articles) + 1)]
        plan.append((section, f"section-{si}.xhtml", hrefs))

    home = add_page("Inside today", HOME_FILE, _home_body(edition, plan))
    toc: list = [home]
    pages = []
    for pi, (section, section_href, hrefs) in enumerate(plan):
        section_page = add_page(section.title, section_href,
                                _section_body(edition, section, hrefs))
        article_pages = []
        for ai, (article, href) in enumerate(zip(section.articles, hrefs)):
            if ai + 1 < len(hrefs):
                following = ("Next", section.articles[ai + 1].title, hrefs[ai + 1])
            elif pi + 1 < len(plan):
                following = ("Next section", plan[pi + 1][0].title, plan[pi + 1][1])
            else:
                following = None
            article_pages.append(add_page(article.title, href, _article_body(
                section, article, number=ai + 1, section_href=section_href,
                following=following)))
        toc.append((epub.Section(section.title, href=section_page.file_name),
                    tuple(article_pages)))
        pages += [section_page, *article_pages]

    book.toc = tuple(toc)
    # The reader's own contents menu is built from this; the page a person
    # actually sees is `home`, so the generated list stays out of the spine.
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav(title="Contents"))
    book.spine = [cover, home, *pages]

    final = out_dir / f"morning-paper-{edition.date.isoformat()}.epub"
    # Written under a temporary name so the server never hands out half a file.
    tmp = final.with_name(f".{final.name}.tmp")
    epub.write_epub(str(tmp), book, {"play_order": {"enabled": True, "start_from": 1}})
    tmp.replace(final)
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the Morning Paper EPUB and front page PNG from an edition file.")
    parser.add_argument("edition", type=Path, help="edition JSON file")
    parser.add_argument("--out", type=Path, default=REPO / "out",
                        help="output directory (default: out/ next to this script)")
    parser.add_argument("--fetch-weather", action="store_true",
                        help="fill in the forecast from Open-Meteo when the edition has none")
    parser.add_argument("--config-dir", type=Path,
                        help="directory to look in for a front page portrait")
    parser.add_argument("--keep-days", type=int, metavar="N",
                        help="after building, delete papers in the output directory "
                             "dated more than N days ago")
    args = parser.parse_args(argv)
    if args.keep_days is not None and args.keep_days < 0:
        parser.error("--keep-days cannot be negative")

    try:
        edition = load_edition(args.edition)
    except (OSError, EditionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.fetch_weather:
        edition = with_weather(edition)
    edition = with_portrait(edition, args.config_dir)

    args.out.mkdir(parents=True, exist_ok=True)
    print(write_front_page(edition, args.out))
    print(write_epub(edition, args.out))
    if args.keep_days is not None:
        for path in prune_output(args.out, args.keep_days, dt.date.today(), edition.date):
            print(f"pruned {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
