"""複数シリーズの並行配信。優先度と、同一相手への二重送信の防止を確認する。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import add_contact

from dm.campaign import Campaign, Limits, Segment, Step
from dm.db import record_delivery
from dm.mailer import run_email_campaigns
from dm.selector import select_across

NOW = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)


def make_campaign(key: str, priority: int = 50, **kwargs) -> Campaign:
    return Campaign(
        key=key,
        name=f"シリーズ {key}",
        channels=kwargs.pop("channels", ["email", "form"]),
        steps=kwargs.pop("steps", [
            Step(key="s1", title="1通目", delay_days=0, subject=f"{key} のご案内",
                 body_text="email/body.txt.j2", form_subject="ご案内", form_body="form/body.txt.j2"),
        ]),
        segment=kwargs.pop("segment", Segment(ranks=["A", "B"])),
        limits=kwargs.pop("limits", Limits(max_per_run=50, max_per_domain_per_run=1)),
        priority=priority,
        **kwargs,
    )


def _mark_sent(conn, contact_id, campaign_key, step_key, when):
    record_delivery(
        conn, run_id=None, contact_id=contact_id, campaign_key=campaign_key,
        step_key=step_key, channel="email", target="a@sample.example.jp", status="sent",
    )
    conn.execute("UPDATE deliveries SET created_at=? WHERE id=(SELECT MAX(id) FROM deliveries)",
                 (when.isoformat(timespec="seconds"),))
    conn.commit()


def test_higher_priority_campaign_claims_the_contact(conn, settings):
    add_contact(conn)
    low = make_campaign("newsletter", priority=10)
    high = make_campaign("urgent", priority=90)

    planned = dict((c.key, r) for c, r in select_across(conn, [low, high], "email", settings, now=NOW))

    assert len(planned["urgent"].plans) == 1
    assert planned["newsletter"].plans == []
    assert "同じ実行で他シリーズが先に確保" in planned["newsletter"].skipped


def test_priority_order_is_deterministic(conn, settings):
    add_contact(conn)
    a = make_campaign("aaa", priority=50)
    b = make_campaign("bbb", priority=50)
    # 同じ優先度ならキー順。実行のたびに入れ替わらない。
    first = [c.key for c, _ in select_across(conn, [b, a], "email", settings, now=NOW)]
    second = [c.key for c, _ in select_across(conn, [a, b], "email", settings, now=NOW)]
    assert first == second == ["aaa", "bbb"]


def test_a_contact_is_never_sent_twice_in_one_run(conn, settings):
    for i in range(3):
        add_contact(conn, dedupe_key=f"email:x{i}@d{i}.jp", contact_email=f"x{i}@d{i}.jp", domain=f"d{i}.jp")

    campaigns = [make_campaign("one", priority=80), make_campaign("two", priority=20)]
    planned = select_across(conn, campaigns, "email", settings, now=NOW)

    targets = [p.target for _, report in planned for p in report.plans]
    assert len(targets) == len(set(targets)) == 3


def test_domain_cap_is_shared_across_campaigns(conn, settings):
    # 同一ドメインの2アドレス。1回の実行では、どちらか1件しか送らない。
    add_contact(conn, dedupe_key="email:a@same.jp", contact_email="a@same.jp", domain="same.jp")
    add_contact(conn, dedupe_key="email:b@same.jp", contact_email="b@same.jp", domain="same.jp")

    campaigns = [make_campaign("one", priority=80), make_campaign("two", priority=20)]
    planned = select_across(conn, campaigns, "email", settings, now=NOW)

    assert sum(len(r.plans) for _, r in planned) == 1


def test_recent_touch_by_another_series_blocks_this_run(conn, settings):
    settings.global_min_interval_days = 3
    contact = add_contact(conn)
    _mark_sent(conn, contact["id"], "newsletter", "s1", NOW - timedelta(days=1))

    other = make_campaign("promo", priority=90)
    planned = dict((c.key, r) for c, r in select_across(conn, [other], "email", settings, now=NOW))
    assert planned["promo"].plans == []
    assert "他シリーズで接触済み（間隔待ち）" in planned["promo"].skipped

    # 3日空けば、別シリーズから送れる
    later = dict((c.key, r) for c, r in select_across(conn, [other], "email", settings,
                                                      now=NOW + timedelta(days=3)))
    assert len(later["promo"].plans) == 1


def test_disabled_campaigns_are_skipped(conn, settings):
    add_contact(conn)
    off = make_campaign("paused", priority=99)
    off.enabled = False
    on = make_campaign("live_one", priority=10)

    planned = select_across(conn, [off, on], "email", settings, now=NOW)
    assert [c.key for c, _ in planned] == ["live_one"]
    assert len(planned[0][1].plans) == 1


def test_campaigns_not_supporting_the_channel_are_skipped(conn, settings):
    add_contact(conn)
    mail_only = make_campaign("mail_only", channels=["email"])
    planned = select_across(conn, [mail_only], "form", settings, now=NOW)
    assert planned == []


def test_high_priority_series_takes_everything_it_can(conn, settings):
    """優先度の高いシリーズは、自分の上限まで宛先を取り切る。

    低いシリーズを飢えさせたくない場合は、高い側に max_per_run を設ける（次のテスト）。
    """
    for i in range(2):
        add_contact(conn, dedupe_key=f"email:y{i}@e{i}.jp", contact_email=f"y{i}@e{i}.jp", domain=f"e{i}.jp")

    campaigns = [make_campaign("one", priority=80), make_campaign("two", priority=20)]
    result = run_email_campaigns(conn, campaigns, settings, dry_run=True)

    assert result.per_campaign == {"one": 2}
    assert result.skip_reasons["同じ実行で他シリーズが先に確保"] == 2


def test_max_per_run_lets_a_lower_priority_series_through(conn, settings):
    for i in range(2):
        add_contact(conn, dedupe_key=f"email:y{i}@e{i}.jp", contact_email=f"y{i}@e{i}.jp", domain=f"e{i}.jp")

    high = make_campaign("one", priority=80, limits=Limits(max_per_run=1, max_per_domain_per_run=1))
    low = make_campaign("two", priority=20)
    result = run_email_campaigns(conn, [high, low], settings, dry_run=True)

    assert result.per_campaign == {"one": 1, "two": 1}
    runs = conn.execute("SELECT campaign_key FROM runs ORDER BY id").fetchall()
    assert [r["campaign_key"] for r in runs] == ["one", "two"]


def test_live_run_across_campaigns_writes_every_message(conn, settings):
    for i in range(2):
        add_contact(conn, dedupe_key=f"email:z{i}@f{i}.jp", contact_email=f"z{i}@f{i}.jp", domain=f"f{i}.jp")

    campaigns = [make_campaign("one", priority=80), make_campaign("two", priority=20)]
    result = run_email_campaigns(
        conn, campaigns, settings, dry_run=False, transport_override="file", ignore_quiet_hours=True
    )
    assert result.sent == 2
    assert len(list(settings.outbox_dir.glob("*.eml"))) == 2

    # 直後に再実行しても、最短間隔に阻まれて誰にも送らない
    again = run_email_campaigns(conn, campaigns, settings, dry_run=True)
    assert again.planned == 0


def test_summary_shows_the_per_series_breakdown(conn, settings):
    for i in range(2):
        add_contact(conn, dedupe_key=f"email:w{i}@g{i}.jp", contact_email=f"w{i}@g{i}.jp", domain=f"g{i}.jp")
    alpha = make_campaign("alpha", priority=80, limits=Limits(max_per_run=1, max_per_domain_per_run=1))
    beta = make_campaign("beta", priority=20)
    result = run_email_campaigns(conn, [alpha, beta], settings, dry_run=True)

    summary = result.summary()
    assert "シリーズ別" in summary
    assert "alpha=1" in summary and "beta=1" in summary
