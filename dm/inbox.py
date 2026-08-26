"""受信箱のスキャン（IMAP）。

定期配信を続けるなら、これが最も重要な保守作業になる。
  - バウンス（宛先不明）を放置すると、送信ドメインの評判が落ちて全体が届かなくなる
  - 「送らないでほしい」という返信を見落とすと、法令違反かつ信用の毀損になる
どちらも自動で検出して、送信禁止リストへ入れる。
"""
from __future__ import annotations

import email
import imaplib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

from .config import Settings
from .db import add_event, add_suppression
from .normalize import EMAIL_RE

HARD_BOUNCE_STATUS = re.compile(r"\b5\.\d{1,3}\.\d{1,3}\b")
SOFT_BOUNCE_STATUS = re.compile(r"\b4\.\d{1,3}\.\d{1,3}\b")
FINAL_RECIPIENT = re.compile(r"^(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*(\S+)", re.I | re.M)
DAEMON_FROM = re.compile(r"(mailer-daemon|postmaster|mail delivery|delivery status|no-?reply)", re.I)

UNSUB_PATTERNS = re.compile(
    r"(配信停止|配信を停止|配信不要|今後.{0,10}(不要|お断り|ご遠慮)|送(ら|信し)ないで|"
    r"メールを止め|受信拒否|削除してください|unsubscribe|opt-?out|remove me)",
    re.I,
)
COMPLAINT_PATTERNS = re.compile(r"(迷惑|スパム|法的措置|通報|spam|abuse)", re.I)


