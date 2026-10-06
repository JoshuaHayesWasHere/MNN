---
name: gadget-mnn-press
description: >-
  Answer questions about the Morning Paper from its Press: whether today's edition printed and
  reached the Kindle, whether the Press is healthy, and reprint today's edition when asked. Use
  with a Linux gadget that has the `mnn` command installed; not for reading the paper's stories,
  changing its sections or feeds, or administering the machine.
---

# MNN Press

The Press is the home machine that prints the Morning Paper each morning and serves it to a Kindle. Use this skill when a Linux gadget in the home has the `mnn` command, and the user asks about the paper or the Press.

## Identify the Device

- Run `command -v mnn` with `system.run` on the Linux gadget. If it prints nothing, this skill does not apply to that gadget: say that `mnn` is not installed there and stop.
- Run `mnn health --json`. Exit status 0 or 1 confirms the gadget can reach the Press. Exit status 3 means the Press could not be reached from that gadget. Go by the exit status, not by `ok`: when `mnn` cannot get an answer it prints a JSON object too, with `"ok": false` and an `error`.

## Prerequisites

- A paired Linux gadget whose `system.run` you are allowed to use.
- `mnn` installed on it, with its settings file already in place (`~/.config/mnn/config` or `/etc/mnn/config`). The user's own setup puts the Press address and token there.
- Do not read, print, copy or edit that settings file, and do not ask the user for the token. `mnn` reads it by itself.

## Workflow

Run exactly one of these three commands with `system.run`, then answer from its output. Each prints a few plain sentences; add `--json` only when you need the fields.

| The user asks | Run | `timeout` |
| --- | --- | --- |
| "Did the paper land?", "Is today's paper out?", "Is it on my Kindle?" | `mnn status` | default |
| "Is the Press healthy?", "Is anything wrong with the paper?" | `mnn health` | default |
| "Reprint it", "Print today's paper again" | `mnn rebuild` | 360 seconds |

1. For status, say whether today's edition is printed and whether the Kindle has collected it, and name any section that was left out. "The Kindle has not collected this printing yet" is about the newest printing only: say it in those terms, because after a reprint the Kindle may still hold the copy it took in the morning.
2. For health, say that the Press is fine, or list each problem it reports in the user's terms. Mention anything under "Worth a look" briefly.
3. For a reprint, run `mnn rebuild` only when the user asked for one in this conversation. It runs every source again and can take a few minutes, so set the timeout shown above.

Read the exit status before the words:

| Exit | Meaning | Say |
| --- | --- | --- |
| 0 | Yes: printed today, healthy, or reprinted | The good news, in a sentence |
| 1 | No: not printed today, a problem, or the reprint failed | What the output says is wrong |
| 2 | `mnn` was used wrongly or its settings file is broken | That `mnn` needs fixing on the gadget; quote the message |
| 3 | The Press could not be reached | That the Press is not answering; it may be off or restarting |
| 4 | The Press refused the reprint | That reprinting is not turned on, or the token in the settings file is missing or wrong |

## Verify the Result

- After `mnn rebuild` exits 0, run `mnn status` once and report what it says. A reprint replaces the edition on the Press. When it is made after the Kindle's morning pickup, the Kindle normally does not collect it: its next wake is the following morning, when the next edition is the one it takes. "Not collected yet" then refers to the reprint, not the morning's copy, so do not tell the user the paper is missing from the Kindle.
- If `mnn rebuild` times out or its result is unclear, run `mnn status` before doing anything else. Do not start a second reprint while one may still be running: the Press answers "a rebuild is already running".
- An unhealthy Press that still shows today's edition as printed did print the paper. Report both facts, and say the paper reached the Kindle only when the output says the Kindle collected it.

## Limits

- Status only. These commands return dates, times, section names and error messages, never the paper's stories. Do not fetch or read the paper itself through this skill.
- Do not retry a failed reprint more than once without asking the user.
- Do not try to repair the Press: no `docker`, `systemctl`, `sudo`, package installs, or edits to the Press's files or feeds. Report what is wrong and let the user decide.
- Do not call the Press's HTTP addresses directly with a token of your own, and do not put the token on a command line.
- The paper prints and reaches the Kindle on its own schedule whether or not you are asked. Never reprint on your own initiative.

## Sources

- [MNN docs: the Press API, `mnn` and this skill](../../docs/press.md#asking-the-press)
- [`mnn` itself, including its settings file and exit statuses](../../bin/mnn)
