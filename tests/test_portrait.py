"""The optional front page portrait: which picture runs, how it is converted
for e-ink, and what the front page looks like with and without one."""

from __future__ import annotations

import datetime as dt
import os
import struct
import textwrap
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from mnn import build_paper
from mnn import press_fetch
from mnn import stage
from mnn import stage_put
from mnn import staging

SAMPLE = Path(__file__).parent.parent / "edition" / "sample.json"
CANVAS = build_paper.WAKE_SCREEN
RIGHT_EDGE = CANVAS.width - CANVAS.margin_x - 1
COVER = build_paper.EPUB_COVER
COVER_EDGE = COVER.width - COVER.margin_x - 1
# The least a portrait's border measures; no other line on the page is as tall.
MIN_SIDE = build_paper.PORTRAIT_MIN_SIDE
STAGE_TOKEN = "test-token"


def photo(path: Path, shade: int = 128, size: tuple[int, int] = (900, 1200)) -> Path:
    """An ordinary picture: mid-tone, colour, not square."""
    Image.new("RGB", size, (shade, shade, shade)).save(path)
    return path


def shades(img: Image.Image) -> set[int]:
    return {shade for _, shade in img.getcolors()}


def front_page(edition: build_paper.Edition) -> Image.Image:
    return build_paper.render_front_page(edition, CANVAS, ready_note=True)


def cover_page(edition: build_paper.Edition) -> Image.Image:
    return build_paper.render_front_page(edition, COVER, ready_note=False)


def black_run(img: Image.Image, x: int) -> tuple[int, int]:
    """The tallest unbroken black line down one column of the page, as its
    top and its height."""
    top = best = run = 0
    for y in range(img.height):
        run = run + 1 if img.getpixel((x, y)) == build_paper.BLACK else 0
        if run > best:
            top, best = y - run + 1, run
    return top, best


def longest_black_run(img: Image.Image, x: int) -> int:
    return black_run(img, x)[1]


def crowded(edition: build_paper.Edition) -> build_paper.Edition:
    """The same paper with as little room under the lead story as a front
    page can have: the masthead at full size, a weather line, a lead headline
    and summary of three lines each, and a note in the footer."""
    front = edition.sections[0]
    lead = replace(front.articles[0], title="A headline that runs on and on " * 8,
                   deck="A summary that says a good deal more than it needs to " * 8)
    return replace(edition, title="News", weather_location="Port Avery",
                   weather_summary="Showers", note="One source could not be reached.",
                   sections=(replace(front, articles=(lead, *front.articles[1:])),
                             *edition.sections[1:]))


def paper_config(config_dir: Path, feeds: str) -> None:
    (config_dir / "sources.toml").write_text(textwrap.dedent(f'''
        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
    '''), encoding="utf-8")


def print_paper(config_dir: Path, data_dir: Path) -> int:
    now = dt.datetime(2026, 10, 4, 6, 40, 5)
    return press_fetch.write_paper(data_dir, config_dir, at=dt.time(6, 40), fetch_weather=False,
                                   force=False, keep_days=14, now=now)


