"""formrunner の制御フロー。ブラウザは差し替えるので外部通信は発生しない。"""
from __future__ import annotations

import pytest
from conftest import add_contact

from dm.formbot import FormOutcome
from dm.mailer import SendAborted


class FakeBrowser:
    """FormBrowser の代わり。process の戻り値を並べて渡す。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, settings, *, headless=True):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def process(self, url, subject, body, *, dry_run=True):
        self.calls.append({"url": url, "subject": subject, "body": body, "dry_run": dry_run})
        return self.outcomes.pop(0) if self.outcomes else FormOutcome(status="submitted")


@pytest.fixture
def run_form(monkeypatch):
    def runner(conn, campaign, settings, outcomes, **kwargs):
        from dm import formrunner

        fake = FakeBrowser(outcomes)
        monkeypatch.setattr(formrunner, "FormBrowser", fake)
        result = formrunner.run_form_campaign(conn, campaign, settings, **kwargs)
        return result, fake

    return runner


def test_dry_run_passes_dry_run_to_the_browser(conn, settings, campaign, run_form):
    add_contact(conn)
    result, fake = run_form(conn, campaign, settings, [FormOutcome(status="dryrun")], dry_run=True)
    assert result.planned == 1
    assert fake.calls[0]["dry_run"] is True
    assert fake.calls[0]["url"] == "https://sample.example.jp/contact/"
    assert conn.execute("SELECT status FROM deliveries").fetchone()["status"] == "dryrun"


def test_successful_submit_is_recorded_and_advances_step(conn, settings, campaign, run_form):
    add_contact(conn)
    result, _ = run_form(
        conn, campaign, settings,
        [FormOutcome(status="submitted", detail="送信完了を確認", evidence="/tmp/shot.png")],
        dry_run=False, ignore_quiet_hours=True,
    )
    assert result.sent == 1
    row = conn.execute("SELECT status, evidence, step_key FROM deliveries").fetchone()
    assert (row["status"], row["evidence"], row["step_key"]) == ("submitted", "/tmp/shot.png", "s1")

    # 同じ相手は待機期間に入るので、次の実行では選ばれない
    result2, _ = run_form(conn, campaign, settings, [], dry_run=True)
    assert result2.planned == 0


def test_captcha_is_counted_as_skipped_not_sent(conn, settings, campaign, run_form):
    add_contact(conn)
    result, _ = run_form(
        conn, campaign, settings,
        [FormOutcome(status="skipped_captcha", detail="CAPTCHAあり")],
        dry_run=False, ignore_quiet_hours=True,
    )
    assert result.sent == 0
    assert result.skip_reasons["skipped_captcha"] == 1
    assert conn.execute("SELECT status FROM deliveries").fetchone()["status"] == "skipped_captcha"


def test_skipped_contact_is_retried_next_time(conn, settings, campaign, run_form):
    add_contact(conn)
    run_form(conn, campaign, settings, [FormOutcome(status="skipped_no_form")],
             dry_run=False, ignore_quiet_hours=True)
    # 送れていないので、次回また s1 の対象になる
    result, fake = run_form(conn, campaign, settings, [FormOutcome(status="submitted")],
                            dry_run=False, ignore_quiet_hours=True)
    assert result.sent == 1
    assert fake.calls[0]["url"] == "https://sample.example.jp/contact/"


def test_failure_is_recorded_with_reason(conn, settings, campaign, run_form):
    add_contact(conn)
    result, _ = run_form(
        conn, campaign, settings,
        [FormOutcome(status="failed", detail="ページを開けません: timeout")],
        dry_run=False, ignore_quiet_hours=True,
    )
    assert result.failed == 1
    row = conn.execute("SELECT status, error FROM deliveries").fetchone()
    assert row["status"] == "failed"
    assert "timeout" in row["error"]


def test_body_failing_checks_is_never_sent(conn, settings, campaign, run_form):
    add_contact(conn)
    (settings.template_dir / "form" / "body.txt.j2").write_text(
        "{{ company_name }} ご担当者様\nご案内です。\n", encoding="utf-8"  # 送信者名も断り方も無い
    )
    result, fake = run_form(conn, campaign, settings, [], dry_run=False, ignore_quiet_hours=True)
    assert result.sent == 0
    assert fake.calls == []          # ブラウザまで到達しない
    assert conn.execute("SELECT status FROM deliveries").fetchone()["status"] == "skipped_compliance"


def test_missing_form_profile_aborts_live_run(conn, settings, campaign, run_form):
    add_contact(conn)
    settings.form_profile.person_sei = ""
    settings.form_profile.phone = ""
    with pytest.raises(SendAborted) as exc:
        run_form(conn, campaign, settings, [], dry_run=False, ignore_quiet_hours=True)
    assert "フォーム入力用プロフィール" in str(exc.value)


def test_quiet_hours_abort_live_run(conn, settings, campaign, run_form, monkeypatch):
    add_contact(conn)
    monkeypatch.setattr("dm.formrunner.in_quiet_hours", lambda *a, **k: True)
    with pytest.raises(SendAborted) as exc:
        run_form(conn, campaign, settings, [], dry_run=False)
    assert "送信抑止時間帯" in str(exc.value)
