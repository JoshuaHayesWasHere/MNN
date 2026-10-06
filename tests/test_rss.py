"""The built-in RSS source, against local fixture feeds."""

from __future__ import annotations

import datetime as dt
import time

import pytest

from mnn import build_paper
from conftest import FIXTURES
from mnn.sources import rss

DATE = dt.date(2026, 10, 4)


def section(feeds: list[str], **config) -> dict:
    return rss.produce(DATE, {"title": "World", "feeds": feeds, "stories": 5, **config})


def test_rss_items_become_plain_text_articles():
    title, items = rss.parse_feed((FIXTURES / "world.rss").read_bytes())
    assert title == "Fixture World"
    bridge = items[0]
    assert bridge["title"] == "Harbour bridge reopens after & repairs"
    # Tags, the script, the "continue reading" trailer and the full article
    # in content:encoded are all gone; entities are decoded.
    assert bridge["body"] == ["The bridge reopened on Monday.", "Traffic was light & orderly."]
    assert bridge["url"] == "https://world.example/bridge"
    assert bridge["date"] == dt.date(2026, 10, 4)
    assert "A headline with no summary" not in [item["title"] for item in items]


def test_unsafe_links_and_invisible_characters_are_dropped():
    _, items = rss.parse_feed((FIXTURES / "world.rss").read_bytes())
    ferry = items[1]
    assert ferry["url"] == ""
    assert ferry["body"] == ["Sailings move to an hourly service from November."]


def test_atom_entries_are_read_too():
    title, items = rss.parse_feed((FIXTURES / "science.atom").read_bytes())
    assert title == "Fixture Science"
    assert items[0]["title"] == "Moss grows faster in the dark"
    assert items[0]["body"] == ["A small study found a surprising result."]
    assert items[0]["url"] == "https://science.example/moss"
    assert items[2]["date"] == dt.date(2026, 10, 3)


def test_atom_xhtml_is_reduced_like_any_other_markup():
    _, items = rss.parse_feed((FIXTURES / "science.atom").read_bytes())
    telescope = items[2]
    assert telescope["title"] == "Telescope sees first light"
    # Paragraphs and line breaks still divide the text; the style sheet and
    # the script are gone.
    assert telescope["body"] == ["Engineers called the images sharp.",
                                 "The mirror took a decade to grind.", "It was worth the wait."]


def test_markup_inside_a_headline_does_not_lose_the_story():
    feed = (b"<rss><channel><item><title>Harbour <b>bridge</b> reopens</title>"
            b"<description>It reopened on Monday.</description></item></channel></rss>")
    (item,) = rss.parse_feed(feed)[1]
    assert item["title"] == "Harbour bridge reopens"


@pytest.mark.parametrize("link, kept", [
    ("https://world.example/bridge", "https://world.example/bridge"),
    ("  HTTPS://World.example/Bridge ", "https://World.example/Bridge"),
    ("Http://world.example/", "http://world.example/"),
    ("https://world.example/a\nb", ""),
    ("https://world.example/a b", ""),
    ("https://world.example/a\u00a0b", ""),
    ("https://world.example/\u202egnp.exe", ""),
    ("https:///no-host", ""),
    ("http://[not-an-address", ""),
    ("ftp://world.example/file", ""),
    ("/relative/path", ""),
])
def test_a_link_is_kept_only_in_a_form_the_builder_prints(link, kept):
    feed = ("<rss><channel><item><title>Bridge reopens</title>"
            f"<link>{link}</link><description>It reopened.</description></item></channel></rss>")
    (item,) = rss.parse_feed(feed.encode("utf-8"))[1]
    assert item["url"] == kept
    # One story's link must never cost the section: the builder takes it as is.
    section = build_paper.parse_section({"title": "World", "articles": [item]}, "section")
    assert section.articles[0].url == kept


@pytest.mark.parametrize("markup, expected", [
    ("<p>One</p><p>Two</p>", ["One", "Two"]),
    ("a<br>b", ["a", "b"]),
    ("&lt;b&gt;escaped twice&lt;/b&gt; &amp;amp; again", ["escaped twice & again"]),
    ("<style>p{color:red}</style>visible<script>hidden()</script>", ["visible"]),
    ("3 < 5 and 7 > 2", ["3 < 5 and 7 > 2"]),
    ("tab\tand\x07bell\u202eand override", ["tab and bell and override"]),
    ("<img src=x onerror=alert(1)>", []),
    ("", []),
    # Only a block tag or a blank line ends a paragraph, not a wrapped line.
    ("<p>We study the problem of\nfoo bar and show that\nit works well in\npractice.</p>",
     ["We study the problem of foo bar and show that it works well in practice."]),
    ("Para one,\nwrapped.\n\nPara two.\r\n \r\nPara three.",
     ["Para one, wrapped.", "Para two.", "Para three."]),
    ("<table><tr><th>Head</th><td>Cell one</td><td>Cell two</td></tr></table>",
     ["Head", "Cell one", "Cell two"]),
    ("<dl><dt>Term</dt><dd>Meaning</dd></dl>", ["Term", "Meaning"]),
    ("before<pre>code\nmore</pre>after", ["before", "code more", "after"]),
])
def test_plain_text(markup, expected):
    assert rss.plain_text(markup) == expected


