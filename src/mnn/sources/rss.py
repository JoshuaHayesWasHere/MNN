"""The default source: one section of the paper from RSS and Atom feeds.

A `[[section]]` in sources.toml that names no other source is handed to
`produce` here:

    [[section]]
    title = "World"
    feeds = ["https://example.com/world.rss", "https://example.org/atom.xml"]
    stories = 5

Optional keys: `feed_timeout` (seconds each feed gets, default 20) and
`max_age_days` (skip items dated more than this many days before the edition,
default 3; undated items are kept).

Stories are taken from the feeds in turn, each feed in its own order, so one
feed that is down or quiet is covered by the others. Only what the feed itself
says is printed: the headline, the feed's own summary reduced to plain text,
and a link back. The linked article is never fetched.

Standard library only.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import html
import http.client
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlsplit

USER_AGENT = "MNN-Press/0.1 (personal feed reader)"
DEFAULT_STORIES = 5
DEFAULT_FEED_TIMEOUT = 20
DEFAULT_MAX_AGE_DAYS = 3
# A feed is a list of headlines. Anything larger than this is not one.
MAX_FEED_BYTES = 8 * 1024 * 1024
TITLE_LIMIT = 200
SUMMARY_LIMIT = 900
SUMMARY_PARAGRAPHS = 3
# A paragraph that would have to be cut shorter than this is left out.
SHORTEST_CUT = 80

ATOM = "http://www.w3.org/2005/Atom"
# Namespaces whose title, link, description and summary describe the story.
# Others (Media RSS, for one) reuse those names for thumbnails and captions.
STORY_NAMESPACES = {"", ATOM, "http://purl.org/atom/ns#", "http://purl.org/rss/1.0/",
                    "http://my.netscape.com/rdf/simple/0.9/"}
DATE_TAGS = ("pubDate", "published", "updated", "date")

BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "tr", "td", "th", "table", "dt", "dd", "pre",
              "blockquote", "section", "article", "figure", "hr", "h1", "h2", "h3", "h4", "h5",
              "h6"}
# A paragraph ends at a block tag or a blank line. A lone line break in a
# feed's text is whitespace like any other.
PARAGRAPH_BREAK = "\n\n"
PARAGRAPH_RE = re.compile(r"\n\s*\n")
SILENT_TAGS = {"script", "style", "head", "title", "noscript", "template", "svg", "math",
               "iframe", "object", "figcaption"}
TAG_RE = re.compile(r"<[A-Za-z/!][^>]*>")
# An address as its scheme, what stands where the host should, and the rest.
ADDRESS_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*:/+|/+)?([^/?#]*)(.*)", re.DOTALL)
# A host with nothing else in it: a name or a bracketed IPv6 address, with or
# without a port number.
HOST_RE = re.compile(r"(?:\[[^\]@]*\]|[^:@\[\]]*)(?::\d*)?")
# Said in place of an error whose own words quote the address back, password
# and all.
UNFETCHABLE = "not an address that can be fetched"
# Control characters, plus the invisible direction and joiner marks that let
# text render as something other than what it says.
UNSAFE_RE = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
# What a feed adds when its summary is only the top of the article: a
# paragraph of its own, or words after the end of the last sentence.
TRAILER_RE = re.compile(r'(?:^|(?<=[.!?…:"”’)]))\s*'
                        r"(?:Continue reading|Read more|Read the full (?:story|article))"
                        r"\s*(?:\.\.\.|…)?\s*$", re.IGNORECASE)


class FeedError(Exception):
    """A feed could not be fetched or read."""


# --- Plain text ------------------------------------------------------------


class _TextOnly(HTMLParser):
    """Keeps the words, drops the markup, and notes where blocks break. A
    namespace prefix on a tag is ignored: XHTML's <html:p> is a <p>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.silent = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.rpartition(":")[2]
        if tag in SILENT_TAGS:
            self.silent += 1
        elif tag in BLOCK_TAGS:
            self.parts.append(PARAGRAPH_BREAK)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.rpartition(":")[2]
        if tag in SILENT_TAGS:
            self.silent = max(0, self.silent - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append(PARAGRAPH_BREAK)

    def handle_data(self, data: str) -> None:
        if not self.silent:
            self.parts.append(data)


def plain_text(markup: str) -> list[str]:
    """Reduce feed text, which may be HTML, to plain paragraphs: no tags, no
    scripts, no control characters, entities decoded, whitespace collapsed.
    Text wrapped over several lines stays one paragraph."""
    text = markup
    # Twice at most: some feeds escape their HTML a second time.
    for _ in range(2):
        parser = _TextOnly()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
        if not TAG_RE.search(text):
            break
    else:
        text = TAG_RE.sub(" ", text)
    text = UNSAFE_RE.sub(" ", text)
    return [paragraph for block in PARAGRAPH_RE.split(text)
            if (paragraph := " ".join(block.split()))]


def shorten(text: str, limit: int) -> str:
    """Cut at the end of a sentence where there is one, else at a word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sentence = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if sentence >= limit // 2:
        return cut[:sentence + 1]
    return cut.rsplit(" ", 1)[0].rstrip(" ,;:.") + "…"


def summary_paragraphs(markup: str) -> list[str]:
    paragraphs = []
    room = SUMMARY_LIMIT
    for paragraph in plain_text(markup)[:SUMMARY_PARAGRAPHS]:
        paragraph = TRAILER_RE.sub("", paragraph)
        if not paragraph:
            continue
        if len(paragraph) > room and room < SHORTEST_CUT:
            break
        paragraph = shorten(paragraph, room)
        paragraphs.append(paragraph)
        room -= len(paragraph)
    return paragraphs


# --- Reading a feed --------------------------------------------------------


def _split(tag: object) -> tuple[str, str]:
    """(namespace, local name) of an element tag."""
    if not isinstance(tag, str):
        return "", ""  # a comment or processing instruction
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


def _children(element: ET.Element) -> dict[str, list[ET.Element]]:
    found: dict[str, list[ET.Element]] = {}
    for child in element:
        namespace, local = _split(child.tag)
        if namespace in STORY_NAMESPACES or local in DATE_TAGS:
            found.setdefault(local, []).append(child)
    return found


def _content(element: ET.Element) -> str:
    """What is inside an element, as markup. Atom may carry XHTML as child
    elements; the element's own tag is never part of it."""
    if len(element):
        return html.escape(element.text or "", quote=False) + "".join(
            ET.tostring(child, encoding="unicode", method="html") for child in element)
    return element.text or ""


def _first_text(children: dict[str, list[ET.Element]], *names: str) -> str:
    for name in names:
        for element in children.get(name, []):
            if text := _content(element).strip():
                return text
    return ""


def _link(children: dict[str, list[ET.Element]]) -> str:
    links = children.get("link", [])
    # Atom: <link rel="alternate" href="..."/>. RSS: <link>...</link>.
    for element in links:
        if element.get("href") and element.get("rel", "alternate") == "alternate":
            return safe_url(element.get("href", ""))
    for element in links:
        if element.text and element.text.strip():
            return safe_url(element.text)
    return ""


def safe_url(url: str) -> str:
    """The link in the form the builder prints, or "" when it is not a plain
    http(s) address."""
    url = url.strip()
    if UNSAFE_RE.search(url) or re.search(r"\s", url):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return parts.scheme + url[len(parts.scheme):]


def _date(children: dict[str, list[ET.Element]]) -> dt.date | None:
    text = _first_text(children, *DATE_TAGS)
    if not text:
        return None
    try:
        return email.utils.parsedate_to_datetime(text).date()
    except (TypeError, ValueError):
        pass
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def parse_feed(data: bytes) -> tuple[str, list[dict]]:
    """(feed title, items) from RSS 2.0, RSS 1.0 or Atom. Each item has a
    plain-text `title` and `body`, a `url` (possibly empty) and a `date`
    (possibly None). Items with no headline or no summary are left out."""
    try:
        # No DTD is fetched and no external entity resolved; expat refuses
        # entity-expansion bombs on its own.
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FeedError(f"not a feed: {exc}") from exc

    channel = next((el for el in root if _split(el.tag)[1] == "channel"), root)
    feed_title = " ".join(plain_text(_first_text(_children(channel), "title")))

    items = []
    for element in root.iter():
        if _split(element.tag)[1] not in ("item", "entry"):
            continue
        children = _children(element)
        title = shorten(" ".join(plain_text(_first_text(children, "title"))), TITLE_LIMIT)
        # The feed's summary, never its copy of the whole article
        # (content:encoded). Atom's <content> is the fallback, cut to length.
        body = summary_paragraphs(_first_text(children, "description", "summary", "content"))
        if title and body:
            items.append({"title": title, "body": body, "url": _link(children),
                          "date": _date(children)})
    if not items and _split(root.tag)[1] not in ("rss", "feed", "RDF"):
        raise FeedError(f"not a feed: the document is <{_split(root.tag)[1]}>")
    return feed_title, items


def fetch(url: str, timeout: float) -> bytes:
    try:
        scheme = urlsplit(url).scheme
    except ValueError as exc:
        raise FeedError(UNFETCHABLE) from exc
    if scheme not in ("http", "https"):
        raise FeedError("not an http(s) address")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.5",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FeedError(str(exc.reason)) from exc
    except http.client.InvalidURL as exc:
        raise FeedError(UNFETCHABLE) from exc
    except (OSError, ValueError) as exc:
        raise FeedError(str(exc) or type(exc).__name__) from exc
    if len(data) > MAX_FEED_BYTES:
        raise FeedError(f"larger than {MAX_FEED_BYTES // (1024 * 1024)} MB")
    return data


def public_address(url: str) -> str:
    """A feed's address as it is recorded: scheme, host and path. A user
    name, password or query string may be a credential, so none is kept."""
    scheme, host, rest = ADDRESS_RE.match(url.strip()).groups("")
    if "@" in host:
        host = host.rpartition("@")[2]
    elif "@" in rest and not HOST_RE.fullmatch(host):
        # A password holding / ? or # cuts the host short: it is after the last @.
        host, rest = "", rest.rpartition("@")[2]
    return scheme + host + re.split("[?#]", rest, maxsplit=1)[0]


def read_feeds(urls: list[str], timeout: float) -> list[tuple[str, object]]:
    """Fetch and parse every feed at once. Each result is (url, (feed title,
    items)) or (url, the FeedError that stopped it). No feed can hold up
    another, and none is waited on past `timeout`."""
    results: dict[str, object] = {}

    def read(url: str) -> None:
        try:
            results[url] = parse_feed(fetch(url, timeout))
        except FeedError as exc:
            results[url] = exc
        except Exception as exc:  # one odd feed must not take the others down
            results[url] = FeedError(f"{type(exc).__name__}: {exc}")

    # Daemon threads: a feed that never answers is abandoned, not joined.
    threads = [threading.Thread(target=read, args=(url,), daemon=True) for url in urls]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return [(url, results.get(url, FeedError(f"no answer within {timeout:g}s"))) for url in urls]


# --- The source ------------------------------------------------------------


def _number(config: dict, key: str, default: float, *, whole: bool = False) -> float:
    value = config.get(key, default)
    kinds = (int,) if whole else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or value <= 0:
        raise ValueError(f"'{key}' must be a positive {'whole ' if whole else ''}number")
    return value


def produce(date: dt.date, config: dict) -> dict:
    feeds = config.get("feeds")
    if not isinstance(feeds, list) or not feeds or not all(isinstance(f, str) for f in feeds):
        raise ValueError("'feeds' must be a list of feed addresses")
    # Each feed once, numbered by its place in the list as it was written.
    places = {url: feeds.index(url) + 1 for url in dict.fromkeys(feeds)}
    stories = int(_number(config, "stories", DEFAULT_STORIES, whole=True))
    timeout = _number(config, "feed_timeout", DEFAULT_FEED_TIMEOUT)
    oldest = date - dt.timedelta(days=_number(config, "max_age_days", DEFAULT_MAX_AGE_DAYS))

    queues, notes = [], []
    for url, result in read_feeds(list(places), timeout):
        if isinstance(result, FeedError):
            notes.append(f"feed {places[url]}, {public_address(url)}: {result}")
            continue
        feed_title, items = result
        byline = feed_title or urlsplit(url).netloc
        queues.append([dict(item, source=byline) for item in items
                       if item["date"] is None or item["date"] >= oldest])
    if len(notes) == len(places):
        raise FeedError("no feed could be read: " + "; ".join(notes))

    # One from each feed in turn, so no single feed fills the section.
    articles, seen = [], set()
    while len(articles) < stories and any(queues):
        for queue in queues:
            if not queue or len(articles) >= stories:
                continue
            item = queue.pop(0)
            keys = {item["title"].casefold(), item["url"] or item["title"].casefold()}
            if keys & seen:
                continue
            seen |= keys
            article = {"title": item["title"], "body": item["body"], "source": item["source"]}
            if item["url"]:
                article["url"] = item["url"]
            articles.append(article)
    return {"title": config.get("title", ""), "articles": articles, "notes": notes}
