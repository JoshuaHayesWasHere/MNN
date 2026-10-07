"""Sending the paper by email, to a mail server that lives in the test."""

from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import socketserver
import threading

import pytest

from mnn import mail, press_fetch, server, staging

TODAY = dt.date.today().isoformat()


class _Smtp(socketserver.StreamRequestHandler):
    """Just enough SMTP for smtplib: it keeps what it is sent."""

    def reply(self, text: str) -> None:
        self.wfile.write(text.encode() + b"\r\n")

    def handle(self) -> None:
        box = self.server.box
        self.reply("220 test ESMTP")
        while line := self.rfile.readline():
            command = line.decode(errors="replace").strip()
            verb = command.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self.wfile.write(b"250-test\r\n250 AUTH PLAIN LOGIN\r\n")
            elif verb == "AUTH":
                given = base64.b64decode(command.split(" ")[2]).decode().split("\0")[1:]
                box["logins"].append(given)
                self.reply("235 ok" if given == ["press@example.com", "app-password"]
                           else "535 bad credentials")
            elif verb == "MAIL":
                self.reply("250 ok")
            elif verb == "RCPT":
                self.reply("550 no such user" if box["refuse"] in command else "250 ok")
            elif verb == "DATA":
                self.reply("354 go on")
                data = b""
                while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                    data += chunk
                box["messages"].append(email.message_from_bytes(data, policy=email.policy.default))
                self.reply("250 queued")
            elif verb == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("250 ok")


@pytest.fixture
def smtp():
    box = {"messages": [], "logins": [], "refuse": "nobody-is-refused"}
    mail_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Smtp)
    mail_server.daemon_threads = True
    mail_server.box = box
    threading.Thread(target=mail_server.serve_forever, daemon=True).start()
    box["env"] = {"MAIL_TO": "reader_abc@kindle.com", "SMTP_HOST": "127.0.0.1",
                  "SMTP_PORT": str(mail_server.server_address[1]), "SMTP_SECURITY": "none",
                  "SMTP_USER": "press@example.com", "SMTP_PASSWORD": "app-password"}
    yield box
    mail_server.shutdown()
    mail_server.server_close()


def print_paper(data_dir, date: str = TODAY, text: bytes = b"an epub") -> None:
    (data_dir / "press").mkdir(parents=True, exist_ok=True)
    (data_dir / f"morning-paper-{date}.epub").write_bytes(text)


def test_mail_is_off_until_it_is_set_up(data_dir):
    print_paper(data_dir)
    assert mail.settings({}) is None
    assert mail.deliver_newest(data_dir, env={}) == "mail: off"
    assert mail.load(data_dir) == {}


def test_the_newest_paper_goes_out_as_an_epub_attachment(data_dir, smtp):
    print_paper(data_dir, "2026-10-01", b"an older paper")
    print_paper(data_dir)
    outcome = mail.deliver_newest(data_dir, env=smtp["env"])
    assert outcome == f"mail: sent morning-paper-{TODAY}.epub to 1 address"
    [message] = smtp["messages"]
    assert message["To"] == "reader_abc@kindle.com" and message["From"] == "press@example.com"
    assert message["Subject"].startswith("The Morning Paper, ")
    [attachment] = list(message.iter_attachments())
    assert attachment.get_filename() == f"morning-paper-{TODAY}.epub"
    assert attachment.get_content_type() == "application/epub+zip"
    assert attachment.get_content() == b"an epub"
    record = mail.load(data_dir)
    assert (record["status"], record["attempts"], record["recipients"]) == ("sent", 1, 1)
    # The record and the status say that it went, never where.
    assert "kindle.com" not in str(record)
    assert "kindle.com" not in str(server.press_status(data_dir))
    assert server.press_status(data_dir)["mail"]["status"] == "sent"


def test_an_edition_is_sent_once_and_again_only_when_it_changes(data_dir, smtp):
    print_paper(data_dir)
    mail.deliver_newest(data_dir, env=smtp["env"])
    assert "already been sent" in mail.deliver_newest(data_dir, env=smtp["env"])
    assert len(smtp["messages"]) == 1
    print_paper(data_dir, text=b"reprinted with a correction")
    assert "mail: sent" in mail.deliver_newest(data_dir, env=smtp["env"])
    assert "mail: sent" in mail.deliver_newest(data_dir, again=True, env=smtp["env"])
    assert len(smtp["messages"]) == 3


def test_several_addresses_and_a_sender_of_its_own(data_dir, smtp):
    print_paper(data_dir)
    env = {**smtp["env"], "MAIL_TO": "a@kindle.com, b@example.org", "MAIL_FROM": "Press <p@example.com>"}
    assert mail.deliver_newest(data_dir, env=env).endswith("to 2 addresses")
    assert smtp["messages"][0]["From"] == "Press <p@example.com>"


