"""The card for a Muse display board: what it says, how it is drawn, and the
address the server hands it out at."""

from __future__ import annotations

import datetime as dt
import io
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from mnn import card
from mnn import server
from mnn import staging

TODAY = dt.date(2026, 10, 5)
BUILT = dt.datetime(2026, 10, 5, 6, 41, 9).astimezone()


def printed(data_dir: Path, date: str = "2026-10-05", failed_section: bool = False) -> None:
    """The records and the paper a print from the Press's own sources leaves."""
    (data_dir / f"morning-paper-{date}.epub").write_bytes(b"paper")
    staging.save_receipt(data_dir, {
        "edition_date": date, "status": "ok", "error": None,
        "built_at": BUILT.isoformat(), "kindle_downloaded_at": None})
    sections = [
        {"title": "Front Page", "status": "ok", "stories": 3, "error": None,
         "headlines": ["Harbour bridge\x1b\nreopens", "Second story"]},
        {"title": "World", "status": "ok", "stories": 5, "error": None,
         "headlines": ["Talks resume in Geneva"]},
    ]
    if failed_section:
        sections.append({"title": "Tides", "status": "failed", "stories": 0,
                         "headlines": [], "error": "timed out after 60s"})
    staging.save_report(data_dir, {
        "mode": "sources", "edition_date": date, "error": None,
        "status": "partial" if failed_section else "ok",
        "ran_at": BUILT.isoformat(), "sections": sections})


def summary(data_dir: Path) -> card.Card:
    return card.summarise(server.press_status(data_dir, headlines=True), TODAY)


# --- What it says -------------------------------------------------------------


def test_a_printed_paper_is_ready_with_its_lead_headline_and_counts(data_dir):
    printed(data_dir)
    assert summary(data_dir) == card.Card(
        "ready", "2026-10-05", "Harbour bridge reopens",
        "8 stories in two sections, printed at 06:41.")


def test_a_section_left_out_is_counted_on_the_card(data_dir):
    printed(data_dir, failed_section=True)
    assert summary(data_dir).footer == (
        "8 stories in two sections, one section left out, printed at 06:41.")


def test_a_staged_paper_is_ready_without_a_headline_or_counts(data_dir, monkeypatch):
    printed(data_dir)
    monkeypatch.setenv("STAGE_URL", "https://stage.example.com")
    assert summary(data_dir) == card.Card(
        "ready", "2026-10-05", "Today's paper is ready to read.", "Printed at 06:41.")


def test_yesterdays_paper_means_todays_is_not_printed_yet(data_dir):
    printed(data_dir, date="2026-10-04")
    assert summary(data_dir) == card.Card(
        "waiting", "2026-10-05", "Today's paper has not been printed yet.",
        "Last paper: Sunday, October 4.")


def test_a_new_press_has_not_printed_anything(data_dir):
    result = summary(data_dir)
    assert (result.state, result.footer) == ("waiting", "No paper has been printed so far.")


def test_a_failed_print_shows_the_reason_made_plain(data_dir):
    printed(data_dir, date="2026-10-04")
    staging.save_receipt(data_dir, {
        "edition_date": "2026-10-05", "status": "failed",
        "error": "no section had\n anything to print", "reported_at": BUILT.isoformat()})
    assert summary(data_dir) == card.Card(
        "failed", "2026-10-05", "no section had anything to print",
        "The previous paper is still being served.")


# --- How it is drawn ----------------------------------------------------------


def cards() -> list[card.Card]:
    return [card.Card("ready", "2026-10-05", "Harbour bridge reopens", "8 stories."),
            card.Card("waiting", "2026-10-05", "Today's paper has not been printed yet.", ""),
            card.Card("failed", "2026-10-05", "no section had anything to print", "Still up.")]


def test_the_card_is_board_sized_and_only_black_and_white():
    for each in cards():
        image = card.render(each)
        assert (image.size, image.mode) == ((800, 480), "1")
        assert set(image.convert("L").tobytes()) == {0, 255}


def test_each_state_is_a_different_picture():
    ready, waiting, failed = (card.render(each).convert("L") for each in cards())
    assert len({ready.tobytes(), waiting.tobytes(), failed.tobytes()}) == 3
    # Told apart by shape too: a point inside the band, clear of the letters
    # and of the frame, is black when ready or failed and white while waiting.
    inside = (40, 80)
    assert [image.getpixel(inside) for image in (ready, waiting, failed)] == [0, 255, 0]
    # The failed card alone has a white frame inside the band.
    assert [image.getpixel((36, 110)) for image in (ready, failed)] == [0, 255]


