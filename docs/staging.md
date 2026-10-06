# Staging

Sources inside the Press are the default. Staging is for an author that
cannot live there: something with access the Press does not have, running on
another machine. It writes a whole edition file, stages it at an HTTPS
address, and the Press prints that instead.

```
 write the edition        staging           the Press            Kindle
    (anywhere)      ->   (one URL)   ->   (a container)   ->   (your LAN)
                         receipt    <-
```

Every hop is outbound or LAN-local. Nothing reaches into your home
network, and nothing is committed to git along the way.

Set `STAGE_URL` and `STAGE_TOKEN` in `.env` and restart. From then on the
Press runs no sources: at edition time it checks staging (and every ten
minutes until 09:00, in case the edition is late), prints what it finds, and
sends a receipt back. Unset them and it writes its own paper again.

## Try the whole chain on one machine

There is no staging server to point at yet? The stack includes one for
demonstration. In `.env`, set `STAGE_URL=http://stage:8485` and any
`STAGE_TOKEN`, then:

```sh
docker compose --profile staging up -d

# stage the sample edition from the host (needs uv; standard library only)
STAGE_URL=http://127.0.0.1:8485 STAGE_TOKEN=<your token> uv run mnn-press stage-put edition/sample.json

# the fetcher checks straight away when it starts
docker compose restart press-fetch
docker compose logs press-fetch
curl http://127.0.0.1:8484/api/display
```

This demo staging server speaks plain HTTP. Real staging belongs somewhere the
edition's author can reach, over HTTPS; see [the contract](#the-staging-contract).

## The staging contract

Staging is a contract, not a product. Everything in this repository that
talks to it uses two settings and three paths:

| Setting | Meaning |
| --- | --- |
| `STAGE_URL` | Base address, for example `https://stage.example.com` |
| `STAGE_TOKEN` | Sent as `Authorization: Bearer <token>` on every request |

| Path | Holds |
| --- | --- |
| `GET/PUT {STAGE_URL}/stage/<name>` | An edition file |
| `GET/PUT {STAGE_URL}/stage/latest` | JSON pointer to the newest edition |
| `GET/PUT {STAGE_URL}/receipt` | JSON delivery receipt from the Press |

The pointer names the edition file with its size and SHA-256, and it is
written last, so the Press never follows it to a file that is missing or half
uploaded:

```json
{
  "date": "2026-10-05",
  "edition": { "name": "edition-2026-10-05.json", "sha256": "...", "size": 6048 },
  "staged_at": "2026-10-05T06:31:02-04:00"
}
```

### The bundled server

```sh
STAGE_TOKEN=... uv run mnn-press stage --cert fullchain.pem --key privkey.pem
```

The server refuses to start without `STAGE_TOKEN`, and wants a certificate
unless you pass `--insecure-http` (for tests, or behind a proxy that adds
TLS). It listens on port 8485 and keeps files in
`~/.local/share/morning-paper-stage` (`--dir`, `STAGE_DIR`).

### Using something else

Any host that answers those three paths the same way can replace the bundled server:
a static HTTPS host with authenticated uploads, or an S3-compatible bucket
(R2, B2, MinIO) with the objects `stage/<name>`, `stage/latest` and `receipt`
under one prefix. No code here imports a cloud SDK.

One caveat with buckets: their native API signs requests rather than taking a
bearer token. To satisfy this contract a bucket needs something small in front
of it that checks the token, such as a worker or a reverse proxy.

### Staging an edition

```sh
uv run mnn-press stage-put edition/2026-10-05.json
```

It uploads the file, checks it, moves the pointer, and exits non-zero if
any step fails.
