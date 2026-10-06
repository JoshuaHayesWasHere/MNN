"""The Press's messages to a Muse gadget, against a fake gadget socket: what
is said, that it is said once, and that nothing depends on it being heard."""

from __future__ import annotations

import datetime as dt
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from mnn import muse
from mnn import press_fetch
from mnn import server
from mnn import staging
from fakes import FakeGadget

BUILT = dt.datetime(2026, 10, 5, 6, 41, 9).astimezone()
PLENTY = 50 * 1024 ** 3


def at(hour: int, minute: int) -> dt.datetime:
    return dt.datetime(2026, 10, 5, hour, minute).astimezone()


@pytest.fixture
def gadget(tmp_path):
    fake = FakeGadget(tmp_path / "musegadget.sock", {"ok": True})
    yield fake
    fake.listener.close()


@pytest.fixture(autouse=True)
def no_muse_settings(monkeypatch):
    for name in (muse.MODE_ENV, muse.DETAIL_ENV, muse.WAIT_ENV, muse.SOCKET_ENV,
                 muse.SESSION_ENV, muse.CARD_ENV):
        monkeypatch.delenv(name, raising=False)


def settings(gadget: FakeGadget, mode: str = "every", **changes) -> muse.Settings:
    return muse.Settings(mode=mode, socket_path=str(gadget.path), **changes)


def printed(data_dir: Path, kindle: str | None = None, status: str = "ok") -> None:
    """The records a morning's print from the Press's own sources leaves."""
    staging.save_receipt(data_dir, {
        "edition_date": "2026-10-05", "status": "ok", "error": None,
        "built_at": BUILT.isoformat(), "kindle_downloaded_at": kindle})
    sections = [
        {"title": "Front Page", "status": "ok", "stories": 3, "error": None,
         "headlines": ["Harbour bridge reopens", "Second story"]},
        {"title": "World", "status": "ok", "stories": 5, "error": None,
         "headlines": ["Talks resume\x1b\nin Geneva"]},
        {"title": "Science", "status": "ok", "stories": 4, "error": None, "headlines": []},
        {"title": "Sport", "status": "ok", "stories": 2, "error": None,
         "headlines": ["Late goal settles it"]},
    ]
    if status == "partial":
        sections.append({"title": "Tides", "status": "failed", "stories": 0,
                         "headlines": [], "error": "timed out after 60s"})
    staging.save_report(data_dir, {
        "mode": "sources", "edition_date": "2026-10-05", "status": status, "error": None,
        "ran_at": BUILT.isoformat(), "sections": sections})


def tick(data_dir: Path, config: muse.Settings, now: dt.datetime, free: int = PLENTY):
    return muse.tick(data_dir, config, now=now, free_bytes=free)


# --- The morning line ---------------------------------------------------------


def test_the_morning_line_waits_for_the_kindle_then_is_sent_once(data_dir, gadget):
    printed(data_dir)
    assert tick(data_dir, settings(gadget), at(6, 43)) == []
    assert gadget.requests == []

    staging.mark_kindle_download(data_dir, "2026-10-05", at(6, 46).isoformat(), "192.0.2.7")
    assert tick(data_dir, settings(gadget), at(6, 46)) == ["delivery"]
    assert gadget.requests == [{"message": (
        "Printed at 06:41, four sections, 14 stories. On the Kindle at 06:46. "
        + muse.SIGN_OFF)}]
    assert "No reply" in muse.SIGN_OFF

    # Nothing more that day, not even after the paper is printed again.
    printed(data_dir)
    assert tick(data_dir, settings(gadget), at(9, 0)) == []
    assert len(gadget.requests) == 1


def test_past_the_deadline_the_line_says_the_kindle_has_not_fetched(data_dir, gadget):
    printed(data_dir)
    assert tick(data_dir, settings(gadget), at(7, 40)) == []
    assert tick(data_dir, settings(gadget), at(7, 42)) == ["delivery"]
    assert gadget.messages[0].startswith(
        "Printed at 06:41, four sections, 14 stories. The Kindle had not fetched it by 07:42; "
        "it stays on the Press for the Kindle's next wake.")
    # A Kindle that turns up afterwards does not earn a second message.
    staging.mark_kindle_download(data_dir, "2026-10-05", at(8, 5).isoformat(), "192.0.2.7")
    assert tick(data_dir, settings(gadget), at(8, 5)) == []


