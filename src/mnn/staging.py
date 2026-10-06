"""Client side of the staging contract, shared by every script that talks to it.

Staging is "an HTTPS place holding a few files behind a bearer token". Given a
base address (STAGE_URL) and a token (STAGE_TOKEN), the whole contract is:

    GET/PUT  {STAGE_URL}/stage/<name>   an edition file
    GET/PUT  {STAGE_URL}/stage/latest   JSON pointer to the newest edition
    GET/PUT  {STAGE_URL}/receipt        JSON delivery receipt from the Press

Nothing here knows which server is on the other end. The stage module in this
repository is one; anything else that answers these paths the same way works.

Staging is optional: with no STAGE_URL the Press writes its own paper and
there is nobody to send a receipt to.

Also holds the Press's local records (the receipt, the report of its last
run, what the Kindle last reported, and the fetcher's heartbeat), so the
fetcher, the server and the gadget commands all read them the same way.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

URL_ENV = "STAGE_URL"
TOKEN_ENV = "STAGE_TOKEN"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TIMEOUT = 60


class StageError(Exception):
    """Staging could not be reached, refused the request, or sent nonsense.
    `status` is the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def configured(url: str | None = None) -> bool:
    """Whether there is a staging server to talk to. An empty STAGE_URL is the
    same as none: docker compose passes the variable through either way."""
    return bool((url or os.environ.get(URL_ENV, "")).strip())


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def describe(name: str, data: bytes) -> dict:
    """How a file is named in the pointer: enough to fetch and verify it."""
    return {"name": name, "sha256": sha256(data), "size": len(data)}


def write_atomic(path: Path, data: bytes) -> None:
    """Write under a temporary name, then rename, so no reader sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                     suffix=".part", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.chmod(0o644)
    temporary.replace(path)


class Stage:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    @classmethod
    def from_env(cls, url: str | None = None, token: str | None = None) -> "Stage":
        url = url or os.environ.get(URL_ENV, "")
        token = token or os.environ.get(TOKEN_ENV, "")
        if not url:
            raise StageError(f"no staging address: set {URL_ENV}")
        if not token:
            raise StageError(f"no staging token: set {TOKEN_ENV}")
        return cls(url, token.strip())

    def _request(self, method: str, path: str, data: bytes | None = None,
                 content_type: str | None = None) -> bytes:
        headers = {"Authorization": f"Bearer {self.token}"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(f"{self.url}/{path}", data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()[:200]
            raise StageError(f"{method} {path}: HTTP {exc.code} {detail}", exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StageError(f"{method} {path}: {exc}") from exc

    def _json(self, path: str) -> dict:
        try:
            value = json.loads(self._request("GET", path))
        except ValueError as exc:
            raise StageError(f"{path} is not JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise StageError(f"{path} is not a JSON object")
        return value

    def get(self, name: str) -> bytes:
        return self._request("GET", f"stage/{_checked(name)}")

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._request("PUT", f"stage/{_checked(name)}", data, content_type)

    def latest(self) -> dict:
        return self._json("stage/latest")

    def put_latest(self, pointer: dict) -> None:
        self.put("latest", dump(pointer), "application/json")

    def receipt(self) -> dict:
        return self._json("receipt")

    def put_receipt(self, receipt: dict) -> None:
        self._request("PUT", "receipt", dump(receipt), "application/json")

    def fetch_verified(self, entry: object) -> bytes:
        """Download a file named in the pointer and check it against the
        size and hash recorded there."""
        if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                or not isinstance(entry.get("sha256"), str)):
            raise StageError(f"pointer entry is malformed: {entry!r}")
        data = self.get(entry["name"])
        if sha256(data) != entry["sha256"]:
            raise StageError(f"{entry['name']} does not match the hash in the pointer "
                             "(still uploading, or corrupted)")
        return data


def _checked(name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise StageError(f"not a valid staging file name: {name!r}")
    return name


def dump(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


# --- The Press's local records -----------------------------------------------


def press_dir(data_dir: Path) -> Path:
    """Fetcher bookkeeping lives beside the served papers but out of their way."""
    return data_dir / "press"


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_receipt(data_dir: Path) -> dict:
    return load_json(press_dir(data_dir) / "receipt.json")


def save_receipt(data_dir: Path, receipt: dict) -> None:
    write_atomic(press_dir(data_dir) / "receipt.json", dump(receipt))


def load_report(data_dir: Path) -> dict:
    """What happened the last time the Press ran its own sources: which
    sections printed, which failed, and why. With staging it only records
    whether staging could be read. Written by press_fetch.py."""
    return load_json(press_dir(data_dir) / "report.json")


def save_report(data_dir: Path, report: dict) -> None:
    write_atomic(press_dir(data_dir) / "report.json", dump(report))


def load_heartbeat(data_dir: Path) -> dict:
    """When the fetcher last showed it was alive, for the server's health
    check. {"at": local time, "loop": whether it schedules itself}."""
    return load_json(press_dir(data_dir) / "fetcher.json")


def save_heartbeat(data_dir: Path, loop: bool) -> None:
    """Never worth stopping the fetcher for: a full disk shows up in health
    on its own."""
    try:
        write_atomic(press_dir(data_dir) / "fetcher.json", dump(
            {"at": dt.datetime.now().isoformat(timespec="seconds"), "loop": loop}))
    except OSError:
        pass


def send_receipt(data_dir: Path, stage: Stage | None = None) -> bool:
    """Push the local receipt to staging. A receipt that could not be sent is
    marked so the next run tries again; the paper itself never waits on this.
    Without staging the receipt stays a local record and nothing is owed."""
    receipt = load_receipt(data_dir)
    if not receipt or (stage is None and not configured()):
        return True
    try:
        stage = stage or Stage.from_env()
        stage.put_receipt({k: v for k, v in receipt.items() if k != "unsent"})
    except StageError as exc:
        receipt["unsent"] = str(exc)
        save_receipt(data_dir, receipt)
        return False
    if receipt.pop("unsent", None) is not None:
        save_receipt(data_dir, receipt)
    return True


def mark_kindle_download(data_dir: Path, edition_date: str, when: str, client: str) -> bool:
    """Record the first time the Kindle confirms it has an edition. Returns
    True when the receipt changed and should be sent again."""
    receipt = load_receipt(data_dir)
    if receipt.get("edition_date") != edition_date or receipt.get("kindle_downloaded_at"):
        return False
    receipt["kindle_downloaded_at"] = when
    receipt["kindle_address"] = client
    save_receipt(data_dir, receipt)
    return True


def load_kindle(data_dir: Path) -> dict:
    """What the Kindle last told the Press. Kept apart from the receipt, which
    is rewritten by every print."""
    return load_json(press_dir(data_dir) / "kindle.json")


def record_kindle(data_dir: Path, when: str, client: str, delivered: list[str],
                  warnings: list[str]) -> dict:
    """Note that the Kindle checked in, and its newest download or warning."""
    kindle = load_kindle(data_dir)
    kindle.update(last_seen_at=when, address=client)
    if delivered:
        kindle.update(edition_date=max(delivered), downloaded_at=when)
    if warnings:
        kindle["last_warning"] = {"at": when, "text": warnings[-1][:300]}
    write_atomic(press_dir(data_dir) / "kindle.json", dump(kindle))
    return kindle
