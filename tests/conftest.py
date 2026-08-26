from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dm.campaign import Campaign, Limits, Segment, Step  # noqa: E402
from dm.config import EmailLimits, FormLimits, FormProfile, Sender, Settings, Unsubscribe  # noqa: E402
from dm.db import init_db  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    templates = tmp_path / "templates"
    (templates / "email").mkdir(parents=True)
    (templates / "form").mkdir(parents=True)
    (templates / "email" / "body.txt.j2").write_text(
        "{{ salutation }}\n\n{{ sender.name }}です。ご案内です。\n\n"
        "配信停止: {{ unsubscribe_url }}\n{{ sender.address }}\n{{ sender.email }}\n",
        encoding="utf-8",
    )
    (templates / "form" / "body.txt.j2").write_text(
        "{{ company_name }} ご担当者様\n\n{{ sender.name }}です。\n"
        "ご案内が不要な場合はその旨ご返信ください。\n{{ sender.email }}\n",
        encoding="utf-8",
    )
    return Settings(
        db_path=tmp_path / "dm.sqlite3",
        outbox_dir=tmp_path / "outbox",
        evidence_dir=tmp_path / "evidence",
        log_dir=tmp_path / "logs",
        template_dir=templates,
        campaign_dir=tmp_path / "campaigns",
        transport="console",
        global_min_interval_days=14,
        quiet_hours=(21, 8),
        sender=Sender(
            name="テスト株式会社",
            person="山田 太郎",
            email="info@test.example.jp",
            phone="03-0000-0000",
            address="東京都千代田区1-1-1",
            url="https://test.example.jp/",
        ),
        unsubscribe=Unsubscribe(
            base_url="https://test.example.jp/unsub",
            email="unsub@test.example.jp",
            secret="test-secret",
        ),
        form_profile=FormProfile(
            company="テスト株式会社", person_sei="山田", person_mei="太郎",
            kana_sei="ヤマダ", kana_mei="タロウ", email="info@test.example.jp",
            phone="03-0000-0000", zip="100-0001", prefecture="東京都",
            address="東京都千代田区1-1-1", url="https://test.example.jp/",
        ),
        email_limits=EmailLimits(max_per_run=50, min_seconds_between_sends=0, jitter_seconds=0, max_per_hour=None),
        form_limits=FormLimits(max_per_run=50, min_seconds_between_submits=0, jitter_seconds=0),
    )


@pytest.fixture
def conn(settings: Settings):
    settings.ensure_dirs()
    connection = init_db(settings.db_path)
    yield connection
    connection.close()


@pytest.fixture
def campaign() -> Campaign:
    return Campaign(
        key="test_campaign",
        name="テスト",
        channels=["email", "form"],
        steps=[
            Step(key="s1", delay_days=0, subject="ご案内1", body_text="email/body.txt.j2",
                 form_subject="ご案内1", form_body="form/body.txt.j2"),
            Step(key="s2", delay_days=21, subject="ご案内2", body_text="email/body.txt.j2",
                 form_subject="ご案内2", form_body="form/body.txt.j2"),
        ],
        segment=Segment(ranks=["A", "B"]),
        limits=Limits(max_per_run=50, max_per_domain_per_run=1, min_interval_days_between_touches=14),
    )


def add_contact(conn, **overrides):
    from dm.db import upsert_contacts

    row = {
        "dedupe_key": overrides.get("dedupe_key") or f"email:{overrides.get('contact_email', 'a@b.jp')}",
        "company_name": "サンプル商店",
        "official_url": "https://sample.example.jp/",
        "domain": "sample.example.jp",
        "contact_email": "a@sample.example.jp",
        "contact_form_url": "https://sample.example.jp/contact/",
        "contact_type": "both",
        "rank": "A",
        "sources": "test",
        "evidence_url": "",
        "flag_freemail": 0,
        "flag_domain_mismatch": 0,
        "email_ok": 1,
        "form_ok": 1,
        "quality_notes": "",
    }
    row.update(overrides)
    upsert_contacts(conn, [row])
    return conn.execute("SELECT * FROM contacts WHERE dedupe_key=?", (row["dedupe_key"],)).fetchone()
