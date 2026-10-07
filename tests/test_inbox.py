"""The inbox source: fresh section files are printed once, and nothing left
in the folder can break the paper."""

from __future__ import annotations

import datetime as dt
import json
import os
import zipfile
from pathlib import Path

import pytest

from mnn import press_fetch
from mnn import press_sources
from mnn import server
from mnn import staging
from mnn.sources import inbox

DATE = dt.date(2026, 10, 5)
NOW = dt.datetime(2026, 10, 5, 6, 40, 5)
AT = dt.time(6, 40)


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "inbox"
    directory.mkdir()
    monkeypatch.setenv(inbox.INBOX_DIR_ENV, str(directory))
    return directory


def section(*headlines: str, title: str | None = "From the Inbox") -> dict:
    made = {"articles": [{"title": headline, "body": [f"All about {headline.lower()}."]}
                         for headline in headlines]}
    if title is not None:
        made["title"] = title
    return made


def leave(folder: Path, name: str, content: object) -> Path:
    path = folder / name
    path.write_text(content if isinstance(content, str) else json.dumps(content),
                    encoding="utf-8")
    return path


def produce(date: dt.date = DATE, **config) -> dict:
    return inbox.produce(date, {"title": "Inbox", "source": "inbox", **config})


def headlines(result: dict) -> list[str]:
    return [article["title"] for article in result["articles"]]


# --- What is fresh ---------------------------------------------------------


def test_a_file_for_today_is_printed_and_moved(folder):
    leave(folder, "2026-10-05.json", section("Bins go out tonight"))
    result = produce()
    assert result["title"] == "From the Inbox"
    assert result["articles"] == [{"title": "Bins go out tonight",
                                   "body": ["All about bins go out tonight."]}]
    assert result["notes"] == [] and "empty" not in result
    assert not (folder / "2026-10-05.json").exists()
    assert (folder / "printed" / "2026-10-05" / "2026-10-05.json").is_file()


def test_an_empty_inbox_is_an_ordinary_day(folder):
    result = produce()
    assert result["articles"] == [] and result["notes"] == []
    assert result["empty"] == "nothing fresh in the inbox"


def test_yesterdays_file_prints_and_an_older_one_does_not(folder):
    leave(folder, "2026-10-04-evening.json", section("Written the night before"))
    old = leave(folder, "2026-10-03.json", section("Missed its day"))
    result = produce()
    assert headlines(result) == ["Written the night before"]
    assert old.is_file()
    assert result["notes"] == ["2026-10-03.json: not printed, it is dated more than 1 day(s) "
                               "before this paper"]


def test_only_stale_files_still_count_as_an_empty_inbox(folder):
    leave(folder, "2026-10-01.json", section("Long gone"))
    result = produce()
    assert result["empty"] and len(result["notes"]) == 1


def test_max_age_days_sets_the_window(folder):
    leave(folder, "2026-10-04.json", section("Yesterday"))
    assert produce(max_age_days=0)["articles"] == []
    leave(folder, "2026-10-02.json", section("Three days back"))
    assert headlines(produce(max_age_days=3)) == ["Yesterday", "Three days back"]


def test_a_file_dated_ahead_waits_for_its_day(folder):
    early = leave(folder, "2026-10-06.json", section("Tomorrow's note"))
    result = produce()
    assert result["articles"] == [] and result["notes"] == [] and result["empty"]
    assert early.is_file()
    assert headlines(produce(DATE + dt.timedelta(days=1))) == ["Tomorrow's note"]


def test_several_fresh_files_share_the_section_newest_first(folder):
    leave(folder, "2026-10-04.json", section("Older", title="Yesterday's Title"))
    leave(folder, "2026-10-05-b.json", section("Second", title="B"))
    leave(folder, "2026-10-05-a.json", section("First", title=None))
    result = produce()
    assert headlines(result) == ["First", "Second", "Older"]
    # The first file that names the section names it.
    assert result["title"] == "B"


def test_a_file_without_a_title_takes_the_sections_own(folder):
    leave(folder, "2026-10-05.json", section("Untitled", title=None))
    assert produce(title="Notes to Self")["title"] == "Notes to Self"