@pytest.mark.parametrize("fetched", [False, True])
def test_with_no_wait_the_line_goes_at_once_and_leaves_the_kindle_out(data_dir, gadget,
                                                                      fetched):
    printed(data_dir, kindle=at(6, 42).isoformat() if fetched else None)
    tick(data_dir, settings(gadget, kindle_wait=None), at(6, 42))
    assert gadget.messages == [
        f"Printed at 06:41, four sections, 14 stories. {muse.SIGN_OFF}"]


def test_headlines_are_only_sent_when_asked_for_and_are_made_plain(data_dir, gadget):
    printed(data_dir, kindle=at(6, 46).isoformat())
    tick(data_dir, settings(gadget, detail="headlines"), at(6, 47))
    assert ('Leading: "Harbour bridge reopens"; "Talks resume in Geneva"; '
            '"Late goal settles it".') in gadget.messages[0]
    assert "\x1b" not in gadget.messages[0] and "\n" not in gadget.messages[0]


def test_a_staged_edition_gets_a_line_without_counts(data_dir, gadget, monkeypatch):
    printed(data_dir, kindle=at(6, 46).isoformat())  # a report left from before staging
    monkeypatch.setenv("STAGE_URL", "https://stage.example.com")
    tick(data_dir, settings(gadget), at(6, 47))
    assert gadget.messages == [f"Printed at 06:41. On the Kindle at 06:46. {muse.SIGN_OFF}"]


def test_a_side_chat_is_named_in_the_request(data_dir, gadget):
    printed(data_dir, kindle=at(6, 46).isoformat())
    tick(data_dir, settings(gadget, session_id="mnn-press"), at(6, 47))
    assert gadget.requests[0]["session_id"] == "mnn-press"


# --- Problems -----------------------------------------------------------------


def test_problems_only_is_silent_on_a_good_morning(data_dir, gadget):
    printed(data_dir, kindle=at(6, 46).isoformat())
    assert tick(data_dir, settings(gadget, "problems"), at(8, 0)) == []
    assert gadget.requests == []


def test_off_sends_nothing_whatever_happened(data_dir, gadget):
    staging.save_receipt(data_dir, {"edition_date": "2026-10-05", "status": "failed",
                                    "error": "boom", "reported_at": BUILT.isoformat()})
    assert tick(data_dir, settings(gadget, "off"), at(8, 0), free=1) == []
    assert gadget.requests == []
    assert not muse.state_path(data_dir).exists()


@pytest.mark.parametrize("mode", ["problems", "every"])
def test_a_failed_print_is_reported_once_with_what_happens_next(data_dir, gadget, mode):
    staging.save_receipt(data_dir, {
        "edition_date": "2026-10-05", "status": "failed", "reported_at": BUILT.isoformat(),
        "error": "every section failed, so there is nothing to print"})
    staging.save_report(data_dir, {"mode": "sources", "edition_date": "2026-10-05",
                                   "status": "failed", "ran_at": BUILT.isoformat(),
                                   "sections": [{"title": "World", "status": "failed"}]})
    assert tick(data_dir, settings(gadget, mode), at(6, 42)) == ["print"]
    assert gadget.messages == [(
        "The Press could not print the 2026-10-05 paper: every section failed, so there is "
        "nothing to print. The previous paper is still being served. The Press will try "
        f"again at its next check. {muse.SIGN_OFF}")]
    # The retries that fail the same way stay quiet.
    assert tick(data_dir, settings(gadget, mode), at(6, 52)) == []


def test_sections_left_out_are_named_with_the_reason(data_dir, gadget):
    printed(data_dir, kindle=at(6, 46).isoformat(), status="partial")
    assert tick(data_dir, settings(gadget, "problems"), at(6, 47)) == ["sections"]
    assert gadget.messages[0].startswith(
        "The 2026-10-05 paper was printed without one section: Tides (timed out after 60s). "
        "The rest of the paper is out as usual, and those sources are tried again at the "
        "next edition.")
    assert tick(data_dir, settings(gadget, "problems"), at(6, 57)) == []