def test_long_summaries_are_cut_at_a_sentence():
    text = "A sentence that is reasonably long. " * 60
    (summary,) = rss.summary_paragraphs(text)
    assert len(summary) <= rss.SUMMARY_LIMIT
    assert summary.endswith("long.")


@pytest.mark.parametrize("markup, expected", [
    ("<p>Traffic was light. Continue reading...</p>", ["Traffic was light."]),
    ("<p>Traffic was light.<a href='/more'>Read more</a></p>", ["Traffic was light."]),
    ("<p>Was traffic light? Read the full story</p>", ["Was traffic light?"]),
    ("<p>Traffic was light.</p><p>READ MORE</p>", ["Traffic was light."]),
    # The same words closing a sentence of the summary's own are kept.
    ("<p>Teachers say every child should read more</p>",
     ["Teachers say every child should read more"]),
    ("<p>Ministers urge pupils to continue reading</p>",
     ["Ministers urge pupils to continue reading"]),
])
def test_only_a_trailer_is_taken_off_the_end_of_a_summary(markup, expected):
    assert rss.summary_paragraphs(markup) == expected


def test_a_summary_does_not_end_in_a_stub_of_a_paragraph():
    second = "A second paragraph of ordinary length follows the first one here."
    nearly_full = "word " * 179 + "end."
    assert len(nearly_full) == rss.SUMMARY_LIMIT - 1
    assert rss.summary_paragraphs(f"<p>{nearly_full}</p><p>{second}</p>") == [nearly_full]
    # A closing paragraph that fits whole is kept, however little room is left.
    assert rss.summary_paragraphs(f"<p>{nearly_full[50:]}</p><p>Short.</p>") == \
        [nearly_full[50:], "Short."]
    # With room to spare the next paragraph is cut to fit, as before.
    roomy = "word " * 120 + "end."
    first, cut = rss.summary_paragraphs(f"<p>{roomy}</p><p>{' '.join([second] * 8)}</p>")
    assert first == roomy and cut.startswith(second) and cut.endswith("here.")
    assert len(first) + len(cut) <= rss.SUMMARY_LIMIT


def test_stories_are_taken_from_each_feed_in_turn(feeds):
    result = section([f"{feeds}/world.rss", f"{feeds}/science.atom"])
    assert result["title"] == "World"
    assert result["notes"] == []
    assert [article["title"] for article in result["articles"]] == [
        "Harbour bridge reopens after & repairs",
        "Moss grows faster in the dark",
        "Ferry timetable changes for winter",
        # The second feed's copy of the bridge story is skipped as a duplicate.
        "Market square gets a new clock",
        "Telescope sees first light",
    ]
    bridge, _, ferry, *_ = result["articles"]
    assert bridge["source"] == "Fixture World"
    assert bridge["url"] == "https://world.example/bridge"
    assert "url" not in ferry


def test_stories_limits_the_section(feeds):
    result = section([f"{feeds}/world.rss", f"{feeds}/science.atom"], stories=2)
    assert len(result["articles"]) == 2


def test_old_items_are_left_out_unless_asked_for(feeds):
    titles = [a["title"] for a in section([f"{feeds}/world.rss"])["articles"]]
    assert "An old story that should have aged out" not in titles
    titles = [a["title"] for a in section([f"{feeds}/world.rss"], max_age_days=60)["articles"]]
    assert "An old story that should have aged out" in titles


def test_an_unreachable_feed_costs_only_itself(feeds, unreachable):
    result = section([unreachable, f"{feeds}/world.rss"])
    assert len(result["articles"]) == 3
    (note,) = result["notes"]
    assert note.startswith(f"feed 1, {unreachable}: ")


def test_a_failed_feed_is_noted_by_its_place_in_the_list(feeds):
    # Feeds that differ only in the query are recorded under one address.
    result = section([f"{feeds}/world.rss", f"{feeds}/world.rss",
                      f"{feeds}/missing.rss?channel_id=one", f"{feeds}/science.atom",
                      f"{feeds}/missing.rss?channel_id=two"])
    assert len(result["articles"]) == 5
    assert result["notes"] == [f"feed 3, {feeds}/missing.rss: HTTP 404",
                               f"feed 5, {feeds}/missing.rss: HTTP 404"]