def test_the_section_stops_at_its_number_of_stories(folder):
    leave(folder, "2026-10-05-a.json", section("One", "Two", "Three"))
    later = leave(folder, "2026-10-05-b.json", section("Four"))
    result = produce(stories=2)
    assert headlines(result) == ["One", "Two"]
    assert later.is_file()  # not printed, so not filed as printed
    assert result["notes"] == [
        "2026-10-05-a.json: only its first 2 of 3 stories fit the section's 2",
        "1 file(s) left waiting: the section already has its 2 stories"]


# --- Never twice -----------------------------------------------------------


def test_a_printed_file_is_not_printed_in_the_next_paper(folder):
    leave(folder, "2026-10-05.json", section("Once only"))
    assert headlines(produce()) == ["Once only"]
    # Within the window tomorrow, but already printed.
    tomorrow = produce(DATE + dt.timedelta(days=1))
    assert tomorrow["articles"] == [] and tomorrow["empty"]


def test_the_same_days_paper_can_be_printed_again(folder):
    leave(folder, "2026-10-05.json", section("Still here on a rebuild"))
    first = produce()
    again = produce()
    assert again["articles"] == first["articles"] and again["notes"] == []


def test_a_corrected_file_replaces_the_one_already_printed(folder):
    leave(folder, "2026-10-05.json", section("With a typo"))
    produce()
    leave(folder, "2026-10-05.json", section("Corrected"))
    assert headlines(produce()) == ["Corrected"]
    assert headlines(produce()) == ["Corrected"]


def test_a_bad_file_does_not_hide_the_one_of_its_name_already_printed(folder):
    leave(folder, "2026-10-05.json", section("Printed this morning"))
    produce()
    bad = leave(folder, "2026-10-05.json", "{ half written")
    result = produce()
    assert headlines(result) == ["Printed this morning"]
    assert len(result["notes"]) == 1
    assert result["notes"][0].startswith("2026-10-05.json: skipped, not JSON")
    assert bad.is_file()
    assert (folder / "printed" / "2026-10-05" / "2026-10-05.json").is_file()


def test_a_corrected_file_that_cannot_be_moved_leaves_the_printed_one_in_the_paper(
        folder, monkeypatch):
    leave(folder, "2026-10-05.json", section("Printed this morning"))
    produce()
    corrected = leave(folder, "2026-10-05.json", section("Corrected"))

    def refuse(*args):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refuse)
    result = produce()
    assert headlines(result) == ["Printed this morning"]
    assert "could not be moved" in result["notes"][0]
    assert corrected.is_file()


def test_a_printed_file_does_not_count_as_left_waiting(folder):
    leave(folder, "2026-10-05-a.json", section("One", "Two"))
    leave(folder, "2026-10-05-b.json", section("Three"))
    produce()
    leave(folder, "2026-10-05-b.json", section("Three, corrected"))
    result = produce(stories=2)
    assert headlines(result) == ["One", "Two"]
    assert result["notes"] == [
        "1 file(s) left waiting: the section already has its 2 stories"]


def test_printed_files_are_deleted_after_keep_days(folder):
    leave(folder, "2026-10-05.json", section("Kept for a while"))
    produce()
    filed = folder / "printed" / "2026-10-05"
    (folder / "printed" / "notes").mkdir()  # not one of the source's own
    produce(DATE + dt.timedelta(days=14))
    assert filed.is_dir()
    produce(DATE + dt.timedelta(days=15))
    assert not filed.exists() and (folder / "printed" / "notes").is_dir()


def test_a_file_that_cannot_be_moved_is_not_printed(folder, monkeypatch):
    waiting = leave(folder, "2026-10-05.json", section("Stuck"))

    def refuse(*args):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refuse)
    result = produce()
    assert result["articles"] == [] and "empty" not in result
    assert "could not be moved" in result["notes"][0]
    assert waiting.is_file()


# --- Bad files -------------------------------------------------------------