def test_a_section_with_nothing_to_print_is_neither_counted_nor_reported(data_dir, gadget):
    printed(data_dir, kindle=at(6, 46).isoformat(), status="partial")
    # What an inbox with no fresh file leaves: an ordinary day, not a failure.
    report = staging.load_report(data_dir)
    report["sections"].append({"title": "Inbox", "status": "empty", "stories": 0,
                               "headlines": [], "error": None,
                               "notes": ["nothing fresh in the inbox"]})
    staging.save_report(data_dir, report)
    assert tick(data_dir, settings(gadget), at(6, 47)) == ["delivery", "sections"]
    assert gadget.messages[0].startswith("Printed at 06:41, four sections, 14 stories.")
    assert gadget.messages[1].startswith(
        "The 2026-10-05 paper was printed without one section: Tides (timed out after 60s). ")


def test_a_long_list_of_failed_sections_is_cut_short_and_keeps_its_ending(data_dir, gadget):
    printed(data_dir, kindle=at(6, 46).isoformat(), status="partial")
    report = staging.load_report(data_dir)
    report["sections"] += [
        {"title": f"Feed {number}", "status": "failed", "stories": 0, "headlines": [],
         "error": "could not be fetched: " + "the network is unreachable " * 8}
        for number in range(1, 12)]
    staging.save_report(data_dir, report)
    assert tick(data_dir, settings(gadget, "problems"), at(6, 47)) == ["sections"]
    message = gadget.messages[0]
    assert message.startswith(
        "The 2026-10-05 paper was printed without 12 sections: Tides (timed out after 60s); "
        "Feed 1 (could not be fetched: ")
    assert "Feed 4 (" in message and "Feed 5 (" not in message
    assert message.endswith(
        "; and 7 more. The rest of the paper is out as usual, and those sources are tried "
        f"again at the next edition. {muse.SIGN_OFF}")


def test_problems_only_reports_a_kindle_that_missed_the_deadline(data_dir, gadget):
    printed(data_dir)
    assert tick(data_dir, settings(gadget, "problems"), at(7, 0)) == []
    assert tick(data_dir, settings(gadget, "problems"), at(7, 42)) == ["delivery"]
    assert gadget.messages[0].startswith(
        "The 2026-10-05 paper was printed at 06:41, but the Kindle had not fetched it by "
        "07:42. It stays on the Press, and the Kindle collects it the next time it wakes.")
    assert tick(data_dir, settings(gadget, "problems"), at(7, 52)) == []


def test_the_longest_wait_still_reports_the_miss(data_dir, gadget):
    config = settings(gadget, "problems",
                      kindle_wait=dt.timedelta(minutes=muse.MAX_WAIT_MINUTES))
    printed(data_dir)
    assert tick(data_dir, config, at(18, 40)) == []
    assert tick(data_dir, config, at(18, 42)) == ["delivery"]
    assert "the Kindle had not fetched it by 18:42" in gadget.messages[0]


def test_a_nearly_full_disk_is_reported_once_per_shortage(data_dir, gadget):
    config = settings(gadget, "problems")
    low, hovering = 150 * 1024 * 1024, 250 * 1024 * 1024
    assert tick(data_dir, config, at(7, 0), free=low) == ["disk"]
    assert gadget.messages[0].startswith("The Press has only 150 MB free")
    assert "freed by hand" in gadget.messages[0]
    assert tick(data_dir, config, at(7, 1), free=low) == []
    # Back over the line, but not by enough to call the shortage over.
    assert tick(data_dir, config, at(7, 2), free=hovering) == []
    assert tick(data_dir, config, at(7, 3), free=low) == []
    assert tick(data_dir, config, at(7, 4)) == []            # recovered
    assert tick(data_dir, config, at(7, 5), free=low) == ["disk"]


