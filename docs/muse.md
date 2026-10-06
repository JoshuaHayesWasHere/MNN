# Muse

[Muse](https://github.com/facebookincubator/muse-gadget-sdk) is an AI agent from Meta.
The paper does not need one: everything on this page is optional and off by default.
With a Muse paired to the machine the Press runs on, through a Muse Linux gadget, the
Press can ask it to write a section, tell it how the morning went, answer its
questions about the paper, and put the paper's state on a display board.

**All of it is experimental.** These pieces were written against the gadget SDK's
source and have only been run against a fake gadget socket, never a paired device.
Each section below says what is untested.

## Asking Muse at print time

A section can be a question. At print time the Press puts it to your Muse
through a paired [Muse Linux gadget](#messages-to-muse) and prints the answer.
It is off unless a section asks:

```toml
[[section]]
title = "From Muse"
source = "ask"
question = "What should I know about today? Two short paragraphs."
wait = 45
```

| Key | Default | Meaning |
| --- | --- | --- |
| `question` | none | What to ask, 1000 characters at most |
| `wait` | `45` | The most seconds the paper waits for the answer, from 1 to 120 |
| `headlines` | `false` | `true` adds today's headlines to the question (three a section, twelve in all) |
| `stories` | `3` | The most articles printed from one answer |
| `folder` | `answers` in the inbox folder | Where the Press looks for the answer; never the inbox folder itself |
| `gadget_folder` | the same path | That folder as the gadget's host sees it; required under `docker compose` |
| `keep_days` | `14` | Days an answer is kept after its paper |

**How the answer comes back.** The gadget service only acknowledges a
message: Muse's reply goes to the chat, not back to the Press. (Upstream pull
request 90 would add a reply; it was still open, with changes requested, when
this was written.) So the question asks Muse to write its answer to a file
with the gadget's own `file.write`, and the Press watches for that file. What
your Muse receives is your question followed by:

> Answer in the printed morning paper of 2026-10-05, not in this chat: write
> the answer with file.write on this device to
> /home/you/MNN/inbox/answers/2026-10-05-1a2b3c4d.json as JSON in this shape:
> {"articles": [{"title": "A short headline", "body": ["First paragraph.",
> "Second."]}]} Plain text only, three articles at most. The paper is printed
> in 45 seconds, without the answer if the file is not there by then, so write
> the file once, straight away. (An automatic request from the Press, the
> morning paper on this network. No reply in the chat is needed.)

The file is one section in [the inbox's format](sources.md#the-inbox) and is read by the
inbox's rules: plain text, 256 KiB at most, links not followed.

**The paper does not wait past the limit.** The other sections are gathered
first, then the question is asked, and the paper waits at most `wait` seconds
(and never past the section's own `timeout` less five seconds, so raise
`timeout` for a `wait` over 55). `wait` is 120 at most, so that the other
sections, the wait and the build are done before the Kindle wakes, five
minutes after the edition time, and within the five minutes `paper_rebuild`
is given. The answer is printed the moment the file lands. If none comes in
time, there is no gadget, the message cannot be delivered, or the answer
cannot be printed, the section is simply not in the paper: the report lists
it as `empty` with the reason, and it does not count as a failure. A section
that is set up wrongly (no `question`, no folder, the inbox folder as its
`folder`, no `gadget_folder` under `docker compose`) does.

**What is sent.** Your question, the paper's date, the path to write to and
how long there is. No story text, unless the section says `headlines = true`:
then the headlines of the sections already in go with it, flattened to plain
text. Headlines are text a feed chose, put in front of a Muse that can run
commands on the gadget's host, so use `headlines` only with feeds you trust.

**Set-up.**

1. Make the folder, writable by the account the gadget runs commands as and
   readable by the Press: `mkdir inbox/answers`, then for example
   `sudo chgrp <that account's group> inbox/answers && chmod 2775 inbox/answers`.
   `file.write` makes its files readable by every account on the host.
   The Press never creates it, so that a folder Muse cannot write to is a
   failure you hear about and not a section that is quietly never there.
   Let the Press write there as well (it can as the folder's owner, and under
   `docker compose` through the group that step 3 gives it): it leaves a
   marker for each question it asks, and deletes old answers.
2. Under `docker compose` the Press sees `/inbox/answers` and the gadget does
   not, so name the host's path: `gadget_folder = "/home/you/MNN/inbox/answers"`.
   The image sets `PRESS_IN_CONTAINER`, and with it a section that has no
   `gadget_folder` fails and says so, in the log and in the report, and the
   rest of the paper prints.
3. Let the Press open the gadget's socket: see
   [Reaching the gadget from the container](#reaching-the-gadget-from-the-container).
   `MUSE_SESSION_ID` and `MUSE_SOCKET` apply; `MUSE_MESSAGES` can stay `off`.

Things to know:

- **A question is asked once a day.** The answer is kept in the folder, so
  printing the same day's paper again prints it again. When the question goes
  out the Press also leaves a marker beside the answer, named like it but
  ending `.asked`. A print that is tried again (after a build that failed, or
  a morning with nothing else to print) or a reprint with no answer yet finds
  the marker, waits up to `wait` seconds for the answer already asked for,
  and does not ask a second time. So a Muse that replies in the chat is asked
  once, not at every try. A question that did not reach the gadget leaves no
  marker and is asked at the next try. To ask again the same day, delete the
  marker, and the answer if one came, or change the question. An answer that
  arrives after the paper has gone is only printed if that day's paper is
  printed again, never in tomorrow's.
- **The file is named for the day and the question**, so two `ask` sections
  can share the folder.
- **`folder` cannot be the inbox folder.** An answer is named the way an inbox
  file is, so there an inbox section would print it too, a late one in
  tomorrow's paper, and old inbox files could be deleted as old answers. A
  section whose `folder` is the inbox folder itself fails and says so, and the
  rest of the paper prints. An inbox section's own `folder` is not checked, so
  do not point one at the answers.
- **An answer is never dropped as a repeat.** A story from a feed is printed
  in one section only, the first that has it. An answer is left out of that:
  an article in it headed like one of the day's stories is printed, and the
  story stays in its own section.
- **Old answers and markers** are deleted `keep_days` after their paper, when
  the Press may write to the folder. When it may not, it leaves no marker
  either, the section's notes in the report say so, and a print that is tried
  again asks again.
- **Only when the Press writes its own paper.** With `STAGE_URL` set, sources
  do not run and nothing is asked.
- **Untested against a paired gadget.** Whether a Muse follows the request,
  and how quickly, is not known: this has only been run against a fake gadget.

## The Muse skill

[integrations/muse/SKILL.md](../integrations/muse/SKILL.md) teaches a [Muse](https://github.com/facebookincubator/muse-gadget-sdk)
to answer "did the paper land?", "is the Press healthy?" and "reprint it" by
running `mnn` through a paired Linux gadget's `system.run`. Give your Muse a
link to this repository, or paste the file into its chat.

On the gadget's machine, install `mnn` as above and write its settings file
for the account the gadget runs commands as. That account should be a
dedicated one with no sudo and no Docker access; `mnn` needs neither, because
it only speaks HTTP to the Press. A gadget starts every command with an empty
environment, which is why `mnn` reads a file.

Nothing in the Press depends on Muse. Without one, the addresses and `mnn`
work the same, and the paper prints and reaches the Kindle as it always did.

## Messages to Muse

If a [Muse Linux gadget](https://github.com/facebookincubator/muse-gadget-sdk)
is paired on the machine the Press runs on, the Press can tell your Muse how
the morning went. It is off by default, and the paper does not depend on it:
the message is sent after the paper is printed and served, and a message that
cannot be delivered is logged and dropped, never retried.

| Setting | Default | Meaning |
| --- | --- | --- |
| `MUSE_MESSAGES` | `off` | `every`: one line a morning, and problems. `problems`: silent on good mornings. `off`: nothing |
| `MUSE_DETAIL` | `counts` | `counts`, or `headlines` to add the leading headline of up to five sections to the morning line |
| `MUSE_KINDLE_WAIT` | `60` | Minutes to wait for the Kindle after a print, twelve hours (`720`) at most: anything longer is cut to that, so the wait ends before the next morning's paper. `0` never waits for, or mentions, the Kindle |
| `MUSE_SESSION_ID` | unset | A side chat to post into (letters, digits, dashes) instead of the main chat |
| `MUSE_SOCKET` | `/run/musegadget/musegadget.sock` | The gadget service's socket. `docker compose` does not pass this on: set `MUSE_RUN_DIR` instead (see [below](#reaching-the-gadget-from-the-container)) |

**The morning line** (`every`) is sent once per edition, when the Kindle
reports its download or the wait runs out, whichever comes first:

> Printed at 06:41, four sections, 14 stories. On the Kindle at 06:46.

> Printed at 06:41, four sections, 14 stories. The Kindle had not fetched it
> by 07:41; it stays on the Press for the Kindle's next wake.

**Problems** (`every` and `problems`) each produce one message saying what
happened and what the Press does next:

| Problem | Said | Once per |
| --- | --- | --- |
| The print failed | The reason; the previous paper is still served; when it is tried again | Edition |
| Sections that failed | Which, and why (the first five by name, then how many more); they are tried again at the next edition | Edition |
| Staging could not be read, with the day's edition not yet printed | The reason; the Press tries again at its next check | Day |
| The Kindle has not fetched within the wait | When it was printed; the Kindle collects it on its next wake (with `every`, the morning line says this instead) | Edition |
| Under 200 MB free in the data directory | How much is left; the Press only deletes its own old editions, so space has to be freed by hand | Shortage, which lasts until 300 MB is free again |

Things to know:

- **Muse reads the message as written by you**, not as a notification, so
  each one ends "(An automatic status note from the Press, the morning paper
  on this network. No reply or action is needed.)". Whether Muse stays quiet,
  and whether your phone notifies, has not been tested.
- **Counts come from the Press's own sources.** A staged edition's line is
  "Printed at 06:41. On the Kindle at 06:46.", with no counts or headlines.
- **The Kindle's download is known only from `kindle/client.sh`.** If
  you fetch the paper by hand over OPDS, set `MUSE_KINDLE_WAIT=0`.
- **Only outcomes from the last twelve hours are reported**, so turning the
  messages on does not replay old news. For a printed paper the twelve hours
  run from the end of the Kindle's wait.
- **A Press that is switched off cannot say so.**

Try the socket by hand with `uv run mnn-press muse "Test from the Press."` (in the
container: `docker compose exec press-server python -m mnn muse "Test from the
Press."`).

### Reaching the gadget from the container

The gadget service runs on the host; `press-server` sends the messages and
the request to show [the desk card](#the-desk-card), and `press-fetch` asks
[its question at print time](#asking-muse-at-print-time), from containers.
The service's socket, `/run/musegadget/musegadget.sock`, is owned
by root and the group of the account the gadget runs commands as, mode 0660,
and needs no other credentials. So each container needs that directory mounted
and that group. `integrations/muse/docker-compose.yml` does both, for both; turn it on in
`.env` (`MUSE_MESSAGES` only if you want the messages):

```sh
MUSE_MESSAGES=every
COMPOSE_FILE=docker-compose.yml:integrations/muse/docker-compose.yml
MUSE_GID=<the number printed by: stat -c %g /run/musegadget/musegadget.sock>
```

The directory is mounted rather than the socket because the service makes a
new socket every time it starts. As installed, its unit does the same to the
directory: `/run/musegadget` is removed when the service stops and made again
when it starts, which would leave the container holding the old one, and
every message dropped, from the service's first restart until the containers
are recreated. So have systemd keep the directory, with a drop-in for the
gadget's unit (`sudo systemctl edit musegadget`):

```ini
[Service]
RuntimeDirectoryPreserve=yes
```

then `docker compose up -d`. Set `MUSE_RUN_DIR` if the service keeps its
socket in another directory. Without Docker there is no mount and no drop-in
to make: add the account that runs the server and the fetcher to that
group instead, and put the `MUSE_` settings in `~/.config/morning-paper/env`
(see [Running without Docker](press.md#running-without-docker)).

**This mount and the drop-in are untested:** no paired gadget was available.
The socket protocol was written against the SDK's source at commit
`3229892e93` and is exercised here only against a fake socket.

## The desk card

The Press serves the paper's state as one picture, sized for a
[Muse display board](https://github.com/facebookincubator/muse-gadget-sdk/blob/main/esp32/devices/README.md),
so a screen on a desk can say whether the paper is ready:

| State | When | Shows |
| --- | --- | --- |
| **Paper ready** (solid band) | Today's paper is printed | The lead headline; stories and sections, any left out, and when it was printed |
| **Not printed yet** (empty frame) | The newest paper is older than today | The date of the last paper |
| **Print failed** (band inside a frame) | The last print failed | The reason; the previous paper is still served |

It is drawn from the same records as `/api/status`, afresh on every request,
so the address never changes. A staged edition's card says it is ready and
when it was printed, with no headline or counts.

| Address | Is |
| --- | --- |
| `http://<press>:8484/card.jpg` | A baseline JPEG, 800 by 480 |
| `http://<press>:8484/card.rgb565` | The same card as raw RGB565, high byte first, 1600 bytes a row |
| either, with `?size=480x480` | The card laid out for another screen, each side 120 to 2000 |

Those are the two formats the SDK's `display.draw_url` takes (not PNG), and
800 by 480 is its two e-paper boards. A JPEG may be smaller than the screen
but not larger; raw data must be exactly as wide as the screen. The card is
pure black and white, letters included, because the black and white e-paper
has no gray and dithers anything else into dots. JPEG compression still
leaves faint gray around the letters; if that shows as speckle on a board,
the raw address is exact, at 768 KB.

**Showing it.** Only Muse can run a command on a gadget, so the Press cannot
draw the card itself. Either ask your Muse ("draw
http://192.168.1.20:8484/card.jpg on the desk display"), or set
`MUSE_CARD_URL` to that address and the Press asks for you: once for each
paper printed, and once when an edition's print fails. It is off unless set,
and does not depend on `MUSE_MESSAGES`. The request goes through the same
[gadget socket](#reaching-the-gadget-from-the-container) as the messages, and
like them it is sent after the paper is printed and served, and logged and
dropped if it cannot be delivered. The address must be one the board can
reach: the Press's address on your network, not `localhost`.

**Untested on a device,** and the request leans on something unproven: no
display board and no paired gadget were available. The format was written
against the SDK's source at commit `3229892e93`. Whether Muse runs
`display.draw_url` on one gadget because another gadget's message asked it to
is not documented; if it does not, the card is still there to be asked for.
The board keeps showing the card until Muse runs `display.show_animation`.

## Limitations

- **The Muse gadget is optional, and a sketch.** `docker compose --profile
  gadget up -d` starts a service that logs the Press's status; it does not
  talk to Muse, and nothing else depends on it. See
  [integrations/muse/gadget/README.md](../integrations/muse/gadget/README.md). The [messages to Muse](#messages-to-muse) do not
  use it: they go through a gadget service installed on the host, and have
  only been run against a fake socket.
- **The desk card has never been on a board.** See
  [the desk card](#the-desk-card).
