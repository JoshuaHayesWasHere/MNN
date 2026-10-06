"""The Press printing from its own sources, staging still taking precedence,
and the outcome being readable by status and health."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import sys
import textwrap
import threading
import urllib.request
import zipfile
from pathlib import Path

import pytest

from mnn import press_fetch
from mnn import press_sources
from mnn import server
from mnn import staging

NOW = dt.datetime(2026, 10, 4, 6, 40, 5)
AT = dt.time(6, 40)
RAISES = "def produce(date, config):\n    raise RuntimeError('the well is dry')\n"


def configure(config_dir: Path, text: str, **modules: str) -> None:
    (config_dir / "sources.toml").write_text(textwrap.dedent(text), encoding="utf-8")
    for name, body in modules.items():
        (config_dir / f"{name}.py").write_text(body, encoding="utf-8")


def write_paper(data_dir: Path, config_dir: Path, now: dt.datetime = NOW,
                force: bool = False) -> int:
    return press_fetch.write_paper(data_dir, config_dir, at=AT, fetch_weather=False,
                                   force=force, keep_days=14, now=now)


def served(data_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in data_dir.iterdir() if path.is_file()}


def words(pages: str) -> str:
    """The pages without the builder's markup. A story's first letter is set
    apart as a drop cap, so its opening words are not one run of text."""
    return re.sub(r"<[^>]+>", "", pages)


def press_commands():
    from mnn import press_commands as module
    return module


@pytest.fixture
def two_sections(config_dir, feeds):
    configure(config_dir, f'''
        title = "The Fixture Times"

        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
        stories = 3

        [[section]]
        title = "Science"
        feeds = ["{feeds}/science.atom"]
        stories = 3
    ''')


def test_the_press_prints_its_own_paper(data_dir, config_dir, two_sections, capsys):
    assert write_paper(data_dir, config_dir) == 0
    assert "printed 2026-10-04 from 2 of 2 sections" in capsys.readouterr().out

    files = served(data_dir)
    assert set(files) == {"morning-paper-2026-10-04.epub", "frontpage-2026-10-04.png"}
    assert server.check_edition({"epub": ("morning-paper-2026-10-04.epub",
                                          files["morning-paper-2026-10-04.epub"]),
                                 "frontpage": ("frontpage-2026-10-04.png",
                                               files["frontpage-2026-10-04.png"])}) == "2026-10-04"

    with zipfile.ZipFile(data_dir / "morning-paper-2026-10-04.epub") as epub:
        pages = "\n".join(epub.read(name).decode("utf-8") for name in epub.namelist()
                          if name.endswith(".xhtml"))
    assert "Harbour bridge reopens after &amp; repairs" in pages
    assert "Moss grows faster in the dark" in pages
    assert "https://world.example/bridge" in pages
    # Feed summaries only, as plain text: none of the feed's markup is left.
    assert "The bridge reopened on Monday." in words(pages)
    assert "THE FULL ARTICLE TEXT" not in pages
    assert "alert(" not in pages and "<script" not in pages and "<b>bridge" not in pages

    report = staging.load_report(data_dir)
    assert (report["mode"], report["status"], report["error"]) == ("sources", "ok", None)
    assert report["edition_date"] == "2026-10-04"
    # Science carries the bridge story too; it is printed once, under World.
    assert [(s["title"], s["status"], s["stories"]) for s in report["sections"]] == \
        [("World", "ok", 3), ("Science", "ok", 2)]
    receipt = staging.load_receipt(data_dir)
    assert (receipt["status"], receipt["edition_date"]) == ("ok", "2026-10-04")
    assert "unsent" not in receipt
    # No work directory or half-written file is left beside the papers.
    assert [p.name for p in (data_dir / "press").iterdir() if p.name.startswith("work-")] == []


def test_a_failed_section_is_left_out_and_recorded(data_dir, config_dir, feeds, capsys):
    configure(config_dir, f'''
        [[section]]
        title = "Dry Well"
        source = "raises"

        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
        stories = 2
    ''', raises=RAISES)
    assert write_paper(data_dir, config_dir) == 0
    assert "from 1 of 2 sections" in capsys.readouterr().out
    assert (data_dir / "morning-paper-2026-10-04.epub").is_file()

    report = staging.load_report(data_dir)
    assert report["status"] == "partial"
    dry, world = report["sections"]
    assert (dry["status"], dry["error"]) == ("failed", "raised RuntimeError: the well is dry")
    assert world["status"] == "ok"

    status = server.press_status(data_dir)
    assert status["mode"] == "sources"
    assert status["edition_date"] == "2026-10-04"
    assert status["failed_sections"] == [
        {"title": "Dry Well", "error": "raised RuntimeError: the well is dry"}]


def test_text_an_epub_cannot_hold_does_not_stop_the_paper(data_dir, config_dir, feeds):
    # Terminal output with an escape sequence left in the headline.
    configure(config_dir, f'''
        [[section]]
        title = "Today"
        source = "terminal"

        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
        stories = 2
    ''', terminal="def produce(date, config):\n"
                  "    return {'title': 'To' + chr(7) + 'day', 'articles': [\n"
                  "        {'title': 'It is ' + chr(27) + '[7m4 October', 'body': ['Clear skies.']}]}\n")
    assert write_paper(data_dir, config_dir) == 0

    report = staging.load_report(data_dir)
    assert (report["status"], report["error"]) == ("ok", None)
    assert [(s["title"], s["status"]) for s in report["sections"]] == \
        [("Today", "ok"), ("World", "ok")]
    with zipfile.ZipFile(data_dir / "morning-paper-2026-10-04.epub") as epub:
        pages = "\n".join(epub.read(name).decode("utf-8") for name in epub.namelist()
                          if name.endswith(".xhtml"))
    assert "[7m4 October" in pages and "Clear skies." in words(pages)
    assert "Harbour bridge reopens" in pages
    assert chr(27) not in pages and chr(7) not in pages


def test_a_feeds_credentials_stay_out_of_the_report_status_and_log(data_dir, config_dir, feeds,
                                                                   capsys):
    configure(config_dir, f'''
        [[section]]
        title = "Private"
        feeds = ["{feeds}/missing.rss?token=s3cret", "http://reader:hunter2@localhost/feed.rss"]
        stories = 2

        [[section]]
        title = "Patchy"
        feeds = ["{feeds}/missing.rss?key=s3cret", "{feeds}/world.rss"]
        stories = 2
    ''')
    assert write_paper(data_dir, config_dir) == 0

    private, patchy = staging.load_report(data_dir)["sections"]
    assert private["error"] == (
        f"raised FeedError: no feed could be read: feed 1, {feeds}/missing.rss: HTTP 404; "
        "feed 2, http://localhost/feed.rss: not an address that can be fetched")
    assert patchy["notes"] == [f"feed 1, {feeds}/missing.rss: HTTP 404"]

    # report.json is what the status endpoint and the morning message read.
    written = [(data_dir / "press" / "report.json").read_text(encoding="utf-8"),
               json.dumps(server.press_status(data_dir)), "".join(capsys.readouterr())]
    for text in written:
        assert f"{feeds}/missing.rss" in text
        assert not any(secret in text for secret in ("s3cret", "hunter2", "reader:"))


def test_when_every_section_fails_yesterdays_paper_stays(data_dir, config_dir, unreachable,
                                                         two_sections, capsys):
    yesterday = NOW - dt.timedelta(days=1)
    assert write_paper(data_dir, config_dir, now=yesterday) == 0
    before = served(data_dir)
    assert set(before) == {"morning-paper-2026-10-03.epub", "frontpage-2026-10-03.png"}

    configure(config_dir, f'''
        [[section]]
        title = "Dry Well"
        source = "raises"

        [[section]]
        title = "Unreachable"
        feeds = ["{unreachable}"]
        stories = 2
    ''', raises=RAISES)
    assert write_paper(data_dir, config_dir) == 1
    assert served(data_dir) == before
    assert "still serving the previous edition" in capsys.readouterr().err

    report = staging.load_report(data_dir)
    assert report["status"] == "failed"
    assert "every section failed" in report["error"]
    assert [s["status"] for s in report["sections"]] == ["failed", "failed"]
    assert "the well is dry" in report["sections"][0]["error"]
    assert "no feed could be read" in report["sections"][1]["error"]
    receipt = staging.load_receipt(data_dir)
    assert (receipt["status"], receipt["build_ok"]) == ("failed", False)

    status = server.press_status(data_dir)
    assert status["edition_date"] == "2026-10-03"
    assert status["last_run"]["status"] == "failed"
    assert len(status["failed_sections"]) == 2

    # Nothing was printed, so the next check tries again and succeeds.
    (config_dir / "sources.toml").unlink()
    (config_dir / "sources.toml").write_text(
        '[[section]]\ntitle = "Mine"\nsource = "mine"\n')
    (config_dir / "mine.py").write_text(
        "def produce(date, config):\n"
        "    return {'title': 'Mine', 'articles': [{'title': 'Back', 'body': ['Again.']}]}\n")
    assert write_paper(data_dir, config_dir, now=NOW + dt.timedelta(minutes=10)) == 0
    assert (data_dir / "morning-paper-2026-10-04.epub").is_file()


def test_a_broken_sources_toml_is_recorded_and_prints_nothing(data_dir, config_dir, capsys):
    configure(config_dir, "[[section]]\nfeeds = []\n")
    assert write_paper(data_dir, config_dir) == 1
    assert served(data_dir) == {}
    assert "missing 'title'" in staging.load_report(data_dir)["error"]


def test_one_paper_per_edition_time(data_dir, config_dir, two_sections, monkeypatch):
    runs = []
    real = press_sources.run_sources
    monkeypatch.setattr(press_sources, "run_sources",
                        lambda *args: runs.append(args[1]) or real(*args))
    # A new install prints at once, even in the small hours.
    assert write_paper(data_dir, config_dir, now=dt.datetime(2026, 10, 4, 2, 0)) == 0
    # The edition time arrives: the morning's paper replaces it.
    assert write_paper(data_dir, config_dir, now=NOW) == 0
    # Retries later that morning, and a restart overnight, print nothing.
    assert write_paper(data_dir, config_dir, now=NOW + dt.timedelta(minutes=10)) == 0
    assert write_paper(data_dir, config_dir, now=dt.datetime(2026, 10, 5, 2, 0)) == 0
    assert len(runs) == 2
    # Tomorrow's edition time prints tomorrow's paper; --force prints any time.
    assert write_paper(data_dir, config_dir, now=NOW + dt.timedelta(days=1)) == 0
    assert write_paper(data_dir, config_dir, now=NOW + dt.timedelta(days=1), force=True) == 0
    assert runs == [dt.date(2026, 10, 4), dt.date(2026, 10, 4), dt.date(2026, 10, 5),
                    dt.date(2026, 10, 5)]
    assert (data_dir / "morning-paper-2026-10-05.epub").is_file()


def test_last_due():
    assert press_fetch.last_due(dt.datetime(2026, 10, 4, 6, 40), AT) == dt.datetime(2026, 10, 4, 6, 40)
    assert press_fetch.last_due(dt.datetime(2026, 10, 4, 6, 39), AT) == dt.datetime(2026, 10, 3, 6, 40)


@pytest.mark.parametrize("at, now, expected", [
    (AT, "2026-10-04T06:00:00", "2026-10-04T06:40:00"),
    (AT, "2026-10-04T06:40:05", "2026-10-04T06:50:00"),
    (AT, "2026-10-04T08:55:00", "2026-10-04T09:00:00"),
    (AT, "2026-10-04T09:00:05", "2026-10-05T06:40:00"),
    # An edition time after the retry window is still a check of its own day.
    (dt.time(9, 30), "2026-10-04T00:05:00", "2026-10-04T09:30:00"),
    (dt.time(9, 30), "2026-10-04T08:00:00", "2026-10-04T09:30:00"),
    (dt.time(9, 30), "2026-10-04T09:30:20", "2026-10-05T09:30:00"),
])
def test_next_check(at, now, expected):
    due = press_fetch.next_check(dt.datetime.fromisoformat(now), at, dt.time(9, 0),
                                 dt.timedelta(minutes=10))
    assert due == dt.datetime.fromisoformat(expected)


# --- Staging still takes precedence ----------------------------------------


@pytest.fixture
def calls(monkeypatch):
    """Record which way main() went, without printing anything."""
    seen = []
    monkeypatch.setattr(press_fetch, "fetch",
                        lambda stage, *a, **k: seen.append(("staging", stage.url)) or 0)
    monkeypatch.setattr(press_fetch, "write_paper",
                        lambda *a, **k: seen.append(("sources", a[1])) or 0)
    return seen


def test_without_stage_url_the_press_runs_its_sources(calls, data_dir, config_dir):
    assert press_fetch.main(["--data-dir", str(data_dir), "--config-dir", str(config_dir)]) == 0
    assert calls == [("sources", config_dir)]


def test_an_empty_stage_url_counts_as_unset(calls, data_dir, monkeypatch):
    # docker compose passes STAGE_URL through as an empty string.
    monkeypatch.setenv("STAGE_URL", "")
    monkeypatch.setenv("STAGE_TOKEN", "")
    assert press_fetch.main(["--data-dir", str(data_dir)]) == 0
    assert calls[0][0] == "sources"


def test_with_stage_url_the_press_fetches_from_staging(calls, data_dir, monkeypatch):
    monkeypatch.setenv("STAGE_URL", "https://stage.example.com/")
    monkeypatch.setenv("STAGE_TOKEN", "secret")
    assert press_fetch.main(["--data-dir", str(data_dir)]) == 0
    assert calls == [("staging", "https://stage.example.com")]


def test_stage_url_without_a_token_does_not_fall_back_to_sources(calls, data_dir, monkeypatch,
                                                                 capsys):
    monkeypatch.setenv("STAGE_URL", "https://stage.example.com")
    assert press_fetch.main(["--data-dir", str(data_dir)]) == 1
    assert calls == []
    assert "STAGE_TOKEN" in capsys.readouterr().err


def test_with_staging_set_no_source_is_run(data_dir, config_dir, two_sections, unreachable,
                                           monkeypatch):
    monkeypatch.setenv("STAGE_URL", unreachable.rsplit("/", 1)[0])
    monkeypatch.setenv("STAGE_TOKEN", "secret")
    monkeypatch.setattr(press_sources, "run_sources",
                        lambda *args: pytest.fail("sources ran although staging is set"))
    # Staging is down, and the Press says so rather than writing its own paper.
    assert press_fetch.main(["--data-dir", str(data_dir), "--config-dir", str(config_dir)]) == 1
    assert served(data_dir) == {}
    # The only record is that staging could not be read: no section was tried.
    report = staging.load_report(data_dir)
    assert (report["mode"], report["status"], report["sections"]) == ("staging", "failed", [])


# --- Receipts, status and health -------------------------------------------


def test_without_staging_a_receipt_is_never_owed(data_dir):
    staging.save_receipt(data_dir, {"edition_date": "2026-10-04", "status": "ok"})
    assert staging.send_receipt(data_dir) is True
    assert "unsent" not in staging.load_receipt(data_dir)


def test_with_staging_an_undelivered_receipt_is_still_marked(data_dir, unreachable, monkeypatch):
    monkeypatch.setenv("STAGE_URL", unreachable.rsplit("/", 1)[0])
    monkeypatch.setenv("STAGE_TOKEN", "secret")
    staging.save_receipt(data_dir, {"edition_date": "2026-10-04", "status": "ok"})
    assert staging.send_receipt(data_dir) is False
    assert staging.load_receipt(data_dir)["unsent"]


def test_status_and_health_commands_report_failed_sections(data_dir, config_dir, feeds,
                                                           monkeypatch):
    configure(config_dir, f'''
        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
        stories = 2

        [[section]]
        title = "Dry Well"
        source = "raises"
    ''', raises=RAISES)
    assert write_paper(data_dir, config_dir) == 0

    commands = press_commands()
    monkeypatch.setenv(server.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setenv(commands.SERVER_URL_ENV, "http://127.0.0.1:9")
    status = commands.paper_status()
    assert status["mode"] == "sources"
    assert status["printed_date"] == "2026-10-04"
    assert (status["staged_date"], status["staging_error"]) == (None, None)
    assert status["last_run"]["status"] == "partial"
    assert [s["title"] for s in status["sections"]] == ["World", "Dry Well"]
    assert status["failed_sections"] == [
        {"title": "Dry Well", "error": "raised RuntimeError: the well is dry"}]

    health = commands.paper_health()
    assert health["mode"] == "sources"
    assert any("'Dry Well' was left out of the last paper: raised RuntimeError" in problem
               for problem in health["problems"])
    assert not any("staging" in problem for problem in health["problems"])


def test_the_server_reports_the_last_run(data_dir, config_dir, feeds):
    configure(config_dir, f'''
        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
        stories = 2

        [[section]]
        title = "Dry Well"
        source = "raises"
    ''', raises=RAISES)
    assert write_paper(data_dir, config_dir) == 0

    with server.PaperServer(("127.0.0.1", 0), data_dir=data_dir,
                            log_path=data_dir / "server.log", refresh_rate=3600,
                            base_url=None) as paper_server:
        threading.Thread(target=paper_server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{paper_server.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{base}/api/status", timeout=5) as response:
                status = json.load(response)
            # The Kindle reporting its download must not leave a receipt
            # waiting on a staging server that does not exist.
            request = urllib.request.Request(
                f"{base}/api/log", method="POST",
                data=b"kindle: downloaded morning-paper-2026-10-04.epub to /mnt/us/home\n")
            urllib.request.urlopen(request, timeout=5).close()
        finally:
            paper_server.shutdown()
    assert status["mode"] == "sources"
    assert status["last_run"]["status"] == "partial"
    assert status["failed_sections"][0]["title"] == "Dry Well"
    receipt = staging.load_receipt(data_dir)
    assert receipt["kindle_downloaded_at"]
    assert "unsent" not in receipt


def test_in_staging_mode_status_leaves_the_sources_report_out(data_dir, monkeypatch):
    staging.save_report(data_dir, {"mode": "sources", "status": "partial", "sections": [
        {"title": "Old", "status": "failed", "error": "from before staging was set"}]})
    monkeypatch.setenv("STAGE_URL", "https://stage.example.com")
    status = server.press_status(data_dir)
    assert status["mode"] == "staging"
    assert "sections" not in status and "failed_sections" not in status
