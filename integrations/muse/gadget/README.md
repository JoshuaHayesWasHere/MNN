# Press gadget (scaffold)

The Press is the machine that prints the paper. This folder gives it three
commands and a sketch of how they become Muse gadget commands.

| Command | Answers |
| --- | --- |
| `paper_status` | Is today's edition printed and on the Kindle, and which sections made it? |
| `paper_rebuild` | Print today's edition again: rerun the sources, or refetch the staged edition |
| `paper_health` | Is the Press itself working? |

## What works today

The commands themselves (`src/mnn/press_commands.py`) are complete and tested. From the repository root:

```sh
uv run mnn-press paper_status
uv run mnn-press paper_health
uv run mnn-press paper_rebuild
```

In the container: `docker compose exec press-server python -m mnn paper_status`.

Each prints one JSON object. `paper_rebuild` and `paper_health` exit non-zero
when the rebuild failed or the Press is unhealthy. When the Press writes its
own paper (no `STAGE_URL`), `paper_status` carries `sections` and
`failed_sections` from the last run, and `paper_health` lists each section
that failed, with the reason, among its `problems`. They read the same
environment as the rest of the project (`STAGE_URL`, `STAGE_TOKEN`,
`PAPER_DATA_DIR`, `PAPER_SERVER`).

A paired Muse Linux gadget can already run these with its stock `system.run`
command, with no changes to the SDK.

## The gadget service

```sh
docker compose --profile gadget up -d
docker compose logs -f press-gadget
```

The service is off unless you ask for the `gadget` profile, and it will not
start without `MUSEGADGET_SDK_TOKEN` in `.env`. Today it only checks that the
token is present, then logs status and health as JSON every fifteen minutes.
**It does not connect to Muse, and the token is not used.**

## What is only sketched

`musegadget_commands.py` registers the three as named gadget commands
(`paper.status`, `paper.rebuild`, `paper.health`). **It has not been run
against the SDK.** It follows the extension point documented in the SDK's
Linux README and was written against `executor.py` at upstream commit
`3229892e93`.

## Gaps

- **No plugin API.** The SDK's only way to add a command is to edit its own
  `executor.py`. That means carrying a patched copy and re-applying the edit
  when the SDK updates.
- **The SDK is not in the image.** It is installed by a script that wants a
  Debian or Ubuntu host with systemd, runs as root, and pairs over Bluetooth
  through the host's BlueZ and D-Bus. None of that fits a small non-root
  container as it stands. Running it beside the Press on the host, or giving a
  dedicated container the host's Bluetooth, are the two routes to try.
- **Bluetooth and root.** Pairing needs a Bluetooth LE adapter and the Muse
  phone app in developer mode. The service runs as root and runs commands as
  an unprivileged account you choose; that account needs this repository, uv,
  and the environment file.
- **Environment.** `run_press_command` needs `MORNING_PAPER_REPO` in the
  service's environment, and the run-as account needs `STAGE_URL` and
  `STAGE_TOKEN` when staging is used. How the SDK passes environment to commands was not verified.
- **Return shape.** The sketch returns whatever `system.run` returns (output
  and exit code) rather than parsed JSON. Whether Muse reads that well for a
  named command is untested.
- **Cloud dependency.** A paired gadget talks to Muse's cloud. The paper does
  not: printing and delivery keep working with the gadget off.

## SDK token

The SDK token (`mgst_...`) is given to the SDK through its installer or the
`MUSEGADGET_SDK_TOKEN` environment variable. Nothing in this repository reads
it, and it must never be committed.
