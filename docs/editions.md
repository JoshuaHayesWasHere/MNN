# Editions and what the build produces

## Writing an edition

An edition is one JSON file. It is what the Press assembles from its sources,
and what a remote author stages. `edition/sample.json` is a complete example.

```json
{
  "title": "The Morning Paper",
  "date": "2026-10-03",
  "note": "Optional line printed at the foot of the front page",
  "weather": { "location": "Your town", "latitude": 40.71, "longitude": -74.01 },
  "sections": [
    {
      "title": "Front Page",
      "articles": [
        {
          "title": "Headline",
          "deck": "Optional one or two sentence standfirst",
          "source": "Optional byline or publication",
          "url": "https://example.com/optional-link-to-the-original",
          "body": ["First paragraph.", "Second paragraph."],
          "quote": "Optional pull quote, set large between rules",
          "quote_by": "Optional name under the quote",
          "why": "Optional 'Why it matters' note, shown in a box"
        }
      ]
    }
  ]
}
```

- `date` (required) names the output files.
- Sections appear in the order given. The first is treated as the front page:
  its stories lead the front page image, followed by the first story of each
  later section: up to seven headlines, fewer when long ones fill the page
  or a portrait shares it.
- A section with no articles is skipped, so a quiet day does not break the
  build.
- `body` is plain text: a list of paragraphs, or one string with blank lines
  between paragraphs. There is no inline markup.
- `grid` is optional: a number puzzle drawn under the story's text. It is
  nine strings of nine characters, each a digit from 1 to 9 or `.` for an
  empty cell, such as `"6.9..2..."`.
- `quote`, `quote_by` and `why` are optional. A story without them simply has
  no pull quote or boxed note.
- `weather` is optional; without it the front page has no weather line.
  `location` is the name to print and `summary` is printed after it. Leave
  `summary` out, give `latitude` and `longitude` (the example's are New York
  City), and build with `--fetch-weather` to have it filled in from
  [Open-Meteo](https://open-meteo.com/) (free, no key). The fetched forecast
  gives temperatures in Fahrenheit.

Files in `edition/` other than the sample are ignored by git, so your own
editions can sit there without ever being committed.

## What the build produces

- `morning-paper-YYYY-MM-DD.epub`: an EPUB 3 with a cover, a contents page, an
  opening page per section, and one page-broken document per story. Serif,
  black on white, no reliance on colour. It passes epubcheck.
- `frontpage-YYYY-MM-DD.png`: exactly 1236x1648, 8-bit grayscale, not
  interlaced, which is what the Kindle draws on wake. It shows the date, the
  weather if the edition gives any, the top headlines, your portrait if you
  have set one, and a "Today's Paper is ready" banner.

### Finding your way around

Nobody should get lost in a paper, so every page carries its own way back:

- **The contents page** ("Inside today") comes straight after the cover and
  lists every section and headline. Tap any of them to go there.
- **The black tab** at the top of a page names where you are ("Front Page, 2
  of 3"). Tapping it goes up one level: from a story to its section, from a
  section to the contents.
- **The bar at the foot of every story** has three large targets: Contents,
  the section, and Next. The line above it names the next story. After the
  last story of a section, Next opens the following section; the last page of
  the paper says so and offers the way back.

These are ordinary links, which KOReader follows on a tap by default. They
were checked by tapping each one in KOReader's desktop build.

The EPUB cover is the same front page drawn at 1194x1536, the space KOReader
gives a page on this panel with default settings, so it is shown pixel for
pixel.

`--keep-days N` deletes old files from the build's output directory. The front
page uses whatever fontconfig resolves `serif` to on the build machine.
