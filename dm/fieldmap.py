"""日本語の問い合わせフォームの入力欄を推定する。

name/id/placeholder/label/近傍の見出しテキストを手がかりに、
「この欄は会社名」「この欄は本文」といった対応づけを行う。
確信が持てない欄には触れない（誤入力して送るより、送らない方が安い）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 各項目の手がかり。(正規表現, 重み) の並び。日本語ラベルと英語 name 属性の両方を見る。
PATTERNS: dict[str, list[tuple[str, int]]] = {
    "company": [
        (r"会社名|企業名|法人名|団体名|貴社名|御社名|社名|屋号", 10),
        (r"\bcompany\b|\bcorp\b|\bkaisha\b|\borganization\b|\borg_?name\b|\bshamei\b", 8),
    ],
    "department": [
        (r"部署|所属|部門", 10),
        (r"\bdepartment\b|\bdivision\b|\bbusho\b|\bsection\b", 8),
    ],
    "name_sei": [
        (r"^(?!.*(会社|企業|法人|団体)).*(姓|お名前\s*\(?姓|名字|苗字)", 10),
        (r"\b(last|family|sei)_?(name|kana)?\b", 8),
    ],
    "name_mei": [
        (r"^(?!.*(会社|企業|法人|団体)).*(名\s*\(?名\)?|下の名前|名前\s*\(名\))", 9),
        (r"\b(first|given|mei)_?name\b", 8),
    ],
    "name": [
        (r"(お名前|氏名|ご担当者|担当者名|担当者様|ご芳名)", 10),
        (r"\b(your_?)?name\b|\bnamae\b|\btantou\b|\bcontact_?person\b", 7),
    ],
    "kana": [
        (r"(ふりがな|フリガナ|カナ|かな)", 10),
        (r"\bkana\b|\bfurigana\b|\bruby\b", 8),
    ],
    "email_confirm": [
        # 確認用メール欄は本来のメール欄より強く判定する（両方に一致するため）
        (r"(メール.*(確認|再入力|もう一度)|確認.*メール)", 14),
        (r"\bemail_?(confirm|conf|check|re|retype|2)\b|\bmail_?confirm\b|\bconfirm_?email\b", 13),
    ],
    "email": [
        (r"(メール|Ｅメール|e-?mail|メールアドレス)", 10),
        (r"\be-?mail\b|\bmail(addr|address)?\b|\bmladdr\b", 8),
    ],
    "phone": [
        (r"(電話|TEL|お電話番号|連絡先番号)", 10),
        (r"\btel\b|\bphone\b|\bdenwa\b|\bmobile\b", 8),
    ],
    "fax": [(r"FAX|ファックス", 10), (r"\bfax\b", 8)],
    "zip": [
        (r"(郵便番号|〒|ZIP)", 10),
        (r"\bzip\b|\bpostal\b|\byubin\b|\bpost_?code\b", 8),
    ],
    "address": [
        (r"(住所|ご住所|所在地)", 10),
        (r"\baddress\b|\bjusho\b|\baddr\b", 8),
    ],
    "prefecture": [(r"都道府県", 10), (r"\bpref(ecture)?\b|\btodofuken\b", 8)],
    "url": [
        (r"(ホームページ|ウェブサイト|URL|サイトURL)", 9),
        (r"\burl\b|\bwebsite\b|\bhomepage\b|\bhp\b", 7),
    ],
    "subject": [
        (r"(件名|表題|タイトル|お問い合わせ件名)", 10),
        (r"\bsubject\b|\btitle\b|\bkenmei\b", 8),
    ],
    "message": [
        (r"(お問い合わせ内容|問合せ内容|ご相談内容|内容|本文|メッセージ|ご質問|詳細|備考|通信欄)", 10),
        (r"\b(message|body|content|inquiry|question|comment|detail|naiyou|honbun|remarks?)\b", 8),
    ],
}

# 触ってはいけない欄（送信の意味が変わる、あるいは相手の運用を壊す）
SKIP_PATTERNS = re.compile(
    r"(captcha|認証|画像認証|パスワード|password|クレジット|card|amount|金額|数量|quantity)",
    re.IGNORECASE,
)

# 同意チェックボックスの手がかり。必須の場合だけチェックする。
AGREE_PATTERNS = re.compile(
    r"(同意|承諾|プライバシー|個人情報|privacy|agree|consent|規約)", re.IGNORECASE
)

# 「返信は不要」等、相手の負担になる選択を避けるためのラジオ/セレクトの優先語
INQUIRY_TYPE_PREFERENCES = [
    r"その他", r"営業|提案|ご提案|セールス", r"お問い合わせ|問合せ|一般", r"資料請求",
]

CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare.com']",
    ".g-recaptcha",
    "[data-sitekey]",
    ".h-captcha",
    ".cf-turnstile",
    "input[name*='captcha' i]",
    "img[src*='captcha' i]",
]

SUBMIT_TEXT_RE = re.compile(r"(送信|確認|申し込|申込|送る|submit|send|confirm|次へ)", re.IGNORECASE)
# 「戻る」「クリア」「リセット」「検索」は押してはいけない
NON_SUBMIT_TEXT_RE = re.compile(r"(戻る|クリア|リセット|取消|キャンセル|検索|reset|clear|cancel|back|search)", re.IGNORECASE)

SUCCESS_TEXT_RE = re.compile(
    r"(ありがとうございま|送信(が)?(完了|されました|しました)|受け付けました|受付けました|"
    r"受付完了|完了いたしました|thank you|successfully sent|送信完了)",
    re.IGNORECASE,
)
ERROR_TEXT_RE = re.compile(
    r"(入力してください|選択してください|必須|エラー|正しく|不正な|失敗しました|required|invalid)",
    re.IGNORECASE,
)
CONFIRM_PAGE_RE = re.compile(r"(確認画面|入力内容の確認|以下の内容で|この内容で(送信)?|ご確認ください)")


@dataclass
class FieldGuess:
    kind: str
    score: int


def classify(*hints: str) -> FieldGuess | None:
    """name/id/placeholder/label などのヒント文字列から欄の種類を推定する。"""
    blob = " ".join(h for h in hints if h).strip()
    if not blob:
        return None
    if SKIP_PATTERNS.search(blob):
        return FieldGuess(kind="skip", score=100)

    best: FieldGuess | None = None
    for kind, patterns in PATTERNS.items():
        for pattern, weight in patterns:
            if re.search(pattern, blob, re.IGNORECASE):
                # 同点なら先に定義された種別（より具体的なもの）を優先する
                if best is None or weight > best.score:
                    best = FieldGuess(kind=kind, score=weight)
                break
    return best


def is_agreement(*hints: str) -> bool:
    blob = " ".join(h for h in hints if h)
    return bool(AGREE_PATTERNS.search(blob))


def preferred_option(options: list[str]) -> str | None:
    """選択肢のうち、営業連絡として最も妥当なものを選ぶ。"""
    for pattern in INQUIRY_TYPE_PREFERENCES:
        for option in options:
            if re.search(pattern, option):
                return option
    return None
