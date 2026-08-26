from __future__ import annotations

from dm.fieldmap import (
    CONFIRM_PAGE_RE,
    NON_SUBMIT_TEXT_RE,
    SUBMIT_TEXT_RE,
    SUCCESS_TEXT_RE,
    classify,
    is_agreement,
    preferred_option,
)


def test_japanese_labels_are_classified():
    assert classify("会社名").kind == "company"
    assert classify("御社名").kind == "company"
    assert classify("お名前").kind == "name"
    assert classify("フリガナ").kind == "kana"
    assert classify("メールアドレス").kind == "email"
    assert classify("電話番号").kind == "phone"
    assert classify("郵便番号").kind == "zip"
    assert classify("ご住所").kind == "address"
    assert classify("件名").kind == "subject"
    assert classify("お問い合わせ内容").kind == "message"


def test_english_name_attributes_are_classified():
    assert classify("", "", "", "", "", "company", "").kind == "company"
    assert classify("", "", "", "", "", "your_name", "").kind == "name"
    assert classify("", "", "", "", "", "email", "").kind == "email"
    assert classify("", "", "", "", "", "message", "").kind == "message"
    assert classify("", "", "", "", "", "tel", "").kind == "phone"


def test_email_confirmation_field_is_distinguished():
    assert classify("メールアドレス（確認用）").kind == "email_confirm"
    assert classify("", "", "", "", "", "email_confirm", "").kind == "email_confirm"


def test_dangerous_fields_are_marked_skip():
    assert classify("画像認証").kind == "skip"
    assert classify("パスワード").kind == "skip"
    assert classify("", "", "", "", "", "captcha_code", "").kind == "skip"


def test_unknown_label_returns_none():
    assert classify("好きな色は？") is None
    assert classify("") is None


def test_agreement_detection():
    assert is_agreement("個人情報の取り扱いに同意する")
    assert is_agreement("プライバシーポリシーに同意します")
    assert not is_agreement("メールマガジンを受け取る")


def test_preferred_option_picks_other_first():
    assert preferred_option(["ご購入について", "その他", "採用について"]) == "その他"
    assert preferred_option(["資料請求", "採用"]) == "資料請求"
    assert preferred_option(["採用について"]) is None


def test_submit_and_non_submit_button_text():
    assert SUBMIT_TEXT_RE.search("送信する")
    assert SUBMIT_TEXT_RE.search("確認画面へ")
    assert NON_SUBMIT_TEXT_RE.search("リセット")
    assert NON_SUBMIT_TEXT_RE.search("検索")


def test_success_and_confirm_page_detection():
    assert SUCCESS_TEXT_RE.search("お問い合わせありがとうございました")
    assert SUCCESS_TEXT_RE.search("送信が完了しました")
    assert CONFIRM_PAGE_RE.search("以下の内容でよろしければ送信してください")
