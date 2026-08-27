"""CSV 1行を正規化し、送信可否（品質）を判定する。

このリストは自動収集由来のため、そのまま送るとバウンス・誤送信を招く行が混ざっている。
取り込み時点で機械的に落とせるものはここで落とす。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# 収集スクリプトがサンプル値を拾ってしまった典型。実在しないので必ず除外する。
PLACEHOLDER_DOMAINS = {
    "domain.com", "example.com", "example.co.jp", "example.jp", "example.net",
    "yourdomain.com", "sample.com", "test.com", "mail.com", "email.com",
}
PLACEHOLDER_LOCALPARTS = {"youraddress", "yourname", "your-email", "sample", "test@test"}

# 収集スクリプトが画像・スクリプトのファイル名をアドレスとして拾うことがある。
# 例: main_slide01_sp@3x.avif（Retina画像の @2x/@3x 記法）
FILE_EXTENSION_TLDS = {
    "avif", "webp", "png", "jpg", "jpeg", "gif", "svg", "ico", "bmp", "tiff",
    "css", "js", "json", "xml", "pdf", "zip", "mp4", "webm", "woff", "woff2", "ttf",
}

# 実在しない・明らかに壊れたトップレベルドメインの形
BROKEN_TLD_RE = re.compile(r"^[a-z]{2,24}$")

FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.co.jp", "ybb.ne.jp", "hotmail.com", "hotmail.co.jp", "outlook.com",
    "outlook.jp", "icloud.com", "me.com", "docomo.ne.jp", "ezweb.ne.jp", "au.com",
    "softbank.ne.jp", "i.softbank.jp", "nifty.com", "ocn.ne.jp", "so-net.ne.jp", "biglobe.ne.jp",
    "plala.or.jp", "excite.co.jp", "live.jp", "aol.com", "crocus.ocn.ne.jp",
}

# 自社ドメインではない集約サイト。フォーム送信しても相手企業には届かない。
AGGREGATOR_DOMAINS = {
    "finance.yahoo.co.jp", "yahoo.co.jp", "google.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "amazon.co.jp", "rakuten.co.jp", "note.com", "ameblo.jp",
    "wikipedia.org", "ja.wikipedia.org", "youtube.com", "linkedin.com", "base.shop",
    "goo.ne.jp", "jimdofree.com", "wixsite.com",
}

# 問い合わせ窓口として不適切なフォームURL（検索フォーム等）
BAD_FORM_PATH_RE = re.compile(r"/(search|find-us|login|signin|cart|mypage|entry/?$)", re.I)


def host_of(url: str) -> str:
    if not url:
        return ""
    try:
        netloc = urlparse(url if "://" in url else f"https://{url}").netloc.lower()
    except ValueError:
        return ""
    return netloc.split("@")[-1].split(":")[0].removeprefix("www.")


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def is_freemail(email: str) -> bool:
    return email_domain(email) in FREEMAIL_DOMAINS


def check_email(email: str) -> tuple[bool, list[str]]:
    """(送信可か, 理由リスト)。"""
    notes: list[str] = []
    if not email:
        return False, ["メールアドレスなし"]
    if not EMAIL_RE.match(email):
        return False, ["メールアドレス形式が不正"]
    domain = email_domain(email)
    local = email.split("@", 1)[0].lower()
    if domain in PLACEHOLDER_DOMAINS or local in PLACEHOLDER_LOCALPARTS:
        return False, ["サンプル/プレースホルダのアドレス"]
    if domain.endswith(".example"):
        return False, ["サンプルドメイン"]

    tld = domain.rsplit(".", 1)[-1]
    if tld in FILE_EXTENSION_TLDS:
        return False, ["ファイル名の誤検出（アドレスではない）"]
    if not BROKEN_TLD_RE.match(tld):
        return False, [f"トップレベルドメインが不正（.{tld}）"]
    if is_freemail(email):
        notes.append("フリーメール")
    return True, notes


def check_form(form_url: str, company_domain: str) -> tuple[bool, list[str]]:
    notes: list[str] = []
    if not form_url:
        return False, ["フォームURLなし"]
    parsed = urlparse(form_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False, ["フォームURL形式が不正"]
    host = host_of(form_url)
    if host in AGGREGATOR_DOMAINS:
        return False, [f"自社サイトではない({host})"]
    if BAD_FORM_PATH_RE.search(parsed.path or ""):
        notes.append("問い合わせ用でない可能性のあるURL")
    if company_domain and host and host.removeprefix("www.") != company_domain.removeprefix("www."):
        notes.append("会社ドメインとフォームドメインが不一致")
    return True, notes


def make_dedupe_key(email: str, form_url: str, domain: str, company: str) -> str:
    """同一企業の重複行を1件に畳む鍵。メール > フォーム > ドメイン+社名 の順で採用。"""
    if email:
        return f"email:{email.lower()}"
    if form_url:
        return f"form:{form_url.lower().rstrip('/')}"
    if domain:
        return f"domain:{domain.lower()}|{company}"
    return f"company:{company}"


def normalize_row(raw: dict[str, Any]) -> dict[str, Any]:
    def get(key: str) -> str:
        return (raw.get(key) or "").strip()

    company = get("company_name")
    email = get("contact_email").lower()
    form_url = get("contact_form_url")
    domain = get("domain").lower().removeprefix("www.")

    email_ok, email_notes = check_email(email)
    form_ok, form_notes = check_form(form_url, domain)

    flag_freemail = 1 if (get("flag_freemail") == "1" or (email and is_freemail(email))) else 0
    flag_mismatch = 1 if get("flag_domain_mismatch") == "1" else 0

    notes = [f"email:{n}" for n in email_notes if n] + [f"form:{n}" for n in form_notes if n]

    return {
        "dedupe_key": make_dedupe_key(email if email_ok else "", form_url if form_ok else "", domain, company),
        "company_name": company,
        "official_url": get("official_url"),
        "domain": domain,
        "contact_email": email if email_ok else "",
        "contact_form_url": form_url if form_ok else "",
        "contact_type": get("contact_type"),
        "rank": get("rank"),
        "sources": get("sources"),
        "evidence_url": get("evidence_url"),
        "flag_freemail": flag_freemail,
        "flag_domain_mismatch": flag_mismatch,
        "email_ok": 1 if email_ok else 0,
        "form_ok": 1 if form_ok else 0,
        "quality_notes": "; ".join(notes),
    }