def test_staging_that_cannot_be_read_is_reported(data_dir, gadget, unreachable, monkeypatch):
    address = unreachable.rsplit("/", 1)[0]
    monkeypatch.setenv("STAGE_URL", address)
    stage = staging.Stage(address, "token")
    assert press_fetch.fetch(stage, data_dir, fetch_weather=False, force=False,
                             keep_days=14) == 1
    report = staging.load_report(data_dir)
    assert (report["mode"], report["status"]) == ("staging", "failed")

    now = dt.datetime.now().astimezone()
    assert muse.tick(data_dir, settings(gadget, "problems"), now=now,
                     free_bytes=PLENTY) == ["staging"]
    assert gadget.messages[0].startswith("The Press could not read staging: GET stage/latest")
    assert "will try again at its next check" in gadget.messages[0]
    assert muse.tick(data_dir, settings(gadget, "problems"), now=now,
                     free_bytes=PLENTY) == []

    # Once staging answers, the record is cleared.
    press_fetch.note_staging(data_dir, None)
    assert staging.load_report(data_dir)["status"] == "ok"


@pytest.mark.parametrize("days_ago, owed", [(0, []), (1, ["staging"])])
def test_staging_is_only_reported_while_the_days_edition_is_not_printed(
        data_dir, gadget, monkeypatch, days_ago, owed):
    monkeypatch.setenv("STAGE_URL", "https://stage.example.com")
    built = BUILT - dt.timedelta(days=days_ago)
    staging.save_receipt(data_dir, {
        "edition_date": built.date().isoformat(), "status": "ok", "error": None,
        "built_at": built.isoformat(),
        "kindle_downloaded_at": (built + dt.timedelta(minutes=5)).isoformat()})
    # A later check of the same morning that staging does not answer.
    monkeypatch.setattr(staging, "now", lambda: at(7, 10).isoformat())
    press_fetch.note_staging(data_dir, "GET stage/latest: timed out")
    assert tick(data_dir, settings(gadget, "problems"), at(7, 11)) == owed
    assert len(gadget.messages) == len(owed)
    assert all("No new paper has been printed" in message for message in gadget.messages)


def test_old_news_is_not_sent_when_the_messages_are_turned_on(data_dir, gadget):
    printed(data_dir, status="partial")
    later = BUILT + dt.timedelta(days=2)
    assert tick(data_dir, settings(gadget), later) == []
    assert gadget.requests == []


@pytest.mark.parametrize("minute, owed", [(40, ["delivery"]), (42, [])])
def test_the_morning_line_ages_from_the_end_of_the_kindles_wait(data_dir, gadget, minute,
                                                                owed):
    printed(data_dir)  # at 06:41, so the hour's wait for the Kindle ends at 07:41
    assert tick(data_dir, settings(gadget), at(19, minute)) == owed


# --- Muse absent --------------------------------------------------------------


def test_with_no_gadget_the_send_is_logged_and_dropped(data_dir, tmp_path, capsys):
    printed(data_dir, kindle=at(6, 46).isoformat())
    config = muse.Settings(mode="every", socket_path=str(tmp_path / "nothing.sock"))
    assert tick(data_dir, config, at(6, 47)) == ["delivery"]
    assert "muse: dropped (delivery): could not reach the gadget service" in capsys.readouterr().err
    # Dropped, not queued: the next look has nothing to say.
    assert tick(data_dir, config, at(6, 48)) == []


@pytest.mark.parametrize("reply, said", [
    ({"ok": False, "error": "not connected to the Muse"}, "not delivered: not connected"),
    (b"", "could not reach the gadget service"),
    (b"[]\n", "not delivered"),
])
def test_a_gadget_that_cannot_deliver_is_a_dropped_send(data_dir, tmp_path, capsys, reply, said):
    fake = FakeGadget(tmp_path / "musegadget.sock", reply)
    printed(data_dir, kindle=at(6, 46).isoformat())
    assert tick(data_dir, settings(fake), at(6, 47)) == ["delivery"]
    assert said in capsys.readouterr().err
    fake.listener.close()


def test_the_watcher_survives_whatever_a_check_raises(data_dir, monkeypatch, capsys):
    wake, calls = threading.Event(), []

    def explode(*args):
        calls.append(args)
        if len(calls) == 2:
            raise SystemExit  # the only way out of the loop, for the test
        raise RuntimeError("the records are unreadable")

    monkeypatch.setattr(muse, "tick", explode)
    with pytest.raises(SystemExit):
        muse.watch(data_dir, muse.Settings(mode="every"), wake, interval=0.01)
    assert "muse: skipped a check: RuntimeError" in capsys.readouterr().err