@pytest.mark.parametrize("content, reason", [
    ("{ not json", "not JSON"),
    ("[1, 2]", "top level must be an object"),
    ('{"title": "Empty"}', "'articles' must be a list"),
    ('{"articles": []}', "it has no articles"),
    ('{"articles": [{"title": "No body"}]}', "'body' must be"),
    ('{"articles": [{"title": "Link", "body": "x", "url": "javascript:alert(1)"}]}',
     "'url' must be an http(s) link"),
    ("[" * 100_000, "not JSON"),
    ('{"articles": [{"title": "Big", "body": "' + "x" * inbox.MAX_FILE_BYTES + '"}]}',
     "larger than 256 KiB"),
])
def test_a_bad_file_is_skipped_and_the_good_one_beside_it_prints(folder, capsys, content,
                                                                  reason):
    bad = leave(folder, "2026-10-05-bad.json", content)
    leave(folder, "2026-10-05-good.json", section("Still printed"))
    result = produce()
    assert headlines(result) == ["Still printed"]
    assert len(result["notes"]) == 1
    assert result["notes"][0].startswith("2026-10-05-bad.json: skipped, ")
    assert reason in result["notes"][0]
    assert bad.is_file()  # left where it was, for its writer to fix
    assert "inbox: 2026-10-05-bad.json: skipped" in capsys.readouterr().err


def test_bytes_that_are_not_text_are_skipped(folder):
    (folder / "2026-10-05.json").write_bytes(b"\xff\xfe\x00{")
    result = produce()
    assert result["articles"] == [] and "not JSON" in result["notes"][0]


def test_only_bad_files_is_a_failure_not_an_empty_day(folder):
    leave(folder, "2026-10-05.json", "{ not json")
    result = produce()
    assert result["articles"] == [] and "empty" not in result


def test_a_link_is_not_followed(folder, tmp_path):
    secret = leave(tmp_path, "elsewhere.json", section("Not in the inbox"))
    (folder / "2026-10-05.json").symlink_to(secret)
    result = produce()
    assert result["articles"] == []
    assert result["notes"] == ["2026-10-05.json: skipped, not a regular file"]


def test_names_without_a_date_are_reported_and_other_files_ignored(folder):
    leave(folder, "notes.json", section("No date"))
    leave(folder, "2026-13-45.json", section("No such day"))
    leave(folder, "README.md", "Not a section file.")
    leave(folder, ".2026-10-05.json", section("A writer's temporary file"))
    leave(folder, "2026-10-05.json.tmp", section("Half written"))
    result = produce()
    assert result["articles"] == [] and "empty" not in result
    assert [note.split(":")[0] for note in result["notes"]] == ["2026-13-45.json", "notes.json"]


@pytest.mark.parametrize("name", ["2026-10-05 muse.json", "2026-10-05-café.json",
                                  "2026-10-05muse.json"])
def test_a_dated_name_with_a_label_that_will_not_do_is_told_what_will(folder, name):
    odd = leave(folder, name, section("Oddly named"))
    result = produce()
    assert result["articles"] == [] and "empty" not in result
    assert result["notes"] == [
        f"{name}: skipped, its name must be a date, as in 2026-10-05.json, optionally "
        "followed by a label, as in 2026-10-05-label.json (a label may only use a-z, A-Z, "
        "0-9, '.', '_' and '-')"]
    assert odd.is_file()


def test_only_the_keys_an_article_has_are_passed_on(folder):
    leave(folder, "2026-10-05.json", {"articles": [{
        "title": "Full", "deck": "A deck", "source": "Muse", "url": "https://example.com/a",
        "body": "One.\n\nTwo.", "quote": "Q", "quote_by": "Someone", "why": "Because",
        "grid": ["123456789"] * 9, "extra": {"ignored": True}}]})
    [article] = produce()["articles"]
    assert set(article) == set(inbox.ARTICLE_KEYS)
    assert article["body"] == "One.\n\nTwo."


# --- Configuration ---------------------------------------------------------


def test_where_the_inbox_is(tmp_path, monkeypatch):
    monkeypatch.delenv(inbox.INBOX_DIR_ENV, raising=False)
    monkeypatch.setenv(press_sources.CONFIG_DIR_ENV, str(tmp_path / "config"))
    assert inbox.inbox_dir({}) == tmp_path / "config" / "inbox"
    monkeypatch.setenv(inbox.INBOX_DIR_ENV, str(tmp_path / "env"))
    assert inbox.inbox_dir({}) == tmp_path / "env"
    assert inbox.inbox_dir({"folder": str(tmp_path / "own")}) == tmp_path / "own"