@dataclass
class IngestResult:
    scanned: int = 0
    hard_bounces: int = 0
    soft_bounces: int = 0
    unsubscribes: int = 0
    complaints: int = 0
    suppressed: int = 0
    details: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"走査 {self.scanned}通 / 恒久バウンス {self.hard_bounces} / 一時バウンス {self.soft_bounces}"
            f" / 配信停止 {self.unsubscribes} / 苦情 {self.complaints} / 新規除外 {self.suppressed}"
        )


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg: Message) -> str:
    chunks: list[str] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            # 配送状況はヘッダの集合として格納されているため、本文としては取り出せない
            for block in part.get_payload():
                if isinstance(block, Message):
                    chunks.append("\n".join(f"{k}: {v}" for k, v in block.items()))
            continue
        if ctype not in ("text/plain", "text/rfc822-headers", "message/rfc822"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        for encoding in (charset, "utf-8", "cp932", "iso-2022-jp", "latin-1"):
            try:
                chunks.append(payload.decode(encoding, errors="strict"))
                break
            except (UnicodeDecodeError, LookupError):
                continue
    return "\n".join(chunks)[:20000]


def _sender_address(msg: Message) -> str:
    raw = _decode(msg.get("Reply-To") or msg.get("From"))
    match = re.search(r"[\w.+\-]+@[\w.\-]+", raw)
    return match.group(0).lower() if match else ""


def classify_message(msg: Message) -> tuple[str | None, str]:
    """(種別, 対象アドレス) を返す。種別は bounce_hard/bounce_soft/unsubscribe/complaint。"""
    subject = _decode(msg.get("Subject"))
    sender = _sender_address(msg)
    body = _body_text(msg)
    blob = f"{subject}\n{body}"

    is_dsn = (
        msg.get_content_type() == "multipart/report"
        or DAEMON_FROM.search(_decode(msg.get("From")) or "")
        or "delivery-status" in (msg.get("Content-Type") or "")
    )
    if is_dsn:
        recipient_match = FINAL_RECIPIENT.search(body)
        recipient = (recipient_match.group(1).strip("<>").lower() if recipient_match else "")
        if not recipient:
            candidates = [a.lower() for a in re.findall(r"[\w.+\-]+@[\w.\-]+", body)]
            recipient = next((a for a in candidates if not DAEMON_FROM.search(a)), "")
        if recipient and EMAIL_RE.match(recipient):
            if HARD_BOUNCE_STATUS.search(body):
                return "bounce_hard", recipient
            if SOFT_BOUNCE_STATUS.search(body):
                return "bounce_soft", recipient
            return "bounce_soft", recipient

    if COMPLAINT_PATTERNS.search(subject) and UNSUB_PATTERNS.search(blob):
        return "complaint", sender
    if UNSUB_PATTERNS.search(blob):
        return "unsubscribe", sender
    return None, sender


def _contact_id_for(conn: sqlite3.Connection, address: str) -> int | None:
    row = conn.execute("SELECT id FROM contacts WHERE contact_email=?", (address.lower(),)).fetchone()
    return int(row["id"]) if row else None


def apply_classification(conn: sqlite3.Connection, kind: str, address: str, detail: str) -> bool:
    """判定結果を送信禁止リストとイベントに反映する。新規除外なら True。"""
    contact_id = _contact_id_for(conn, address)
    add_event(conn, contact_id=contact_id, type=kind, detail=f"{address}: {detail}"[:500])
    if kind == "bounce_soft":
        # 一時的な不達は記録のみ。同一宛先で3回続いたら除外する。
        count = conn.execute(
            "SELECT COUNT(*) n FROM events WHERE type='bounce_soft' AND detail LIKE ?", (f"{address}%",)
        ).fetchone()["n"]
        if count < 3:
            return False
        return add_suppression(conn, "email", address, "一時バウンスが3回継続", "inbox")
    reason = {
        "bounce_hard": "宛先不明（恒久エラー）",
        "unsubscribe": "配信停止の申し出",
        "complaint": "苦情の申し出",
    }[kind]
    return add_suppression(conn, "email", address, reason, "inbox")


def ingest_imap(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    days: int = 7,
    mailbox: str = "INBOX",
    mark_seen: bool = False,
    limit: int = 500,
) -> IngestResult:
    cfg = settings.imap
    if not cfg.host or not cfg.user:
        raise RuntimeError("DM_IMAP_HOST / DM_IMAP_USER が未設定です")

    result = IngestResult()
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    client = imaplib.IMAP4_SSL(cfg.host, cfg.port)
    try:
        client.login(cfg.user, cfg.password)
        client.select(mailbox)
        status, data = client.search(None, f'(SINCE "{since}")')
        if status != "OK":
            raise RuntimeError(f"IMAP検索に失敗しました: {status}")
        ids = data[0].split()[-limit:]
        for msg_id in ids:
            status, payload = client.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            result.scanned += 1
            msg = email.message_from_bytes(payload[0][1])
            kind, address = classify_message(msg)
            if not kind or not address:
                continue
            counter = {
                "bounce_hard": "hard_bounces", "bounce_soft": "soft_bounces",
                "unsubscribe": "unsubscribes", "complaint": "complaints",
            }[kind]
            setattr(result, counter, getattr(result, counter) + 1)
            detail = _decode(msg.get("Subject"))[:200]
            if apply_classification(conn, kind, address, detail):
                result.suppressed += 1
                result.details.append(f"{kind}: {address}")
            if mark_seen:
                client.store(msg_id, "+FLAGS", "\\Seen")
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return result


def ingest_eml_dir(conn: sqlite3.Connection, directory: Any) -> IngestResult:
    """IMAPを使わず、保存済み .eml から取り込む（テスト・手動運用向け）。"""
    from pathlib import Path

    result = IngestResult()
    for path in sorted(Path(directory).glob("**/*.eml")):
        result.scanned += 1
        msg = email.message_from_bytes(path.read_bytes())
        kind, address = classify_message(msg)
        if not kind or not address:
            continue
        counter = {
            "bounce_hard": "hard_bounces", "bounce_soft": "soft_bounces",
            "unsubscribe": "unsubscribes", "complaint": "complaints",
        }[kind]
        setattr(result, counter, getattr(result, counter) + 1)
        if apply_classification(conn, kind, address, _decode(msg.get("Subject"))[:200]):
            result.suppressed += 1
            result.details.append(f"{kind}: {address}")
    return result