# --- Settings -----------------------------------------------------------------


def test_the_messages_are_off_unless_asked_for():
    config = muse.Settings.from_env()
    assert (config.mode, config.detail) == ("off", "counts")
    assert config.kindle_wait == dt.timedelta(minutes=60)
    assert config.socket_path == "/run/musegadget/musegadget.sock"
    assert muse.start(Path("/nonexistent"), config) is None


def test_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("MUSE_MESSAGES", "Problems")
    monkeypatch.setenv("MUSE_DETAIL", "headlines")
    monkeypatch.setenv("MUSE_KINDLE_WAIT", "0")
    monkeypatch.setenv("MUSE_SOCKET", "/tmp/elsewhere.sock")
    monkeypatch.setenv("MUSE_SESSION_ID", "mnn-press")
    assert muse.Settings.from_env() == muse.Settings(
        mode="problems", detail="headlines", kindle_wait=None,
        socket_path="/tmp/elsewhere.sock", session_id="mnn-press")


def test_a_setting_that_cannot_be_read_falls_back_quietly(monkeypatch, capsys):
    monkeypatch.setenv("MUSE_MESSAGES", "always")
    monkeypatch.setenv("MUSE_DETAIL", "everything")
    monkeypatch.setenv("MUSE_KINDLE_WAIT", "soon")
    monkeypatch.setenv("MUSE_SESSION_ID", "not a name")
    assert muse.Settings.from_env() == muse.Settings()
    assert capsys.readouterr().err.count("muse: ") == 4


@pytest.mark.parametrize("asked", ["721", "2880", "10000000000", "10000000000000"])
def test_a_wait_of_any_length_is_cut_to_twelve_hours_and_still_reports_a_miss(
        data_dir, gadget, monkeypatch, capsys, asked):
    monkeypatch.setenv("MUSE_MESSAGES", "every")
    monkeypatch.setenv("MUSE_SOCKET", str(gadget.path))
    monkeypatch.setenv("MUSE_KINDLE_WAIT", asked)
    config = muse.Settings.from_env()
    assert config.kindle_wait == dt.timedelta(hours=12)
    assert (f"muse: MUSE_KINDLE_WAIT={asked!r} is more than twelve hours; using 720"
            in capsys.readouterr().err)

    # The wait is over before the next morning's paper replaces this one.
    printed(data_dir)
    assert tick(data_dir, config, at(18, 40)) == []
    assert tick(data_dir, config, at(18, 42)) == ["delivery"]
    assert "The Kindle had not fetched it by 18:42" in gadget.messages[0]


def test_a_wait_of_twelve_hours_is_taken_as_asked(monkeypatch, capsys):
    monkeypatch.setenv("MUSE_KINDLE_WAIT", "720")
    assert muse.Settings.from_env().kindle_wait == dt.timedelta(hours=12)
    assert capsys.readouterr().err == ""


# --- The card -----------------------------------------------------------------

CARD_URL = "http://192.168.1.20:8484/card.jpg"


def test_the_card_is_asked_for_once_after_a_print_and_again_after_a_reprint(data_dir, gadget):
    printed(data_dir)
    config = settings(gadget, mode="off", card_url=CARD_URL)
    assert tick(data_dir, config, at(6, 42)) == ["card"]
    assert tick(data_dir, config, at(6, 43)) == []
    assert f"run display.draw_url on it with the url {CARD_URL} " in gadget.messages[0]
    # It asks for an action, so it must not end by saying none is needed.
    assert gadget.messages[0].endswith(muse.CARD_SIGN_OFF)
    assert "action is needed" not in gadget.messages[0]

    receipt = staging.load_receipt(data_dir)
    staging.save_receipt(data_dir, {**receipt, "built_at": at(9, 0).isoformat()})
    assert tick(data_dir, config, at(9, 1)) == ["card"]
    assert len(gadget.messages) == 2


