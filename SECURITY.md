# Security

## Reporting a problem

Please report a vulnerability privately, not in a public issue:

**[Report a vulnerability](https://github.com/JoshuaHayesWasHere/MNN/security/advisories/new)**

That opens a private advisory only the maintainer can see. Say what you
found, how to reproduce it, and what it lets someone do. The aim is to reply
within a week. If the report holds, a fix is published with credit to you,
unless you would rather not be named.

Fixes land on `main` and in the next release. Only the newest release is
supported.

## What is already known

MNN is built for a home network you trust, and some of what it does is by
design and written down in [docs/security.md](docs/security.md). Before
reporting, check whether it is already there. In particular:

- The Press serves its papers, its status and its health to anyone on the
  LAN, over plain HTTP, with no password.
- The Kindle runs the client script the Press serves, as root, checked by
  SHA-256 and not signed. Anyone on the network who can pose as the Press can
  run commands on the Kindle.
- A source module in `config/` is code you chose to run inside the Press.
- With an agent connected, status, and headlines if you turn them on, leave
  your network for that agent's service.

A way to do more than those allow is very much worth a report. So is
anything that reaches the Press or the Kindle from outside the home network,
gets past the token on `POST /api/rebuild` or on staging, or makes the Press
read or write a file outside its data, config and inbox folders.
