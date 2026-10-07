# Sources

A source writes one section of the paper. The Press runs them all at edition
time and prints whatever came back.

## The editor

A section with `source = "editor"` is chosen and written by Claude. The feed
reader collects more stories than the section needs; the editor picks the
ones that matter to you, puts the most important first, and rewrites each as
a short article with a headline, a standfirst and a line on why it matters.

```toml
[[section]]
title = "Front Page"
source = "editor"
feeds = [
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://www.theguardian.com/world/rss",
]
stories = 4
brief = "I follow AI research and local transport. Skip sport and celebrity."
timeout = 180
```

The default paper's front page is already one. To turn it on, put a key in
`.env` and restart (`make restart`):

```sh
ANTHROPIC_API_KEY=sk-ant-...
```

| Key | Default | Meaning |
| --- | --- | --- |
| `feeds` | none | Where the stories come from, as for any feed section |
| `stories` | `5` | The most stories printed |
| `brief` | none | What you care about, in your own words, 2000 characters at most |
| `candidates` | `24` | How many stories the editor chooses from, 60 at most |
| `model` | `claude-opus-5-5` | The Claude model that edits |
| `effort` | `medium` | `low`, `medium` or `high`: how much thought goes into the edit |
| `timeout` | `60` | Seconds for the whole section. The feeds get two fifths and the editor the rest, so give an editor section `180` |

`feed_timeout` and `max_age_days` work as they do for a feed section.

- **The brief is the whole setting.** Write it the way you would tell a
  person: what you follow, what you are tired of, what you want more of.
  Change it and tomorrow's paper changes.
- **It never costs you the section.** With no key, or when the request
  fails, is declined, or comes back unusable, the section is printed straight
  from its feeds, as a feed section would be, and the report says why
  (`printed from the feeds, unedited: ...`).
- **It only edits what the feeds said.** The editor sees each story's
  headline and summary, not the article, and is told to add nothing that is
  not there. It is still a model summarising a summary: treat it as an edit,
  not a source, and follow the link when a story matters.
- **Links and bylines are always the feed's.** The editor refers to a story
  by number and cannot supply an address or a publication of its own. Text it
  writes about a story it was not shown is dropped.
- **What leaves your network** is your brief and the headline, summary and
  feed name of each candidate story, sent to Anthropic's API. See
  [Security](security.md).
- **What it costs** is one request a day per editor section, of a few
  thousand tokens. `effort = "low"` and fewer `candidates` make it cheaper;
  a different `model` changes the price.
- **Not yet run against the live API here.** The request was written against
  the Anthropic SDK and is tested with a stand-in for Claude.

## The puzzle page

A section with `source = "sudoku"` prints the day's sudoku, with its solution
on the next page. The default paper ends with one.

```toml
[[section]]
title = "Puzzles"
source = "sudoku"
difficulty = "medium"      # easy, medium or hard
```

The puzzle is made from the date, so a reprint carries the same one, and
every puzzle has exactly one solution. Nothing is fetched. An e-reader shows
the grid; there is no writing on the page, so the solving is done on paper or
in your head.

## Choosing your feeds

The default paper is [`sources.toml`](../sources.toml), baked into the image: a
few general-interest public feeds. To change it, copy it into `config/` and
edit the copy:

```sh
cp sources.toml config/sources.toml
```

```toml
title = "The Harbour Gazette"        # optional masthead

[weather]                            # optional: the front page forecast
location = "Lisbon"
latitude = 38.72
longitude = -9.14

[[section]]
title = "Front Page"
feeds = ["https://feeds.bbci.co.uk/news/rss.xml"]
stories = 3

[[section]]
title = "World"
feeds = [
    "https://www.theguardian.com/world/rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
]
stories = 5
```

One `[[section]]` per section, printed in the order written; the first leads
the front page. `title` names it, `feeds` lists RSS or Atom addresses, and
`stories` is how many to print. Stories are taken from the feeds in turn, so
one feed that is down is covered by the others. Optional per section:
`timeout` (seconds the whole section may take, default 60), `feed_timeout`
(seconds each feed may take, default 20) and `max_age_days` (skip older
items, default 3).

Only what the feed itself carries is printed: its headline, its summary
reduced to plain text, the feed's name as the byline, and a link back. The
linked article is never fetched.

## Where your files live

