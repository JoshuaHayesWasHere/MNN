"""The Press's HTTP API (status, health, rebuild), the token that guards
rebuild, and the mnn command that wraps all three."""

from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import json
import socket
import subprocess
import textwrap
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mnn import press_fetch
from mnn import server
from mnn import staging

REPO = Path(__file__).parent.parent
TOKEN = "correct-horse-battery-staple"
AT = dt.time(6, 40)
MORNING = dt.datetime(2026, 10, 4, 8, 0)


def load_mnn():
    """bin/mnn is a single executable file with no .py suffix, loaded under
    another name so it does not shadow the mnn package."""
    loader = importlib.machinery.SourceFileLoader("mnn_client", str(REPO / "bin" / "mnn"))
    spec = importlib.util.spec_from_loader("mnn_client", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


mnn = load_mnn()


@pytest.fixture
def press(data_dir):
    """Start a Press server on a free port: press(token) returns its address."""
    running = []

    def start(token: str | None = None, edition_time: dt.time | None = None) -> str:
        paper_server = server.PaperServer(
            ("127.0.0.1", 0), data_dir=data_dir, log_path=data_dir / "server.log",
            refresh_rate=3600, base_url=None, edition_time=edition_time, rebuild_token=token)
        threading.Thread(target=paper_server.serve_forever, daemon=True).start()
        running.append(paper_server)
        return f"http://127.0.0.1:{paper_server.server_address[1]}"

    yield start
    for paper_server in running:
        paper_server.shutdown()
        paper_server.server_close()


def call(url: str, method: str = "GET", token: str | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, method=method, headers=headers,
                                     data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


def send_raw(base: str, request_line: str) -> bytes:
    """Send one request line exactly as written, which no HTTP client would
    do for a malformed one. Returns everything the server answered."""
    host, port = base.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as connection:
        connection.sendall(request_line.encode("latin-1") + b"\r\n\r\n")
        answer = b""
        while chunk := connection.recv(4096):
            answer += chunk
    return answer


def print_edition(data_dir: Path, date: str, kindle_at: str | None = None) -> None:
    """Leave the data directory as a good print of `date` would."""
    (data_dir / f"morning-paper-{date}.epub").write_bytes(b"epub")
    (data_dir / f"frontpage-{date}.png").write_bytes(b"png")
    staging.save_receipt(data_dir, {"edition_date": date, "status": "ok", "error": None,
                                    "built_at": f"{date}T06:40:09",
                                    "kindle_downloaded_at": kindle_at})


def heartbeat(data_dir: Path, at: dt.datetime, loop: bool = True) -> None:
    staging.write_atomic(staging.press_dir(data_dir) / "fetcher.json",
                         staging.dump({"at": at.isoformat(timespec="seconds"), "loop": loop}))


def healthy(data_dir: Path, now: dt.datetime = MORNING) -> None:
    print_edition(data_dir, now.date().isoformat())
    heartbeat(data_dir, now - dt.timedelta(minutes=2))


def codes(health: dict, kind: str = "problems") -> list[str]:
    return [found["code"] for found in health[kind]]


def write_settings(tmp_path: Path, url: str, token: str | None = None) -> str:
    path = tmp_path / "mnn.conf"
    path.write_text(f"# test settings\nurl = {url}\n" + (f'token = "{token}"\n' if token else ""),
                    encoding="utf-8")
    return str(path)


@pytest.fixture
def fake_rebuild(monkeypatch):
    """Stand in for the fetcher, and count how often it was asked to run."""
    calls = []

    def run(data_dir: Path) -> dict:
        calls.append(data_dir)
        return {"ok": True, "detail": "printed"}

    monkeypatch.setattr(server, "run_rebuild", run)
    return calls


# --- Health ------------------------------------------------------------------


def test_a_press_that_printed_today_is_fine(data_dir):
    healthy(data_dir)
    health = server.press_health(data_dir, AT, now=MORNING)
    assert health["ok"] is True
    assert (health["problems"], health["warnings"]) == ([], [])
    assert health["edition_date"] == health["today"] == "2026-10-04"
    assert health["edition_time"] == "06:40"
    assert health["rebuild_enabled"] is False


def test_todays_edition_missing_after_the_edition_time(data_dir):
    print_edition(data_dir, "2026-10-03")
    heartbeat(data_dir, MORNING)
    health = server.press_health(data_dir, AT, now=MORNING)
    assert health["ok"] is False
    assert codes(health) == ["edition_missing"]
    assert "due at 06:40" in health["problems"][0]["message"]
    assert "2026-10-03" in health["problems"][0]["message"]


def test_yesterdays_edition_is_fine_until_todays_is_due(data_dir):
    print_edition(data_dir, "2026-10-03")
    for now in (dt.datetime(2026, 10, 4, 5, 0), dt.datetime(2026, 10, 4, 6, 50)):
        heartbeat(data_dir, now)
        assert server.press_health(data_dir, AT, now=now)["ok"] is True


def test_a_press_that_has_printed_nothing_says_so(data_dir):
    heartbeat(data_dir, MORNING)
    assert codes(server.press_health(data_dir, AT, now=MORNING)) == ["no_edition"]


def test_a_failed_print_is_a_problem(data_dir):
    healthy(data_dir)
    staging.save_receipt(data_dir, {"edition_date": "2026-10-04", "status": "failed",
                                    "error": "every section failed"})
    health = server.press_health(data_dir, AT, now=MORNING)
    assert codes(health) == ["print_failed"]
    assert "every section failed" in health["problems"][0]["message"]


def test_a_fetcher_that_stopped_signing_in_is_a_problem(data_dir):
    healthy(data_dir)
    heartbeat(data_dir, MORNING - dt.timedelta(hours=1))
    health = server.press_health(data_dir, AT, now=MORNING)
    assert codes(health) == ["fetcher_stopped"]
    assert health["fetcher_seen_at"] == "2026-10-04T07:00:00"


def test_a_fetcher_that_never_ran_is_a_problem(data_dir):
    print_edition(data_dir, "2026-10-04")
    assert codes(server.press_health(data_dir, AT, now=MORNING)) == ["fetcher_stopped"]


def test_a_timer_run_fetcher_is_only_due_at_each_edition_time(data_dir):
    print_edition(data_dir, "2026-10-04")
    evening = dt.datetime(2026, 10, 4, 21, 0)
    heartbeat(data_dir, dt.datetime(2026, 10, 4, 6, 40, 3), loop=False)
    assert server.press_health(data_dir, AT, now=evening)["ok"] is True
    heartbeat(data_dir, dt.datetime(2026, 10, 3, 9, 0), loop=False)
    health = server.press_health(data_dir, AT, now=evening)
    assert codes(health) == ["fetcher_stopped"]
    assert "did not run at 06:40" in health["problems"][0]["message"]


def test_a_nearly_full_disk_is_a_problem(data_dir, monkeypatch):
    healthy(data_dir)
    usage = type(server.shutil.disk_usage(data_dir))
    monkeypatch.setattr(server.shutil, "disk_usage",
                        lambda path: usage(10**10, 10**10 - 50 * 2**20, 50 * 2**20))
    health = server.press_health(data_dir, AT, now=MORNING)
    assert codes(health) == ["disk_low"]
    assert health["free_mb"] == 50


def test_a_section_left_out_is_a_warning_not_a_problem(data_dir):
    healthy(data_dir)
    staging.save_report(data_dir, {"status": "partial", "sections": [
        {"title": "World", "status": "ok", "headlines": ["Harbour bridge reopens"]},
        {"title": "Dry Well", "status": "failed", "error": "timed out after 60s"}]})
    health = server.press_health(data_dir, AT, now=MORNING)
    assert health["ok"] is True
    assert codes(health, "warnings") == ["section_left_out"]
    assert "'Dry Well'" in health["warnings"][0]["message"]


def test_the_fetcher_signs_in(data_dir, monkeypatch):
    monkeypatch.setattr(press_fetch, "write_paper", lambda *args, **kwargs: 0)
    assert press_fetch.main(["--data-dir", str(data_dir)]) == 0
    assert staging.load_heartbeat(data_dir)["loop"] is False
    # A reprint someone asked for says nothing about the schedule.
    heartbeat(data_dir, MORNING, loop=True)
    assert press_fetch.main(["--data-dir", str(data_dir), "--force"]) == 0
    assert staging.load_heartbeat(data_dir) == {"at": "2026-10-04T08:00:00", "loop": True}


def test_health_over_http_answers_503_when_something_is_wrong(data_dir, press):
    base = press()
    code, health = call(f"{base}/api/health")
    assert code == 503 and health["ok"] is False
    now = dt.datetime.now()
    healthy(data_dir, now)
    code, health = call(f"{base}/api/health")
    assert code == 200 and health["ok"] is True


# --- Status only, never story text -------------------------------------------


def test_status_and_health_carry_no_story_text(data_dir, config_dir, feeds, press):
    (config_dir / "sources.toml").write_text(textwrap.dedent(f'''
        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
        stories = 2
    '''), encoding="utf-8")
    assert press_fetch.write_paper(data_dir, config_dir, at=AT, fetch_weather=False,
                                   force=False, keep_days=14) == 0
    headlines = staging.load_report(data_dir)["sections"][0]["headlines"]
    assert headlines

    base = press(TOKEN)
    _, status = call(f"{base}/api/status")
    _, health = call(f"{base}/api/health")
    assert status["today"] == dt.date.today().isoformat()
    assert status["sections"][0]["title"] == "World"
    assert status["sections"][0]["stories"] == 2
    for answer in (json.dumps(status), json.dumps(health)):
        assert "headlines" not in answer
        assert not any(headline in answer for headline in headlines)


# --- Rebuild and its token ---------------------------------------------------


def test_rebuild_is_off_when_the_press_has_no_token(press, fake_rebuild):
    base = press()
    for offered in (None, TOKEN, ""):
        code, answer = call(f"{base}/api/rebuild", "POST", offered)
        assert code == 403 and answer["ok"] is False
        assert "off" in answer["error"]
    assert fake_rebuild == []


def test_an_empty_token_setting_counts_as_none(press, fake_rebuild):
    # docker compose passes PRESS_TOKEN through as "" when it is not set.
    base = press("  ")
    request = urllib.request.Request(f"{base}/api/rebuild", method="POST", data=b"",
                                     headers={"Authorization": "Bearer "})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=5)
    assert refused.value.code == 403
    assert fake_rebuild == []


def test_rebuild_refuses_without_the_right_token(press, fake_rebuild):
    base = press(TOKEN)
    for offered in (None, "wrong", TOKEN + "x", TOKEN[:-1]):
        code, answer = call(f"{base}/api/rebuild", "POST", offered)
        assert code == 401 and answer["ok"] is False
    # The token is only taken from the Authorization header.
    code, _ = call(f"{base}/api/rebuild?token={TOKEN}", "POST")
    assert code == 401
    assert fake_rebuild == []


def test_rebuild_is_not_a_get(press, fake_rebuild):
    base = press(TOKEN)
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/rebuild", headers={"Authorization": f"Bearer {TOKEN}"}), timeout=5)
    assert refused.value.code == 404
    assert fake_rebuild == []


def test_rebuild_with_the_token_runs_the_fetcher(data_dir, press, fake_rebuild):
    print_edition(data_dir, dt.date.today().isoformat())
    code, answer = call(f"{press(TOKEN)}/api/rebuild", "POST", TOKEN)
    assert code == 200
    assert answer == {"ok": True, "today": dt.date.today().isoformat(),
                      "edition_date": dt.date.today().isoformat(), "detail": "printed"}
    assert fake_rebuild == [data_dir]


def test_a_failed_rebuild_says_why(press, monkeypatch):
    monkeypatch.setattr(server, "run_rebuild",
                        lambda data_dir: {"ok": False, "detail": "error: every section failed"})
    code, answer = call(f"{press(TOKEN)}/api/rebuild", "POST", TOKEN)
    assert code == 500
    assert (answer["ok"], answer["detail"]) == (False, "error: every section failed")


def test_only_one_rebuild_runs_at_a_time(press, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(data_dir: Path) -> dict:
        started.set()
        release.wait(10)
        return {"ok": True, "detail": ""}

    monkeypatch.setattr(server, "run_rebuild", slow)
    base = press(TOKEN)
    first = []
    thread = threading.Thread(
        target=lambda: first.append(call(f"{base}/api/rebuild", "POST", TOKEN)))
    thread.start()
    assert started.wait(10)
    code, answer = call(f"{base}/api/rebuild", "POST", TOKEN)
    release.set()
    thread.join(10)
    assert code == 409 and "already running" in answer["error"]
    assert first[0][0] == 200


def test_rebuild_really_reprints_the_paper(data_dir, config_dir, feeds, press, monkeypatch):
    (config_dir / "sources.toml").write_text(textwrap.dedent(f'''
        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
    '''), encoding="utf-8")
    monkeypatch.setenv("PRESS_CONFIG_DIR", str(config_dir))
    # A test must not call the forecast service.
    monkeypatch.setattr(server, "REBUILD_ARGS", ["--force"])
    today = dt.date.today().isoformat()

    code, answer = call(f"{press(TOKEN)}/api/rebuild", "POST", TOKEN)
    assert code == 200, answer
    assert answer["ok"] is True and answer["edition_date"] == today
    assert answer["detail"].startswith(f"printed {today}")
    assert (data_dir / f"morning-paper-{today}.epub").is_file()
    first = staging.load_receipt(data_dir)["built_at"]
    assert first

    # Already printed today, and still it prints again.
    code, answer = call(f"{press(TOKEN)}/api/rebuild", "POST", TOKEN)
    assert code == 200 and answer["ok"] is True


def test_the_fetcher_is_not_handed_the_token(data_dir, monkeypatch):
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, env=kwargs["env"])
        return server.subprocess.CompletedProcess(command, 0, "printed", "")

    monkeypatch.setenv(server.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(server.subprocess, "run", run)
    assert server.run_rebuild(data_dir) == {"ok": True, "detail": "printed"}
    assert "--force" in seen["command"] and str(data_dir) in seen["command"]
    assert server.TOKEN_ENV not in seen["env"]
    assert TOKEN not in " ".join(seen["command"])


def test_the_token_never_appears_in_logs_status_or_health(data_dir, press, fake_rebuild,
                                                          capfd):
    healthy(data_dir, dt.datetime.now())
    base = press(TOKEN)
    assert call(f"{base}/api/rebuild", "POST", TOKEN)[0] == 200
    assert call(f"{base}/api/rebuild", "POST", "wrong")[0] == 401
    # Sent where it does not belong: refused, and still not kept.
    assert call(f"{base}/api/rebuild?token={TOKEN}", "POST")[0] == 401
    # The same in request lines too malformed to be read, each of which the
    # server turns away in a different way.
    for line in (f"POST /api/rebuild?token={TOKEN}&note=two words HTTP/1.1",
                 f"POST /api/rebuild?note=two words&token={TOKEN} HTTP/1.1",
                 f"POST /api/rebuild?note=two words&token={TOKEN}"):
        assert b"Error code: 400" in send_raw(base, line)
    _, status = call(f"{base}/api/status")
    _, health = call(f"{base}/api/health")
    assert health["rebuild_enabled"] is True
    assert TOKEN not in json.dumps(status) + json.dumps(health)

    captured = capfd.readouterr()
    assert "/api/rebuild" in captured.err  # the request log is being captured
    assert captured.err.count(" 400 ") >= 3  # and the refusals are in it
    assert TOKEN not in captured.out + captured.err
    for path in data_dir.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path


# --- The mnn command ---------------------------------------------------------


def test_mnn_status_says_the_paper_landed(data_dir, press, tmp_path, capsys):
    today = dt.date.today().isoformat()
    print_edition(data_dir, today, kindle_at=f"{today}T06:45:31-04:00")
    settings = write_settings(tmp_path, press())
    assert mnn.main(["status", "--config", settings]) == 0
    out = capsys.readouterr().out
    assert "is printed, and the Kindle collected it at 06:45." in out

    assert mnn.main(["status", "--config", settings, "--json"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["edition_date"] == answer["today"] == today


def test_mnn_status_when_the_kindle_has_not_collected_it(data_dir, press, tmp_path, capsys):
    print_edition(data_dir, dt.date.today().isoformat())
    assert mnn.main(["status", "--config", write_settings(tmp_path, press())]) == 0
    assert "The Kindle has not collected this printing yet." in capsys.readouterr().out


def test_mnn_status_after_a_reprint_speaks_of_that_printing(data_dir, config_dir, feeds, press,
                                                            tmp_path, capsys):
    (config_dir / "sources.toml").write_text(textwrap.dedent(f'''
        [[section]]
        title = "World"
        feeds = ["{feeds}/world.rss"]
    '''), encoding="utf-8")

    def reprint() -> int:
        return press_fetch.write_paper(data_dir, config_dir, at=AT, fetch_weather=False,
                                       force=True, keep_days=14)

    today = dt.date.today().isoformat()
    settings = write_settings(tmp_path, press())
    assert reprint() == 0
    assert staging.mark_kindle_download(data_dir, today, f"{today}T06:45:31-04:00", "192.0.2.7")
    assert mnn.main(["status", "--config", settings]) == 0
    assert "the Kindle collected it at 06:45." in capsys.readouterr().out

    # The Kindle keeps its morning copy; the receipt is now the reprint's.
    assert reprint() == 0
    assert mnn.main(["status", "--config", settings]) == 0
    out = capsys.readouterr().out
    assert "is printed. The Kindle has not collected this printing yet." in out


def test_mnn_status_leaves_the_kindle_out_after_a_failed_reprint(data_dir, press, tmp_path,
                                                                capsys):
    today = dt.date.today().isoformat()
    print_edition(data_dir, today, kindle_at=f"{today}T06:45:31-04:00")
    # What the fetcher leaves behind when a reprint fails: the morning's
    # paper still there, and a receipt that knows nothing of its collection.
    staging.save_receipt(data_dir, {"edition_date": today, "status": "failed",
                                    "error": "every section failed",
                                    "kindle_downloaded_at": None})
    assert mnn.main(["status", "--config", write_settings(tmp_path, press())]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Today's paper (") and "is printed.\n" in out
    assert "The last print failed: every section failed" in out
    assert "Kindle" not in out


def test_mnn_status_exits_1_when_today_is_not_printed(data_dir, press, tmp_path, capsys):
    settings = write_settings(tmp_path, press())
    assert mnn.main(["status", "--config", settings]) == 1
    assert capsys.readouterr().out == "No paper has been printed yet.\n"

    print_edition(data_dir, "2026-01-02")
    staging.save_report(data_dir, {"status": "partial", "sections": [
        {"title": "Dry Well", "status": "failed", "error": "timed out after 60s"}]})
    assert mnn.main(["status", "--config", settings]) == 1
    out = capsys.readouterr().out
    assert "has not been printed. The newest edition is from Friday, January 2." in out
    assert "Left out: Dry Well (timed out after 60s)." in out


def test_mnn_health(data_dir, press, tmp_path, capsys):
    settings = write_settings(tmp_path, press())
    assert mnn.main(["health", "--config", settings]) == 1
    out = capsys.readouterr().out
    assert out.startswith("The Press has a problem:\n  - no edition has been printed yet")

    healthy(data_dir, dt.datetime.now())
    assert mnn.main(["health", "--config", settings]) == 0
    assert capsys.readouterr().out == "The Press is fine.\n"

    assert mnn.main(["health", "--config", settings, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_mnn_rebuild_sends_the_token_from_its_settings_file(data_dir, press, tmp_path,
                                                            fake_rebuild, capsys):
    print_edition(data_dir, dt.date.today().isoformat())
    settings = write_settings(tmp_path, press(TOKEN), TOKEN)
    assert mnn.main(["rebuild", "--config", settings]) == 0
    assert capsys.readouterr().out.startswith("Reprinted today's paper (")
    assert len(fake_rebuild) == 1

    assert mnn.main(["rebuild", "--config", settings, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_mnn_rebuild_reports_a_failed_reprint(press, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server, "run_rebuild",
                        lambda data_dir: {"ok": False, "detail": "error: every section failed"})
    settings = write_settings(tmp_path, press(TOKEN), TOKEN)
    assert mnn.main(["rebuild", "--config", settings]) == 1
    assert capsys.readouterr().out == "The reprint failed: error: every section failed\n"


def test_mnn_rebuild_exits_4_when_refused(press, tmp_path, fake_rebuild, capsys):
    # No token in the settings file: nothing is sent at all.
    assert mnn.main(["rebuild", "--config", write_settings(tmp_path, press(TOKEN))]) == 4
    assert "needs the Press token" in capsys.readouterr().err
    # The wrong token.
    assert mnn.main(["rebuild", "--config", write_settings(tmp_path, press(TOKEN), "wrong")]) == 4
    assert "The Press refused" in capsys.readouterr().err
    # Rebuild is off at the Press.
    settings = write_settings(tmp_path, press(), TOKEN)
    assert mnn.main(["rebuild", "--config", settings]) == 4
    assert "rebuild is off" in capsys.readouterr().err
    assert mnn.main(["rebuild", "--config", settings, "--json"]) == 4
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is False
    assert fake_rebuild == []


def test_mnn_exits_3_when_the_press_is_unreachable(unreachable, tmp_path, capsys):
    address = unreachable.rsplit("/", 1)[0]
    assert mnn.main(["health", "--config", write_settings(tmp_path, address)]) == 3
    assert f"Could not reach the Press at {address}" in capsys.readouterr().err
    assert mnn.main(["status", "--url", address, "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_mnn_exits_3_when_the_address_is_not_a_press(feeds, capsys):
    assert mnn.main(["health", "--url", feeds]) == 3
    assert "did not answer as the Press does (HTTP 404)" in capsys.readouterr().err


def test_mnn_exits_3_when_the_address_does_not_speak_http(tmp_path, capsys):
    with socket.create_server(("127.0.0.1", 0)) as listener:
        def greet() -> None:
            for _ in range(2):
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    connection.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")

        greeter = threading.Thread(target=greet, daemon=True)
        greeter.start()
        settings = write_settings(tmp_path, f"http://127.0.0.1:{listener.getsockname()[1]}")
        assert mnn.main(["health", "--config", settings]) == 3
        assert "did not answer as the Press does" in capsys.readouterr().err
        assert mnn.main(["health", "--config", settings, "--json"]) == 3
        assert json.loads(capsys.readouterr().out)["ok"] is False
        greeter.join(5)


def test_mnn_status_exits_3_when_the_press_is_out_of_date(data_dir, press, tmp_path,
                                                          monkeypatch, capsys):
    today = dt.date.today().isoformat()
    print_edition(data_dir, today)
    # An older Press answers /api/status without saying what day it is.
    monkeypatch.setattr(server, "press_status", lambda data_dir: {
        "mode": "sources", "edition_date": today, "last_print": {"edition_date": today}})
    settings = write_settings(tmp_path, press())
    assert mnn.main(["status", "--config", settings]) == 3
    captured = capsys.readouterr()
    assert "did not answer as the Press does (HTTP 200). Is the Press up to date?" in captured.err
    assert captured.out == ""
    assert mnn.main(["status", "--config", settings, "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_mnn_exits_2_when_the_address_or_token_cannot_be_sent(press, tmp_path, fake_rebuild,
                                                              capsys):
    settings = write_settings(tmp_path, press(TOKEN), TOKEN)
    assert mnn.main(["status", "--config", settings, "--url", "http://127.0.0.1:port"]) == 2
    assert "http://127.0.0.1:port" in capsys.readouterr().err
    assert mnn.main(["status", "--config", settings, "--url", "http://127.0.0.1:8484/a b"]) == 2
    assert "cannot be used" in capsys.readouterr().err
    assert mnn.main(["status", "--config", settings, "--url", "http://127.0.0.1:port",
                     "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False

    # A stray space inside the address in the settings file.
    assert mnn.main(["status", "--config",
                     write_settings(tmp_path, "http://127.0.0.1:84 84")]) == 2
    capsys.readouterr()

    # A token no HTTP header can carry: said plainly, and not echoed.
    snowman = TOKEN + "\u2603"
    assert mnn.main(["rebuild", "--config", write_settings(tmp_path, press(TOKEN), snowman)]) == 2
    captured = capsys.readouterr()
    assert "token" in captured.err
    assert TOKEN not in captured.err and "\u2603" not in captured.err
    assert "\\u2603" not in captured.err
    assert fake_rebuild == []


def test_mnn_runs_with_the_bare_environment_a_muse_gadget_gives_it(data_dir, press, tmp_path):
    """A Muse gadget's system.run starts a command with HOME, a system PATH
    and little else. The settings file under that home is found on its own."""
    home = tmp_path / "home"
    (home / ".config" / "mnn").mkdir(parents=True)
    (home / ".config" / "mnn" / "config").write_text(
        f"url = {press(TOKEN)}\ntoken = {TOKEN}\n", encoding="utf-8")
    healthy(data_dir, dt.datetime.now())
    result = subprocess.run(
        [str(REPO / "bin" / "mnn"), "health"], capture_output=True, text=True, timeout=30,
        env={"HOME": str(home), "LANG": "C.UTF-8",
             "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})
    assert (result.returncode, result.stdout, result.stderr) == (0, "The Press is fine.\n", "")


def test_mnn_asks_the_press_directly_whatever_proxy_the_environment_names(
        data_dir, press, tmp_path, unreachable, fake_rebuild):
    """A fresh process, because that is when a proxy in the environment is
    read. Nothing listens at the proxy, so only a direct request can work."""
    proxy = unreachable.rsplit("/", 1)[0]
    home = tmp_path / "home"
    (home / ".config" / "mnn").mkdir(parents=True)
    (home / ".config" / "mnn" / "config").write_text(
        f"url = {press(TOKEN)}\ntoken = {TOKEN}\n", encoding="utf-8")
    healthy(data_dir, dt.datetime.now())
    env = {"HOME": str(home), "LANG": "C.UTF-8",
           "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           "http_proxy": proxy, "HTTP_PROXY": proxy}
    for command, says in (("health", "The Press is fine.\n"),
                          ("rebuild", "Reprinted today's paper (")):
        result = subprocess.run([str(REPO / "bin" / "mnn"), command], capture_output=True, text=True,
                                timeout=30, env=env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith(says)
    assert len(fake_rebuild) == 1


def test_mnn_without_a_settings_file_uses_the_default_address(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mnn, "CONFIG_PATHS", ("~/.config/mnn/config", str(tmp_path / "etc")))
    assert mnn.load_settings(None) == {}
    asked = []
    monkeypatch.setattr(mnn, "ask", lambda url, command, token: (
        asked.append((url, command, token)), (200, {"ok": True}))[1])
    assert mnn.main(["health"]) == 0
    assert asked == [("http://127.0.0.1:8484", "health", None)]


def test_mnn_reads_the_machine_wide_file_when_the_account_has_none(
        data_dir, press, tmp_path, unreachable, fake_rebuild, monkeypatch, capsys):
    """/etc/mnn/config (a stand-in for it here) serves an account with no file
    of its own, token included; the account's own file comes first."""
    home, machine_wide = tmp_path / "home", tmp_path / "etc" / "mnn" / "config"
    machine_wide.parent.mkdir(parents=True)
    machine_wide.write_text(f"url = {press(TOKEN)}\ntoken = {TOKEN}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(mnn, "CONFIG_PATHS", ("~/.config/mnn/config", str(machine_wide)))
    healthy(data_dir, dt.datetime.now())
    assert mnn.main(["health"]) == 0
    assert capsys.readouterr().out == "The Press is fine.\n"
    assert mnn.main(["rebuild"]) == 0
    assert capsys.readouterr().out.startswith("Reprinted today's paper (")
    assert len(fake_rebuild) == 1

    own = home / ".config" / "mnn" / "config"
    own.parent.mkdir(parents=True)
    address = unreachable.rsplit("/", 1)[0]
    own.write_text(f"url = {address}\n", encoding="utf-8")
    assert mnn.main(["health"]) == 3
    assert f"Could not reach the Press at {address}" in capsys.readouterr().err


def test_mnn_settings_file_mistakes_exit_2_without_echoing_the_line(tmp_path, capsys):
    path = tmp_path / "mnn.conf"
    path.write_text(f"url = http://127.0.0.1:8484\ntokn = {TOKEN}\n", encoding="utf-8")
    assert mnn.main(["status", "--config", str(path)]) == 2
    err = capsys.readouterr().err
    assert "line 2" in err and TOKEN not in err

    assert mnn.main(["status", "--config", str(tmp_path / "missing")]) == 2
    assert "no settings file" in capsys.readouterr().err

    path.write_text("url = ftp://press\n", encoding="utf-8")
    assert mnn.main(["status", "--config", str(path)]) == 2


def test_mnn_only_sends_the_token_to_rebuild(monkeypatch):
    sent = []

    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"today": "2026-10-05"}'

    def open_request(request, timeout):
        sent.append((request.get_method(), request.full_url, request.get_header("Authorization")))
        return Reply()

    monkeypatch.setattr(mnn.DIRECT, "open", open_request)
    for command in ("status", "health", "rebuild"):
        mnn.ask("http://press:8484", command, TOKEN)
    assert sent == [("GET", "http://press:8484/api/status", None),
                    ("GET", "http://press:8484/api/health", None),
                    ("POST", "http://press:8484/api/rebuild", f"Bearer {TOKEN}")]
