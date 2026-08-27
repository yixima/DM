"""送信前のドメイン検証。DNSは差し替えるので外部通信は発生しない。"""
from __future__ import annotations

import pytest
from conftest import add_contact

from dm import verify as v


@pytest.fixture
def fake_mx(monkeypatch):
    """{ドメイン: True/False/None} を渡す。未登録は False（到達不可）とみなす。"""
    def install(verdicts: dict[str, bool | None]):
        calls: list[str] = []

        def check(domain, timeout=8.0):
            calls.append(domain)
            return verdicts.get(domain, False)

        monkeypatch.setattr(v, "has_mail_exchanger", check)
        return calls
    return install


def _contacts(conn, *domains):
    for i, domain in enumerate(domains):
        add_contact(
            conn, dedupe_key=f"email:c{i}@{domain}",
            contact_email=f"c{i}@{domain}", domain=domain,
        )


def test_unreachable_domain_is_removed_from_sending(conn, fake_mx):
    fake_mx({"alive.jp": True, "dead.jp": False})
    _contacts(conn, "alive.jp", "dead.jp")

    result = v.verify_email_domains(conn)
    assert result.reachable == 1
    assert result.unreachable == 1
    assert result.contacts_disabled == 1

    rows = {r["contact_email"]: r["email_ok"] for r in
            conn.execute("SELECT contact_email, email_ok FROM contacts")}
    assert rows["c0@alive.jp"] == 1
    assert rows["c1@dead.jp"] == 0


def test_disabled_contact_records_the_reason(conn, fake_mx):
    fake_mx({"dead.jp": False})
    _contacts(conn, "dead.jp")
    v.verify_email_domains(conn)
    notes = conn.execute("SELECT quality_notes FROM contacts").fetchone()["quality_notes"]
    assert "メールを受け取れないドメイン" in notes


def test_undetermined_domain_is_left_alone(conn, fake_mx):
    """§3-15: 「確認できない」を「宛先が無い」と同一視しない。"""
    fake_mx({"unknown.jp": None})
    _contacts(conn, "unknown.jp")

    result = v.verify_email_domains(conn)
    assert result.undetermined == 1
    assert result.contacts_disabled == 0
    assert conn.execute("SELECT email_ok FROM contacts").fetchone()["email_ok"] == 1


def test_dry_run_does_not_change_contacts(conn, fake_mx):
    fake_mx({"dead.jp": False})
    _contacts(conn, "dead.jp")

    result = v.verify_email_domains(conn, apply=False)
    assert result.unreachable == 1
    assert result.contacts_disabled == 0
    assert conn.execute("SELECT email_ok FROM contacts").fetchone()["email_ok"] == 1


def test_results_are_cached_so_reruns_are_cheap(conn, fake_mx):
    calls = fake_mx({"alive.jp": True})
    _contacts(conn, "alive.jp")

    v.verify_email_domains(conn)
    assert calls == ["alive.jp"]

    v.verify_email_domains(conn)
    assert calls == ["alive.jp"]          # 2回目は引き直さない

    v.verify_email_domains(conn, recheck=True)
    assert calls == ["alive.jp", "alive.jp"]


def test_undetermined_domains_are_retried_next_run(conn, fake_mx):
    """判定不能はキャッシュに残さず、次回また引く。"""
    calls = fake_mx({"flaky.jp": None})
    _contacts(conn, "flaky.jp")
    v.verify_email_domains(conn)
    v.verify_email_domains(conn)
    assert calls == ["flaky.jp", "flaky.jp"]


def test_limit_splits_the_work_and_reports_the_remainder(conn, fake_mx):
    fake_mx({f"d{i}.jp": True for i in range(5)})
    _contacts(conn, *[f"d{i}.jp" for i in range(5)])

    first = v.verify_email_domains(conn, limit=2)
    assert first.queried == 2
    assert first.not_checked == 3
    assert first.reachable == 2

    second = v.verify_email_domains(conn, limit=2)
    assert second.queried == 2
    assert second.cached == 2
    assert second.not_checked == 1


def test_summary_reports_the_unreachable_share(conn, fake_mx):
    fake_mx({"a.jp": True, "b.jp": True, "c.jp": True, "dead.jp": False})
    _contacts(conn, "a.jp", "b.jp", "c.jp", "dead.jp")
    summary = v.verify_email_domains(conn).summary()
    assert "到達不可の割合: 25.0%" in summary


def test_verified_contacts_are_excluded_from_the_next_plan(conn, settings, campaign, fake_mx):
    from dm.selector import select

    fake_mx({"alive.jp": True, "dead.jp": False})
    _contacts(conn, "alive.jp", "dead.jp")
    v.verify_email_domains(conn)

    plans = select(conn, campaign, "email", settings).plans
    assert [p.target for p in plans] == ["c0@alive.jp"]