def test_a_failed_send_is_recorded_tried_again_and_then_given_up(data_dir, smtp):
    print_paper(data_dir)
    env = {**smtp["env"], "SMTP_PASSWORD": "wrong"}
    for attempt in range(1, mail.MAX_ATTEMPTS + 1):
        outcome = mail.deliver_newest(data_dir, env=env)
        assert "was not sent: the mail server did not accept the user name and password" in outcome
        assert mail.load(data_dir)["attempts"] == attempt
    assert f"was not sent after {mail.MAX_ATTEMPTS} tries" in mail.deliver_newest(data_dir, env=env)
    # smtplib offers a second way of signing in after the first is refused.
    assert len([login for login in smtp["logins"] if login]) == mail.MAX_ATTEMPTS
    health = server.press_health(data_dir, dt.time(6, 40))
    assert any(warning["code"] == "mail_unsent" for warning in health["warnings"])
    # Fixed, it goes at the next try that is asked for, and the warning clears.
    assert "mail: sent" in mail.deliver_newest(data_dir, again=True, env=smtp["env"])
    health = server.press_health(data_dir, dt.time(6, 40))
    assert not any(warning["code"] == "mail_unsent" for warning in health["warnings"])


def test_a_refused_recipient_and_an_unreachable_server_are_named(data_dir, smtp):
    print_paper(data_dir)
    smtp["refuse"] = "reader_abc@kindle.com"
    assert "refused every recipient" in mail.deliver_newest(data_dir, env=smtp["env"])
    smtp["refuse"] = "nobody-is-refused"
    env = {**smtp["env"], "SMTP_PORT": "1"}
    assert "could not be reached" in mail.deliver_newest(data_dir, again=True, env=env)
    assert smtp["messages"] == []


@pytest.mark.parametrize("env, message", [
    ({"MAIL_TO": "a@kindle.com"}, "MAIL_TO is set but SMTP_HOST is not"),
    ({"SMTP_HOST": "smtp.example.com"}, "SMTP_HOST is set but MAIL_TO is not"),
    ({"MAIL_TO": "not an address", "SMTP_HOST": "h", "SMTP_USER": "u@example.com"},
     "not an email address"),
    ({"MAIL_TO": "a@kindle.com", "SMTP_HOST": "h"}, "MAIL_FROM is not set"),
    ({"MAIL_TO": "a@kindle.com", "SMTP_HOST": "h", "MAIL_FROM": "p@example.com",
      "SMTP_SECURITY": "tls13"}, "SMTP_SECURITY must be one of"),
    ({"MAIL_TO": "a@kindle.com", "SMTP_HOST": "h", "MAIL_FROM": "p@example.com",
      "SMTP_PORT": "smtp"}, "SMTP_PORT must be a number"),
    ({"MAIL_TO": "a@kindle.com\r\nBcc: x@example.com", "SMTP_HOST": "h",
      "MAIL_FROM": "p@example.com"}, "not an email address"),
])
def test_half_set_or_mistyped_settings_are_an_error_not_off(data_dir, env, message):
    with pytest.raises(mail.MailError, match=message):
        mail.settings(env)
    print_paper(data_dir)
    assert message in mail.deliver_newest(data_dir, env=env)
    assert mail.load(data_dir)["status"] == "failed"


def test_the_default_port_follows_the_security(monkeypatch):
    base = {"MAIL_TO": "a@kindle.com", "SMTP_HOST": "h", "MAIL_FROM": "p@example.com"}
    assert mail.settings(base).port == 587 and mail.settings(base).security == "starttls"
    assert mail.settings({**base, "SMTP_SECURITY": "SSL"}).port == 465


def test_the_fetcher_mails_the_paper_it_has_just_printed(data_dir, config_dir, feeds, smtp,
                                                        monkeypatch, capsys):
    (config_dir / "sources.toml").write_text(
        f'[[section]]\ntitle = "World"\nfeeds = ["{feeds}/world.rss"]\nstories = 2\n')
    for key, value in smtp["env"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("STAGE_URL", raising=False)
    arguments = ["--data-dir", str(data_dir), "--config-dir", str(config_dir)]
    assert press_fetch.main(arguments) == 0
    assert "mail: sent morning-paper-" in capsys.readouterr().out
    assert len(smtp["messages"]) == 1
    sent = next(smtp["messages"][0].iter_attachments()).get_content()
    assert staging.sha256(sent) == mail.load(data_dir)["sha256"]
    # The next check prints nothing new and sends nothing twice.
    assert press_fetch.main(arguments) == 0
    assert len(smtp["messages"]) == 1 and "mail:" not in capsys.readouterr().out


def test_mail_going_wrong_never_fails_the_print(data_dir, config_dir, feeds, smtp, monkeypatch,
                                               capsys):
    (config_dir / "sources.toml").write_text(
        f'[[section]]\ntitle = "World"\nfeeds = ["{feeds}/world.rss"]\nstories = 2\n')
    for key, value in {**smtp["env"], "SMTP_PORT": "1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("STAGE_URL", raising=False)
    assert press_fetch.main(["--data-dir", str(data_dir), "--config-dir", str(config_dir)]) == 0
    captured = capsys.readouterr()
    assert "printed " in captured.out and "was not sent" in captured.err


def test_the_mail_command_sends_by_hand(data_dir, smtp, monkeypatch, capsys):
    print_paper(data_dir)
    for key, value in smtp["env"].items():
        monkeypatch.setenv(key, value)
    assert mail.main(["--data-dir", str(data_dir)]) == 0
    assert mail.main(["--data-dir", str(data_dir)]) == 0
    assert mail.main(["--data-dir", str(data_dir), "--again"]) == 0
    assert len(smtp["messages"]) == 2
    monkeypatch.setenv("SMTP_PORT", "1")
    assert mail.main(["--data-dir", str(data_dir), "--again"]) == 1
