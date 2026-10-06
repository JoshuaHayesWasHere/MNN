# Kindle side

The Kindle fetches the paper by itself every morning. You install it once, by
copying one file over USB or typing one line; after that the Press serves
everything the Kindle runs, so there is nothing to update by hand.

> **The Kindle runs whatever the Press serves, over plain HTTP on your home
> network.** On every wake it asks the Press which client script to run,
> downloads it if it is new, and runs it as root. There is no TLS and no
> signature: the Kindle trusts the home network. The SHA-256 checks described
> below catch a broken or half-downloaded file, not a false Press. Anyone on
> that network who can pose as the Press can run commands on the Kindle as
> root. Only install this on a network you trust, and never point it at an
> address outside it.

## What you need

- A Kindle Paperwhite 11, jailbroken, with KOReader installed.
- The Press running on the same network (see the top-level README).
- The Press's LAN address and port. Give that machine a DHCP reservation on
  the router so the address does not change. The examples use
  `192.168.1.50:8484`.

## Install

### Without typing

1. On a computer on the same network, open
   `http://192.168.1.50:8484/kindle/install.sh?download` in a browser. It
   saves a file named `Install MNN.sh`, with the Press's address already in
   it.
2. Plug the Kindle into the computer and copy the file into its `documents`
   folder (or whichever folder is KOReader's home). Eject the Kindle.
3. In KOReader's file browser, long-press `Install MNN.sh` and choose
   **Execute**.

Nothing is shown while it runs. Within a minute or so the newest paper
appears in the file browser, beside a new `Start MNN.sh`; `Install MNN.sh`
can then be deleted.

Download the file by the Press's address on the network, as above, and not
by `localhost` on the Press's own machine: the address in the browser is the
one the Kindle will use. From a clone of this repository on the machine the
Press runs on, `make kindle KINDLE=/path/to/Kindle` does steps 1 and 2 with
the right address.

This is the same **Execute** that `Start MNN.sh` uses after a restart. The
installer run from a file is covered by the tests; this route has not yet
been tried on a Kindle.

### With one line

In KOReader, open the terminal (top menu, tools, **Terminal emulator**) and
type one line:

```sh
curl http://192.168.1.50:8484/kindle/install.sh | sh
```

If the Kindle has no `curl`:

```sh
wget -q -O - http://192.168.1.50:8484/kindle/install.sh | sh
```

Either way, that is the whole install. It creates `/mnt/us/mnn`, records the Press
address there, and starts the bootstrap, which fetches the newest paper
straight away. Installing again is safe: it replaces the bootstrap and
restarts it, which is also how to point the Kindle at a different Press.

## What happens each morning

The Kindle stays an ordinary Kindle. The power button, KOReader and Amazon's
own interface all work as before; nothing of Amazon's is stopped or replaced.
Two small shell scripts run in the background.

1. Each time the Kindle goes into its screensaver, the **client** draws the
   newest front page over the whole screen. When the Kindle is about to
   suspend, the client asks its power service to wake it by its clock (the
   RTC) five minutes after the Press's edition time: 06:45 by default. On a
   charger the Kindle does not suspend; the client simply waits until then.
2. On that wake the client asks the power service to wake the Kindle fully,
   as a press of the power button would, because a clock wake alone leaves
   the radio off. The **bootstrap** (`/mnt/us/mnn/bootstrap.sh`) then
   downloads the Press's manifest: a short plain-text list of the client, the paper and the
   screens, each with its SHA-256, plus when to wake next.
3. If the manifest names a client the Kindle does not have, the bootstrap
   downloads it, checks its SHA-256 against the manifest and that the shell
   can parse it, keeps the last client that completed a wake as
   `client.prev`, and puts the new one in place. If any of that fails it runs
   the last good copy and reports the failure to the Press. A new client that
   fails outright before it has completed a wake is put back to the previous
   one at once, and the previous one does that wake's work, so the Kindle is
   never left asleep with nothing to wake it. One that runs without a
   complaint but does not complete a wake within three is put back as well.
4. The client downloads today's EPUB into KOReader's home folder, where the
   file browser shows it, and the day's screens, checking each against the
   manifest.
5. It logs the download to the Press, which records it as the receipt.
6. The Kindle goes back to sleep by itself when its usual idle time is up
   (about ten minutes), and step 1 puts the new front page on the screen.

If today's edition is not out, the Kindle keeps the one it has, logs that, and
looks again in ten minutes, the retry interval the manifest names. It does the
same when the paper or a screen fails to download or does not match the
manifest. Each of those looks is a full wake that keeps the Kindle up for its
idle time, so they are bounded:

- While the Press answers, the Kindle retries at that interval at most
  fourteen times in a row. With the default edition time that lasts until
  09:05, just past the Press's own last attempt to print. After that it looks
  every three hours, and still wakes at the next edition time when the Press
  says one is coming sooner.
- When the Press cannot be reached at all (it is off, or the Kindle is away
  from home), the Kindle tries again every hour, three times, and then every
  six hours.

One wake that reaches the Press and finds today's paper and its screens in
place starts both counts again, and the Kindle is back to one wake a morning.

It keeps the newest seven papers and deletes older ones, along with their
KOReader `.sdr` folders.

Nothing survives a restart of the Kindle. To start it again, open KOReader's
file browser, long-press `Start MNN.sh` in the home folder and choose
**Execute**. Running the install line again does the same.

The bootstrap is deliberately small and never changes. Everything else is in
the client, so a fix or a new feature in `kindle/client.sh` on the Press
reaches the Kindle on its next wake.

## What it was tried on

A Kindle Paperwhite 11 on firmware 5.18.1, with KOReader open and with it
closed. There the Kindle's clock wake fires about ten seconds early, the radio
rejoins the network within a few seconds of the full wake, and a whole wake
(manifest, paper, receipt) takes under fifteen seconds.

The front page is drawn with FBInk when a build with image support is
installed (`/mnt/us/libkh/bin/fbink`; the copy bundled with KOReader has
none), and with the Kindle's own `eips` otherwise.

## Checking on it

From the Press, `paper_status` shows the Kindle's last successful download
and its last warning:

```sh
docker compose exec press-server python -m mnn paper_status
```

```json
"kindle": {
  "edition_date": "2026-10-05",
  "downloaded_at": "2026-10-05T06:45:31-04:00",
  "last_seen_at": "2026-10-05T06:45:34-04:00",
  "last_warning": null
}
```

Every line the Kindle logs is also in `server.log` in the Press's data
directory, and in `/mnt/us/mnn/mnn.log` on the Kindle. To see what the Kindle
is being told, read the manifest from any machine on the network:

```sh
curl http://192.168.1.50:8484/kindle/manifest
```

On the Kindle, from KOReader's terminal or over SSH:

```sh
sh /mnt/us/mnn/bootstrap.sh once      # fetch and draw now, without sleeping after
echo 300 > /mnt/us/mnn/wake-in        # next wake in 300 seconds instead (used once)
```

`wake-in` is read at the end of a cycle, when the client decides how long to
sleep, so write it and then restart the bootstrap by running the install line
again. The shortest wait is 300 seconds, whether the number comes from
`wake-in` or from the Press; a smaller one is raised to that.

## Settings

None are needed. To override a default, create `/mnt/us/mnn/client.conf`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `MNN_LIBRARY` | KOReader's home folder, else `/mnt/us/documents` | Where papers are put |
| `MNN_KEEP` | `7` | How many papers to keep on the Kindle |
| `MNN_DISPLAY` | `frontpage` | What the screen shows between reads; `none` draws nothing |

The wake time is not a Kindle setting. It follows the Press's `EDITION_TIME`.

## Removing it

```sh
kill "$(cat /mnt/us/mnn/bootstrap.pid)"
rm -rf /mnt/us/mnn
```

Papers already downloaded stay in the library, and so does `Start MNN.sh`,
which no longer does anything and can be deleted.

## The files here

| File | Where it runs |
| --- | --- |
| `install.sh` | Once, on the Kindle. The Press fills in its own address as it serves it |
| `bootstrap.sh` | On the Kindle, for good. Keeps the client up to date and runs it |
| `client.sh` | On the Kindle, replaced whenever the Press serves a new one |

All three are written for the Kindle's BusyBox shell and are served by the
Press at `/kindle/<name>`, as is every file the manifest names. Display modes, meaning what the screen shows
between reads, are functions in `client.sh`; adding one does not touch the
bootstrap.