def test_the_card_is_not_asked_for_unless_its_address_is_set(data_dir, gadget):
    printed(data_dir)
    assert tick(data_dir, settings(gadget, kindle_wait=None), at(6, 42)) == ["delivery"]
    assert "draw_url" not in gadget.messages[0]


def test_the_card_is_asked_for_after_the_morning_line(data_dir, gadget):
    printed(data_dir)
    config = settings(gadget, kindle_wait=None, card_url=CARD_URL)
    assert tick(data_dir, config, at(6, 42)) == ["delivery", "card"]
    assert gadget.messages[0].startswith("Printed at 06:41, four sections, 14 stories.")
    assert gadget.messages[0].endswith(muse.SIGN_OFF)
    assert gadget.messages[1].endswith(muse.CARD_SIGN_OFF)


def test_a_failed_print_is_reported_before_the_card_is_asked_for(data_dir, gadget):
    staging.save_receipt(data_dir, {
        "edition_date": "2026-10-05", "status": "failed", "error": "no sections",
        "reported_at": BUILT.isoformat()})
    config = settings(gadget, "problems", card_url=CARD_URL)
    assert tick(data_dir, config, at(6, 42)) == ["print", "card"]
    assert gadget.messages[0].startswith("The Press could not print the 2026-10-05 paper")
    assert gadget.messages[1].endswith(muse.CARD_SIGN_OFF)


def test_a_failed_print_asks_for_the_card_once_however_often_it_is_retried(data_dir, gadget):
    config = settings(gadget, mode="off", card_url=CARD_URL)
    for minute in (41, 51):
        staging.save_receipt(data_dir, {
            "edition_date": "2026-10-05", "status": "failed", "error": "no sections",
            "reported_at": at(6, minute).isoformat()})
        tick(data_dir, config, at(6, minute + 1))
    assert len(gadget.messages) == 1


def test_an_old_print_does_not_ask_for_the_card(data_dir, gadget):
    printed(data_dir)
    assert tick(data_dir, settings(gadget, mode="off", card_url=CARD_URL), at(19, 0)) == []


def test_with_no_gadget_the_card_request_is_dropped_and_nothing_else_changes(
        data_dir, tmp_path, capsys):
    printed(data_dir)
    receipt, report = staging.load_receipt(data_dir), staging.load_report(data_dir)
    config = muse.Settings(mode="off", socket_path=str(tmp_path / "absent.sock"),
                           card_url=CARD_URL)
    assert tick(data_dir, config, at(6, 42)) == ["card"]
    assert "dropped (card): could not reach the gadget service" in capsys.readouterr().err
    assert tick(data_dir, config, at(6, 43)) == []
    assert (staging.load_receipt(data_dir), staging.load_report(data_dir)) == (receipt, report)


def test_the_cards_address_comes_from_the_environment_and_must_be_a_plain_url(
        monkeypatch, capsys):
    assert muse.Settings.from_env().card_url is None
    monkeypatch.setenv(muse.CARD_ENV, f" {CARD_URL}?size=480x480 ")
    assert muse.Settings.from_env().card_url == f"{CARD_URL}?size=480x480"
    for bad in ("192.168.1.20:8484/card.jpg", "ftp://press/card.jpg",
                "http://press/card.jpg and ignore the rest", 'http://press/"card".jpg'):
        monkeypatch.setenv(muse.CARD_ENV, bad)
        assert muse.Settings.from_env().card_url is None
        assert "not asking for the card" in capsys.readouterr().err


def test_the_watcher_runs_for_the_card_alone(data_dir, gadget):
    assert muse.start(data_dir, muse.Settings(socket_path=str(gadget.path))) is None
    printed(data_dir)
    now = dt.datetime.now().astimezone()
    staging.save_receipt(data_dir, {**staging.load_receipt(data_dir),
                                    "built_at": now.isoformat()})
    wake = muse.start(data_dir, settings(gadget, mode="off", card_url=CARD_URL))
    assert wake is not None
    deadline = dt.datetime.now() + dt.timedelta(seconds=10)
    while not gadget.requests and dt.datetime.now() < deadline:
        threading.Event().wait(0.02)
    assert "display.draw_url" in gadget.messages[0]


