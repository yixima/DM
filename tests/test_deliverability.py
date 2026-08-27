"""到達性チェック。DNSは差し替えるので外部通信は発生しない。"""
from __future__ import annotations

import pytest
from conftest import add_contact

from dm import deliverability as dl
from dm.db import sent_today
from dm.mailer import run_email_campaign


@pytest.fixture
def fake_dns(monkeypatch):
    """{名前: [TXTレコード]} を渡す。未登録の名前は「レコードなし」を返す。"""
    def install(zone: dict[str, list[str]], unreachable: bool = False):
        def resolve(name, timeout=8.0):
            if unreachable:
                raise dl.DnsUnavailable(name)
            return zone.get(name, [])
        monkeypatch.setattr(dl, "resolve_txt", resolve)
    return install


# ---------------------------------------------------------------- SPF

def test_spf_missing_is_reported(fake_dns):
    fake_dns({})
    check = dl.check_spf("example.jp")
    assert check.status == "ng"
    assert "SPF レコードがありません" in check.detail


def test_spf_valid(fake_dns):
    fake_dns({"example.jp": ["v=spf1 include:_spf.example.net ~all"]})
    assert dl.check_spf("example.jp").status == "ok"


def test_spf_permissive_all_is_rejected(fake_dns):
    fake_dns({"example.jp": ["v=spf1 +all"]})
    check = dl.check_spf("example.jp")
    assert check.status == "ng"
    assert "+all" in check.detail


def test_duplicate_spf_records_are_rejected(fake_dns):
    fake_dns({"example.jp": ["v=spf1 include:a ~all", "v=spf1 include:b ~all"]})
    assert dl.check_spf("example.jp").status == "ng"


def test_spf_without_all_is_a_warning(fake_dns):
    fake_dns({"example.jp": ["v=spf1 include:_spf.example.net"]})
    assert dl.check_spf("example.jp").status == "warn"


# ---------------------------------------------------------------- DKIM

def test_dkim_found_on_a_common_selector(fake_dns):
    fake_dns({"selector1._domainkey.example.jp": ["v=DKIM1; k=rsa; p=MIGfMA0G"]})
    check = dl.check_dkim("example.jp")
    assert check.status == "ok"
    assert "selector1" in check.detail


def test_dkim_missing_is_reported(fake_dns):
    fake_dns({})
    assert dl.check_dkim("example.jp").status == "ng"


def test_dkim_custom_selector_can_be_given(fake_dns):
    fake_dns({"20260801._domainkey.example.jp": ["v=DKIM1; p=abc"]})
    assert dl.check_dkim("example.jp").status == "ng"          # 既定のセレクタでは見つからない
    assert dl.check_dkim("example.jp", ("20260801",)).status == "ok"


# ---------------------------------------------------------------- DMARC

def test_dmarc_valid(fake_dns):
    fake_dns({"_dmarc.example.jp": ["v=DMARC1; p=none; rua=mailto:d@example.jp"]})
    assert dl.check_dmarc("example.jp").status == "ok"


def test_dmarc_missing_is_reported(fake_dns):
    fake_dns({})
    check = dl.check_dmarc("example.jp")
    assert check.status == "ng"
    assert "p=none" in check.fix


def test_dmarc_without_report_address_is_a_warning(fake_dns):
    fake_dns({"_dmarc.example.jp": ["v=DMARC1; p=none"]})
    assert dl.check_dmarc("example.jp").status == "warn"


# ---------------------------------------------------------------- 確認不可の扱い

def test_unreachable_dns_is_unknown_not_a_failure(fake_dns):
    """§3-15: 「確認できない」と「設定されていない」を同一視しない。"""
    fake_dns({}, unreachable=True)
    for check in (dl.check_spf("example.jp"), dl.check_dmarc("example.jp"), dl.check_dkim("example.jp")):
        assert check.status == "unknown", check
        assert "不明" in check.detail


def test_report_separates_failures_from_unknowns(fake_dns, settings):
    fake_dns({}, unreachable=True)
    report = dl.run_checks(settings, sent_today=0)
    assert report.failures == []
    assert len(report.unknowns) >= 3


# ---------------------------------------------------------------- From の整合

def test_aligned_domains_pass(settings):
    settings.sender.reply_to = settings.sender.email
    settings.unsubscribe.email = f"unsub@{settings.sender.email.split('@')[1]}"
    assert dl.check_from_alignment(settings).status == "ok"


def test_mismatched_reply_to_is_a_warning(settings):
    settings.sender.reply_to = "someone@other.example.com"
    check = dl.check_from_alignment(settings)
    assert check.status == "warn"
    assert "返信先" in check.detail


def test_missing_sender_email_is_a_failure(settings):
    settings.sender.email = ""
    assert dl.check_from_alignment(settings).status == "ng"


# ---------------------------------------------------------------- 1日あたりの上限

def test_daily_volume_above_gmail_threshold_warns(settings):
    settings.email_limits.max_per_day = 8000
    check = dl.check_daily_volume(0, settings)
    assert check.status == "warn"
    assert "5,000" in check.fix


def test_daily_cap_stops_sending_once_reached(conn, settings, campaign):
    settings.email_limits.max_per_day = 1
    for i in range(3):
        add_contact(conn, dedupe_key=f"email:p{i}@q{i}.jp", contact_email=f"p{i}@q{i}.jp", domain=f"q{i}.jp")

    first = run_email_campaign(
        conn, campaign, settings, dry_run=False, transport_override="file", ignore_quiet_hours=True
    )
    assert first.sent == 1
    assert sent_today(conn, settings.timezone) == 1

    second = run_email_campaign(
        conn, campaign, settings, dry_run=False, transport_override="file", ignore_quiet_hours=True
    )
    assert second.sent == 0
    assert any("本日の上限に到達" in reason for reason in second.skip_reasons)


def test_dry_run_does_not_consume_the_daily_budget(conn, settings, campaign):
    settings.email_limits.max_per_day = 1
    add_contact(conn)
    run_email_campaign(conn, campaign, settings, dry_run=True)
    assert sent_today(conn, settings.timezone) == 0

    live = run_email_campaign(
        conn, campaign, settings, dry_run=False, transport_override="file", ignore_quiet_hours=True
    )
    assert live.sent == 1


# ---------------------------------------------------------------- 専用送信アドレス

def test_shared_representative_address_is_flagged(settings):
    for local in ("info", "contact", "support", "sales", "otoiawase"):
        settings.sender.email = f"{local}@test.example.jp"
        check = dl.check_dedicated_sender(settings)
        assert check.status == "warn", local
        assert "共用されがち" in check.detail


def test_dedicated_address_passes(settings):
    for local in ("pr", "news", "dm", "info-campaign"):
        settings.sender.email = f"{local}@test.example.jp"
        assert dl.check_dedicated_sender(settings).status == "ok", local


def test_missing_sender_address_is_a_failure(settings):
    settings.sender.email = ""
    assert dl.check_dedicated_sender(settings).status == "ng"


def test_dedicated_sender_check_is_part_of_the_report(fake_dns, settings):
    fake_dns({}, unreachable=True)
    names = [c.name for c in dl.run_checks(settings, sent_today=0).checks]
    assert "DM専用の送信アドレス" in names