| On the host | In the container | What it is |
| --- | --- | --- |
| `./config/sources.toml` | `/config/sources.toml` | Your sections and feeds; replaces the default |
| `./config/<name>.py` | `/config/<name>.py` | A source of your own |
| `./config/portrait.png` | `/config/portrait.png` | A picture for the front page |
| `./config/portrait-rain.png` | `/config/portrait-rain.png` | The picture used instead on rainy days |
| `./sources.toml` | `/app/sources.toml` | The default, used when you have none |
| `./inbox/<date>.json` | `/inbox/<date>.json` | A section left for [the inbox](#the-inbox) |
| `./inbox/answers/` | `/inbox/answers/` | Where a Muse writes what it was [asked at print time](muse.md#asking-muse-at-print-time) |

`docker compose` mounts `./config` (next to `docker-compose.yml`) read-only at
`/config`; set `PRESS_CONFIG` in `.env` to mount a different directory.
Everything in `config/` except its README is ignored by git. The Press reads
these files fresh before every print, so save your edit and it is in
tomorrow's paper: no rebuild, no restart. To see it now, run `paper_rebuild`.

Without Docker the same files go in `~/.config/morning-paper/`
(`PRESS_CONFIG_DIR`, or `--config-dir`).

## A front page portrait

Put a picture named `portrait.png` in `config/` and the front page prints it
beside the headlines. Add `portrait-rain.png` and that one is printed instead
on rainy days. There is nothing to set in `sources.toml`; the two file names
are the whole setting. Remove the files and the front page goes back to
three rows of headlines.

- **Any ordinary picture works.** `.png`, `.jpg`, `.jpeg` and `.webp` are
  read, and capitals in the file name do not matter (`Portrait.JPG` is
  fine). The Press crops it square around the head and shoulders (the middle
  of the picture, leaning towards the top), fits it to the space, and turns
  it into pure black and white with a Floyd-Steinberg dither inside a thin
  black border, which is what e-ink draws sharpest. A large photo straight
  from a camera is fine.
- **The picture is fitted to the space the page has that day.** The space
  is a square beside the headlines, about 520 pixels across on an ordinary
  day. It is smaller under a long lead story, and not the same on the wake
  screen as on the cover inside the EPUB, so there is no exact size to
  prepare a picture at: give the Press a picture at least that large and it
  does the fitting. Only a picture that already matches the day's space to
  the pixel and is already pure black and white is printed as it is; any
  other is converted, one you have dithered yourself included.
- **A rainy day** is one where the forecast for your `[weather]` location
  gives a 30% chance of rain or more. With no `[weather]` coordinates, no
  answer from the forecast service, or no `portrait-rain` picture, the plain
  portrait is printed. A paper whose weather line was written by hand (a
  `summary` in the edition) has no forecast to go on, so it gets the plain
  one too. A `portrait-rain` picture with no plain one beside it is printed
  on rainy days only; on any other day the paper has no portrait and the log
  says the picture is waiting for rain.
- **A picture that cannot be used is skipped.** A rainy-day picture that
  cannot be read gives way to the plain one, and when no picture can be read
  the paper is printed without a portrait. Each gets a warning in the log.
  So does any other file in `config/` whose name starts with `portrait`
  (`portrait.heic`, `portrait.png.png`, `portrait-rainy.jpg`), since that is
  usually a portrait the Press cannot use.
- **With staging** the portrait still comes from the Press's own `config/`;
  it is not part of the staged edition.

Building by hand, point the builder at the directory:
`uv run mnn-press build edition/sample.json --config-dir config`. The forecast
is fetched only with `--fetch-weather`, so without that flag every day gets
the plain portrait.

## Writing a source

A source is a Python module with one function. Save this as
`config/tides.py`:

```python
def produce(date, config):
    return {
        "title": config["title"],
        "articles": [
            {"title": f"High water at {config['station']}",
             "body": ["First paragraph.", "Second paragraph."]},
        ],
    }
```

and give it a section:

```toml
[[section]]
title = "Tides"
source = "tides"
station = "Cascais"
```

`date` is the edition's `datetime.date`. `config` is that `[[section]]` table
as a dict, so any key you add reaches your code. The return value is one
section in the [edition format](editions.md#writing-an-edition): an article needs a
`title` and a `body`, and may carry `deck`, `source` (a byline), `url`,
`quote`, `quote_by` and `why`. A section may also return `notes`, a list of
strings recorded with its outcome. A source with nothing to say today returns
no articles and `empty`, a string saying why: its section is left out without
counting as a failure.
`source` defaults to `rss`; a module in `config/` wins over a built-in one of
the same name (the others are [`editor`](#the-editor), [`sudoku`](#the-puzzle-page),
[`inbox`](#the-inbox) and
[`ask`](muse.md#asking-muse-at-print-time)). The image carries the standard library,
Pillow, ebooklib and the Anthropic SDK, and a source can import the modules beside it.

## The inbox

The inbox is a folder. Leave a section file in it and the next paper prints
it; leave nothing and the section is simply not there. Anything that can write
a file can be one of the paper's writers: a Muse (through its gadget's
`file.write`), a script, a cron job, or you with a text editor. It needs no
staging server and no token.

Turn it on by giving it a section in `config/sources.toml`, wherever in the
paper you want it:

```toml
[[section]]
title = "From the Inbox"
source = "inbox"
```

A file is one section of the [edition format](editions.md#writing-an-edition), as JSON,
named for the day it is meant for: `2026-10-05.json`, or
`2026-10-05-label.json` when more than one writer leaves a file. A label may
only use `a-z`, `A-Z`, `0-9`, `.`, `_` and `-`: no spaces and no accented
letters. For example, `inbox/2026-10-05-muse.json`:

```json
{
  "title": "From Your Muse",
  "articles": [
    {
      "title": "Three things for Monday",
      "deck": "Optional one or two sentence standfirst",
      "source": "Muse",
      "body": [
        "The dentist is at 14:30, and the car is due back by five.",
        "Rain from mid-afternoon, so the walk is better before lunch."
      ],
      "why": "Optional note, shown in a box"
    }
  ]
}
```

`title` is optional; without it the section takes its title from
`sources.toml`. Each article needs a `title` and a `body` and may carry
`deck`, `source`, `url`, `quote`, `quote_by` and `why`, exactly as in an
edition. Text is plain: there is no markup.

- **Only a fresh file is printed.** A file prints when its name is dated the
  day of the paper or the day before, so one written the previous evening, or
  by a writer in another time zone, is not missed. `max_age_days` on the
  section changes the window (default 1; 0 is today only; 7 at most). A file
  dated in the future waits for its day. An older one is left alone and noted
  in the report.
- **Nothing waiting, no section.** The rest of the paper prints, and the
  report lists the section as `empty`, not as a failure.
- **A file is printed once.** When it is printed the Press moves it to
  `inbox/printed/<paper's date>/`, so it cannot appear in a later paper.
  Printing the same day's paper again (`paper_rebuild`) reads it back from
  there, and a corrected file left under the same name replaces it. If the
  corrected file cannot be printed, the copy already printed is used. Those
  folders are deleted 14 days after their paper (`keep_days`).
- **A bad file is skipped.** A file that is not JSON, is not a section, is
  larger than 256 KiB, is not a regular file, or is not named as above is
  left where it is, logged, and named in the report. Other fresh files still
  print, and so does the rest of the paper.
- **Several files share the section,** newest first, up to `stories` stories
  (default 10).
- **Write the file in one step.** A Muse's `file.write` replaces the file
  atomically. A script should write to a name that does not end in `.json`
  and rename it, so the Press never reads half a file.

`docker compose` mounts `./inbox` (next to `docker-compose.yml`, beside
`config/`) at `/inbox`; set `PRESS_INBOX` in `.env` to mount a different
directory. The Press must be able to write to it (it runs as uid 1000 in the
container), since moving a printed file is what stops it being printed twice:
a file it cannot move is not printed. Everything in `inbox/` except its README
is ignored by git. Without Docker the folder is `~/.config/morning-paper/inbox`
(`PRESS_INBOX_DIR`), and `folder` on the section points it somewhere else.

**The inbox is the most personal content the paper can carry.** A feed is
public; what a Muse writes about your day is not, and it travels from wherever
it was written into your home. Once printed it is part of the paper, and the
Press serves its papers to anyone on the home network without a password (see
[Security](security.md)). Its first headline can also appear on the front page
image the Kindle shows on wake. Put in the inbox only what you would leave on
the kitchen table. What you ask a Muse to write is set in your Muse, never in
this repository.

## When a source fails

Every source runs in a process of its own, all at once, each against its own
`timeout`, and every feed against its own `feed_timeout` (an `ask` section
runs once the others are in). A source that
raises, runs out of time, or returns nothing loses its own section; the rest
of the paper prints. A feed that cannot be read is skipped, noted by its place
in the section's `feeds` list and its address, and the section is filled from
its other feeds. If every section fails, nothing is printed,
yesterday's paper stays where it was, and the Press tries again at the next
check.

What happened to each section is in `press/report.json` in the data directory
(`/data/press/report.json` in the container), rewritten on every run:

```json
{
  "mode": "sources",
  "edition_date": "2026-10-05",
  "status": "partial",
  "error": null,
  "ran_at": "2026-10-05T06:40:03-04:00",
  "config": "/config/sources.toml",
  "sections": [
    { "title": "World", "source": "rss", "status": "ok", "stories": 5,
      "headlines": ["..."], "error": null, "seconds": 0.4,
      "notes": ["feed 2, https://example.com/rss: HTTP 503"] },
    { "title": "Tides", "source": "tides", "status": "failed", "stories": 0,
      "headlines": [], "error": "timed out after 60s", "seconds": 60.0, "notes": [] }
  ]
}
```

`status` is `ok` when no section failed, `partial` when some failed and were
left out, and `failed` when nothing was printed (`error` says why). A section
whose own `status` is `empty` had nothing to print today (an inbox with no
fresh file, a question Muse did not answer in time); the reason is the first
of its `notes` and it is not a failure.
`config` is the `sources.toml` that was read. The same record, without the
headlines, is served at `GET /api/status` and reported by `paper_status` and
`paper_health`.
