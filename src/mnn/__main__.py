"""One command for every part of the Press: `python -m mnn <command>`, or
`mnn-press <command>` where the project is installed (`uv run mnn-press`).

    mnn-press server      the LAN server the Kindle talks to
    mnn-press fetch       get today's edition and print it
    mnn-press build       edition file in, EPUB and front page out
    mnn-press stage       the staging server
    mnn-press stage-put   upload an edition to staging
    mnn-press muse        send one message to a paired Muse gadget
    mnn-press mail        send the newest paper by email, if it has not gone
    mnn-press paper_status | paper_rebuild | paper_health

Everything after the command goes to it unchanged, so `mnn-press fetch
--help` lists the fetcher's own options."""

from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "server": "mnn.server",
    "fetch": "mnn.press_fetch",
    "build": "mnn.build_paper",
    "stage": "mnn.stage",
    "stage-put": "mnn.stage_put",
    "muse": "mnn.muse",
    "mail": "mnn.mail",
}
# press_commands takes the command's own name as its first argument.
PAPER_COMMANDS = ("paper_status", "paper_rebuild", "paper_health")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    name, rest = args[0], args[1:]
    if name in PAPER_COMMANDS:
        module, rest = "mnn.press_commands", args
    elif name in COMMANDS:
        module = COMMANDS[name]
    else:
        print(f"mnn-press: no command named {name!r}; try --help", file=sys.stderr)
        return 2
    sys.argv[0] = f"mnn-press {name}"
    return importlib.import_module(module).main(rest)


if __name__ == "__main__":
    sys.exit(main())
