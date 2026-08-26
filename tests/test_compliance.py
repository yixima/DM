from __future__ import annotations

from dm.compliance import check_email_body, check_form_body, check_settings
from dm.config import Sender, Unsubscribe


def _valid_body(settings) -> str:
    return (
        "サンプル商店 御中\n\nご案内です。\n"
        f"{settings.sender.name}\n{settings.sender.address}\n{settings.sender.email}\n"
        f"配信停止: {settings.unsubscribe.base_url}?e=x&t=y\n"
    )


def test_valid_email_body_passes(settings):
    result = check_email_body(settings, "ご案内", _valid_body(settings))
    assert result.ok, result.errors


def test_missing_optout_is_rejected(settings):
    body = _valid_body(settings).replace("配信停止: ", "")
    result = check_email_body(settings, "ご案内", body)
    assert not result.ok
    assert any("配信停止" in e for e in result.errors)


def test_missing_address_is_rejected(settings):
    body = _valid_body(settings).replace(settings.sender.address, "")
    result = check_email_body(settings, "ご案内", body)
    assert not result.ok
    assert any("住所" in e for e in result.errors)


def test_missing_sender_name_is_rejected(settings):
    body = _valid_body(settings).replace(settings.sender.name, "")
    result = check_email_body(settings, "ご案内", body)
    assert not result.ok


def test_empty_subject_is_rejected(settings):
    result = check_email_body(settings, "   ", _valid_body(settings))
    assert not result.ok


def test_unrendered_template_syntax_is_rejected(settings):
    body = _valid_body(settings) + "\n{{ company_name }}"
    result = check_email_body(settings, "ご案内", body)
    assert not result.ok
    assert any("未展開" in e for e in result.errors)


def test_form_body_requires_opt_out_note(settings):
    body = f"ご案内です。\n{settings.sender.name}\n{settings.sender.email}\n"
    assert not check_form_body(settings, "ご案内", body).ok
    body += "今後のご案内が不要な場合はご返信ください。\n"
    assert check_form_body(settings, "ご案内", body).ok


def test_settings_check_reports_missing_sender_fields(settings):
    settings.sender = Sender()
    settings.unsubscribe = Unsubscribe()
    errors = check_settings(settings, "email")
    assert len(errors) >= 5
    assert any("住所" in e for e in errors)
