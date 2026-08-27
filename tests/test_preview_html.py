"""プレビューHTMLの生成。実文面がそのまま入ることと、テーマ両対応を確認する。"""
from __future__ import annotations

from conftest import add_contact

from dm.preview_html import build_preview_html, write_preview_html


def test_preview_contains_the_actual_text_that_would_be_sent(settings, conn, campaign):
    contact = add_contact(conn)
    page = build_preview_html(settings, campaign, contact)

    assert "サンプル商店 御中" in page          # 差し込み後の呼びかけ
    assert settings.sender.name in page          # 署名ブロック
    assert "ご案内1" in page and "ご案内2" in page  # 全ステップの件名
    assert "{{" not in page                      # 未展開の記法が残っていない


def test_preview_covers_every_step_and_both_channels(settings, conn, campaign):
    contact = add_contact(conn)
    page = build_preview_html(settings, campaign, contact)
    for step in campaign.steps:
        assert f'id="{step.key}"' in page
    assert page.count(">メール<") == len(campaign.steps)
    assert page.count(">フォーム<") == len(campaign.steps)


def test_single_channel_output(settings, conn, campaign):
    contact = add_contact(conn)
    page = build_preview_html(settings, campaign, contact, channels=("email",))
    assert ">メール<" in page
    assert ">フォーム<" not in page


def test_preview_is_theme_aware_and_paints_its_own_background(settings, conn, campaign):
    contact = add_contact(conn)
    page = build_preview_html(settings, campaign, contact)
    assert "prefers-color-scheme: dark" in page
    assert ':root[data-theme="dark"]' in page
    assert "background:var(--ground)" in page


def test_step_title_is_used_when_present(settings, conn, campaign):
    campaign.steps[0].title = "はじめのご挨拶"
    contact = add_contact(conn)
    page = build_preview_html(settings, campaign, contact)
    assert "はじめのご挨拶" in page


def test_write_preview_html_creates_the_file(settings, conn, campaign, tmp_path):
    contact = add_contact(conn)
    target = write_preview_html(tmp_path / "out" / "preview.html", settings, campaign, contact)
    assert target.exists()
    assert target.read_text(encoding="utf-8").startswith("<title>")
