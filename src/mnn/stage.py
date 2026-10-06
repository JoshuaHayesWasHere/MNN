"""A small staging server: the place an edition waits between the machine that
writes it and the Press that prints it.

    GET/PUT  /stage/<name>   an edition file
    GET/PUT  /stage/latest   JSON pointer to the newest edition
    GET/PUT  /receipt        JSON delivery receipt written back by the Press

Every request needs "Authorization: Bearer $STAGE_TOKEN". With no STAGE_TOKEN
the server refuses to start: there is no unauthenticated mode.

It serves HTTPS when given a certificate and key. Plain HTTP has to be asked
for by name (--insecure-http), for tests or behind a proxy that adds TLS.

Usage:
    STAGE_TOKEN=... uv run mnn-press stage --cert fullchain.pem --key privkey.pem
"""

from __future__ import annotations

import argparse
import hmac
import os
import signal
import ssl
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from mnn.staging import NAME_RE, TOKEN_ENV, write_atomic

DIR_ENV = "STAGE_DIR"
MAX_BYTES = 32 * 1024 * 1024
# A reader must never be handed one of these as if it were a staged file.
RECEIPT_FILE = "receipt.json"


def default_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "morning-paper-stage"


class StageServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, directory: Path, token: str) -> None:
        super().__init__(address, StageHandler)
        self.directory = directory
        self.token = token


class StageHandler(BaseHTTPRequestHandler):
    server: StageServer
    server_version = "MorningPaperStage/0.1"
    timeout = 30

    def target(self) -> Path | None:
        """The file a request path refers to, or None if the path is not part
        of the contract. Names are matched whole, so nothing can climb out of
        the directory."""
        path = urlsplit(self.path).path
        if path == "/receipt":
            return self.server.directory / RECEIPT_FILE
        if path.startswith("/stage/"):
            name = path.removeprefix("/stage/")
            if NAME_RE.fullmatch(name):
                return self.server.directory / "stage" / name
        return None

    def authorised(self) -> bool:
        offered = self.headers.get("Authorization", "")
        if hmac.compare_digest(offered.encode("utf-8", "replace"),
                               f"Bearer {self.server.token}".encode("utf-8")):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", "Bearer")
        self.reply_body(b"missing or wrong bearer token\n")
        return False

    def reply(self, status: HTTPStatus, text: str) -> None:
        self.send_response(status)
        self.reply_body(text.encode("utf-8"))

    def reply_body(self, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        # The token is checked before the path, so an outsider cannot even
        # learn which files exist.
        if not self.authorised():
            return
        target = self.target()
        if target is None:
            self.reply(HTTPStatus.NOT_FOUND, "not found\n")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self.reply(HTTPStatus.NOT_FOUND, "not found\n")
            return
        self.send_response(HTTPStatus.OK)
        is_json = target.suffix == ".json" or target.name == "latest"
        self.reply_body(body, "application/json" if is_json else "application/octet-stream")

    do_HEAD = do_GET

    def do_PUT(self) -> None:
        if not self.authorised():
            return
        target = self.target()
        if target is None:
            self.reply(HTTPStatus.NOT_FOUND, "not found\n")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.reply(HTTPStatus.LENGTH_REQUIRED, "Content-Length required\n")
            return
        if not 0 <= length <= MAX_BYTES:
            self.reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"body limited to {MAX_BYTES} bytes\n")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.reply(HTTPStatus.BAD_REQUEST, "body shorter than Content-Length\n")
            return
        try:
            write_atomic(target, body)
        except OSError as exc:
            self.reply(HTTPStatus.INTERNAL_SERVER_ERROR, f"could not store the file: {exc}\n")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Staging server for the Morning Paper.")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8485, help="port (default: 8485)")
    parser.add_argument("--dir", type=Path, default=os.environ.get(DIR_ENV) or default_dir(),
                        help=f"where staged files are kept (default: ${DIR_ENV}, "
                             "else ~/.local/share/morning-paper-stage)")
    parser.add_argument("--cert", type=Path, help="TLS certificate chain (PEM)")
    parser.add_argument("--key", type=Path, help="TLS private key (PEM)")
    parser.add_argument("--insecure-http", action="store_true",
                        help="serve plain HTTP: the token and editions cross the network "
                             "unencrypted unless something in front adds TLS")
    args = parser.parse_args(argv)

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        print(f"error: {TOKEN_ENV} is not set; refusing to run without a token", file=sys.stderr)
        return 2
    if bool(args.cert) != bool(args.key):
        parser.error("--cert and --key go together")
    if not args.cert and not args.insecure_http:
        parser.error("give --cert and --key for HTTPS, or --insecure-http to accept plain HTTP")

    directory = Path(args.dir).expanduser()
    (directory / "stage").mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with StageServer((args.host, args.port), directory=directory, token=token) as server:
        scheme = "http"
        if args.cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(args.cert, args.key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            scheme = "https"
        print(f"staging {directory} on {scheme}://{args.host}:{args.port}", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