@pytest.mark.parametrize("path, reason", [
    ("missing.rss", "HTTP 404"),
    ("broken", "not a feed"),
    ("page", "not a feed"),
])
def test_a_bad_feed_is_noted_and_the_rest_print(feeds, path, reason):
    result = section([f"{feeds}/{path}", f"{feeds}/science.atom"])
    assert len(result["articles"]) == 3
    assert reason in result["notes"][0]


def test_a_slow_feed_is_abandoned_at_its_timeout(feeds):
    started = time.monotonic()
    result = section([f"{feeds}/slow", f"{feeds}/world.rss"], feed_timeout=0.5)
    assert time.monotonic() - started < 3
    assert len(result["articles"]) == 3
    assert f"{feeds}/slow" in result["notes"][0]


def test_no_readable_feed_is_an_error_that_names_each_one(feeds, unreachable):
    with pytest.raises(rss.FeedError) as caught:
        section([unreachable, f"{feeds}/missing.rss"])
    assert unreachable in str(caught.value)
    assert "HTTP 404" in str(caught.value)


def test_a_summary_wrapped_over_lines_is_one_paragraph():
    feed = b"""<rss><channel><item>
        <title>Wrapped</title>
        <description>
            We study the problem of
            foo bar and show that
            it works well in
            practice, across many settings.
        </description>
    </item></channel></rss>"""
    (item,) = rss.parse_feed(feed)[1]
    assert item["body"] == ["We study the problem of foo bar and show that it works well in "
                            "practice, across many settings."]


@pytest.mark.parametrize("address, recorded", [
    ("https://example.com/world.rss", "https://example.com/world.rss"),
    ("https://example.com:8443/feed/@author", "https://example.com:8443/feed/@author"),
    ("https://example.com/rss?token=s3cret&page=2#top", "https://example.com/rss"),
    ("https://reader:hunter2@example.com/rss", "https://example.com/rss"),
    ("https://s3cret@example.com/rss?key=hunter2", "https://example.com/rss"),
    (" reader:hunter2@example.com/rss ", "example.com/rss"),
    ("//reader:hunter2@example.com/rss", "//example.com/rss"),
    ("https:/reader:hunter2@example.com/rss", "https:/example.com/rss"),
    ("http://[::1]:8080/rss?key=hunter2", "http://[::1]:8080/rss"),
    ("https://example.com/search?q=a@b.example", "https://example.com/search"),
    # A password that holds / # or ? cuts the host short.
    ("http://reader:hun/ter2@example.com/rss", "http://example.com/rss"),
    ("http://reader:hunter#2@example.com/rss", "http://example.com/rss"),
    ("http://reader:hun?ter2@example.com:8080/rss?key=s3cret", "http://example.com:8080/rss"),
    ("reader:hun/ter2@example.com/rss", "example.com/rss"),
    ("", ""),
])
def test_an_address_is_recorded_without_what_may_be_a_credential(address, recorded):
    assert rss.public_address(address) == recorded


def test_a_failed_feed_is_noted_without_its_credentials(feeds):
    private = f"{feeds}/missing.rss?token=s3cret"
    spaced = f"{feeds}/missing.rss?token=s3 cret"
    signed_in = "http://reader:hunter2@localhost/feed.rss"
    slashed = "http://reader:hun/ter2@localhost/feed.rss"
    odd_host = "http://reader:hunter2@exa\N{ACCOUNT OF}mple.com/feed.rss"
    failing = [private, spaced, signed_in, slashed, odd_host]
    result = section([*failing, f"{feeds}/world.rss"])
    assert len(result["articles"]) == 3
    assert result["notes"] == [
        f"feed 1, {feeds}/missing.rss: HTTP 404",
        f"feed 2, {feeds}/missing.rss: not an address that can be fetched",
        "feed 3, http://localhost/feed.rss: not an address that can be fetched",
        "feed 4, http://localhost/feed.rss: not an address that can be fetched",
        "feed 5, http://exa\N{ACCOUNT OF}mple.com/feed.rss: not an address that can be fetched",
    ]

    with pytest.raises(rss.FeedError) as caught:
        section(failing)
    error = str(caught.value)
    assert f"feed 1, {feeds}/missing.rss: HTTP 404" in error
    assert not any(secret in error
                   for secret in ("s3cret", "s3 cret", "hunter2", "ter2", "reader"))


def test_only_http_feeds_are_fetched(tmp_path):
    local = tmp_path / "feed.rss"
    local.write_bytes((FIXTURES / "world.rss").read_bytes())
    with pytest.raises(rss.FeedError, match="not an http"):
        section([local.as_uri()])


@pytest.mark.parametrize("config", [
    {"feeds": []}, {"feeds": "https://example.com/rss"}, {"stories": 0}, {"stories": "five"},
    {"feed_timeout": -1},
])
def test_a_bad_section_table_is_refused(config):
    with pytest.raises(ValueError):
        rss.produce(DATE, {"title": "World", "feeds": ["https://example.com/rss"], **config})
