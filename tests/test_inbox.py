from __future__ import annotations

import email

from conftest import add_contact
from dm.db import load_suppressions
from dm.inbox import apply_classification, classify_message, ingest_eml_dir

HARD_BOUNCE = """From: Mail Delivery Subsystem <MAILER-DAEMON@mx.example.jp>
To: info@test.example.jp
Subject: Undelivered Mail Returned to Sender
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain

This is the mail system at host mx.example.jp.

--B
Content-Type: message/delivery-status

Final-Recipient: rfc822; a@sample.example.jp
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 User unknown

--B--
"""

SOFT_BOUNCE = HARD_BOUNCE.replace("Status: 5.1.1", "Status: 4.2.2").replace(
    "550 5.1.1 User unknown", "452 4.2.2 Mailbox full"
)

UNSUB_REPLY = """From: 担当者 <a@sample.example.jp>
To: info@test.example.jp
Subject: Re: 【ご案内】サービスのご案内
Content-Type: text/plain; charset=utf-8

今後のメール配信は不要です。配信停止をお願いします。
"""

NORMAL_REPLY = """From: 担当者 <a@sample.example.jp>
To: info@test.example.jp
Subject: Re: 【ご案内】サービスのご案内
Content-Type: text/plain; charset=utf-8

資料を拝見しました。詳しい話を聞かせてください。
"""


def _msg(raw: str):
    # 実運用（IMAP/.eml）と同じくバイト列から解析する
    return email.message_from_bytes(raw.encode("utf-8"))


def test_hard_bounce_is_detected():
    kind, address = classify_message(_msg(HARD_BOUNCE))
    assert kind == "bounce_hard"
    assert address == "a@sample.example.jp"


def test_soft_bounce_is_detected():
    kind, address = classify_message(_msg(SOFT_BOUNCE))
    assert kind == "bounce_soft"
    assert address == "a@sample.example.jp"


def test_unsubscribe_reply_is_detected():
    kind, address = classify_message(_msg(UNSUB_REPLY))
    assert kind == "unsubscribe"
    assert address == "a@sample.example.jp"


def test_ordinary_reply_is_left_alone():
    kind, _ = classify_message(_msg(NORMAL_REPLY))
    assert kind is None


def test_hard_bounce_is_suppressed_immediately(conn):
    add_contact(conn)
    assert apply_classification(conn, "bounce_hard", "a@sample.example.jp", "User unknown") is True
    assert "a@sample.example.jp" in load_suppressions(conn)["email"]


def test_soft_bounce_needs_three_occurrences(conn):
    add_contact(conn)
    for _ in range(2):
        assert apply_classification(conn, "bounce_soft", "a@sample.example.jp", "Mailbox full") is False
    assert apply_classification(conn, "bounce_soft", "a@sample.example.jp", "Mailbox full") is True
    assert "a@sample.example.jp" in load_suppressions(conn)["email"]


def test_ingest_from_eml_directory(conn, tmp_path):
    add_contact(conn)
    maildir = tmp_path / "maildir"
    maildir.mkdir()
    (maildir / "1.eml").write_text(HARD_BOUNCE, encoding="utf-8")
    (maildir / "2.eml").write_text(UNSUB_REPLY, encoding="utf-8")
    (maildir / "3.eml").write_text(NORMAL_REPLY, encoding="utf-8")

    result = ingest_eml_dir(conn, maildir)
    assert result.scanned == 3
    assert result.hard_bounces == 1
    assert result.unsubscribes == 1
    assert result.suppressed == 1  # 同じアドレスなので除外登録は1件
    assert "a@sample.example.jp" in load_suppressions(conn)["email"]


def test_suppression_blocks_future_selection(conn, settings, campaign):
    from dm.selector import select

    add_contact(conn)
    apply_classification(conn, "unsubscribe", "a@sample.example.jp", "配信停止希望")
    assert select(conn, campaign, "email", settings).plans == []
