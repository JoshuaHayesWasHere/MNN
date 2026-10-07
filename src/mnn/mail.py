"""Sends each new paper by email, as an EPUB attachment.

This is how the paper reaches a reader that cannot fetch it: above all a
Kindle that is not jailbroken, through Amazon's Send to Kindle address. It is
off unless both `MAIL_TO` and `SMTP_HOST` are set.

    MAIL_TO         where to send it; several addresses separated by commas
    SMTP_HOST       your mail provider's SMTP server
    SMTP_PORT       default 587
    SMTP_USER       the account to sign in as, if the server asks for one
    SMTP_PASSWORD   its password (for most providers, an app password)
    MAIL_FROM       the sender; default SMTP_USER
    SMTP_SECURITY   starttls (the default), ssl, or none

Each edition is sent once. A paper printed again with different contents is
sent again; one that comes out the same is not. A failed send is tried again
at the fetcher's next check, five times at most, and never holds up the
paper: it is printed and served whether or not the mail goes.

    uv run mnn-press mail            send the newest paper now, if it has not gone
    uv run mnn-press mail --again    send it even if it has

Standard library only.
"""

from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

from mnn import server, staging

RECORD = "mail.json"
MAX_ATTEMPTS = 5
TIMEOUT = 60
SECURITIES = ("starttls", "ssl", "none")
# Send to Kindle takes attachments up to 50 MB; a paper is a fraction of one.
MAX_BYTES = 50 * 1024 * 1024


class MailError(Exception):
    """The paper could not be sent, or the settings do not add up."""


@dataclass(frozen=True)
class Settings:
    to: tuple[str, ...]
    sender: str
    host: str
    port: int
    user: str
    password: str
    security: str


def settings(env: dict | None = None) -> Settings | None:
    """The mail settings, or None when mail is off. Half a set of settings is
    an error, so a typo is heard about and not taken for "off"."""
    env = os.environ if env is None else env
    to = tuple(address.strip() for address in env.get("MAIL_TO", "").split(",") if address.strip())
    host = env.get("SMTP_HOST", "").strip()
    if not to and not host:
        return None
    if not to:
        raise MailError("SMTP_HOST is set but MAIL_TO is not")
    if not host:
        raise MailError("MAIL_TO is set but SMTP_HOST is not")
    user = env.get("SMTP_USER", "").strip()
    sender = env.get("MAIL_FROM", "").strip() or user
    for address in (*to, sender):
        if "@" not in parseaddr(address)[1] or any(c in address for c in "\r\n"):
            raise MailError(f"not an email address: {address[:60]!r}" if address
                            else "MAIL_FROM is not set, and there is no SMTP_USER to use")
    security = env.get("SMTP_SECURITY", "starttls").strip().lower() or "starttls"
    if security not in SECURITIES:
        raise MailError("SMTP_SECURITY must be one of: " + ", ".join(SECURITIES))
    try:
        port = int(env.get("SMTP_PORT", "").strip() or (465 if security == "ssl" else 587))
    except ValueError as exc:
        raise MailError("SMTP_PORT must be a number") from exc
    return Settings(to=to, sender=sender, host=host, port=port, user=user,
                    password=env.get("SMTP_PASSWORD", ""), security=security)


def message(config: Settings, name: str, paper: bytes, date: str) -> EmailMessage:
    mail = EmailMessage()
    mail["From"] = config.sender
    mail["To"] = ", ".join(config.to)
    mail["Subject"] = f"The Morning Paper, {server.long_date(date)}"
    mail["Date"] = formatdate(localtime=True)
    mail["Message-ID"] = make_msgid(domain=parseaddr(config.sender)[1].rpartition("@")[2])
    mail.set_content(f"Today's paper is attached: {name}\n")
    mail.add_attachment(paper, maintype="application", subtype="epub+zip", filename=name)
    return mail


def send(config: Settings, mail: EmailMessage) -> None:
    try:
        if config.security == "ssl":
            connection = smtplib.SMTP_SSL(config.host, config.port, timeout=TIMEOUT,
                                          context=ssl.create_default_context())
        else:
            connection = smtplib.SMTP(config.host, config.port, timeout=TIMEOUT)
        with connection:
            if config.security == "starttls":
                connection.starttls(context=ssl.create_default_context())
            if config.user:
                connection.login(config.user, config.password)
            refused = connection.send_message(mail)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError("the mail server did not accept the user name and password") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailError("the mail server refused every recipient") from exc
    except smtplib.SMTPException as exc:
        raise MailError(f"the mail server said: {' '.join(str(exc).split())[:200]}") from exc
    except (OSError, ssl.SSLError) as exc:
        raise MailError(f"the mail server could not be reached: {exc}") from exc
    if refused:
        raise MailError(f"the mail server refused {len(refused)} of {len(config.to)} recipients")


def load(data_dir: Path) -> dict:
    return staging.load_json(staging.press_dir(data_dir) / RECORD)


def deliver_newest(data_dir: Path, *, again: bool = False, env: dict | None = None) -> str:
    """Send the newest paper if it has not gone yet. Returns one line saying
    what happened, and never raises: mail must not get in the paper's way."""
    record = load(data_dir)

    def note(status: str, error: str | None, **fields) -> None:
        staging.write_atomic(staging.press_dir(data_dir) / RECORD,
                             staging.dump({**fields, "status": status, "error": error,
                                           "at": staging.now()}))

    try:
        config = settings(env)
    except MailError as exc:
        note("failed", str(exc), edition_date=record.get("edition_date"),
             sha256=record.get("sha256"), attempts=record.get("attempts", 0))
        return f"mail: not sent: {exc}"
    if config is None:
        return "mail: off"
    papers = server.editions(data_dir, server.EPUB_RE)
    if not papers:
        return "mail: no paper to send yet"
    date, path = papers[0]
    try:
        paper = path.read_bytes()
    except OSError as exc:
        return f"mail: {path.name} could not be read: {exc}"
    fingerprint = staging.sha256(paper)
    same = record.get("edition_date") == date and record.get("sha256") == fingerprint
    attempts = record.get("attempts", 0) if same else 0
    if same and not again:
        if record.get("status") == "sent":
            return f"mail: {path.name} has already been sent"
        if attempts >= MAX_ATTEMPTS:
            return f"mail: {path.name} was not sent after {attempts} tries: {record.get('error')}"
    fields = {"edition_date": date, "sha256": fingerprint, "attempts": attempts + 1,
              "recipients": len(config.to)}
    try:
        if len(paper) > MAX_BYTES:
            raise MailError("the paper is larger than 50 MB")
        send(config, message(config, path.name, paper, date))
    except MailError as exc:
        note("failed", str(exc), **fields)
        return f"mail: {path.name} was not sent: {exc}"
    note("sent", None, **fields)
    return f"mail: sent {path.name} to {len(config.to)} " \
           f"{'address' if len(config.to) == 1 else 'addresses'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the newest paper by email.")
    parser.add_argument("--data-dir", type=Path,
                        default=os.environ.get(server.DATA_DIR_ENV) or server.default_data_dir(),
                        help="the server's data directory")
    parser.add_argument("--again", action="store_true",
                        help="send it even if it has already been sent")
    args = parser.parse_args(argv)
    outcome = deliver_newest(Path(args.data_dir).expanduser(), again=args.again)
    print(outcome)
    return 0 if outcome.startswith(("mail: sent", "mail: off")) or "already been sent" in outcome \
        else 1


if __name__ == "__main__":
    sys.exit(main())
