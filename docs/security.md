# Security

- **The Press only reaches out.** It fetches the feeds you list (and the
  forecast) over HTTP(S) and listens only on the LAN.
- **Feed text is treated as untrusted.** Summaries are stripped to plain text
  (no markup, scripts, or control characters), cut to length, and escaped
  again when the EPUB is written. Links are kept only if they are http(s).
- **A feed's address is recorded without its secrets.** When a feed cannot be
  read, the report, `/api/status` and the log name it by its place in the
  `feeds` list and by scheme, host and path. A query string or a user name
  and password in the address is left out, since it may be a credential. The
  feed reader does not sign in, so an address that carries a user name and
  password is never read.
- **The inbox is personal, and the paper is not private.** A file left in
  `inbox/` is printed into a paper that anyone on the LAN can download. The
  server never serves the inbox folder itself, only the papers made from it.
  Files there are read as data, never run: at most 256 KiB each, plain text
  only, and symbolic links not followed. A file carrying a link that is not
  http(s) is skipped. Whoever can write to the folder can write in your paper.
- **The editor sends your feeds' headlines to Anthropic.** A section with
  `source = "editor"` sends your brief and the headline, summary and feed
  name of each candidate story to Anthropic's API, with your
  `ANTHROPIC_API_KEY`. Feed text is put in front of a model, so the editor is
  told to treat it as material and not as instructions, can only choose among
  the stories it was shown, and has its answer read as plain text with the
  feed's own links and bylines. The key is in the environment of the server
  and the fetcher, where a source module of your own can read it.
- **Your calendar is in the paper, and with a key it goes to Anthropic.** A
  section with `source = "day"` prints today's events into a paper anyone on
  the LAN can download, and its first headline can appear on the front page
  the Kindle shows on wake. With `ANTHROPIC_API_KEY` set, your brief and the
  day's events (title, time, place) are sent to Anthropic's API; event text
  is treated there as material, not instructions, and the agenda that is
  printed comes from the calendar, not from the model. A calendar's address
  is a password to it: keep it in `config/sources.toml` and nowhere else.
- **A source module is code you chose to run.** Anything in `config/` runs
  inside the Press container with its permissions. Only put code there that
  you trust.
- **Messages to Muse leave the house.** With `MUSE_MESSAGES` set, status goes
  to Muse's cloud: times, counts, section names and error text. Headlines go
  too only with `MUSE_DETAIL=headlines`. Muse reads these as messages from
  you, and headlines and error text can come from a feed, so they are
  flattened to one line of plain text and cut to length first; with
  `headlines`, text a feed chose is put in front of your Muse, so use it only
  with feeds you trust.
- **The card puts a headline on a screen in a room.** Anyone who can see
  the board, or reach the Press on the LAN, can read it. With `MUSE_CARD_URL`
  only the card's address goes to Muse's cloud; the board fetches the picture
  itself, inside your network.
- **The gadget socket's group can message your Muse.** `integrations/muse/docker-compose.yml`
  gives that group to `press-server` and `press-fetch` only.
- **A question asked at print time leaves the house, and its answer is
  printed.** An `ask` section sends your question, the date and a file path to
  Muse's cloud, and headlines only with `headlines = true`. The answer is read
  as data under the inbox's rules, and like the inbox it ends up in a paper
  anyone on the LAN can download. Whoever can write to the answers folder can
  write in your paper.
- **Staging, when used, is the only part that faces the internet,** and every
  request to it needs the token. Run it over HTTPS; the token is a password.
- **The Press and the Kindle stay on the LAN.** Reading from the server,
  including `/api/status` and `/api/health`, is open to anyone on that
  network, as is appending to `server.log` (capped at 64 KiB per request,
  never rotated). Status and health carry no story text. There is no TLS on
  the LAN side. Do not forward its port.
- **Reprinting needs a token.** `POST /api/rebuild` is the only request that
  makes the Press do work. It is off until `PRESS_TOKEN` is set, and then it
  needs that token. With no TLS the token crosses the LAN in the clear, so it
  keeps out the other devices on your network, not someone who can watch its
  traffic; the most it buys them is an extra reprint.
- **A Muse gets a dedicated account.** A paired gadget gives Muse a shell as
  the account it runs commands as. Use one with no sudo and no Docker access:
  `mnn` needs neither.
- **Tokens live in `.env`** (ignored by git), in `mnn`'s settings file, or
  in another file only you can read.
  Never in the repository, and never in the image.
- **The Kindle trusts the Press, and the Press trusts its sources.** With
  staging set, whoever holds the staging token decides what the Kindle shows.
- **The Kindle runs what the Press serves.** Once installed, it takes its
  client script from the Press over plain HTTP and runs it as root. Files are
  checked against the SHA-256 in the Press's manifest, which catches a broken
  download; nothing is signed. Anyone on the home network who can pose as the
  Press can run commands on the Kindle as root.
