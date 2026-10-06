"""Scaffold: the Press commands as Muse Linux Device SDK commands.

NOT TESTED AGAINST THE SDK. This file follows the extension point the SDK
documents ("add a spec to COMMAND_SPECS and a branch in Executor.run" in
linux/src/musegadget/executor.py, read at upstream commit 3229892e93). It has
never been loaded by a running musegadget service. See README.md here for what
is still open.

The SDK has no plugin loader, so wiring this in means editing your copy of
executor.py. The edit is two additions:

    from press_gadget_bridge import PRESS_COMMAND_SPECS, run_press_command
    COMMAND_SPECS.update(PRESS_COMMAND_SPECS)

and, at the top of the `try:` block in Executor.run:

    if command in PRESS_COMMAND_SPECS:
        return run_press_command(self, command, params, timeout_ms)

(with this file copied next to executor.py as press_gadget_bridge.py).

Nothing here reads or stores the SDK token. The SDK takes it from
MUSEGADGET_SDK_TOKEN or its own installer; keep it out of this repository.
"""

from __future__ import annotations

import os

# Where this repository is checked out on the Press. Read from the environment
# of the musegadget service so nothing machine-specific is committed.
REPO_ENV = "MORNING_PAPER_REPO"

# How to start the Press's command in that checkout: "uv run mnn-press" on
# bare metal, "python -m mnn" in the Press image.
RUNNER = os.environ.get("MORNING_PAPER_RUNNER", "uv run mnn-press")

_NO_PARAMS: dict = {}

PRESS_COMMAND_SPECS = {
    "paper.status": {
        "description": (
            "Morning Paper: is today's edition printed by the Press and downloaded "
            "by the Kindle, and which sections made it in? Returns JSON."
        ),
        "params": _NO_PARAMS,
        "timeout_ms": 30_000,
    },
    "paper.rebuild": {
        "description": (
            "Morning Paper: print today's edition again (rerun the sources, or "
            "refetch the staged edition), replacing today's paper. Returns JSON "
            "with the result and the new status."
        ),
        "params": _NO_PARAMS,
        "timeout_ms": 330_000,
    },
    "paper.health": {
        "description": (
            "Morning Paper: is the Press working? Server answering, disk space, "
            "timers, and whether the last print failed. Returns JSON."
        ),
        "params": _NO_PARAMS,
        "timeout_ms": 30_000,
    },
}


def run_press_command(executor, command: str, params: dict, timeout_ms: int | None) -> dict:
    """Run one Press command through the SDK's own system.run, so it executes
    as the unprivileged run-as account with that account's limits, exactly as
    any other command Muse sends."""
    repo = os.environ.get(REPO_ENV)
    if not repo:
        return {"ok": False, "error": f"{REPO_ENV} is not set for the musegadget service"}
    name = command.replace(".", "_")
    spec_timeout = PRESS_COMMAND_SPECS[command]["timeout_ms"]
    return executor.system_run(
        {"command": f"{RUNNER} {name}", "cwd": repo,
         "timeout_ms": spec_timeout},
        timeout_ms or spec_timeout)
