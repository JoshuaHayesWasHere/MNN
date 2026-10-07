"""sources.toml, finding and loading sources, and keeping one source's
failure away from the others."""

from __future__ import annotations

import datetime as dt
import json
import textwrap
import time
from pathlib import Path

import pytest

from mnn import build_paper
from mnn import press_sources
from mnn.press_sources import ConfigError

DATE = dt.date(2026, 10, 4)

GOOD = '''
def produce(date, config):
    return {"title": config["title"], "articles": [
        {"title": f"{config['title']} story for {date.isoformat()}",
         "body": ["Written by a source of the reader's own."]}]}
'''


def parse(text: str) -> press_sources.Paper:
    return press_sources.parse_config(textwrap.dedent(text), Path("sources.toml"))


def write_config(config_dir: Path, text: str, **modules: str) -> press_sources.Paper:
    (config_dir / "sources.toml").write_text(textwrap.dedent(text), encoding="utf-8")
    for name, body in modules.items():
        (config_dir / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return press_sources.load_config(config_dir)


def run(config_dir: Path, text: str, **modules: str) -> tuple[list[dict], dict[str, dict]]:
    paper = write_config(config_dir, text, **modules)
    sections, outcomes = press_sources.run_sources(paper, DATE, config_dir)
    assert [outcome["title"] for outcome in outcomes] == [s.title for s in paper.sections]
    return sections, {outcome["title"]: outcome for outcome in outcomes}


# --- sources.toml ----------------------------------------------------------


def test_the_default_sources_toml_is_a_paper_of_public_feeds():
    paper = press_sources.load_config(None)
    assert paper.path == press_sources.DEFAULT_CONFIG
    assert len(paper.sections) >= 3
    # The front page is the editor's; with no key it prints from its feeds.
    assert [spec.source for spec in paper.sections][0] == "editor"
    for spec in paper.sections:
        assert spec.source in ("rss", "editor")
        assert spec.config["stories"] > 0
        assert spec.config["feeds"]
        assert all(feed.startswith("https://") for feed in spec.config["feeds"])


def test_sections_keep_their_order_and_their_whole_table():
    paper = parse('''
        title = "The Harbour Gazette"
        note = "Printed at home"

        [weather]
        location = "Lisbon"
        latitude = 38.72
        longitude = -9.14

        [[section]]
        title = " World "
        feeds = ["https://example.com/rss"]
        stories = 3

        [[section]]
        title = "Tides"
        source = "tides"
        timeout = 5
        station = "Cascais"
    ''')
    assert paper.title == "The Harbour Gazette"
    assert paper.note == "Printed at home"
    assert paper.weather == {"location": "Lisbon", "latitude": 38.72, "longitude": -9.14}
    world, tides = paper.sections
    assert (world.title, world.source, world.timeout) == ("World", "rss", 60)
    assert world.config == {"title": "World", "feeds": ["https://example.com/rss"], "stories": 3}
    assert (tides.title, tides.source, tides.timeout) == ("Tides", "tides", 5)
    assert tides.config["station"] == "Cascais"


@pytest.mark.parametrize("text, message", [
    ("title = 'No sections'", "at least one"),
    ("[[section]]\nfeeds = []", "missing 'title'"),
    ("[[section]]\ntitle = 3", "'title' must be a string"),
    ("[[section]]\ntitle = 'A'\nsource = '../escape'", "'source' must be a module name"),
    ("[[section]]\ntitle = 'A'\ntimeout = 0", "'timeout' must be a positive number"),
    ("[[section]]\ntitle = 'A'\ntimeout = true", "'timeout' must be a positive number"),
    ("[[section]]\ntitle = 'A'\ntimeout = inf", "'timeout' must be a positive number"),
    ("[[section]]\ntitle = 'A'\ntimeout = nan", "'timeout' must be a positive number"),
    ("[[section]]\ntitle = 'A'\ntimeout = 3000000", "a day\\) at most"),
    ("[[section]]\ntitle = 'A'\ntimeout = 1" + "0" * 400, "a day\\) at most"),
    ("section = 'nope'", "at least one"),
    ("weather = 'sunny'\n[[section]]\ntitle = 'A'", "'weather' must be"),
    ("[weather]\nlocation = 5\n[[section]]\ntitle = 'A'", "weather: 'location' must be a string"),
    ("[weather]\nlatitude = 2026-10-04\nlongitude = 1\n[[section]]\ntitle = 'A'",
     "weather: 'latitude' must be a number"),
    ("[weather]\nlatitude = 38.72\nlongitude = '-9.14'\n[[section]]\ntitle = 'A'",
     "weather: 'longitude' must be a number"),
    ("[weather]\nlatitude = 38.72\n[[section]]\ntitle = 'A'", "must be given together"),
    ("[[section]\ntitle = 'A'", "sources.toml"),
])
def test_a_bad_sources_toml_says_what_is_wrong(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(text)


def test_the_weather_table_always_fits_in_an_edition():
    # TOML has dates and times, which an edition file cannot hold.
    paper = parse('''
        [weather]
        location = " Lisbon "
        summary = "Clear, high 24"
        since = 2026-10-04
        checked = 06:40:00

        [[section]]
        title = "World"
    ''')
    assert paper.weather == {"location": "Lisbon", "summary": "Clear, high 24"}
    edition = json.loads(json.dumps(press_sources.assemble(paper, DATE, [])))
    assert edition["weather"] == paper.weather


def test_a_readers_sources_toml_replaces_the_default(config_dir):
    assert press_sources.config_path(config_dir) == press_sources.DEFAULT_CONFIG
    paper = write_config(config_dir, '''
        [[section]]
        title = "Mine"
        feeds = ["https://example.com/rss"]
        stories = 1
    ''')
    assert paper.path == config_dir / "sources.toml"
    assert [spec.title for spec in paper.sections] == ["Mine"]


def test_the_config_directory_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(press_sources.CONFIG_DIR_ENV, str(tmp_path))
    assert press_sources.default_config_dir() == tmp_path
    monkeypatch.delenv(press_sources.CONFIG_DIR_ENV)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert press_sources.default_config_dir() == tmp_path / "morning-paper"


# --- Finding and loading ---------------------------------------------------


def test_the_built_in_rss_source_loads(config_dir):
    path = press_sources.find_source("rss", config_dir)
    assert path == press_sources.BUILTIN_DIR / "rss.py"
    assert callable(press_sources.load_source(path).produce)


def test_a_readers_module_is_found_and_wins_over_a_built_in(config_dir):
    (config_dir / "tides.py").write_text(GOOD)
    (config_dir / "rss.py").write_text(GOOD)
    assert press_sources.find_source("tides", config_dir) == config_dir / "tides.py"
    assert press_sources.find_source("rss", config_dir) == config_dir / "rss.py"
    module = press_sources.load_source(config_dir / "tides.py")
    assert module.produce(DATE, {"title": "Tides"})["articles"][0]["title"] == \
        "Tides story for 2026-10-04"


def test_an_edited_module_is_read_again(config_dir):
    path = config_dir / "tides.py"
    path.write_text("def produce(date, config):\n    return 'first'\n")
    assert press_sources.load_source(path).produce(DATE, {}) == "first"
    path.write_text("def produce(date, config):\n    return 'second, and longer'\n")
    assert press_sources.load_source(path).produce(DATE, {}) == "second, and longer"


def test_a_missing_source_says_where_it_looked(config_dir):
    with pytest.raises(press_sources.SourceNotFound, match="tides.py"):
        press_sources.find_source("tides", config_dir)


def test_a_module_without_produce_is_refused(config_dir):
    (config_dir / "empty.py").write_text("x = 1\n")
    with pytest.raises(AttributeError, match="no produce"):
        press_sources.load_source(config_dir / "empty.py")


# --- Failure isolation -----------------------------------------------------


def test_each_failure_costs_only_its_own_section(config_dir, feeds, unreachable):
    started = time.monotonic()
    sections, outcomes = run(
        config_dir,
        f'''
        [[section]]
        title = "Raises"
        source = "raises"

        [[section]]
        title = "Feeds"
        feeds = ["{feeds}/world.rss", "{unreachable}"]
        stories = 2

        [[section]]
        title = "Hangs"
        source = "hangs"
        timeout = 1

        [[section]]
        title = "Nothing"
        source = "nothing"

        [[section]]
        title = "Empty"
        source = "empty"

        [[section]]
        title = "Unreachable"
        feeds = ["{unreachable}"]
        stories = 2

        [[section]]
        title = "Missing"
        source = "not_there"

        [[section]]
        title = "Garbled"
        source = "garbled"

        [[section]]
        title = "Exits"
        source = "exits"

        [[section]]
        title = "Mine"
        source = "good"
        ''',
        raises="def produce(date, config):\n    raise RuntimeError('the well is dry')\n",
        hangs="import time\ndef produce(date, config):\n    time.sleep(600)\n",
        nothing="def produce(date, config):\n    return None\n",
        empty="def produce(date, config):\n    return {'title': 'Empty', 'articles': []}\n",
        garbled="def produce(date, config):\n"
                "    return {'title': 'Garbled', 'articles': [{'title': 'No body'}]}\n",
        exits="import os\ndef produce(date, config):\n    os._exit(3)\n",
        good=GOOD)

    # The hanging source was stopped at its own one second, not waited out.
    assert time.monotonic() - started < 30
    assert [section["title"] for section in sections] == ["Feeds", "Mine"]
    assert len(sections[0]["articles"]) == 2

    assert outcomes["Feeds"]["status"] == "ok"
    assert outcomes["Feeds"]["stories"] == 2
    assert outcomes["Feeds"]["headlines"][0] == "Harbour bridge reopens after & repairs"
    assert unreachable in outcomes["Feeds"]["notes"][0]
    assert outcomes["Mine"]["status"] == "ok"

    reasons = {
        "Raises": "raised RuntimeError: the well is dry",
        "Hangs": "timed out after 1s",
        "Nothing": "returned nothing",
        "Empty": "returned no stories",
        "Unreachable": "no feed could be read",
        "Missing": "no source named 'not_there'",
        "Garbled": "cannot print",
        "Exits": "stopped without a result (exit code 3)",
    }
    for title, reason in reasons.items():
        assert outcomes[title]["status"] == "failed", title
        assert outcomes[title]["stories"] == 0, title
        assert reason in outcomes[title]["error"], title


def test_a_source_that_fails_to_import_fails_alone(config_dir):
    sections, outcomes = run(
        config_dir,
        '''
        [[section]]
        title = "Broken"
        source = "broken"

        [[section]]
        title = "Mine"
        source = "good"
        ''',
        broken="def produce(date, config:\n", good=GOOD)
    assert [section["title"] for section in sections] == ["Mine"]
    assert "SyntaxError" in outcomes["Broken"]["error"]


def test_a_source_can_import_the_modules_beside_it(config_dir):
    sections, _ = run(
        config_dir,
        '''
        [[section]]
        title = "Helped"
        source = "helped"
        ''',
        helper="WORDS = ['From a helper module.']\n",
        helped="import helper\ndef produce(date, config):\n"
               "    return {'title': 'Helped', 'articles': [{'title': 'T', 'body': helper.WORDS}]}\n")
    assert sections[0]["articles"][0]["body"] == ["From a helper module."]


def test_what_a_source_returns_is_cleaned_before_it_is_printed(config_dir):
    sections, outcomes = run(
        config_dir,
        '''
        [[section]]
        title = "From the table"
        source = "loose"
        ''',
        loose="def produce(date, config):\n"
              "    return {'articles': [{'title': '  Spaced   out ', 'body': 'One.\\n\\nTwo.',\n"
              "                          'url': 'https://example.com/a', 'extra': object,\n"
              "                          'quote': ' A pull   quote ', 'quote_by': 'Its speaker',\n"
              "                          'why': 'Why it matters.'}],\n"
              "            'notes': 'one feed was slow'}\n")
    assert sections == [{"title": "From the table", "articles": [
        {"title": "Spaced out", "body": ["One.", "Two."], "url": "https://example.com/a",
         "quote": "A pull quote", "quote_by": "Its speaker", "why": "Why it matters."}]}]
    assert outcomes["From the table"]["notes"] == ["one feed was slow"]


def test_every_part_of_an_article_in_the_edition_format_is_printed(config_dir):
    # edition/sample.json is the article shape the builder reads.
    sample = press_sources.REPO / "edition" / "sample.json"
    articles = [article
                for section in json.loads(sample.read_text(encoding="utf-8"))["sections"]
                for article in section["articles"]]
    sections, _ = run(
        config_dir,
        f'''
        [[section]]
        title = "Sample"
        source = "sample"
        edition = "{sample}"
        ''',
        sample="import json, pathlib\n"
               "def produce(date, config):\n"
               "    text = pathlib.Path(config['edition']).read_text(encoding='utf-8')\n"
               "    return {'title': 'Sample', 'articles': [\n"
               "        article for section in json.loads(text)['sections']\n"
               "        for article in section['articles']]}\n")
    assert build_paper.parse_section(sections[0], "printed") == \
        build_paper.parse_section({"title": "Sample", "articles": articles}, "sample")


def test_characters_an_epub_cannot_hold_are_replaced(config_dir):
    sections, outcomes = run(
        config_dir,
        '''
        [[section]]
        title = "Today"
        source = "terminal"
        ''',
        terminal="def produce(date, config):\n"
                 "    return {'title': 'To' + chr(7) + 'day', 'articles': [{\n"
                 "        'title': 'It is ' + chr(27) + '[7m4' + chr(27) + '[0m October',\n"
                 "        'body': ['Clear' + chr(0) + ' skies.'], 'deck': chr(0xFFFE),\n"
                 "        'source': 'cal' + chr(0xDC80), 'url': 'https://example.com/' + chr(8)}]}\n")
    mark = "\N{REPLACEMENT CHARACTER}"
    assert sections == [{"title": f"To{mark}day", "articles": [{
        "title": f"It is {mark}[7m4{mark}[0m October", "body": [f"Clear{mark} skies."],
        "deck": mark, "source": f"cal{mark}", "url": f"https://example.com/{mark}"}]}]
    assert outcomes["Today"]["status"] == "ok"
    assert outcomes["Today"]["headlines"] == [f"It is {mark}[7m4{mark}[0m October"]


def test_a_story_is_printed_in_one_section_only(config_dir, feeds):
    sections, outcomes = run(
        config_dir,
        f'''
        [[section]]
        title = "Front Page"
        feeds = ["{feeds}/world.rss"]
        stories = 2

        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
        stories = 3

        [[section]]
        title = "Again"
        feeds = ["{feeds}/world.rss"]
        stories = 1
        ''')
    assert [section["title"] for section in sections] == ["Front Page", "World"]
    assert [a["title"] for a in sections[1]["articles"]] == ["Market square gets a new clock"]
    assert outcomes["World"]["stories"] == 1
    assert outcomes["Again"]["status"] == "failed"
    assert "already in an earlier section" in outcomes["Again"]["error"]


def test_assemble_matches_the_edition_format(tmp_path):
    paper = parse('''
        title = "The Harbour Gazette"
        [weather]
        location = "Lisbon"
        latitude = 38.72
        longitude = -9.14
        [[section]]
        title = "World"
    ''')
    sections = [{"title": "World", "articles": [{"title": "T", "body": ["B"], "source": "S"}]}]
    path = tmp_path / "edition.json"
    path.write_text(json.dumps(press_sources.assemble(paper, DATE, sections)))
    edition = build_paper.load_edition(path)
    assert (edition.title, edition.date, edition.weather_location) == \
        ("The Harbour Gazette", DATE, "Lisbon")
    assert edition.weather_coordinates == (38.72, -9.14)
    assert edition.sections[0].articles[0].source == "S"


def test_a_source_with_nothing_to_say_today_is_left_out_without_failing(config_dir):
    sections, outcomes = run(config_dir, '''
        [[section]]
        title = "Mine"
        source = "mine"

        [[section]]
        title = "Quiet"
        source = "quiet"
    ''', mine=GOOD, quiet='''
        def produce(date, config):
            return {"articles": [], "empty": " nothing  happened ", "notes": ["looked twice"]}
    ''')
    assert [section["title"] for section in sections] == ["Mine"]
    quiet = outcomes["Quiet"]
    assert (quiet["status"], quiet["stories"], quiet["error"]) == ("empty", 0, None)
    assert quiet["notes"] == ["nothing happened", "looked twice"]
