"""Stand-ins shared by the tests."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path


class FakeGadget:
    """Stands in for the gadget service: one JSON line in, one out. `muse`,
    when given, is called with each message before the service answers, as
    the Muse behind the gadget would be."""

    def __init__(self, path: Path, reply: dict | bytes, muse=None) -> None:
        self.path, self.reply, self.muse, self.requests = path, reply, muse, []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection, connection.makefile("rb") as lines:
                self.requests.append(json.loads(lines.readline()))
                if self.muse is not None:
                    self.muse(self.requests[-1]["message"])
                reply = self.reply
                try:
                    connection.sendall(reply if isinstance(reply, bytes)
                                       else json.dumps(reply).encode() + b"\n")
                except OSError:
                    pass  # whoever asked has stopped waiting

    @property
    def messages(self) -> list[str]:
        return [request["message"] for request in self.requests]
