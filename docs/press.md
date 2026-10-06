# Running and asking the Press

The [README](../README.md) covers the one-line install. This page is the reference:
running the Press by hand, what each run does, its HTTP addresses, the `mnn`
command, and running without Docker.

## The installer

[install.sh](../install.sh) asks no questions. It:

- installs Docker with the compose plugin from Docker's apt repository, if
  either is missing;
- puts the Press in `~/mnn`, or updates the copy already there;
- writes `~/mnn/.env` with this machine's time zone, unless a `.env` exists;
- builds and starts the Press, and waits until it answers;
- prints the Press's address and what to do on the Kindle next.

It uses sudo only where root is needed: to install packages (Docker, and git
or curl if missing), to start the Docker service, and to run Docker in a
session that is not in the `docker` group. When it installs Docker it adds the
current user to that group, which takes effect at the next login.

Run it again at any time to update and restart the Press. If the Press already
runs on the machine from another directory, such as a clone started by hand,
the installer stops and leaves that copy alone. It was tested in Debian and
Ubuntu containers, not yet on a real Pi.

To install on a port other than 8484, end the line with
`| sh -s -- --port 9000`; `--help` lists the rest. The installer never changes
a `.env` that exists, so after the first run the port, like every other
setting, is edited in `~/mnn/.env` and applied by running the installer again.

## Run your own Press

