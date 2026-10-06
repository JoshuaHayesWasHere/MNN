"""Shared fixtures. Nothing here touches the network: feeds come from a
server on 127.0.0.1 that hands out the files in tests/fixtures."""

from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class _FeedHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        name = self.path.lstrip("/")
        if name == "slow":
            # Longer than any timeout a test sets, shorter than the test run.
            time.sleep(5)
            body = b"<rss><channel/></rss>"
        elif name == "broken":
            body = b"<rss><channel><item><title>cut off"
        elif name == "page":
            body = b"<html><body><p>Not a feed at all.</p></body></html>"
        elif (FIXTURES / name).is_file():
            body = (FIXTURES / name).read_bytes()
        else:
            self.send_error(404)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the reader gave up on a slow feed, as it should

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="session")
def feeds() -> str:
    """Base address of the fixture feed server, e.g. http://127.0.0.1:40123."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def unreachable() -> str:
    """An address nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/feed.rss"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    directory.mkdir()
    return directory


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    (directory / "press").mkdir(parents=True)
    return directory


@pytest.fixture(autouse=True)
def no_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests start with staging unset, whatever the developer's shell says."""
    monkeypatch.delenv("STAGE_URL", raising=False)
    monkeypatch.delenv("STAGE_TOKEN", raising=False)
