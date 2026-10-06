"""The paper's state as one small picture, for a screen on a Muse gadget.

A Muse display board draws whatever its `display.draw_url` command is pointed
at, so the Press serves a card that says where the morning stands: the paper
is ready (with the lead headline and how much is in it), it has not been
printed yet, or the print failed. It is made from the same records as
/api/status, so the two never disagree.

The board decides the format. At SDK commit 3229892e93 it takes a baseline
(not progressive) JPEG no larger than its screen, or raw RGB565 (high byte
first) exactly as wide as its screen, and tells them apart by the first two
bytes; PNG is not accepted. The two e-paper boards are 800 by 480 and have no
gray, so the card is drawn in pure black and white, letters included.

The server hands it out at /card.jpg and /card.rgb565 (see server.py).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import math
import re

from PIL import Image, ImageDraw, ImageFont

from mnn import build_paper, muse

TITLE = "The Morning Paper"
# The e-paper boards the SDK supports, and the size every length below is for.
DEFAULT_SIZE = (800, 480)
MIN_SIDE, MAX_SIDE = 120, 2000
SIZE_RE = re.compile(r"(\d{3,4})x(\d{3,4})")
# A one-bit image: Pillow then sets the letters without gray edges, which an
# e-paper board would otherwise dither into stray dots.
BLACK, WHITE = 0, 1
LABELS = {"ready": "PAPER READY", "waiting": "NOT PRINTED YET", "failed": "PRINT FAILED"}
MAX_BODY_LINES = 6
MIN_FONT = 8


@dataclasses.dataclass(frozen=True)
class Card:
    state: str  # "ready", "waiting" or "failed"
    date: str   # the edition the card is about, YYYY-MM-DD
    body: str
    footer: str


def long_date(iso: str) -> str:
    try:
        date = dt.date.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{date:%A, %B} {date.day}"


def summarise(status: dict, today: dt.date) -> Card:
    """What the card says, from the report /api/status gives with each
    section's headlines added. The headline and the counts exist only when
    the Press wrote the paper itself; a staged edition's card says that it is
    ready and when it was printed."""
    last = status.get("last_print") or {}
    newest = status.get("edition_date")
    if last.get("status") == "failed":
        return Card("failed", last.get("edition_date") or today.isoformat(),
                    muse.clean(last.get("error") or "No reason was recorded.", 240),
                    "The previous paper is still being served." if newest
                    else "No paper has been printed so far.")
    if not newest or newest < today.isoformat():
        return Card("waiting", today.isoformat(), "Today's paper has not been printed yet.",
                    f"Last paper: {long_date(newest)}." if newest
                    else "No paper has been printed so far.")

    sections = status.get("sections") or []
    if (status.get("last_run") or {}).get("edition_date") != newest:
        sections = []
    printed = [section for section in sections
               if isinstance(section, dict) and section.get("status") == "ok"]
    facts = []
    if printed:
        stories = sum(int(section.get("stories") or 0) for section in printed)
        facts.append(f"{stories} {'story' if stories == 1 else 'stories'} in "
                     f"{muse.count(len(printed), 'section')}")
        missed = sum(1 for section in sections
                     if isinstance(section, dict) and section.get("status") == "failed")
        if missed:
            facts.append(f"{muse.count(missed, 'section')} left out")
    if last.get("edition_date") == newest:
        try:
            built = dt.datetime.fromisoformat(str(last.get("built_at"))).astimezone()
            facts.append(f"printed at {built:%H:%M}")
        except ValueError:
            pass
    headline = next((section["headlines"][0] for section in printed
                     if section.get("headlines")), None)
    footer = ", ".join(facts)
    return Card("ready", newest,
                muse.clean(headline, 240) if headline else "Today's paper is ready to read.",
                footer[:1].upper() + footer[1:] + "." if footer else "")


# --- Drawing ------------------------------------------------------------------


def parse_size(text: str) -> tuple[int, int] | None:
    """A size written as 800x480, or None if it is not one a board could have."""
    match = SIZE_RE.fullmatch(text.strip().lower())
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE):
        return None
    return width, height


def _fitted(draw: ImageDraw.ImageDraw, text: str, pattern: str, size: int,
            max_width: int) -> ImageFont.FreeTypeFont:
    """The face at `size`, or as much smaller as it takes to fit on one line."""
    font = build_paper._font(pattern, size)
    while size > MIN_FONT and draw.textlength(text, font=font) > max_width:
        size -= 1
        font = build_paper._font(pattern, size)
    return font


def _wrapped(draw: ImageDraw.ImageDraw, text: str, font, max_width: int,
             max_lines: int) -> list[str]:
    """build_paper's word wrap with every line kept inside `max_width`. That
    wrap never breaks a word, so one wider than the line (a path in a
    failure's reason, a long word on a narrow board) is first carried over
    onto as many lines as it needs, and a last line its ellipsis makes too
    wide is cut."""
    words = []
    for word in text.split():
        while len(word) > 1 and draw.textlength(word, font=font) > max_width:
            fits = 1
            while draw.textlength(word[:fits + 1], font=font) <= max_width:
                fits += 1
            words.append(word[:fits])
            word = word[fits:]
        words.append(word)
    lines = build_paper._wrap(draw, " ".join(words), font, max_width, max_lines)
    while lines and len(lines[-1]) > 2 and draw.textlength(lines[-1], font=font) > max_width:
        lines[-1] = lines[-1][:-2] + "…"
    return lines


def render(card: Card, size: tuple[int, int] = DEFAULT_SIZE) -> Image.Image:
    """The card as a one-bit image. The state is the widest thing on it and
    is told apart by shape as well as by words: a solid band when the paper
    is ready, an empty frame while it is not, a band inside a frame when the
    print failed."""
    width, height = size
    scale = math.sqrt(width * height / (DEFAULT_SIZE[0] * DEFAULT_SIZE[1]))

    def px(length: float) -> int:
        return max(1, round(length * scale))

    image = Image.new("1", size, WHITE)
    draw = ImageDraw.Draw(image)
    margin = px(28)
    left, right, inner = margin, width - margin, width - 2 * margin

    # Masthead and date share a line when there is room for both.
    date = long_date(card.date)
    masthead = _fitted(draw, TITLE, "serif:bold", px(32), inner)
    date_font = _fitted(draw, date, "serif", px(24), inner)
    y = px(50)
    draw.text((left, y), TITLE, font=masthead, fill=BLACK, anchor="ls")
    if (draw.textlength(TITLE, font=masthead) + px(24)
            + draw.textlength(date, font=date_font)) <= inner:
        draw.text((right, y), date, font=date_font, fill=BLACK, anchor="rs")
    else:
        y += px(32)
        draw.text((left, y), date, font=date_font, fill=BLACK, anchor="ls")
    y += px(16)

    band = (left, y, right, y + px(92))
    solid = card.state != "waiting"
    draw.rectangle(band, fill=BLACK if solid else WHITE, outline=BLACK, width=px(4))
    if card.state == "failed":
        gap = px(8)
        draw.rectangle((band[0] + gap, band[1] + gap, band[2] - gap, band[3] - gap),
                       outline=WHITE, width=px(3))
    label = LABELS[card.state]
    draw.text((width / 2, (band[1] + band[3]) / 2), label,
              font=_fitted(draw, label, "serif:bold", px(56), inner - px(48)),
              fill=WHITE if solid else BLACK, anchor="mm")
    y = band[3] + px(18)

    bottom = height - px(22)
    if card.footer:
        footer_font = build_paper._font("serif", max(MIN_FONT, px(23)))
        footer = _wrapped(draw, card.footer, footer_font, inner, 1)[0]
        draw.text((left, bottom), footer, font=footer_font, fill=BLACK, anchor="ls")
        bottom -= px(34)
        draw.line((left, bottom, right, bottom), fill=BLACK, width=px(2))
        bottom -= px(8)

    body_size = max(MIN_FONT, px(42))
    body_font = build_paper._font("serif:bold", body_size)
    line_height = round(body_size * 1.2)
    lines = max(1, min(MAX_BODY_LINES, (bottom - y) // line_height))
    for line in _wrapped(draw, card.body, body_font, inner, lines):
        draw.text((left, y), line, font=body_font, fill=BLACK)
        y += line_height
    return image


def jpeg(image: Image.Image) -> bytes:
    """Baseline JPEG, the way the SDK's own image tool writes one: three
    components with the colour halved, never progressive."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=95, subsampling="4:2:0",
                              progressive=False)
    return buffer.getvalue()


def rgb565(image: Image.Image) -> bytes:
    """Raw RGB565, two bytes a pixel with the high byte first. Black is 0000
    and white is FFFF, so each pixel is its gray value twice."""
    gray = image.convert("L")
    return Image.merge("LA", (gray, gray)).tobytes()
