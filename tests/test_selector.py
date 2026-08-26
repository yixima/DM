from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import add_contact
from dm.db import add_suppression, record_delivery
from dm.selector import select

NOW = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)


def _mark_sent(conn, contact_id, campaign_key, step_key, when, channel="email", status="sent"):
    record_delivery(
        conn, run_id=None, contact_id=contact_id, campaign_key=campaign_key,
        step_key=step_key, channel=channel, target="a@sample.example.jp", status=status,
    )
    conn.execute("UPDATE deliveries SET created_at=? WHERE id=(SELECT MAX(id) FROM deliveries)",
                 (when.isoformat(timespec="seconds"),))
    conn.commit()


def test_first_run_selects_first_step(conn, settings, campaign):
    add_contact(conn)
    report = select(conn, campaign, "email", settings, now=NOW)
    assert len(report.plans) == 1
    assert report.plans[0].step.key == "s1"


def test_second_step_waits_for_delay(conn, settings, campaign):
    contact = add_contact(conn)
    _mark_sent(conn, contact["id"], campaign.key, "s1", NOW - timedelta(days=10))

    report = select(conn, campaign, "email", settings, now=NOW)
    assert report.plans == []
    assert "次コンテンツの待機期間中" in report.skipped

    later = select(conn, campaign, "email", settings, now=NOW + timedelta(days=15))
    assert [p.step.key for p in later.plans] == ["s2"]


def test_completed_contact_is_not_selected_again(conn, settings, campaign):
    contact = add_contact(conn)
    _mark_sent(conn, contact["id"], campaign.key, "s1", NOW - timedelta(days=60))
    _mark_sent(conn, contact["id"], campaign.key, "s2", NOW - timedelta(days=30))
    report = select(conn, campaign, "email", settings, now=NOW)
    assert report.plans == []
    assert "全コンテンツ配信済み" in report.skipped


def test_global_min_interval_blocks_recent_touch(conn, settings, campaign):
    contact = add_contact(conn)
    # s2 の待機期間(21日)は満了しているが、別キャンペーンのフォーム送信が3日前にある
    _mark_sent(conn, contact["id"], campaign.key, "s1", NOW - timedelta(days=30))
    _mark_sent(conn, contact["id"], "other_campaign", "x", NOW - timedelta(days=3),
               channel="form", status="submitted")
    report = select(conn, campaign, "email", settings, now=NOW)
    assert report.plans == []
    assert "最短接触間隔の待機中" in report.skipped

    # 14日空けば再開する
    resumed = select(conn, campaign, "email", settings, now=NOW + timedelta(days=12))
    assert [p.step.key for p in resumed.plans] == ["s2"]


def test_dry_run_does_not_count_as_contact(conn, settings, campaign):
    contact = add_contact(conn)
    _mark_sent(conn, contact["id"], campaign.key, "s1", NOW - timedelta(days=1), status="dryrun")
    report = select(conn, campaign, "email", settings, now=NOW)
    assert [p.step.key for p in report.plans] == ["s1"]


def test_suppressed_email_is_excluded(conn, settings, campaign):
    add_contact(conn)
    add_suppression(conn, "email", "a@sample.example.jp", "配信停止の申し出")
    report = select(conn, campaign, "email", settings, now=NOW)
    assert report.plans == []
    assert report.skipped["配信停止・除外リスト"] == 1


def test_suppressed_domain_blocks_form_channel(conn, settings, campaign):
    add_contact(conn)
    add_suppression(conn, "domain", "sample.example.jp", "先方からの依頼")
    report = select(conn, campaign, "form", settings, now=NOW)
    assert report.plans == []


def test_one_send_per_domain_per_run(conn, settings, campaign):
    add_contact(conn, dedupe_key="email:a@sample.example.jp", contact_email="a@sample.example.jp")
    add_contact(conn, dedupe_key="email:b@sample.example.jp", contact_email="b@sample.example.jp")
    report = select(conn, campaign, "email", settings, now=NOW)
    assert len(report.plans) == 1
    assert report.skipped["同一ドメインの1回あたり上限"] == 1


def test_rank_a_is_prioritised(conn, settings, campaign):
    add_contact(conn, dedupe_key="email:b@x.jp", contact_email="b@x.jp", domain="x.jp", rank="B")
    add_contact(conn, dedupe_key="email:a@y.jp", contact_email="a@y.jp", domain="y.jp", rank="A")
    report = select(conn, campaign, "email", settings, limit=1, now=NOW)
    assert report.plans[0].target == "a@y.jp"


def test_run_limit_defers_the_rest(conn, settings, campaign):
    for i in range(5):
        add_contact(conn, dedupe_key=f"email:c{i}@d{i}.jp", contact_email=f"c{i}@d{i}.jp", domain=f"d{i}.jp")
    report = select(conn, campaign, "email", settings, limit=2, now=NOW)
    assert len(report.plans) == 2
    assert report.skipped["今回の上限に到達（次回に繰越）"] == 3


def test_segment_rank_filter(conn, settings, campaign):
    campaign.segment.ranks = ["A"]
    add_contact(conn, dedupe_key="email:b@x.jp", contact_email="b@x.jp", domain="x.jp", rank="B")
    report = select(conn, campaign, "email", settings, now=NOW)
    assert report.plans == []
    assert "セグメント対象外(ランク)" in report.skipped


def test_form_channel_daily_cap_per_site(conn, settings, campaign):
    contact = add_contact(conn)
    record_delivery(
        conn, run_id=None, contact_id=contact["id"], campaign_key="other",
        step_key="x", channel="form", target="https://sample.example.jp/contact/", status="submitted",
    )
    report = select(conn, campaign, "form", settings, now=datetime.now(timezone.utc))
    assert report.plans == []