@pytest.fixture(autouse=True)
def built_in_face(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every page here is drawn in the face that ships with Pillow, as on a
    machine with no fonts. How many headlines fit on a page depends on the
    face, and the serif a machine has is not the same everywhere."""
    monkeypatch.setattr(build_paper, "_font_file", lambda pattern: None)


@pytest.fixture
def stage_url(tmp_path: Path) -> str:
    """A staging server on 127.0.0.1 with nothing staged yet."""
    directory = tmp_path / "staged"
    (directory / "stage").mkdir(parents=True)
    with stage.StageServer(("127.0.0.1", 0), directory=directory, token=STAGE_TOKEN) as running:
        threading.Thread(target=running.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{running.server_address[1]}"
        running.shutdown()


@pytest.fixture
def kickers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every letter-spaced label drawn on the page, section kickers included."""
    drawn: list[str] = []
    tracked = build_paper._tracked

    def spy(draw, xy, text, *args, **kwargs):
        drawn.append(text)
        return tracked(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(build_paper, "_tracked", spy)
    return drawn


def test_without_a_portrait_the_front_page_has_three_rows_of_headlines(config_dir, kickers):
    edition = build_paper.with_portrait(build_paper.load_edition(SAMPLE), config_dir)
    assert edition.portrait is None

    page = front_page(edition)
    # The third row carries the fifth and sixth secondary headlines.
    assert {"SCIENCE", "CULTURE"} <= set(kickers)
    assert longest_black_run(page, RIGHT_EDGE) < MIN_SIDE


def test_with_a_portrait_it_takes_the_right_of_the_page(config_dir, kickers):
    photo(config_dir / "portrait.jpg")
    edition = build_paper.with_portrait(build_paper.load_edition(SAMPLE), config_dir)
    assert edition.portrait is not None

    page = front_page(edition)
    assert page.size == (CANVAS.width, CANVAS.height) and page.mode == "L"
    # The portrait's border runs down the right margin, and the headlines
    # beside it are one column, so the page holds fewer of them.
    assert longest_black_run(page, RIGHT_EDGE) >= MIN_SIDE
    assert "LOCAL" in kickers and "CULTURE" not in kickers
    # The cover inside the EPUB is drawn the same way.
    assert longest_black_run(cover_page(edition), COVER_EDGE) >= MIN_SIDE


@pytest.mark.parametrize("draw_page, edge", [(front_page, RIGHT_EDGE), (cover_page, COVER_EDGE)])
def test_the_portrait_fits_the_room_a_long_lead_story_leaves(config_dir, draw_page, edge):
    photo(config_dir / "portrait.png")
    edition = build_paper.with_portrait(build_paper.load_edition(SAMPLE), config_dir)

    usual = longest_black_run(draw_page(edition), edge)
    tight = longest_black_run(draw_page(crowded(edition)), edge)
    # Smaller, and no taller than the room: a portrait that reached the
    # banner below it would run on into the banner's own black edge.
    assert MIN_SIDE <= tight < usual


def test_a_photo_is_converted_for_eink(tmp_path):
    with Image.open(photo(tmp_path / "photo.png")) as source:
        portrait = build_paper.eink_portrait(source.convert("L"), 400)

    assert portrait.size == (400, 400)
    # Dithered: nothing but black and white, and a mid-tone becomes a mix.
    assert shades(portrait) == {build_paper.BLACK, build_paper.WHITE}
    border = build_paper.PORTRAIT_BORDER
    assert all(portrait.getpixel((x, y)) == build_paper.BLACK
               for x in range(400) for y in (0, border - 1, 400 - border, 399))
    inside = portrait.crop((border, border, 400 - border, 400 - border))
    assert shades(inside) == {build_paper.BLACK, build_paper.WHITE}


def test_a_picture_already_converted_is_left_alone():
    ready = Image.new("L", (400, 400), build_paper.WHITE)
    ready.putpixel((200, 200), build_paper.BLACK)
    assert build_paper.eink_portrait(ready, 400) is ready


def test_a_ready_made_portrait_is_printed_as_it_is_only_where_it_matches_the_slot(config_dir):
    edition = build_paper.load_edition(SAMPLE)
    photo(config_dir / "portrait.png")
    top, side = black_run(front_page(build_paper.with_portrait(edition, config_dir)), RIGHT_EDGE)

    # A black and white pattern of exactly the day's slot inside its own
    # black edge, saved the way a reader would prepare one: a 1-bit PNG.
    ready = Image.new("1", (side, side), 0)
    ready.putdata([0 if min(x, y, side - 1 - x, side - 1 - y) < 4
                   else 255 * ((x * 7 + y * 13) % 5 < 2)
                   for y in range(side) for x in range(side)])
    ready.save(config_dir / "portrait.png")
    edition = build_paper.with_portrait(edition, config_dir)

    right = RIGHT_EDGE + 1
    printed = front_page(edition).crop((right - side, top, right, top + side))
    assert shades(printed) == {build_paper.BLACK, build_paper.WHITE}
    assert printed.tobytes() == ready.convert("L").tobytes()
    # The cover's slot is another size, so there the same picture is fitted
    # to the slot like any other.
    cover_side = longest_black_run(cover_page(edition), COVER_EDGE)
    assert cover_side != side and cover_side >= MIN_SIDE


@pytest.mark.parametrize("rain_chance, expected", [
    (None, "portrait.png"),     # no forecast
    (0, "portrait.png"),
    (29, "portrait.png"),
    (30, "portrait-rain.png"),
    (95, "portrait-rain.png"),
])
def test_the_rainy_day_portrait_runs_when_rain_is_likely(config_dir, rain_chance, expected):
    photo(config_dir / "portrait.png")
    photo(config_dir / "portrait-rain.png")
    assert build_paper.find_portraits(config_dir, rain_chance)[0] == config_dir / expected


def test_rain_without_a_rainy_day_portrait_uses_the_plain_one(config_dir):
    photo(config_dir / "portrait.png")
    assert build_paper.find_portraits(config_dir, 80) == [config_dir / "portrait.png"]


def test_a_rainy_day_portrait_alone_is_not_used_on_dry_days(config_dir):
    photo(config_dir / "portrait-rain.png")
    assert build_paper.find_portraits(config_dir, 10) == []
    assert build_paper.find_portraits(config_dir, 60) == [config_dir / "portrait-rain.png"]


@pytest.mark.parametrize("rain_chance", [None, 10])
def test_a_rainy_day_portrait_left_waiting_with_no_plain_one_is_logged(config_dir, capsys,
                                                                       rain_chance):
    photo(config_dir / "portrait-rain.png")
    edition = replace(build_paper.load_edition(SAMPLE), rain_chance=rain_chance)

    assert build_paper.with_portrait(edition, config_dir).portrait is None
    err = capsys.readouterr().err
    assert f"{config_dir / 'portrait-rain.png'} waits for a rainy day" in err
    assert "no plain portrait is set" in err

    # Nothing is said once it rains, or once there is a plain portrait.
    assert build_paper.with_portrait(replace(edition, rain_chance=60),
                                     config_dir).portrait is not None
    photo(config_dir / "portrait.png")
    assert build_paper.with_portrait(edition, config_dir).portrait is not None
    assert capsys.readouterr().err == ""


def test_capitals_in_a_portrait_file_name_do_not_matter(config_dir):
    photo(config_dir / "Portrait.JPG")
    photo(config_dir / "PORTRAIT-RAIN.Png")
    assert build_paper.find_portraits(config_dir, 10) == [config_dir / "Portrait.JPG"]
    assert build_paper.find_portraits(config_dir, 60) == [config_dir / "PORTRAIT-RAIN.Png",
                                                          config_dir / "Portrait.JPG"]
    assert build_paper.with_portrait(build_paper.load_edition(SAMPLE),
                                     config_dir).portrait is not None


@pytest.mark.parametrize("name", ["portrait.heic", "portrait.png.png", "portrait-rainy.jpg",
                                  "Portrait"])
def test_a_file_that_only_looks_like_a_portrait_is_logged(config_dir, capsys, name):
    (config_dir / name).write_bytes(b"a picture, but not one the Press reads")
    # A folder of spare pictures is not a near miss.
    (config_dir / "portrait-originals").mkdir()
    edition = build_paper.with_portrait(build_paper.load_edition(SAMPLE), config_dir)

    assert edition.portrait is None
    err = capsys.readouterr().err
    assert f"{config_dir / name} looks like a portrait" in err and "ignoring it" in err
    assert "portrait-originals" not in err


def test_an_unusable_rainy_day_portrait_is_logged_and_the_plain_one_runs(config_dir, capsys):
    photo(config_dir / "portrait.png")
    (config_dir / "portrait-rain.heic").write_bytes(b"a picture, but not one the Press reads")

    assert build_paper.find_portraits(config_dir, 80) == [config_dir / "portrait.png"]
    err = capsys.readouterr().err
    assert "portrait-rain.heic looks like a portrait" in err and "ignoring it" in err


def test_a_rainy_day_portrait_that_cannot_be_read_gives_way_to_the_plain_one(config_dir, capsys):
    photo(config_dir / "portrait.png", shade=255)
    (config_dir / "portrait-rain.png").write_bytes(b"this is not a picture")
    edition = replace(build_paper.load_edition(SAMPLE), rain_chance=80.0)

    edition = build_paper.with_portrait(edition, config_dir)
    assert shades(edition.portrait) == {255}
    assert longest_black_run(front_page(edition), RIGHT_EDGE) >= MIN_SIDE
    err = capsys.readouterr().err
    assert "portrait-rain.png cannot be read" in err and "using portrait.png instead" in err


def test_no_config_directory_means_no_portrait_and_no_warning(tmp_path, capsys):
    edition = build_paper.with_portrait(build_paper.load_edition(SAMPLE), tmp_path / "nowhere")
    assert edition.portrait is None
    assert "portrait" not in capsys.readouterr().err


@pytest.mark.parametrize("name, mode", [
    ("portrait.jpg", "RGB"), ("portrait.png", "RGB"), ("portrait.webp", "RGB"),
    # Kinds of picture that cannot be shrunk until they are made colour or gray.
    ("portrait.png", "RGBA"), ("portrait.png", "P"), ("portrait.png", "1"),
    ("portrait.png", "I;16"),
])
def test_a_large_picture_is_read_at_a_fraction_of_its_size(config_dir, name, mode):
    Image.new("L", (1500, 2000), 128).convert(mode).save(config_dir / name)
    edition = build_paper.with_portrait(build_paper.load_edition(SAMPLE), config_dir)

    width, height = edition.portrait.size
    # A quarter of the camera's pixels at most, and still no smaller than
    # the slot it has to fill.
    assert width * height * 4 <= 1500 * 2000
    assert min(width, height) >= longest_black_run(front_page(edition), RIGHT_EDGE) >= MIN_SIDE


def test_a_sixteen_bit_gray_picture_keeps_its_shades(config_dir):
    # Black at the top to white at the bottom, sixteen bits to the shade.
    ramp = Image.linear_gradient("L").convert("I;16").point(lambda shade: shade * 257)
    ramp.save(config_dir / "portrait.png")
    portrait = build_paper.with_portrait(build_paper.load_edition(SAMPLE), config_dir).portrait

    top, middle, bottom = (portrait.getpixel((128, y)) for y in (8, 128, 248))
    assert top < 32 and 96 < middle < 160 and bottom > 224


def test_a_photo_taken_on_its_side_is_turned_upright(config_dir):
    # Stored lying down, dark half on the left, with the camera's note that
    # it is to be shown turned a quarter clockwise: dark half on top.
    stored = Image.new("L", (400, 300), 255)
    stored.paste(0, (0, 0, 200, 300))
    exif = Image.Exif()
    exif[0x0112] = 6
    stored.save(config_dir / "portrait.jpg", exif=exif)
    portrait = build_paper.with_portrait(build_paper.load_edition(SAMPLE), config_dir).portrait

    assert portrait.size == (300, 400)
    assert portrait.getpixel((150, 50)) < 64 and portrait.getpixel((150, 350)) > 192


@pytest.mark.parametrize("rain_chance, shade", [(80.0, 0), (10.0, 255)])
def test_the_forecast_picks_the_portrait_that_is_printed(config_dir, monkeypatch, rain_chance,
                                                         shade):
    photo(config_dir / "portrait.png", shade=255)
    photo(config_dir / "portrait-rain.png", shade=0)
    monkeypatch.setattr(build_paper, "fetch_weather",
                        lambda date, coordinates: ("Showers", rain_chance))
    edition = replace(build_paper.load_edition(SAMPLE), weather_summary="",
                      weather_coordinates=(40.71, -74.01))

    edition = build_paper.with_portrait(build_paper.with_weather(edition), config_dir)
    assert edition.rain_chance == rain_chance
    assert shades(edition.portrait) == {shade}


def test_an_unreadable_portrait_is_logged_and_the_paper_still_prints(config_dir, data_dir, feeds,
                                                                     capsys):
    (config_dir / "portrait.png").write_bytes(b"this is not a picture")
    paper_config(config_dir, feeds)

    assert print_paper(config_dir, data_dir) == 0

    assert (data_dir / "morning-paper-2026-10-04.epub").is_file()
    with Image.open(data_dir / "frontpage-2026-10-04.png") as page:
        assert longest_black_run(page, RIGHT_EDGE) < MIN_SIDE
    err = capsys.readouterr().err
    assert "portrait.png cannot be read" in err and "printing without it" in err


def test_a_portrait_with_damaged_exif_does_not_stop_the_paper(config_dir, data_dir, feeds):
    # A picture that opens but cannot be turned upright: the orientation tag
    # asks for a turn, and a second tag (the resolution unit, stored as text
    # where a number belongs) is one the picture library cannot write back.
    def tag(number: int, kind: int, count: int, value: bytes) -> bytes:
        return struct.pack("<HHI4s", number, kind, count, value)

    exif = (b"Exif\x00\x00II*\x00" + struct.pack("<IH", 8, 2)
            + tag(0x0112, 3, 1, struct.pack("<HH", 6, 0))
            + tag(0x0128, 2, 2, b"2\x00\x00\x00")
            + struct.pack("<I", 0))
    Image.new("RGB", (300, 400), (128, 128, 128)).save(config_dir / "portrait.jpg", exif=exif)
    paper_config(config_dir, feeds)

    assert print_paper(config_dir, data_dir) == 0
    assert (data_dir / "morning-paper-2026-10-04.epub").is_file()
    assert (data_dir / "frontpage-2026-10-04.png").is_file()


@pytest.mark.parametrize("failure", [struct.error("required argument is not an integer"),
                                     MemoryError(), RuntimeError("no such luck")])
def test_whatever_goes_wrong_reading_a_portrait_costs_only_the_portrait(
        config_dir, data_dir, feeds, monkeypatch, capsys, failure):
    def fail(image, **kwargs):
        raise failure

    monkeypatch.setattr(build_paper.ImageOps, "exif_transpose", fail)
    photo(config_dir / "portrait.png")
    paper_config(config_dir, feeds)

    assert print_paper(config_dir, data_dir) == 0

    assert (data_dir / "morning-paper-2026-10-04.epub").is_file()
    with Image.open(data_dir / "frontpage-2026-10-04.png") as page:
        assert longest_black_run(page, RIGHT_EDGE) < MIN_SIDE
    err = capsys.readouterr().err
    assert "portrait.png cannot be read" in err and type(failure).__name__ in err
    assert "printing without it" in err


def test_a_portrait_that_cannot_be_converted_costs_only_the_portrait(config_dir, data_dir, feeds,
                                                                    monkeypatch, capsys):
    def fail(picture, side):
        raise RuntimeError("no such luck")

    monkeypatch.setattr(build_paper, "eink_portrait", fail)
    photo(config_dir / "portrait.png")
    paper_config(config_dir, feeds)

    assert print_paper(config_dir, data_dir) == 0

    assert (data_dir / "morning-paper-2026-10-04.epub").is_file()
    with Image.open(data_dir / "frontpage-2026-10-04.png") as page:
        assert longest_black_run(page, RIGHT_EDGE) < MIN_SIDE
    err = capsys.readouterr().err
    assert "portrait cannot be converted" in err and "RuntimeError" in err
    assert "printing without it" in err


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="nothing is closed to root")
def test_a_config_directory_that_cannot_be_read_costs_only_the_portrait(config_dir, capsys):
    photo(config_dir / "portrait.png")
    config_dir.chmod(0)
    try:
        edition = build_paper.with_portrait(build_paper.load_edition(SAMPLE), config_dir)
    finally:
        config_dir.chmod(0o755)

    assert edition.portrait is None
    err = capsys.readouterr().err
    assert f"portrait in {config_dir} cannot be read" in err and "printing without it" in err


def test_with_staging_the_portrait_still_comes_from_the_press_config_directory(
        config_dir, data_dir, stage_url):
    # The staged edition is the sample, which has no picture of its own to give.
    pointer = stage_put.stage_edition(staging.Stage(stage_url, STAGE_TOKEN), SAMPLE)
    photo(config_dir / "portrait.png")

    assert press_fetch.main(["--data-dir", str(data_dir), "--config-dir", str(config_dir),
                             "--url", stage_url, "--token", STAGE_TOKEN]) == 0

    with Image.open(data_dir / f"frontpage-{pointer['date']}.png") as page:
        assert longest_black_run(page, RIGHT_EDGE) >= MIN_SIDE


def test_the_press_prints_the_portrait_from_its_config_directory(config_dir, data_dir, feeds):
    photo(config_dir / "portrait.png")
    paper_config(config_dir, feeds)

    assert print_paper(config_dir, data_dir) == 0
    with Image.open(data_dir / "frontpage-2026-10-04.png") as page:
        assert longest_black_run(page, RIGHT_EDGE) >= MIN_SIDE
