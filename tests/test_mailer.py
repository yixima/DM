from __future__ import annotations

import pytest
from conftest import add_contact

from dm.config import Sender
from dm.mailer import SendAborted, run_email_campaign
from dm.render import unsubscribe_token, verify_unsubscribe_token
from dm.transports import build_message


def test_dry_run_records_but_does_not_advance_step(conn, settings, campaign):
    add_contact(conn)
    result = run_email_campaign(conn, campaign, settings, dry_run=True)
    assert result.planned == 1
    assert result.sent == 0

    rows = conn.execute("SELECT status, step_key FROM deliveries").fetchall()
    assert [(r["status"], r["step_key"]) for r in rows] == [("dryrun", "s1")]

    # dry-run のあとでも、次回の対象は s1 のまま
    again = run_email_campaign(conn, campaign, settings, dry_run=True)
    assert again.planned == 1


def test_live_send_via_file_transport_advances_step(conn, settings, campaign, tmp_path):
    add_contact(conn)
    result = run_email_campaign(
        conn, campaign, settings, dry_run=False, transport_override="file", ignore_quiet_hours=True
    )
    assert result.sent == 1
    written = list(settings.outbox_dir.glob("*.eml"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8", errors="ignore")
    assert "List-Unsubscribe" in body

    # 2通目は s2 の待機期間中なので選ばれない
    second = run_email_campaign(conn, campaign, settings, dry_run=True)
    assert second.planned == 0


def test_send_aborts_when_sender_details_missing(conn, settings, campaign):
    add_contact(conn)
    settings.sender = Sender(name="", email="", address="", url="")
    with pytest.raises(SendAborted) as exc:
        run_email_campaign(conn, campaign, settings, dry_run=False, ignore_quiet_hours=True)
    assert "送信できません" in str(exc.value)


def test_send_aborts_during_quiet_hours(conn, settings, campaign, monkeypatch):
    add_contact(conn)
    monkeypatch.setattr("dm.mailer.in_quiet_hours", lambda *a, **k: True)
    with pytest.raises(SendAborted) as exc:
        run_email_campaign(conn, campaign, settings, dry_run=False, transport_override="file")
    assert "送信抑止時間帯" in str(exc.value)


def test_body_failing_compliance_is_skipped_not_sent(conn, settings, campaign):
    add_contact(conn)
    (settings.template_dir / "email" / "body.txt.j2").write_text(
        "{{ salutation }}\n配信停止の案内がない本文です。\n", encoding="utf-8"
    )
    result = run_email_campaign(
        conn, campaign, settings, dry_run=False, transport_override="file", ignore_quiet_hours=True
    )
    assert result.sent == 0
    assert list(settings.outbox_dir.glob("*.eml")) == []
    status = conn.execute("SELECT status FROM deliveries").fetchone()["status"]
    assert status == "skipped_compliance"


def test_unsubscribe_token_roundtrip():
    token = unsubscribe_token("secret", "Foo@Example.jp")
    assert verify_unsubscribe_token("secret", "foo@example.jp", token)
    assert not verify_unsubscribe_token("secret", "other@example.jp", token)
    assert not verify_unsubscribe_token("other-secret", "foo@example.jp", token)


def test_built_message_has_required_headers(settings):
    msg = build_message(
        settings,
        to_address="a@b.jp",
        subject="件名",
        text="本文",
        unsubscribe_url="https://x/unsub?e=a",
        unsubscribe_mailto="mailto:unsub@x",
    )
    assert msg["To"] == "a@b.jp"
    assert settings.sender.name in msg["From"]
    assert "List-Unsubscribe-Post" in msg
    assert msg["Auto-Submitted"] == "auto-generated"
