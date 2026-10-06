# MNN: Muse News Network, the vision

A newspaper of your own on a jailbroken Kindle Paperwhite 11. Every morning
the Kindle wakes to a front page, and the day's full edition is already there,
downloaded, ready to read with coffee.

You decide what is in it. The project supplies the press, not the newsroom.

## The two planes

**This repository is a template, not a database.** It contains the Press,
its built-in feed reader, the builder, the staging server, the Kindle-side
scripts, and one sample edition. Nothing in it is anyone's actual news. Real
editions, EPUBs, and front pages are never committed. Clone it and you have a
paper the same morning, then make it your own by editing one file of feeds.

**The data plane is a directory, not a git history.** Each morning the Press
runs its sources, assembles the edition, builds the EPUB and front page, and
moves them into a data directory outside the repository, from which it serves
them to the Kindle. No commits, no pushes, no repo churn. The Kindle only ever
talks to the Press on the home network.

```
sources (in the Press) -> assembler -> build -> data directory -> server -> Kindle
```

**Sources live in the Press by default; staging is the upgrade.** A source is
a small module that writes one section. The default one reads RSS feeds, so
a fresh install prints real headlines with nothing to configure. A reader's
own sources sit beside the Press in a mounted directory. When the content
lives somewhere the Press cannot reach, a remote author writes the whole
edition and stages it, and the Press prints that instead:

```
remote author -> staging -> the Press -> Kindle        (receipt travels back)
```

## The paper itself

The sections are yours: world news, a local beat, a hobby, a weekly long read.
An edition is a list of sections and stories, and the Press prints whatever it
is given, in the order given.

A few things hold for any good morning paper, and the design leans on them:

- **One lead story.** The front page has a single headline that matters most,
  and a handful of others beneath it.
- **Short sections.** A quiet section is skipped. A real paper does not pad.
- **A calm voice.** It is read before coffee. Interesting, never overwhelming.
- **The front page is a summary.** Date, weather, and headlines share it; the
  full articles live in the EPUB for KOReader.

Sections can run on their own cadence. A daily beat, a twice-weekly one, and a
Sunday feature can share one paper, because each morning's edition simply
carries what is due that day.

## Reliability principles

- Every content source is an independent producer, run on its own with its
  own time limit. The assembler merges whatever showed up; a failed source
  costs its section, never the paper, and the Press records which sections
  failed and why.
- Printing is atomic: a half-built edition is never visible to the Kindle,
  and yesterday's paper stays served until today's is complete.
- The Kindle logs every download back to the Press, so "did this morning's
  paper arrive?" is a fact, not a hope. A missed morning is reported, not
  discovered.
- If everything fails, the Kindle shows yesterday's paper. The failure mode
  is a stale newspaper, never a broken device.

## Iteration principles

Design and content are separate. The edition file carries words and no
layout, so the look can change without touching a single story, and the
stories can change without touching the look. Judge a design from previews
rendered at true size (1236x1648): the Kindle is a reading device, not a
design workbench. Every knob worth turning (typography, hierarchy, section
rhythm) respects the medium: e-ink rewards restraint, pure black and white,
and whitespace.

## Who runs what

By default, two pieces:

- **The Press writes, formats and serves.** A container, not a machine: it
  runs identically on a Pi, a NAS, a mini-PC, or a cloud box, so the Press
  is never stranded on dying hardware. Each morning it runs its sources (the
  feeds in `sources.toml`, plus any modules the reader has added), assembles
  the edition, renders the EPUB and front page (this is its craft: builder,
  theme, typography), and serves them locally.
- **The Kindle reads.** Outbound only, on the LAN: fetch, verify, open in
  KOReader, log the download back to the Press. It stays deliberately
  boring: no cloud pairing, no account to manage.

With staging, the writing moves out and two more pieces join:

- **A remote author writes the edition.** Content that needs access the
  Press does not have is produced where that access lives: a laptop, a
  server, a scheduled job, an assistant. The author emits one edition file
  (content only, no layout) and stages it. It never reaches into the home
  network.
- **Staging is a contract, not a cloud.** It is an HTTPS place holding an
  edition, a pointer to the latest one, and a receipt, behind a bearer
  token. The repository ships a small standard-library staging server that
  runs anywhere; an S3-compatible bucket with a token check in front of it
  satisfies the same contract. One URL and one token to configure: running
  the paper never means adopting someone else's cloud account.
- **The Press fetches instead of writing.** With a staging address set it
  runs no sources: it checks staging each morning, pulls the edition, and
  prints it the same way.

**The receipt travels back.** The Press writes a delivery receipt to staging,
so whatever wrote the edition can confirm the paper arrived instead of
assuming it.

Every hop is outbound or LAN-local. No inbound routes into the home.

## Optional: the Press as a Muse gadget

The Press answers three questions on its own: `paper_status` (is today's
edition printed, which sections made it, and is it on the Kindle?),
`paper_rebuild` (print it again), and `paper_health`. They are plain
commands, and nothing about the paper depends on anything more.

For anyone who uses Muse, the Press can also pair as a Muse gadget through
the Muse Linux Device SDK and expose those three as gadget commands, so "did
the paper land this morning?" can be asked of the Press instead of read from
logs. The SDK is a voice for the Press, not its transport: printing and
delivery work the same with the gadget off. Today this is a sketch; see
[integrations/muse/gadget/README.md](../integrations/muse/gadget/README.md).

Other devices can join in too. Anything that can read `/api/display` on the
LAN can show that the paper is ready.
