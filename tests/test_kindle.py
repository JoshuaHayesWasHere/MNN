"""The Kindle flow, end to end on this machine: the Press serves the scripts,
a shell installs and runs them against it, and the Press records what the
"Kindle" reports. Nothing here needs a real device or the network."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mnn import server
from mnn import staging

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((REPO / "kindle").glob("*.sh"))
# The Kindle's shell is BusyBox ash, so use it where it is installed.
SHELL = ["busybox", "sh"] if shutil.which("busybox") else ["sh"]
TODAY = dt.date.today().isoformat()
YESTERDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()
# Old enough to be late whatever time of day the tests run.
LAST_WEEK = (dt.date.today() - dt.timedelta(days=7)).isoformat()


def epub_bytes(text: str = "paper") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("content.txt", text, zipfile.ZIP_STORED)
    return buffer.getvalue()


def print_edition(data_dir: Path, date: str, text: str = "paper") -> None:
    (data_dir / f"morning-paper-{date}.epub").write_bytes(epub_bytes(text))
    (data_dir / f"frontpage-{date}.png").write_bytes(f"\x89PNG not a real page {date} {text}".encode())


@pytest.fixture
def press(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A Press on 127.0.0.1 with an empty data directory."""
    monkeypatch.delenv("STAGE_URL", raising=False)
    monkeypatch.delenv("STAGE_TOKEN", raising=False)
    data_dir = tmp_path / "data"
    (data_dir / "press").mkdir(parents=True)
    httpd = server.PaperServer(("127.0.0.1", 0), data_dir=data_dir,
                               log_path=data_dir / "server.log", refresh_rate=3600,
                               base_url=None, edition_time=dt.time(6, 40))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def get(url: str, **headers: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post_log(press, text: str) -> None:
    request = urllib.request.Request(f"{press.url}/api/log", data=text.encode(), method="POST")
    with urllib.request.urlopen(request, timeout=10) as reply:
        assert reply.status == 204


# --- The Press endpoints -----------------------------------------------------


def test_the_home_page_waits_for_the_first_paper(press):
    status, body = get(f"{press.url}/")
    page = body.decode()
    assert status == 200 and "Nothing has been printed yet" in page
    assert 'http-equiv="refresh"' in page and "<img" not in page


def test_the_home_page_shows_the_newest_paper_and_the_ways_onto_a_reader(press):
    print_edition(press.data_dir, "2026-10-03")
    print_edition(press.data_dir, TODAY)
    page = get(f"{press.url}/")[1].decode()
    assert f'href="/paper/morning-paper-{TODAY}.epub"' in page
    assert '<img src="/frontpage.png"' in page and 'http-equiv="refresh"' not in page
    assert "/kindle/install.sh?download" in page and f"{press.url}/opds" in page
    assert "Earlier papers" in page and "morning-paper-2026-10-03.epub" in page


def test_the_home_page_never_carries_a_hostile_host_header(press):
    print_edition(press.data_dir, TODAY)
    page = get(f"{press.url}/", Host='x"><script>alert(1)</script>')[1].decode()
    assert "<script>" not in page


def test_install_script_carries_the_address_it_was_fetched_from(press):
    status, body = get(f"{press.url}/kindle/install.sh")
    assert status == 200
    script = body.decode()
    assert f'PRESS="{press.url}"' in script
    assert server.PRESS_PLACEHOLDER not in script


def test_install_script_uses_the_host_the_kindle_asked_for(press):
    _, body = get(f"{press.url}/kindle/install.sh", Host="192.168.1.50:8484")
    assert 'PRESS="http://192.168.1.50:8484"' in body.decode()


def test_install_script_can_be_saved_as_a_file_for_the_kindle(press):
    with urllib.request.urlopen(f"{press.url}/kindle/install.sh?download", timeout=10) as reply:
        assert reply.headers["Content-Disposition"] == 'attachment; filename="Install MNN.sh"'
        assert f'PRESS="{press.url}"' in reply.read().decode()
    with urllib.request.urlopen(f"{press.url}/kindle/install.sh", timeout=10) as reply:
        assert reply.headers["Content-Disposition"] is None
    with urllib.request.urlopen(f"{press.url}/kindle/bootstrap.sh?download", timeout=10) as reply:
        assert reply.headers["Content-Disposition"] is None


def test_a_hostile_host_header_never_reaches_the_script(press):
    _, body = get(f"{press.url}/kindle/install.sh", Host='x"; rm -rf /mnt/us; "')
    assert "rm -rf" not in body.decode()
    assert f'PRESS="{press.url}"' in body.decode()


def test_a_base_url_that_is_not_shell_safe_is_refused(press):
    press.base_url = 'http://press.example/$(reboot)'
    status, body = get(f"{press.url}/kindle/install.sh")
    assert status == 500
    assert b"reboot" not in body


@pytest.mark.parametrize("name", ["bootstrap.sh", "client.sh"])
def test_bootstrap_and_client_are_served_as_they_are(press, name):
    status, body = get(f"{press.url}/kindle/{name}")
    assert status == 200
    assert body == (REPO / "kindle" / name).read_bytes()


@pytest.mark.parametrize("name", ["README.md", "../server.py", "nothing.sh", "", "press/receipt.json",
                                  "server.log", "morning-paper-2026-01-01.epub"])
def test_nothing_but_the_scripts_and_stored_editions_is_served(press, name):
    status, _ = get(f"{press.url}/kindle/{name}")
    assert status == 404


def manifest_records(text: str) -> dict[str, list[str]]:
    lines = text.splitlines()
    assert lines[0] == "mnn 1" and lines[-1] == "end"
    return {line.split()[0]: line.split()[1:] for line in lines[1:-1]}


def test_the_manifest_pins_every_file_and_each_can_be_fetched(press):
    print_edition(press.data_dir, TODAY)
    status, body = get(f"{press.url}/kindle/manifest")
    assert status == 200
    records = manifest_records(body.decode())
    assert records["edition"][0] == TODAY

    name, digest, size = records["client"]
    client = (REPO / "kindle" / "client.sh").read_bytes()
    assert (name, digest, int(size)) == ("client.sh", staging.sha256(client), len(client))

    name, digest, size = records["paper"]
    _, paper = get(f"{press.url}/kindle/{name}")
    assert name == f"morning-paper-{TODAY}.epub"
    assert (staging.sha256(paper), len(paper)) == (digest, int(size))

    role, _, _, name, digest = records["screen"]
    _, page = get(f"{press.url}/kindle/{name}")
    assert (role, name) == ("ready", f"frontpage-{TODAY}.png")
    assert staging.sha256(page) == digest


MORNING = dt.time(6, 40)


def manifest_at(data_dir: Path, clock: str) -> dict[str, list[str]]:
    now = dt.datetime.combine(dt.date.today(), dt.time.fromisoformat(clock))
    return manifest_records(server.kindle_manifest(data_dir, now, MORNING, 3600))


def test_with_todays_paper_out_the_next_wake_is_tomorrow_morning(press):
    print_edition(press.data_dir, TODAY)
    records = manifest_at(press.data_dir, "06:45")
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    assert records["edition"] == [TODAY, "fresh"]
    assert records["next"] == [f"{tomorrow}T06:45", "in", "86400", "retry", "600"]


def test_before_the_edition_time_yesterdays_paper_is_still_fresh(press):
    print_edition(press.data_dir, YESTERDAY)
    records = manifest_at(press.data_dir, "05:45")
    assert records["edition"] == [YESTERDAY, "fresh"]
    assert records["next"][:3] == [f"{TODAY}T06:45", "in", "3600"]


def test_past_the_edition_time_without_todays_paper_it_is_late_and_retried(press):
    print_edition(press.data_dir, YESTERDAY)
    records = manifest_at(press.data_dir, "07:00")
    assert records["edition"] == [YESTERDAY, "late"]
    assert records["next"] == [f"{TODAY}T07:10", "in", "600", "retry", "600"]
    assert records["paper"][0] == f"morning-paper-{YESTERDAY}.epub"


def test_with_no_paper_at_all_the_manifest_still_names_the_client(press):
    records = manifest_at(press.data_dir, "12:00")
    assert records["edition"] == ["-", "missing"]
    assert "client" in records and "paper" not in records and "screen" not in records


def test_an_edition_without_its_front_page_is_not_offered_yet(press):
    print_edition(press.data_dir, YESTERDAY)
    (press.data_dir / f"morning-paper-{TODAY}.epub").write_bytes(epub_bytes())
    assert manifest_at(press.data_dir, "07:00")["edition"] == [YESTERDAY, "late"]


def test_display_is_unchanged(press):
    print_edition(press.data_dir, TODAY)
    _, body = get(f"{press.url}/api/display")
    assert set(json.loads(body)) == {"status", "image_url", "filename", "refresh_rate",
                                     "edition_date", "paper_url"}


# --- The receipt -------------------------------------------------------------


def test_a_download_is_recorded_with_its_edition_and_time(press):
    post_log(press, f"kindle: downloaded morning-paper-{TODAY}.epub to /mnt/us/documents\n")
    kindle = staging.load_kindle(press.data_dir)
    assert kindle["edition_date"] == TODAY
    assert dt.datetime.fromisoformat(kindle["downloaded_at"]).tzinfo is not None
    assert kindle["address"] == "127.0.0.1"
    assert kindle["last_seen_at"] == kindle["downloaded_at"]


def test_a_download_also_marks_the_receipt_for_that_edition(press):
    staging.save_receipt(press.data_dir, {"edition_date": TODAY, "kindle_downloaded_at": None})
    post_log(press, f"kindle: replaced morning-paper-{TODAY}.epub in /mnt/us/documents\n")
    assert staging.load_receipt(press.data_dir)["kindle_downloaded_at"] is not None


def test_lines_that_are_not_downloads_keep_the_last_download(press):
    post_log(press, f"kindle: downloaded morning-paper-{YESTERDAY}.epub to /x\n")
    first = staging.load_kindle(press.data_dir)
    post_log(press, f"kindle: already have morning-paper-{YESTERDAY}.epub\n"
                    f"kindle: today's edition is not out, keeping {YESTERDAY}\n")
    kindle = staging.load_kindle(press.data_dir)
    assert (kindle["edition_date"], kindle["downloaded_at"]) == (YESTERDAY, first["downloaded_at"])
    assert "last_warning" not in kindle


def test_a_warning_from_either_script_is_kept(press):
    post_log(press, "kindle-bootstrap: warning: the downloaded client does not parse, "
                    "running the last good copy\n")
    warning = staging.load_kindle(press.data_dir)["last_warning"]
    assert warning["text"].startswith("the downloaded client does not parse")
    post_log(press, "kindle: warning: download of morning-paper-2026-01-01.epub failed\n")
    assert "download of" in staging.load_kindle(press.data_dir)["last_warning"]["text"]


def test_other_devices_log_lines_are_not_taken_for_the_kindle(press):
    post_log(press, "thermostat: downloaded morning-paper-2026-01-01.epub\nhello\n")
    assert staging.load_kindle(press.data_dir) == {}
    assert "thermostat" in (press.data_dir / "server.log").read_text()


def test_paper_status_shows_the_kindles_last_download(press, monkeypatch):
    from mnn import press_commands as commands
    monkeypatch.setenv(server.DATA_DIR_ENV, str(press.data_dir))
    print_edition(press.data_dir, TODAY)
    assert commands.paper_status()["kindle"]["edition_date"] is None

    post_log(press, f"kindle: downloaded morning-paper-{TODAY}.epub to /mnt/us/documents\n")
    kindle = commands.paper_status()["kindle"]
    assert kindle["edition_date"] == TODAY
    assert kindle["downloaded_at"] == staging.load_kindle(press.data_dir)["downloaded_at"]


def test_the_status_endpoint_shows_the_kindles_last_download(press):
    _, body = get(f"{press.url}/api/status")
    assert json.loads(body)["kindle"]["edition_date"] is None

    post_log(press, f"kindle: downloaded morning-paper-{TODAY}.epub to /mnt/us/documents\n")
    _, body = get(f"{press.url}/api/status")
    kindle = json.loads(body)["kindle"]
    assert kindle["edition_date"] == TODAY
    assert kindle["downloaded_at"] == staging.load_kindle(press.data_dir)["downloaded_at"]


# --- The scripts -------------------------------------------------------------


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_pass_shellcheck_as_busybox_sh(script):
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(["shellcheck", "--shell=busybox", str(script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_parse_in_the_kindles_shell(script):
    assert subprocess.run([*SHELL, "-n", str(script)]).returncode == 0


@pytest.fixture
def kindle(tmp_path: Path, press):
    """A stand-in Kindle: an install directory and a library folder. Returns a
    function that runs a command in the Kindle's shell."""
    home = tmp_path / "mnn"
    library = tmp_path / "library"
    home.mkdir()
    library.mkdir()
    (home / "client.conf").write_text(f'MNN_LIBRARY="{library}"\n')
    # Waiting is not what is being tested: sleeps of the retry loop are skipped.
    shims = tmp_path / "bin"
    shims.mkdir()
    (shims / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (shims / "sleep").chmod(0o755)
    # The screen: a stand-in for the Kindle's eips that notes what it was given.
    screen = tmp_path / "screen"
    screen.mkdir()
    (screen / "eips").write_text(f'#!/bin/sh\necho "$@" >>"{screen}/drawn"\n')
    (screen / "eips").chmod(0o755)
    # The Kindle's power service: it announces one suspend, then the "Kindle"
    # sleeps; and it notes what it was asked for. That sleep is the real one,
    # whatever the shim above does to the others.
    (screen / "lipc-wait-event").write_text(
        f'#!/bin/sh\n[ -e "{screen}/suspended" ] && exec "{shutil.which("sleep")}" 600\n'
        f'touch "{screen}/suspended"\necho "readyToSuspend 1"\n')
    (screen / "lipc-wait-event").chmod(0o755)
    (screen / "lipc-set-prop").write_text(f'#!/bin/sh\necho "$@" >>"{screen}/props"\n')
    (screen / "lipc-set-prop").chmod(0o755)

    def run(command: str, *, fast: bool = False) -> subprocess.CompletedProcess:
        env = {**os.environ, "MNN_DIR": str(home),
               "PATH": f"{screen}{os.pathsep}{os.environ['PATH']}"}
        shell = SHELL
        if fast:
            env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
            # BusyBox's shell can have sleep built in, and then never looks
            # for the stand-in above. Plain sh finds it on PATH.
            shell = ["sh"]
        return subprocess.run([*shell, "-c", command], env=env, capture_output=True,
                              text=True, timeout=60)

    run.home, run.library, run.drawn, run.props = home, library, screen / "drawn", screen / "props"
    run.bin = screen
    yield run
    pid_file = home / "bootstrap.pid"
    if pid_file.exists():
        os.kill(int(pid_file.read_text()), signal.SIGTERM)


def fetch_command(press, name: str) -> str:
    tool = "curl -fsS" if shutil.which("curl") else "wget -q -O -"
    return f"{tool} {press.url}/kindle/{name}"


def install(press, kindle) -> None:
    """The one line from the README, then stop the loop it starts once its
    first wake is over: these tests drive single cycles themselves."""
    result = kindle(f"{fetch_command(press, 'install.sh')} | sh")
    assert result.returncode == 0, result.stderr
    # The client has done its work and the "Kindle" has gone to sleep.
    wait_for(lambda: "going to sleep, RTC wake in" in (kindle.home / "mnn.log").read_text())
    stop(kindle)


def start(kindle, **kwargs) -> None:
    """Start the bootstrap's loop and leave it running."""
    kindle(f'sh "{kindle.home}/bootstrap.sh" >/dev/null 2>&1 </dev/null &', **kwargs)


def stop(kindle) -> None:
    pid = int((kindle.home / "bootstrap.pid").read_text())
    os.kill(pid, signal.SIGTERM)
    wait_for(lambda: not Path(f"/proc/{pid}").exists())


def sleeps(kindle) -> list[int]:
    """How long each scheduled wake so far meant to sleep, from the Kindle's
    own log."""
    return [int(seconds) for seconds in re.findall(
        r"kindle: sleeping for (\d+) seconds", (kindle.home / "mnn.log").read_text())]


def scheduled_wake(kindle, **kwargs) -> int:
    """One wake as the schedule runs it: start the loop, let the client go to
    sleep, stop the loop. Returns how long the client meant to sleep."""
    before = len(sleeps(kindle))
    start(kindle, **kwargs)
    wait_for(lambda: len(sleeps(kindle)) > before)
    stop(kindle)
    return sleeps(kindle)[-1]


def away_from_the_edition_time(press) -> None:
    """Put the Press's edition time an hour behind the clock, so what it tells
    the Kindle about a late paper does not depend on when the tests run."""
    press.edition_time = (dt.datetime.now() - dt.timedelta(hours=1)).time()


def wait_for(check, seconds: float = 20):
    deadline = time.monotonic() + seconds
    while True:
        try:
            if value := check():
                return value
        except (OSError, ValueError):
            pass
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.1)


def once(kindle, **kwargs) -> subprocess.CompletedProcess:
    return kindle(f'sh "{kindle.home}/bootstrap.sh" once', **kwargs)


def press_log(press) -> str:
    log = press.data_dir / "server.log"
    return log.read_text() if log.exists() else ""


def test_the_one_line_install_fetches_the_paper_and_reports_it(press, kindle):
    print_edition(press.data_dir, TODAY)
    result = kindle(f"{fetch_command(press, 'install.sh')} | sh")
    assert result.returncode == 0, result.stderr
    assert (kindle.home / "press.conf").read_text() == f'PRESS="{press.url}"\n'
    assert (kindle.home / "bootstrap.sh").read_bytes() == (REPO / "kindle/bootstrap.sh").read_bytes()

    paper = kindle.library / f"morning-paper-{TODAY}.epub"
    wait_for(paper.exists)
    assert paper.read_bytes() == (press.data_dir / paper.name).read_bytes()
    wait_for(lambda: staging.load_kindle(press.data_dir).get("edition_date") == TODAY)
    # The front page it drew is the one the Press serves.
    # Nothing is drawn over whoever is using the Kindle. When its power service
    # is about to suspend, the front page goes up and an RTC wake is asked for.
    page = kindle.home / "screens" / f"frontpage-{TODAY}.png"
    wait_for(lambda: "going to sleep, RTC wake in" in (kindle.home / "mnn.log").read_text())
    assert kindle.drawn.read_text().split() == ["-f", "-g", str(page)]
    assert page.read_bytes() == (press.data_dir / page.name).read_bytes()
    prop = kindle.props.read_text().split()
    assert prop[:3] == ["-i", "com.lab126.powerd", "rtcWakeup"]
    wait = int((press.data_dir / "server.log").read_text().split("sleeping for ")[1].split()[0])
    assert wait - 30 <= int(prop[3]) <= wait
    # The bootstrap is still up, waiting for the next wake, and stops cleanly.
    pid = int((kindle.home / "bootstrap.pid").read_text())
    assert Path(f"/proc/{pid}").exists()
    os.kill(pid, signal.SIGTERM)
    wait_for(lambda: not Path(f"/proc/{pid}").exists())
    assert not (kindle.home / "bootstrap.pid").exists()


def test_installing_again_replaces_the_running_bootstrap(press, kindle):
    print_edition(press.data_dir, TODAY)
    kindle(f"{fetch_command(press, 'install.sh')} | sh")
    first = int(wait_for(lambda: (kindle.home / "bootstrap.pid").read_text()))
    kindle(f"{fetch_command(press, 'install.sh')} | sh")
    wait_for(lambda: not Path(f"/proc/{first}").exists())
    second = int(wait_for(lambda: (kindle.home / "bootstrap.pid").read_text()))
    assert second != first


def test_the_script_left_in_the_library_starts_it_again_after_a_restart(press, kindle):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)  # leaves nothing running, as after a restart
    start = kindle.library / "Start MNN.sh"
    assert kindle(f'sh "{start}"').returncode == 0
    pid = int(wait_for(lambda: (kindle.home / "bootstrap.pid").read_text()))
    assert Path(f"/proc/{pid}").exists()
    # Started twice, there is still one schedule.
    kindle(f'sh "{start}"')
    time.sleep(1)
    assert int((kindle.home / "bootstrap.pid").read_text()) == pid


def test_the_installer_copied_to_the_kindle_as_a_file_installs_it(press, kindle):
    """The install with nothing typed: the file saved from the Press, copied
    over USB, and run from KOReader's file browser."""
    print_edition(press.data_dir, TODAY)
    _, body = get(f"{press.url}/kindle/install.sh?download")
    saved = kindle.library / server.INSTALLER_NAME
    saved.write_bytes(body)
    result = kindle(f'sh "{saved}" </dev/null')
    assert result.returncode == 0, result.stderr
    wait_for(lambda: "going to sleep, RTC wake in" in (kindle.home / "mnn.log").read_text())
    stop(kindle)
    assert (kindle.home / "press.conf").read_text() == f'PRESS="{press.url}"\n'
    assert any(path.suffix == ".epub" for path in kindle.library.iterdir())


def test_install_refuses_a_copy_that_did_not_come_from_the_press(kindle):
    result = kindle(f'sh "{REPO}/kindle/install.sh"')
    assert result.returncode != 0
    assert "must be downloaded from the Press" in result.stderr
    assert not (kindle.home / "bootstrap.sh").exists()


def test_install_refuses_a_bootstrap_that_arrived_cut_short(press, kindle, served_scripts):
    whole = (served_scripts / "bootstrap.sh").read_text()
    # Short by its last line only, so what arrives still parses.
    (served_scripts / "bootstrap.sh").write_text(whole[:whole.rstrip("\n").rindex("\n") + 1])
    result = kindle(f"{fetch_command(press, 'install.sh')} | sh")
    assert result.returncode != 0
    assert "incomplete" in result.stderr
    assert not list(kindle.home.glob("bootstrap*"))


@pytest.fixture
def stalled_bootstrap(kindle):
    """A running bootstrap caught in a download that does not end until the
    connection this yields, with the bootstrap's pid, is closed."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(20)
        (kindle.home / "press.conf").write_text(
            f'PRESS="http://127.0.0.1:{listener.getsockname()[1]}"\n')
        shutil.copy(REPO / "kindle" / "bootstrap.sh", kindle.home)
        start(kindle)
        pid = int(wait_for((kindle.home / "bootstrap.pid").read_text))
        download, _ = listener.accept()
        with download:
            yield pid, download


def test_installing_again_waits_for_a_bootstrap_that_is_slow_to_stop(
        press, kindle, stalled_bootstrap):
    old, download = stalled_bootstrap
    with ThreadPoolExecutor() as pool:
        again = pool.submit(kindle, f"{fetch_command(press, 'install.sh')} | sh")
        # The old bootstrap cannot stop before its download ends, and the new
        # one is not started beside it.
        time.sleep(3)
        assert not again.done()
        download.close()
        assert again.result(timeout=30).returncode == 0
    assert not Path(f"/proc/{old}").exists()
    new = int(wait_for((kindle.home / "bootstrap.pid").read_text))
    assert new != old and Path(f"/proc/{new}").exists()


def test_a_bootstrap_slow_to_stop_leaves_the_next_ones_pid_file_alone(
        press, kindle, stalled_bootstrap):
    old, download = stalled_bootstrap
    pid_file = kindle.home / "bootstrap.pid"
    os.kill(old, signal.SIGTERM)
    # What install.sh does when it has waited long enough: start the next one.
    pid_file.unlink()
    (kindle.home / "press.conf").write_text(f'PRESS="{press.url}"\n')
    start(kindle)
    new = int(wait_for(pid_file.read_text))
    # Only now does the old one's download end, and the old bootstrap with it.
    download.close()
    wait_for(lambda: not Path(f"/proc/{old}").exists())
    assert int(pid_file.read_text()) == new
    assert Path(f"/proc/{new}").exists()


@pytest.fixture
def no_setsid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything on PATH but setsid, which not every Kindle's shell has."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for directory in os.get_exec_path():
        for tool in Path(directory).absolute().glob("*"):
            link = tools / tool.name
            if tool.name != "setsid" and not link.is_symlink():
                link.symlink_to(tool)
    monkeypatch.setenv("PATH", str(tools))


def test_without_setsid_it_still_installs_and_starts_again_after_a_restart(
        press, kindle, no_setsid):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)  # leaves nothing running, as after a restart
    assert kindle(f'sh "{kindle.library}/Start MNN.sh"').returncode == 0
    pid = int(wait_for((kindle.home / "bootstrap.pid").read_text))
    assert Path(f"/proc/{pid}").exists()


def test_a_cycle_downloads_once_and_then_keeps_what_it_has(press, kindle):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    first = staging.load_kindle(press.data_dir)["downloaded_at"]

    assert once(kindle).returncode == 0
    assert f"kindle: already have morning-paper-{TODAY}.epub" in press_log(press)
    assert staging.load_kindle(press.data_dir)["downloaded_at"] == first


def test_a_cycle_on_demand_draws_the_front_page_straight_away(press, kindle):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    kindle.drawn.unlink(missing_ok=True)
    assert once(kindle).returncode == 0
    page = kindle.home / "screens" / f"frontpage-{TODAY}.png"
    assert kindle.drawn.read_text().split() == ["-f", "-g", str(page)]
    # What it did without waiting on the network has reached the Press too.
    assert f"kindle: drew {page.name}" in press_log(press)
    assert not (kindle.home / "unsent.log").exists()


def test_screens_of_earlier_editions_are_cleared_out(press, kindle):
    print_edition(press.data_dir, YESTERDAY)
    install(press, kindle)
    print_edition(press.data_dir, TODAY)
    assert once(kindle).returncode == 0
    assert [path.name for path in (kindle.home / "screens").iterdir()] == [f"frontpage-{TODAY}.png"]


def test_a_reprinted_paper_replaces_the_copy_on_the_kindle(press, kindle):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    print_edition(press.data_dir, TODAY, "a longer, corrected paper")
    assert once(kindle).returncode == 0
    paper = kindle.library / f"morning-paper-{TODAY}.epub"
    assert paper.read_bytes() == (press.data_dir / paper.name).read_bytes()
    assert b"corrected" in paper.read_bytes()
    assert f"kindle: replaced morning-paper-{TODAY}.epub" in press_log(press)


def test_without_todays_edition_the_old_one_is_kept_and_that_is_logged(press, kindle):
    print_edition(press.data_dir, LAST_WEEK)
    install(press, kindle)
    assert once(kindle).returncode == 0
    assert (kindle.library / f"morning-paper-{LAST_WEEK}.epub").exists()
    assert f"kindle: today's edition is not out, keeping {LAST_WEEK}" in press_log(press)
    assert staging.load_kindle(press.data_dir)["edition_date"] == LAST_WEEK


def test_a_late_paper_is_checked_for_again_soon(press, kindle):
    away_from_the_edition_time(press)
    print_edition(press.data_dir, LAST_WEEK)
    result = kindle(f"{fetch_command(press, 'install.sh')} | sh")
    assert result.returncode == 0
    wait_for(lambda: "kindle: sleeping for 600 seconds" in press_log(press))


@pytest.mark.parametrize("name", [f"morning-paper-{TODAY}.epub", f"frontpage-{TODAY}.png"],
                         ids=["paper", "screen"])
def test_a_download_that_fails_is_tried_again_soon(press, kindle, monkeypatch, name):
    print_edition(press.data_dir, TODAY)
    manifest = server.kindle_manifest(press.data_dir, dt.datetime.now(), MORNING, 3600)
    monkeypatch.setattr(server, "kindle_manifest", lambda *args: manifest)
    # Today's edition is out, but one file arrives other than the manifest
    # describes it. Left at that, the next wake would be tomorrow's.
    (press.data_dir / name).write_bytes(b"not what the manifest describes")
    result = kindle(f"{fetch_command(press, 'install.sh')} | sh")
    assert result.returncode == 0, result.stderr
    wait_for(lambda: sleeps(kindle) == [600])


def test_a_paper_that_stays_late_is_looked_for_less_often(press, kindle):
    away_from_the_edition_time(press)
    print_edition(press.data_dir, LAST_WEEK)
    install(press, kindle)
    for _ in range(12):
        assert once(kindle).returncode == 0
    # The fourteenth wake in a row without today's paper is the last quick try.
    assert scheduled_wake(kindle) == 600
    assert scheduled_wake(kindle) == 3 * 3600

    # A wake that finds today's paper starts the count again.
    print_edition(press.data_dir, TODAY)
    assert once(kindle).returncode == 0
    (press.data_dir / f"morning-paper-{TODAY}.epub").unlink()
    (press.data_dir / f"frontpage-{TODAY}.png").unlink()
    assert scheduled_wake(kindle) == 600
    assert sleeps(kindle) == [600, 600, 3 * 3600, 600]


def test_a_press_that_cannot_be_reached_is_tried_hourly_then_every_six_hours(press, kindle):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    home = f'PRESS="{press.url}"\n'
    # Nothing listens there: the Press is off, or the Kindle is away from home.
    away = 'PRESS="http://127.0.0.1:1"\n'
    (kindle.home / "press.conf").write_text(away)
    assert [scheduled_wake(kindle, fast=True) for _ in range(4)] == [3600, 3600, 3600, 6 * 3600]

    # One complete wake at home starts the count again.
    (kindle.home / "press.conf").write_text(home)
    assert once(kindle).returncode == 0
    (kindle.home / "press.conf").write_text(away)
    assert scheduled_wake(kindle, fast=True) == 3600


def power_events(kindle, *events: str) -> None:
    """Have the stand-in power service announce these events, one to each
    listener in turn, and then stay quiet."""
    script = kindle.bin / "events"
    script.write_text("".join(f"{event}\n" for event in events))
    (kindle.bin / "lipc-wait-event").write_text(
        f'#!/bin/sh\nevent=$(head -n 1 "{script}")\n'
        f'[ -n "$event" ] || exec "{shutil.which("sleep")}" 600\n'
        f'sed -i 1d "{script}"\necho "$event"\n')


def test_the_front_page_is_drawn_again_when_the_end_of_a_screensaver_went_unheard(press, kindle):
    print_edition(press.data_dir, TODAY)
    # The Kindle sits in its screensaver. It is woken from a suspend by its
    # button and later put back to sleep, and the news that it had left its
    # screensaver in between never reaches the client.
    state = kindle.bin / "lipc-get-prop"
    state.write_text('#!/bin/sh\n[ "$2" = state ] && echo screenSaver\n')
    state.chmod(0o755)
    power_events(kindle, "wakeupFromSuspend 0", "goingToScreenSaver 2")
    result = kindle(f"{fetch_command(press, 'install.sh')} | sh", fast=True)
    assert result.returncode == 0, result.stderr
    page = kindle.home / "screens" / f"frontpage-{TODAY}.png"
    # Once as the paper arrived, and once for the screensaver that followed.
    wait_for(lambda: kindle.drawn.read_text().splitlines() == [f"-f -g {page}"] * 2)


def test_the_next_wake_is_never_sooner_than_the_bootstrap_would_start_one(press, kindle):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    # Any sooner, and the bootstrap would spend the difference with no client
    # running to hear the Kindle go to sleep.
    (kindle.home / "wake-in").write_text("120\n")
    assert scheduled_wake(kindle) == 300


def test_a_power_service_that_cannot_be_heard_is_not_asked_in_a_tight_loop(press, kindle):
    asked = kindle.bin / "asked"
    # It returns at once with no event, as when the service is not there.
    (kindle.bin / "lipc-wait-event").write_text(f'#!/bin/sh\necho asked >>"{asked}"\n')
    print_edition(press.data_dir, TODAY)
    result = kindle(f"{fetch_command(press, 'install.sh')} | sh")
    assert result.returncode == 0, result.stderr
    wait_for(asked.exists)
    time.sleep(1)
    # A tight loop would have asked hundreds of times by now.
    assert len(asked.read_text().splitlines()) <= 2


def test_old_papers_are_removed_but_the_newest_few_stay(press, kindle):
    print_edition(press.data_dir, TODAY)
    for day in range(1, 10):
        old = kindle.library / f"morning-paper-2026-01-{day:02d}.epub"
        old.write_bytes(b"PK old")
        Path(str(old).removesuffix(".epub") + ".sdr").mkdir()
    install(press, kindle)
    kept = sorted(path.name for path in kindle.library.iterdir() if path.suffix != ".sh")
    assert kept == sorted(
        [f"morning-paper-{TODAY}.epub"]
        + [f"morning-paper-2026-01-{day:02d}.{ext}" for day in range(4, 10)
           for ext in ("epub", "sdr")])


@pytest.fixture
def served_scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of kindle/ the Press serves instead, so a test can break it."""
    directory = tmp_path / "served"
    shutil.copytree(REPO / "kindle", directory)
    monkeypatch.setattr(server, "KINDLE_DIR", directory)
    return directory


def test_a_client_that_does_not_parse_is_not_run_and_the_last_good_copy_is(
        press, kindle, served_scripts):
    print_edition(press.data_dir, YESTERDAY)
    install(press, kindle)
    good = (kindle.home / "client.sh").read_bytes()

    (served_scripts / "client.sh").write_text("#!/bin/sh\nif then fi (\n")
    print_edition(press.data_dir, TODAY)
    assert once(kindle).returncode == 0
    assert (kindle.home / "client.sh").read_bytes() == good
    assert (kindle.library / f"morning-paper-{TODAY}.epub").exists()
    warning = staging.load_kindle(press.data_dir)["last_warning"]["text"]
    assert warning == "the downloaded client does not parse, running the last good copy"
    assert not list(kindle.home.glob("client.new*"))


def test_a_client_that_does_not_match_the_manifest_is_not_run(press, kindle, served_scripts,
                                                              monkeypatch):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    good = (kindle.home / "client.sh").read_bytes()

    # The manifest pins one client; what arrives is another.
    manifest = server.kindle_manifest(press.data_dir, dt.datetime.now(), MORNING, 3600)
    monkeypatch.setattr(server, "kindle_manifest", lambda *args: manifest.replace(
        staging.sha256(good), staging.sha256(b"the client the Press meant to serve")))
    (served_scripts / "client.sh").write_text("#!/bin/sh\ntouch \"$MNN_DIR/intruder-ran\"\n")
    assert once(kindle).returncode == 0
    assert (kindle.home / "client.sh").read_bytes() == good
    assert not (kindle.home / "intruder-ran").exists()
    warning = staging.load_kindle(press.data_dir)["last_warning"]["text"]
    assert warning == "the downloaded client does not match the manifest, running the last good copy"


def test_a_paper_that_does_not_match_the_manifest_is_not_filed(press, kindle, monkeypatch):
    print_edition(press.data_dir, TODAY)
    manifest = server.kindle_manifest(press.data_dir, dt.datetime.now(), MORNING, 3600)
    install(press, kindle)
    paper = kindle.library / f"morning-paper-{TODAY}.epub"
    paper.unlink()

    # The manifest describes one paper; what the Press hands over is another.
    monkeypatch.setattr(server, "kindle_manifest", lambda *args: manifest)
    print_edition(press.data_dir, TODAY, "not the paper the manifest describes")
    assert once(kindle).returncode == 0
    assert list(kindle.library.glob("*.epub")) == [] and list(kindle.library.glob(".*")) == []
    assert "did not match the manifest" in staging.load_kindle(press.data_dir)["last_warning"]["text"]


def test_when_the_manifest_cannot_be_had_the_kindle_keeps_what_it_has(press, kindle, served_scripts):
    print_edition(press.data_dir, YESTERDAY)
    install(press, kindle)
    (served_scripts / "client.sh").unlink()  # the Press cannot build a manifest
    print_edition(press.data_dir, TODAY)
    assert once(kindle, fast=True).returncode == 0
    assert (kindle.library / f"morning-paper-{YESTERDAY}.epub").exists()
    assert not (kindle.library / f"morning-paper-{TODAY}.epub").exists()
    assert "could not download the manifest" in staging.load_kindle(press.data_dir)["last_warning"]["text"]


def test_a_new_client_that_fails_outright_is_put_back_within_the_same_wake(
        press, kindle, served_scripts):
    print_edition(press.data_dir, YESTERDAY)
    install(press, kindle)
    good = (kindle.home / "client.sh").read_bytes()
    log = kindle.home / "mnn.log"
    asleep = log.read_text().count("going to sleep, RTC wake in")

    (served_scripts / "client.sh").write_text(
        '#!/bin/sh\necho broken >>"$MNN_DIR/broken-ran"\nexit 3\n')
    print_edition(press.data_dir, TODAY)
    # The next morning: a wake on the schedule, and a suspend after it.
    (kindle.bin / "suspended").unlink()
    kindle.props.unlink()
    start(kindle)
    wait_for(lambda: log.read_text().count("going to sleep, RTC wake in") > asleep)
    stop(kindle)

    # The new client ran once and was put back; the Press was told why.
    assert (kindle.home / "broken-ran").read_text().splitlines() == ["broken"]
    assert (kindle.home / "client.sh").read_bytes() == good
    warning = staging.load_kindle(press.data_dir)["last_warning"]["text"]
    assert warning == ("the new client exited with status 3 before completing a wake, "
                       "back to the previous one")
    # The last good client did that same wake's work: today's paper, its
    # front page on the screen, and an RTC wake asked for before the suspend.
    assert (kindle.library / f"morning-paper-{TODAY}.epub").exists()
    page = kindle.home / "screens" / f"frontpage-{TODAY}.png"
    assert kindle.drawn.read_text().splitlines()[-1].split() == ["-f", "-g", str(page)]
    assert kindle.props.read_text().split()[:3] == ["-i", "com.lab126.powerd", "rtcWakeup"]

    # The same bad build is still on the Press, and is not installed again.
    assert once(kindle).returncode == 0
    assert (kindle.home / "client.sh").read_bytes() == good
    assert (kindle.home / "broken-ran").read_text().splitlines() == ["broken"]


def test_a_client_that_never_completes_a_wake_is_put_back_and_not_installed_again(
        press, kindle, served_scripts):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    good = (kindle.home / "client.sh").read_bytes()

    # It returns without a complaint, and without doing a wake's work.
    idle = b'#!/bin/sh\necho idle >>"$MNN_DIR/idle-ran"\n'
    (served_scripts / "client.sh").write_bytes(idle)
    for _ in range(4):
        assert once(kindle).returncode == 0
        assert (kindle.home / "client.sh").read_bytes() == idle
    assert once(kindle).returncode == 0
    assert (kindle.home / "client.sh").read_bytes() == good
    assert len((kindle.home / "idle-ran").read_text().splitlines()) == 4
    assert "did not complete a healthy wake in three, back to the previous one" in press_log(press)
    # The same bad build is still on the Press, and is left there.
    assert once(kindle).returncode == 0
    assert (kindle.home / "client.sh").read_bytes() == good

    # A fixed client is a new build, and is taken.
    fixed = good + b"# fixed\n"
    (served_scripts / "client.sh").write_bytes(fixed)
    assert once(kindle).returncode == 0
    assert (kindle.home / "client.sh").read_bytes() == fixed
    assert not (kindle.home / "probation").exists()


def test_a_second_bad_client_does_not_cost_the_kindle_its_last_good_one(
        press, kindle, served_scripts):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    good = (kindle.home / "client.sh").read_bytes()

    # One build that does not complete a wake, then another before the first
    # has used up its tries.
    (served_scripts / "client.sh").write_text("#!/bin/sh\nexit 0\n")
    assert once(kindle).returncode == 0
    (served_scripts / "client.sh").write_text("#!/bin/sh\nexit 4\n")
    # What is put back is the client that worked, not the first bad build.
    assert once(kindle).returncode == 0
    assert (kindle.home / "client.sh").read_bytes() == good


def test_what_could_not_be_reported_is_sent_on_the_next_wake(press, kindle):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    (kindle.home / "unsent.log").write_text("kindle: warning: could not join Wi-Fi\n")
    assert once(kindle).returncode == 0
    assert not (kindle.home / "unsent.log").exists()
    assert "kindle: warning: could not join Wi-Fi" in press_log(press)


def test_a_new_client_reaches_the_kindle_without_reinstalling(press, kindle, served_scripts):
    print_edition(press.data_dir, TODAY)
    install(press, kindle)
    bootstrap = (kindle.home / "bootstrap.sh").read_bytes()
    client = (served_scripts / "client.sh").read_text()
    (served_scripts / "client.sh").write_text(
        client.replace("display_none() { :; }",
                       'display_none() { :; }\ndisplay_wall() { log "wall display drawn"; }')
              .replace("    none) display_none ;;",
                       "    none) display_none ;;\n    wall) display_wall ;;"))
    with (kindle.home / "client.conf").open("a") as conf:
        conf.write("MNN_DISPLAY=wall\n")
    assert once(kindle).returncode == 0
    assert "kindle: wall display drawn" in press_log(press)
    assert (kindle.home / "bootstrap.sh").read_bytes() == bootstrap
