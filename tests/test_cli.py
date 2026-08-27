"""CLI の配線確認。実送信はせず、dry-run と読み取り系のみ動かす。"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from dm.cli import main
from dm.throttle import in_quiet_hours

CSV = textwrap.dedent(
    """\
    company_name,official_url,domain,contact_email,contact_form_url,contact_type,rank,sources,evidence_url,flag_freemail,flag_domain_mismatch
    サンプル商店,https://sample.example.jp/,sample.example.jp,a@sample.example.jp,https://sample.example.jp/contact/,both,A,test,,,
    テスト工業,https://kojo.example.jp/,kojo.example.jp,b@kojo.example.jp,,email,A,test,,,
    ダミー社,https://dummy.example.jp/,dummy.example.jp,info@domain.com,https://dummy.example.jp/contact/,none,B,test,,,1
    """
)

CAMPAIGN = textwrap.dedent(
    """\
    key: cli_test
    name: CLIテスト
    channels: [email, form]
    limits:
      max_per_run: 10
    steps:
      - key: s1
        delay_days: 0
        subject: "ご案内 {{ company_name }}"
        body_text: email/body.txt.j2
        form_subject: "ご案内"
        form_body: form/body.txt.j2
    """
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "contacts.csv").write_text(CSV, encoding="utf-8")
    (tmp_path / "campaigns").mkdir()
    (tmp_path / "campaigns" / "cli_test.yaml").write_text(CAMPAIGN, encoding="utf-8")
    templates = tmp_path / "templates"
    (templates / "email").mkdir(parents=True)
    (templates / "form").mkdir(parents=True)
    (templates / "email" / "body.txt.j2").write_text(
        "{{ salutation }}\n{{ sender.name }}\n{{ sender.address }}\n{{ sender.email }}\n"
        "配信停止: {{ unsubscribe_url }}\n",
        encoding="utf-8",
    )
    (templates / "form" / "body.txt.j2").write_text(
        "{{ company_name }} ご担当者様\n{{ sender.name }}\n{{ sender.email }}\n"
        "今後のご案内が不要な場合はご返信ください。\n",
        encoding="utf-8",
    )
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text(
        textwrap.dedent(
            f"""\
            paths:
              contacts_csv: {tmp_path}/data/contacts.csv
              db: {tmp_path}/state/dm.sqlite3
              outbox: {tmp_path}/state/outbox
              evidence: {tmp_path}/state/evidence
              logs: {tmp_path}/state/logs
              campaigns: {tmp_path}/campaigns
              templates: {templates}
            sending:
              transport: console
            """
        ),
        encoding="utf-8",
    )
    for key, value in {
        "DM_SENDER_NAME": "テスト株式会社",
        "DM_SENDER_PERSON": "山田 太郎",
        "DM_SENDER_EMAIL": "info@test.example.jp",
        "DM_SENDER_PHONE": "03-0000-0000",
        "DM_SENDER_ADDRESS": "東京都千代田区1-1-1",
        "DM_SENDER_URL": "https://test.example.jp/",
        "DM_UNSUBSCRIBE_BASE_URL": "https://test.example.jp/unsub",
        "DM_UNSUBSCRIBE_EMAIL": "unsub@test.example.jp",
        "DM_UNSUBSCRIBE_SECRET": "cli-test-secret",
    }.items():
        monkeypatch.setenv(key, value)
    return settings_file


def run(settings_file: Path, *args: str) -> int:
    return main(["--settings", str(settings_file), *args])


def test_import_then_plan_then_dry_run(project, capsys):
    assert run(project, "init") == 0
    assert run(project, "import") == 0
    out = capsys.readouterr().out
    assert "登録された宛先     : 3" in out

    assert run(project, "plan", "--campaign", "cli_test", "--channel", "email") == 0
    out = capsys.readouterr().out
    assert "対象 2件" in out          # info@domain.com の1件はメール不可

    assert run(project, "plan", "--campaign", "cli_test", "--channel", "form") == 0
    assert "対象 2件" in capsys.readouterr().out   # フォームURLのある2件

    assert run(project, "send", "--campaign", "cli_test") == 0
    assert "[dry-run]" in capsys.readouterr().out


def test_doctor_reports_ok_when_configured(project, capsys):
    run(project, "import")
    capsys.readouterr()
    assert run(project, "doctor") == 0
    assert "問題は見つかりませんでした" in capsys.readouterr().out


def test_doctor_fails_when_sender_missing(project, capsys, monkeypatch):
    run(project, "import")
    capsys.readouterr()
    monkeypatch.setenv("DM_SENDER_ADDRESS", "")
    assert run(project, "doctor") == 1
    assert "住所" in capsys.readouterr().out


def test_preview_runs_compliance_check(project, capsys):
    run(project, "import")
    capsys.readouterr()
    assert run(project, "preview", "--campaign", "cli_test", "--channel", "email") == 0
    assert "チェック: OK" in capsys.readouterr().out


def test_unknown_campaign_is_reported(project, capsys):
    run(project, "import")
    capsys.readouterr()
    assert run(project, "plan", "--campaign", "no_such") == 1
    assert "見つかりません" in capsys.readouterr().err


def test_suppression_removes_target_from_plan(project, capsys):
    run(project, "import")
    run(project, "suppress", "--kind", "email", "--value", "a@sample.example.jp")
    capsys.readouterr()
    assert run(project, "plan", "--campaign", "cli_test", "--channel", "email") == 0
    assert "対象 1件" in capsys.readouterr().out


def test_quiet_hours_window_wraps_midnight():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tokyo = ZoneInfo("Asia/Tokyo")
    assert in_quiet_hours((21, 8), now=datetime(2026, 9, 1, 22, 0, tzinfo=tokyo)) is True
    assert in_quiet_hours((21, 8), now=datetime(2026, 9, 1, 3, 0, tzinfo=tokyo)) is True
    assert in_quiet_hours((21, 8), now=datetime(2026, 9, 1, 10, 0, tzinfo=tokyo)) is False
    assert in_quiet_hours((21, 8), now=datetime(2026, 9, 1, 20, 59, tzinfo=tokyo)) is False
    assert in_quiet_hours((0, 0), now=datetime(2026, 9, 1, 3, 0, tzinfo=tokyo)) is False


def _shrink_list(tmp_path) -> None:
    """CSVを1件だけに縮める（別セッションの書き出し失敗を模す）。"""
    lines = CSV.strip().split("\n")
    (tmp_path / "data" / "contacts.csv").write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")


def test_sudden_shrink_is_refused(project, tmp_path, capsys):
    """宛先が急に減るCSVは、書き込む前に止める。"""
    run(project, "import")
    capsys.readouterr()
    _shrink_list(tmp_path)

    assert run(project, "import") == 2
    err = capsys.readouterr().err
    assert "取り込みを中止しました" in err
    assert "宛先は1件も変更していません" in err

    # 宛先はそのまま残っている
    assert run(project, "plan", "--campaign", "cli_test", "--channel", "email") == 0
    assert "対象 2件" in capsys.readouterr().out


def test_missing_contacts_are_reported_but_not_deleted(project, tmp_path, capsys):
    run(project, "import")
    capsys.readouterr()
    _shrink_list(tmp_path)

    assert run(project, "import", "--force") == 0
    out = capsys.readouterr().out
    assert "今回のCSVに無かった宛先: 2件" in out
    assert "削除はしていません" in out


def test_deactivate_missing_pauses_them(project, tmp_path, capsys):
    run(project, "import")
    _shrink_list(tmp_path)
    capsys.readouterr()

    assert run(project, "import", "--force", "--deactivate-missing") == 0
    assert "送信対象から外しました" in capsys.readouterr().out

    assert run(project, "plan", "--campaign", "cli_test", "--channel", "email") == 0
    assert "対象 1件" in capsys.readouterr().out


def test_import_from_a_watched_folder(project, tmp_path, capsys):
    """別セッションの書き出し先フォルダから、最新のCSVを拾う。"""
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "master_contacts_20260901_120000.csv").write_text(CSV, encoding="utf-8")
    capsys.readouterr()

    assert run(project, "import", "--from-dir", str(folder)) == 0
    out = capsys.readouterr().out
    assert "master_contacts_20260901_120000.csv" in out
    assert "登録された宛先     : 3" in out
