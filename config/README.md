# Your paper's settings

docker compose mounts this directory into the Press, read-only, at `/config`.
Everything in it except this file is ignored by git.

| Put here | The Press sees | What it does |
| --- | --- | --- |
| `sources.toml` | `/config/sources.toml` | Replaces the default sections and feeds |
| `<name>.py` | `/config/<name>.py` | A source of your own, used by `source = "<name>"` |
| `portrait.png` | `/config/portrait.png` | A picture for the front page |
| `portrait-rain.png` | `/config/portrait-rain.png` | The picture used instead on rainy days |

Start from the default: `cp sources.toml config/sources.toml`. The Press reads
these files fresh before every print, so a saved edit is in the next paper
with no rebuild and no restart. See "Choosing your feeds" and "A front page portrait"
in [docs/sources.md](../docs/sources.md).

Section files for the `inbox` source do not go here: they go in `inbox/`,
beside this directory.