@pytest.mark.parametrize("size", [(800, 480), (480, 480), (170, 320), (120, 120), (2000, 2000)])
def test_any_board_size_and_any_length_of_text_can_be_drawn(size):
    long = card.Card("ready", "2026-10-05", "Unbreakable" * 40 + " word " * 200, "x " * 400)
    assert card.render(long, size).size == size


def ink(image: Image.Image) -> tuple[int, int, int, int]:
    """The box all the black on a card lies in."""
    return ImageChops.invert(image.convert("L")).getbbox()


@pytest.mark.parametrize("state, body, size", [
    ("failed", "/home/pi/.config/morning-paper/sources.toml: Invalid value", (800, 480)),
    ("ready", "Internationalisation talks resume", (170, 320)),
    ("ready", "Unbreakable" * 40, (800, 480)),
])
def test_a_word_wider_than_the_card_is_carried_over_not_run_off_the_edge(state, body, size):
    short = ink(card.render(card.Card(state, "2026-10-05", "Short", ""), size))
    long = ink(card.render(card.Card(state, "2026-10-05", body, ""), size))
    # The band is the widest thing on the card: nothing is drawn right of it.
    assert long[2] == short[2]
    # The rest of the word is on the lines below, not lost.
    assert long[3] > short[3]


def test_the_jpeg_is_one_the_board_can_decode():
    data = card.jpeg(card.render(cards()[0]))
    assert data[:2] == b"\xff\xd8"  # how the board recognises a JPEG
    assert b"\xff\xc0" in data and b"\xff\xc2" not in data  # baseline, not progressive
    image = Image.open(io.BytesIO(data))
    assert (image.format, image.size, image.mode) == ("JPEG", (800, 480), "RGB")
    assert not image.info.get("progressive")


def test_the_raw_card_is_two_bytes_a_pixel_and_exactly_black_or_white():
    image = card.render(cards()[0])
    data = card.rgb565(image)
    assert len(data) == 800 * 480 * 2
    assert set(data) == {0x00, 0xFF}
    assert data[:2] == b"\xff\xff"  # the top left corner is paper
    x, y = 40, 80  # inside the band
    assert data[(y * 800 + x) * 2:(y * 800 + x) * 2 + 2] == b"\x00\x00"


@pytest.mark.parametrize("text, size", [
    ("800x480", (800, 480)), (" 480X480 ", (480, 480)), ("170x320", (170, 320)),
    ("99x480", None), ("800x2001", None), ("800", None), ("800x480x2", None), ("", None)])
def test_a_size_is_read_or_refused(text, size):
    assert card.parse_size(text) == size


# --- The address --------------------------------------------------------------


@pytest.fixture
def press(data_dir):
    paper = server.PaperServer(("127.0.0.1", 0), data_dir=data_dir,
                               log_path=data_dir / "server.log", refresh_rate=3600,
                               base_url=None)
    threading.Thread(target=paper.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{paper.server_address[1]}"
    paper.shutdown()
    paper.server_close()


def get(url: str) -> tuple[str, bytes]:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.headers["Content-Type"], response.read()


def test_the_card_is_served_as_a_jpeg_at_one_address_whatever_the_state(press, data_dir):
    content_type, before = get(f"{press}/card.jpg")
    assert content_type == "image/jpeg"
    assert Image.open(io.BytesIO(before)).size == (800, 480)

    today = dt.date.today().isoformat()
    printed(data_dir, date=today)
    content_type, after = get(f"{press}/card.jpg")
    assert content_type == "image/jpeg" and after != before
    status = server.press_status(data_dir, headlines=True)
    assert after == card.jpeg(card.render(card.summarise(status, dt.date.today())))
    assert card.summarise(status, dt.date.today()).state == "ready"


def test_the_card_is_served_raw_and_at_another_boards_size(press):
    content_type, data = get(f"{press}/card.rgb565?size=480x480")
    assert content_type == "application/octet-stream"
    assert len(data) == 480 * 480 * 2
    assert Image.open(io.BytesIO(get(f"{press}/card.jpg?size=170x320")[1])).size == (170, 320)


def test_a_size_no_board_has_is_refused(press):
    with pytest.raises(urllib.error.HTTPError) as refused:
        get(f"{press}/card.jpg?size=huge")
    assert refused.value.code == 400