These are the steps by hand, in place of the [installer](#the-installer) and not
after it. They need Docker with Compose on a machine on the same network as
the Kindle. On a Press the installer set up, the settings and commands below
apply in `~/mnn`, where `.env` already exists: edit that file instead of
copying `.env.example` over it.

```sh
git clone https://github.com/JoshuaHayesWasHere/MNN.git && cd MNN
make up
```

`make up` is `docker compose up -d --build`; every `make` command here is a
thin wrapper, and `make help` lists them.

That is the whole Press, and it needs no settings. `press-server` answers the
Kindle on port 8484. `press-fetch` prints a first paper from the default feeds
as soon as it starts, then a new one at 06:40 each morning (trying again every
ten minutes until 09:00 if nothing could be printed). Both restart with the
machine. Everything they keep is in one volume, `press-data`.

Then point a Kindle at it. In KOReader's terminal, type one line:

```sh
curl http://<press-address>:8484/kindle/install.sh | sh
```

From then on the Kindle wakes itself five minutes after the edition time,
downloads the paper into KOReader's library and draws the front page. If the
paper is late or a download fails it looks again every ten minutes, for a
bounded number of tries. It runs
whatever the Press serves, over plain HTTP: see
[kindle/README.md](../kindle/README.md). To read a paper without installing
anything, add the OPDS catalog `http://<press-address>:8484/opds` in KOReader.

Every setting is optional and lives in `.env` (`cp .env.example .env`): `TZ`
(so 06:40 means your 06:40; the default is UTC), `EDITION_TIME`, `PRESS_PORT`,
`PRESS_CONFIG` and `PRESS_INBOX` (to mount other directories), `STAGE_URL`
with `STAGE_TOKEN` to print a staged edition instead, `MUSE_MESSAGES` to have
the Press [tell your Muse](muse.md#messages-to-muse) how the morning went,
`MUSE_CARD_URL` to have it ask Muse to show [the desk card](muse.md#the-desk-card),
and `PRESS_TOKEN` to allow reprints over HTTP (see
[Asking the Press](#asking-the-press)).

Useful commands:

```sh
make logs       # follow the fetcher
make status     # did today's paper print, and has the Kindle collected it?
make health     # is the Press itself working?
make reprint    # print today's paper again
curl http://<press-address>:8484/api/status
curl http://<press-address>:8484/api/health
```

## The image

One image (`mnn-press`, about 64 MB) runs every service; only the command
differs. It is Python slim plus Pillow, ebooklib, and four Noto Serif font
files. It runs as a non-root user, contains no compilers, and builds for
arm64 and amd64 with nothing architecture-specific. The default
`sources.toml` is baked in; no setting or secret is. Those come from `.env`
at run time, and your own sources from the mounted `config/`.

## The Press

```sh
uv run mnn-press fetch --fetch-weather
```

One run gets today's edition and prints it. With `STAGE_URL` unset, the
edition comes from the sources:

- **Already printed since the last edition time (`--at`, 06:40):** exits 0
  and does nothing. A new install has printed nothing yet, so it prints at
  once.
- **A paper:** runs every source, assembles the sections that came back,
  builds the EPUB and front page, checks them against what the Kindle needs,
  moves them into the server's data directory with an atomic rename, prunes
  old editions, and writes `press/report.json`. Sections whose source failed
  are left out and named there.
- **Nothing to print:** every section failed or was empty, `sources.toml`
  could not be read, or the build failed. Yesterday's paper stays exactly
  where it was, the report says why, and the exit status is 1. The next run
  tries again.

`--force` prints again regardless, which is what `paper_rebuild` and
`POST /api/rebuild` do.

With `STAGE_URL` set, one run reads the pointer and does one of three things:

- **Nothing new:** exits 0 and prints nothing.
- **A new edition:** downloads it (checking size and hash), builds the EPUB
  and front page, checks them against what the Kindle needs (a real EPUB, a
  PNG that is exactly 1236x1648 8-bit grayscale), moves them into the
  server's data directory with an atomic rename, prunes editions more than 14
  days older than the newest (`--keep-days`), and sends a receipt.
- **A failure:** yesterday's paper stays exactly where it was, the receipt
  says what went wrong, and the exit status is 1. A download that failed is
  tried again on the next run; an edition that would not build is not retried
  until it changes (or you pass `--force`).

If staging itself cannot be read, there is no edition to pin a receipt to. The
run exits 1 and notes the reason in `press/report.json` (`"mode": "staging"`,
`"status": "failed"`), which goes back to `ok` the next time staging answers.

The receipt, as the edition's author reads it from `{STAGE_URL}/receipt`:

```json
{
  "edition_date": "2026-10-05",
  "status": "ok",
  "build_ok": true,
  "error": null,
  "built_at": "2026-10-05T06:40:09-04:00",
  "files": { "epub": { "name": "morning-paper-2026-10-05.epub", "size": 90318 },
             "frontpage": { "name": "frontpage-2026-10-05.png", "size": 83924 } },
  "kindle_downloaded_at": "2026-10-05T06:45:31-04:00"
}
```

`kindle_downloaded_at` starts as `null`. When the Kindle logs that it has the
edition, the server fills it in and sends the receipt
again, so one place answers "did this morning's paper arrive?".

Without staging the same receipt is kept in the data directory
(`press/receipt.json`) and sent nowhere.

The Press wraps this in three commands, run as `mnn-press <name>`:
`paper_status`, `paper_rebuild` and `paper_health`. In both modes they say
whether today's paper printed and reached the Kindle; when the Press writes
its own paper they also list each section and why any was left out.

### Asking the Press

Three questions have their own HTTP addresses on the server, so a shell, cron,
home automation or a Muse can ask them without touching Docker:

| Question | Address | Command |
| --- | --- | --- |
| Did the paper land? | `GET /api/status` | `mnn status` |
| Is the Press healthy? | `GET /api/health` | `mnn health` |
| Reprint it | `POST /api/rebuild` | `mnn rebuild` |

Status and health are read-only and carry status only: dates, times, section
names, story counts and error messages, never a headline or a story.

**Health** says whether the Press is fine and, if not, what is wrong:

```json
{
  "ok": false,
  "problems": [
    { "code": "edition_missing",
      "message": "today's edition has not been printed (it was due at 06:40); the newest is 2026-10-04" }
  ],
  "warnings": [],
  "checked_at": "2026-10-05T08:00:00-04:00",
  "mode": "sources",
  "today": "2026-10-05",
  "edition_date": "2026-10-04",
  "edition_time": "06:40",
  "fetcher_seen_at": "2026-10-05T07:58:12",
  "free_mb": 20480,
  "rebuild_enabled": false
}
```

It answers 200 when `ok` is true and 503 when it is not, with the same JSON
either way, so `curl -f` and a script reading `ok` agree. A problem means the
paper is not arriving, or soon will not:

| `code` | Means |
| --- | --- |
| `no_edition` | Nothing has been printed yet (normal for the first moments of a new install) |
| `edition_missing` | Today's edition is not there 15 minutes after the edition time |
| `print_failed` | The last print failed; the message carries the reason |
| `fetcher_stopped` | The fetcher has not signed in for 15 minutes (or, run from a timer, did not run at the edition time) |
| `disk_low` | Less than 200 MB free where papers are kept |

A warning is worth a look, but the paper printed: `section_left_out` (a
source failed, with the reason) and `receipt_unsent` (staging could not be
told).

**Rebuild** reprints today's edition: it runs the fetcher with `--force` and
answers when it is done, which can take a few minutes. A reprint runs the
fetcher with its default options rather than any flags the scheduled fetcher
was started with, so if you raised `--keep-days` there, a reprint still prunes
editions more than 14 days older than the newest. It is off until the Press
has a token. Set `PRESS_TOKEN` in `.env` (`openssl rand -hex 32` makes a good
one) and run `docker compose up -d` (without Docker, put it in
`~/.config/morning-paper/env` and restart the server); then:

```sh
curl -X POST -H "Authorization: Bearer $PRESS_TOKEN" http://<press-address>:8484/api/rebuild
```

```json
{ "ok": true, "today": "2026-10-05", "edition_date": "2026-10-05",
  "detail": "printed 2026-10-05 from 4 of 4 sections: morning-paper-2026-10-05.epub, frontpage-2026-10-05.png" }
```

| Reply | Means |
| --- | --- |
| 200, `"ok": true` | Reprinted |
| 500, `"ok": false` | The reprint failed; `detail` says why, and the previous paper is still served |
| 401 | The token is missing or wrong |
| 403 | The Press has no `PRESS_TOKEN`, so rebuild is off |
| 409 | A rebuild is already running |

The token is only ever read from the `Authorization` header. It is not
written to any log (the request log leaves out query strings, in case a client
puts it there by mistake), and status and health never include it (health only
says whether rebuild is on).

### The `mnn` command

[`mnn`](../bin/mnn) wraps those three addresses. It is one file that needs only
Python 3.9 or later and its standard library, which Debian and Raspberry Pi
OS already have, so it can be copied to any machine on the LAN:

```sh
sudo install -m 755 bin/mnn /usr/local/bin/mnn
mnn status
```

```
Today's paper (Monday, October 5) is printed, and the Kindle collected it at 06:45.
```

`mnn health` prints `The Press is fine.` or lists what is wrong. `mnn rebuild`
reprints and says how it went. Add `--json` to get the Press's JSON instead of
sentences. When `mnn` itself stops (exit status 3 or 4 below, or 2 over its
settings file or the Press address), `--json` prints the reason as
`{"ok": false, "error": "..."}`.

Its settings come from a file, never from the environment, so it behaves the
same from cron, a systemd unit, or anything else that starts a command with
next to nothing set. It reads the first of these that exists:

| File | For |
| --- | --- |
| the one named by `--config FILE` | Trying something out |
| `~/.config/mnn/config` | One account |
| `/etc/mnn/config` | Every account on the machine |

```
# where the Press answers (default: http://127.0.0.1:8484)
url = http://127.0.0.1:8484
# the Press's PRESS_TOKEN; only `mnn rebuild` uses it
token = a-long-random-string
```

A file named with `--config` has to exist, and is then the only one read.
`--url URL` names the Press for one run, in place of the file's `url`.

With no file, `mnn status` and `mnn health` work on the Press's own machine
against the default address. Once the file holds a token it is a password:
make it readable only by the account that runs `mnn` (`chmod 600`, or `640`
with that account's group for the file in `/etc`). No root is needed to read
it or to run `mnn`.

`mnn` talks to the Press directly: a proxy named in the environment
(`http_proxy` and the like) is ignored, like the rest of the environment.

| Exit status | Means |
| --- | --- |
| 0 | Yes: today's paper is printed, the Press is fine, or the reprint worked |
| 1 | No: not printed today, something is wrong, or the reprint failed |
| 2 | `mnn` was used wrongly, or its settings file could not be read |
| 3 | The Press could not be reached, or did not answer as the Press does |
| 4 | The Press refused: rebuild is off there, or the token is missing or wrong |

So a cron line that speaks up only when something is wrong is
`mnn health >/dev/null || mnn health`.

## The server

| Endpoint | Returns |
| --- | --- |
| `GET /opds` | OPDS 1.2 feed of every stored EPUB, newest first |
| `GET /paper/<file>.epub` | One EPUB |
| `GET /paper/latest.txt` | File name of the newest EPUB, as plain text |
| `GET /api/display` | Compact JSON: `image_url`, `filename`, `refresh_rate`, `edition_date`, `paper_url` |
| `GET /frontpage.png` | The newest front page |
| `GET /api/status` | JSON: `mode` (`sources` or `staging`), `today`, `edition_date`, `last_print`, `kindle` (its last download), and in sources mode `last_run`, `sections` and `failed_sections` |
| `GET /card.jpg`, `GET /card.rgb565` | That status as a small black and white picture: [the desk card](muse.md#the-desk-card) |
| `GET /api/health` | JSON: `ok`, `problems`, `warnings`; 503 when something is wrong. See [Asking the Press](#asking-the-press) |
| `GET /kindle/install.sh` | The Kindle installer, with this server's address filled in. With `?download`, a browser saves it as `Install MNN.sh` |
| `GET /kindle/manifest` | Plain text for the Kindle: the client, the paper and the screens with their SHA-256, whether the edition is `fresh`, `late` or `missing`, and when to wake next |
| `GET /kindle/<name>` | Any file the manifest names, and `bootstrap.sh` |
| `POST /api/rebuild` | Reprints today's edition. Needs `Authorization: Bearer <PRESS_TOKEN>`; off while that is unset |
| `POST /api/log` | Appends each line of the body to `server.log`; replies 204. The Kindle's lines also update its record in `press/kindle.json` |

Settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `--data-dir`, `PAPER_DATA_DIR` | `~/.local/share/morning-paper` | Where papers and `server.log` are kept |
| `STAGE_URL`, `STAGE_TOKEN` | unset | Optional: where to send the receipt when the Kindle reports a download |
| `MUSE_MESSAGES` and friends | `off` | Optional: [messages to Muse](muse.md#messages-to-muse) |
| `MUSE_CARD_URL` | unset | Optional: ask Muse to show [the desk card](muse.md#the-desk-card) after each print |
| `PRESS_TOKEN` | unset | Optional: the token `POST /api/rebuild` asks for. Unset, rebuild is off |
| `--host`, `--port` | `0.0.0.0`, `8484` | Where to listen |
| `--refresh-rate` | `3600` | Seconds the Kindle sleeps between polls |
| `--edition-time HH:MM` | unset | Tell the Kindle to sleep until the next edition instead |
| `--base-url` | from the request | Address the Kindle should call back on |

With `--edition-time` set, once today's paper is out the Kindle is told to
sleep until five minutes after tomorrow's edition time: one wake a day instead
of twenty-four. If the paper is late, `/api/display` gives `--refresh-rate` as
the wait. The Kindle's manifest gives ten minutes instead, and the Kindle
bounds its own retries: at most fourteen in a row at that interval, then one
every three hours, and when the Press cannot be reached, three an hour apart
and then one every six hours (see [kindle/README.md](../kindle/README.md)).
Without `--edition-time` the manifest names `--refresh-rate` as the next wake,
and while the newest paper is not today's the Kindle retries sooner than that,
for the same fourteen tries.

[Health](#asking-the-press) goes by the same time. Without `--edition-time` it
cannot tell that today's paper is late: `edition_missing` is reported only
once the newest edition is two days old, and a fetcher run from a timer is
given 25 hours before `fetcher_stopped`.

## Running without Docker

Compose is the documented path. If you would rather run on bare metal, you
need [uv](https://docs.astral.sh/uv/) and `deploy/systemd/` has user units: the
server as a service, and a timer that runs the fetcher on the same
schedule the container uses. They assume the repository is at `~/repos/MNN`
and uv is `/usr/bin/uv`.

```sh
uv sync
mkdir -p ~/.config/morning-paper ~/.config/systemd/user

# Optional: your own sections and feeds
cp sources.toml ~/.config/morning-paper/sources.toml

# Optional: a folder for the inbox source
mkdir -p ~/.config/morning-paper/inbox

# Optional: print a staged edition instead of running sources
cat > ~/.config/morning-paper/env <<'ENV'
STAGE_URL=https://stage.example.com
STAGE_TOKEN=your-staging-token
ENV
chmod 600 ~/.config/morning-paper/env

ln -s ~/repos/MNN/deploy/systemd/morning-paper-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now morning-paper-server.service morning-paper-fetch.timer
loginctl enable-linger "$USER"    # keep them running when you are logged out
```

After upgrading an install that runs this way, restart the server and run the
fetch unit once instead of waiting for the timer:

```sh
systemctl --user restart morning-paper-server.service
systemctl --user start morning-paper-fetch.service
```

[Health](#asking-the-press) reports `fetcher_stopped` until the fetcher has
signed in, which it does on every scheduled run. With today's paper already
printed, that run prints nothing.

Every script also runs by hand: `uv run mnn-press server`, `uv run mnn-press fetch`,
`uv run mnn-press stage`, `uv run mnn-press stage-put edition/sample.json`.

## What is in the repository

| Path | What it is |
| --- | --- |
| `Makefile` | The everyday commands: `make up`, `make status`, `make test`; `make help` lists them |
| `install.sh` | The one-line [install](../README.md#install) of the Press |
| `Dockerfile`, `docker-compose.yml` | The Press as a container, and the whole stack in one file |
| `.env.example` | The optional settings; copy it to `.env` to change one |
| `sources.toml` | The default paper |
| `config/` | Where your own `sources.toml`, source modules and front page portrait go |
| `inbox/` | Where section files are left for the inbox source to print |
| `edition/sample.json` | The sample edition: invented stories from an invented town |
| `src/mnn/` | The Python package; `mnn-press <command>` runs any part of it |
| `src/mnn/press_fetch.py` | `fetch`: get the edition, build, hand to the server, record how it went |
| `src/mnn/press_sources.py` | Reads `sources.toml`, runs each source on its own, assembles the edition |
| `src/mnn/sources/` | The built-in sources: `rss`, `inbox` and `ask` |
| `src/mnn/build_paper.py` | `build`: edition file in, EPUB and front page PNG out |
| `src/mnn/server.py` | `server`: the LAN server the Kindle talks to |
| `src/mnn/card.py` | The desk card: the paper's state as a small picture for a display board |
| `src/mnn/staging.py` | The staging contract's client, and the Press's local records |
| `src/mnn/stage.py`, `stage_put.py` | `stage` and `stage-put`: the staging server, and the upload to it |
| `src/mnn/muse.py` | `muse`: messages to a paired Muse gadget, and the request to show the desk card |
| `src/mnn/press_commands.py` | `paper_status`, `paper_rebuild` and `paper_health` |
| `bin/mnn` | One standalone file to ask the Press over HTTP: `mnn status`, `mnn health`, `mnn rebuild` |
| `kindle/` | What the Kindle runs: a one-line installer, a bootstrap and the client |
| `integrations/muse/` | The Muse skill, the gadget scaffold, and the compose file that mounts the gadget socket |
| `deploy/systemd/` | For running without Docker: the server, and a fetch timer |
| `docs/` | These pages |
| `tests/` | `make test`; feeds come from local fixtures, never the network |
