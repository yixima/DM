"""メール送信の実体。console / file / smtp を差し替え可能にする。

console : 画面に出すだけ。テンプレートの確認用。
file    : state/outbox に .eml を保存。実送信せずに最終形を検品できる。
smtp    : 実送信。
"""
from __future__ import annotations

import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .config import Settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class TransportError(RuntimeError):
    pass


def build_message(
    settings: Settings,
    *,
    to_address: str,
    subject: str,
    text: str,
    html: str | None = None,
    unsubscribe_url: str = "",
    unsubscribe_mailto: str = "",
) -> EmailMessage:
    sender = settings.sender
    msg = EmailMessage()
    msg["From"] = f"{sender.name} <{sender.email}>" if sender.name else sender.email
    msg["To"] = to_address
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(sender.email.rsplit("@", 1)[-1] or "localhost"))
    if sender.reply_to:
        msg["Reply-To"] = sender.reply_to
    # ワンクリック配信停止（RFC 8058）。受信側のUIから停止できるほど苦情率は下がる。
    targets = [f"<{u}>" for u in (unsubscribe_mailto, unsubscribe_url) if u]
    if targets:
        msg["List-Unsubscribe"] = ", ".join(targets)
        if unsubscribe_url:
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["Auto-Submitted"] = "auto-generated"

    msg.set_content(text, subtype="plain", charset="utf-8")
    if html:
        msg.add_alternative(html, subtype="html", charset="utf-8")
    return msg


class Transport:
    name = "base"

    def send(self, msg: EmailMessage) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ConsoleTransport(Transport):
    name = "console"

    def send(self, msg: EmailMessage) -> str:
        print("-" * 72)
        print(f"To: {msg['To']}")
        print(f"Subject: {msg['Subject']}")
        body = msg.get_body(preferencelist=("plain",))
        print(body.get_content() if body else "")
        return "console"


class FileTransport(Transport):
    name = "file"

    def __init__(self, outbox: Path) -> None:
        self.outbox = outbox
        self.outbox.mkdir(parents=True, exist_ok=True)

    def send(self, msg: EmailMessage) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe = _SAFE.sub("_", str(msg["To"]))[:60]
        path = self.outbox / f"{stamp}_{safe}.eml"
        path.write_bytes(bytes(msg))
        return str(path)


class SmtpTransport(Transport):
    name = "smtp"

    def __init__(self, settings: Settings) -> None:
        cfg = settings.smtp
        if not cfg.host:
            raise TransportError("DM_SMTP_HOST が未設定です")
        self.cfg = cfg
        self._client: smtplib.SMTP | smtplib.SMTP_SSL | None = None

    def _connect(self) -> smtplib.SMTP | smtplib.SMTP_SSL:
        if self._client is not None:
            try:
                self._client.noop()
                return self._client
            except (smtplib.SMTPException, OSError):
                self._client = None
        cfg = self.cfg
        if cfg.port == 465:
            client: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
                cfg.host, cfg.port, timeout=30, context=ssl.create_default_context()
            )
        else:
            client = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
            if cfg.starttls:
                client.starttls(context=ssl.create_default_context())
        if cfg.user:
            client.login(cfg.user, cfg.password)
        self._client = client
        return client

    def send(self, msg: EmailMessage) -> str:
        client = self._connect()
        try:
            client.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            raise TransportError(f"宛先が拒否されました: {exc.recipients}") from exc
        except smtplib.SMTPException as exc:
            self._client = None
            raise TransportError(f"SMTPエラー: {exc}") from exc
        return str(msg["Message-ID"])

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.quit()
            except (smtplib.SMTPException, OSError):
                pass
            self._client = None


def get_transport(settings: Settings, override: str | None = None) -> Transport:
    name = (override or settings.transport or "console").lower()
    if name == "console":
        return ConsoleTransport()
    if name == "file":
        return FileTransport(settings.outbox_dir)
    if name == "smtp":
        return SmtpTransport(settings)
    raise TransportError(f"未知の transport: {name}")