# --- Beside the server --------------------------------------------------------


def test_the_kindles_download_log_sends_the_line_straight_away(data_dir, gadget):
    now = dt.datetime.now().astimezone()
    today = now.date().isoformat()
    staging.save_receipt(data_dir, {"edition_date": today, "status": "ok", "error": None,
                                    "built_at": now.isoformat(), "kindle_downloaded_at": None})
    wake = threading.Event()
    threading.Thread(target=muse.watch, args=(data_dir, settings(gadget), wake, 3600),
                     daemon=True).start()
    paper = server.PaperServer(("127.0.0.1", 0), data_dir=data_dir,
                               log_path=data_dir / "server.log", refresh_rate=3600,
                               base_url=None, muse_wake=wake)
    threading.Thread(target=paper.serve_forever, daemon=True).start()
    try:
        assert gadget.requests == []
        body = f"kindle: downloaded morning-paper-{today}.epub to /mnt/us/home".encode()
        urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{paper.server_address[1]}/api/log", data=body), timeout=10)
        deadline = dt.datetime.now() + dt.timedelta(seconds=10)
        while not gadget.requests and dt.datetime.now() < deadline:
            threading.Event().wait(0.02)
        assert f"Printed at {now:%H:%M}. On the Kindle at " in gadget.messages[0]
    finally:
        paper.shutdown()
        paper.server_close()


@pytest.mark.parametrize("silent", [False, True])
def test_an_unreachable_gadget_does_not_keep_the_paper_from_the_kindle(
        data_dir, tmp_path, monkeypatch, capsys, silent):
    path = tmp_path / "musegadget.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if silent:
        # Listening but never answering: the send connects, then waits.
        listener.bind(str(path))
        listener.listen()
        monkeypatch.setattr(muse, "SEND_TIMEOUT", 0.2)
    now = dt.datetime.now().astimezone()
    today = now.date().isoformat()
    (data_dir / f"morning-paper-{today}.epub").write_bytes(b"PK the paper")
    staging.save_receipt(data_dir, {"edition_date": today, "status": "ok", "error": None,
                                    "built_at": now.isoformat(), "kindle_downloaded_at": None})
    config = muse.Settings(mode="every", socket_path=str(path))
    wake = threading.Event()
    threading.Thread(target=muse.watch, args=(data_dir, config, wake, 3600),
                     daemon=True).start()
    paper = server.PaperServer(("127.0.0.1", 0), data_dir=data_dir,
                               log_path=data_dir / "server.log", refresh_rate=3600,
                               base_url=None, muse_wake=wake)
    threading.Thread(target=paper.serve_forever, daemon=True).start()
    address = f"http://127.0.0.1:{paper.server_address[1]}"
    try:
        body = f"kindle: downloaded morning-paper-{today}.epub to /mnt/us/home".encode()
        with urllib.request.urlopen(urllib.request.Request(f"{address}/api/log", data=body),
                                    timeout=10) as reply:
            assert reply.status == 204
        with urllib.request.urlopen(f"{address}/paper/morning-paper-{today}.epub",
                                    timeout=10) as reply:
            assert reply.read() == b"PK the paper"
        logged = ""
        deadline = dt.datetime.now() + dt.timedelta(seconds=10)
        while "muse: dropped" not in logged and dt.datetime.now() < deadline:
            threading.Event().wait(0.02)
            logged += capsys.readouterr().err
        assert "muse: dropped (delivery): could not reach the gadget service" in logged
        assert staging.load_receipt(data_dir)["kindle_downloaded_at"]
    finally:
        paper.shutdown()
        paper.server_close()
        listener.close()


def test_the_command_line_sends_one_message(gadget, monkeypatch, capsys):
    monkeypatch.setenv("MUSE_SOCKET", str(gadget.path))
    assert muse.main(["Test", "from the Press."]) == 0
    assert gadget.messages == [f"Test from the Press. {muse.SIGN_OFF}"]
    monkeypatch.setenv("MUSE_SOCKET", str(gadget.path) + ".gone")
    assert muse.main(["Test"]) == 1
    assert "could not reach the gadget service" in capsys.readouterr().err
