"""Placeholder for the Press's Muse gadget service.

What it does today: checks that an SDK token was supplied, then logs the
Press's status and health as JSON on a fixed interval, so
`docker compose logs press-gadget` answers "did the paper land?".

What it does not do: talk to Muse. The Muse Linux Device SDK is not in the
image (see README.md here for why), so the token is checked for presence and
never used. When the SDK link is added, it replaces the loop below and
registers the commands in musegadget_commands.py.

The token comes from MUSEGADGET_SDK_TOKEN in the environment and nowhere else.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

from mnn import press_commands

TOKEN_ENV = "MUSEGADGET_SDK_TOKEN"
INTERVAL_ENV = "PRESS_GADGET_INTERVAL"


def main() -> int:
    if not os.environ.get(TOKEN_ENV, "").strip():
        print(f"error: {TOKEN_ENV} is not set. Get a token for your gadget, put it in "
              ".env, and start this service again.", file=sys.stderr)
        return 2
    try:
        interval = max(60, int(os.environ.get(INTERVAL_ENV, "900")))
    except ValueError:
        print(f"error: {INTERVAL_ENV} must be a number of seconds", file=sys.stderr)
        return 2

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print("press-gadget scaffold: the Muse SDK link is not implemented; "
          f"logging status every {interval}s instead", flush=True)
    while True:
        report = {"status": press_commands.paper_status(),
                  "health": press_commands.paper_health()}
        print(json.dumps(report), flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
