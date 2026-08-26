from __future__ import annotations

from dm.normalize import check_email, check_form, host_of, make_dedupe_key, normalize_row


def test_placeholder_addresses_are_rejected():
    for address in ("info@domain.com", "youraddress@domain.com", "user@example.com"):
        ok, reasons = check_email(address)
        assert ok is False, address
        assert reasons


def test_valid_business_address_passes():
    ok, notes = check_email("assist-tokyo@artec-kk.co.jp")
    assert ok is True
    assert notes == []


def test_freemail_is_allowed_but_flagged():
    ok, notes = check_email("aikawa@crocus.ocn.ne.jp")
    assert ok is True
    assert "フリーメール" in notes


def test_malformed_address_is_rejected():
    assert check_email("not-an-address")[0] is False
    assert check_email("")[0] is False


def test_aggregator_form_urls_are_rejected():
    ok, _ = check_form("https://finance.yahoo.co.jp/search/", "finance.yahoo.co.jp")
    assert ok is False


def test_own_site_form_passes():
    ok, notes = check_form("https://www.artec-kk.co.jp/contact/", "artec-kk.co.jp")
    assert ok is True
    assert notes == []


def test_search_like_path_is_flagged_but_kept():
    ok, notes = check_form("https://www.ahmcompany.com/find-us", "ahmcompany.com")
    assert ok is True
    assert any("問い合わせ用でない" in n for n in notes)


def test_host_of_strips_www_and_port():
    assert host_of("https://www.example.co.jp:443/contact") == "example.co.jp"
    assert host_of("") == ""


def test_dedupe_key_prefers_email():
    assert make_dedupe_key("A@B.jp", "https://x/", "b.jp", "会社").startswith("email:")
    assert make_dedupe_key("", "https://x/contact/", "b.jp", "会社").startswith("form:")
    assert make_dedupe_key("", "", "b.jp", "会社").startswith("domain:")


def test_normalize_row_drops_unusable_values():
    row = normalize_row({
        "company_name": "（株）RKT",
        "official_url": "https://rktjh.jp/",
        "domain": "rktjh.jp",
        "contact_email": "info@domain.com",
        "contact_form_url": "https://rktjh.jp/contact/",
        "contact_type": "none",
        "rank": "A",
        "sources": "tigs101",
        "evidence_url": "",
        "flag_freemail": "",
        "flag_domain_mismatch": "1",
    })
    assert row["contact_email"] == ""      # プレースホルダは落とす
    assert row["email_ok"] == 0
    assert row["form_ok"] == 1             # フォームは使える
    assert row["flag_domain_mismatch"] == 1
