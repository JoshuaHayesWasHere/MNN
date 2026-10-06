# The inbox

docker compose mounts this directory into the Press at `/inbox`. A section
file left here is printed in the next paper by the `inbox` source. Everything
in it except this file is ignored by git.

| Put here | What happens |
| --- | --- |
| `2026-10-05.json`, `2026-10-05-label.json` | Printed in the paper of that day (or the day after), then moved to `printed/` |
| A file dated in the future | Waits for its day |
| A file that is not a section, is over 256 KiB, or is not named as above | Left here, reported, skipped |

A label may only use `a-z`, `A-Z`, `0-9`, `.`, `_` and `-`: no spaces and no
accented letters.

The Press only looks here when your `sources.toml` has a section with
`source = "inbox"`. It needs to write to this directory, to move what it has
printed. See "The inbox" in [docs/sources.md](../docs/sources.md#the-inbox) for the file format.

This is the most personal content the paper can carry, and the Press serves
its papers to anyone on the home network without a password.

## Answers

`answers/` in here is where a Muse writes its answer when a section with
`source = "ask"` puts a question to it at print time. Make the folder yourself,
writable by the account the gadget runs commands as and by the Press, which
leaves a small `.asked` marker there for each question it has put. Answers do
not go in this directory itself: a section told to look for them here fails.
See "Asking Muse at print time" in [docs/muse.md](../docs/muse.md#asking-muse-at-print-time).