def test_a_missing_folder_is_a_failure_that_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(inbox.INBOX_DIR_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(FileNotFoundError, match="does not exist"):
        produce()


@pytest.mark.parametrize("config", [{"max_age_days": 8}, {"max_age_days": -1},
                                    {"max_age_days": 1.5}, {"stories": 0}, {"keep_days": True},
                                    {"folder": ""}])
def test_settings_that_make_no_sense_are_refused(folder, config):
    with pytest.raises(ValueError):
        produce(**config)


# --- In the paper ----------------------------------------------------------

PAPER = '''
[[section]]
title = "Front Page"
source = "local"

[[section]]
title = "Inbox"
source = "inbox"
'''
LOCAL = '''
def produce(date, config):
    return {"articles": [{"title": "The harbour reopens", "body": ["It did."]}]}
'''


def write_paper(data_dir: Path, config_dir: Path, now: dt.datetime = NOW,
                force: bool = False) -> int:
    (config_dir / "sources.toml").write_text(PAPER, encoding="utf-8")
    (config_dir / "local.py").write_text(LOCAL, encoding="utf-8")
    return press_fetch.write_paper(data_dir, config_dir, at=AT, fetch_weather=False,
                                   force=force, keep_days=14, now=now)


def pages(data_dir: Path, date: str) -> str:
    with zipfile.ZipFile(data_dir / f"morning-paper-{date}.epub") as book:
        return "".join(book.read(name).decode("utf-8") for name in book.namelist()
                       if name.endswith(".xhtml"))


def test_the_paper_prints_without_the_section_when_nothing_is_waiting(folder, data_dir,
                                                                      config_dir, capsys):
    assert write_paper(data_dir, config_dir) == 0
    assert "warning" not in capsys.readouterr().err
    report = staging.load_report(data_dir)
    assert report["status"] == "ok"
    outcome = report["sections"][1]
    assert (outcome["status"], outcome["stories"], outcome["error"]) == ("empty", 0, None)
    assert outcome["notes"] == ["nothing fresh in the inbox"]
    assert server.press_status(data_dir)["failed_sections"] == []
    text = pages(data_dir, "2026-10-05")
    assert "The harbour reopens" in text and "Inbox" not in text


def test_the_paper_prints_a_waiting_file_once(folder, data_dir, config_dir):
    leave(folder, "2026-10-05-muse.json", section("Your day, by your Muse", title="From Muse"))
    assert write_paper(data_dir, config_dir) == 0
    report = staging.load_report(data_dir)
    assert report["status"] == "ok"
    assert report["sections"][1]["headlines"] == ["Your day, by your Muse"]
    text = pages(data_dir, "2026-10-05")
    assert "From Muse" in text and "Your day, by your Muse" in text

    # Printed again the same day, it is still there; the next day it is not.
    assert write_paper(data_dir, config_dir, force=True) == 0
    assert "Your day, by your Muse" in pages(data_dir, "2026-10-05")
    assert write_paper(data_dir, config_dir, now=NOW + dt.timedelta(days=1)) == 0
    assert "Your day, by your Muse" not in pages(data_dir, "2026-10-06")


def test_a_bad_file_never_breaks_the_print(folder, data_dir, config_dir, capsys):
    leave(folder, "2026-10-05.json", '{"articles": "all of them"}')
    assert write_paper(data_dir, config_dir) == 0
    report = staging.load_report(data_dir)
    assert report["status"] == "partial"
    assert report["sections"][0]["status"] == "ok"
    assert report["sections"][1]["status"] == "failed"
    assert "2026-10-05.json: skipped" in report["sections"][1]["error"]
    assert "section 'Inbox' left out" in capsys.readouterr().err
    assert "The harbour reopens" in pages(data_dir, "2026-10-05")


def test_a_paper_of_nothing_but_an_empty_inbox_prints_nothing(folder, data_dir, config_dir):
    (config_dir / "sources.toml").write_text(
        '[[section]]\ntitle = "Inbox"\nsource = "inbox"\n', encoding="utf-8")
    assert press_fetch.write_paper(data_dir, config_dir, at=AT, fetch_weather=False,
                                   force=False, keep_days=14, now=NOW) == 1
    report = staging.load_report(data_dir)
    assert report["error"] == "no section had anything to print today"
    assert server.press_status(data_dir)["failed_sections"] == []
