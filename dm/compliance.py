"""送信前の法令・運用チェック（fail-closed）。

特定電子メール法が広告宣伝メールに求める表示:
  - 送信者（会社）の氏名または名称
  - 送信者の住所
  - 苦情・問い合わせを受け付ける連絡先
  - 受信拒否（配信停止）の通知ができる旨と、その通知先URLまたはメールアドレス

問い合わせフォーム送信についても、送信者が誰かを本文で明示し、
今後の連絡が不要な場合の連絡方法を書くことを必須にしている。
チェックを通らない内容は「送らない」。警告して続行はしない。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings

OPT_OUT_HINTS = ("配信停止", "配信の停止", "受信拒否", "今後の配信", "unsubscribe")


@dataclass
class ComplianceResult:
    ok: bool
    errors: list[str]
    warnings: list[str]

    def report(self) -> str:
        lines = [f"NG: {e}" for e in self.errors] + [f"注意: {w}" for w in self.warnings]
        return "\n".join(lines) if lines else "OK"


def check_settings(settings: Settings, channel: str) -> list[str]:
    errors = [f"送信者情報が未設定: {m}" for m in settings.sender.missing()]
    if channel == "email":
        errors += [f"配信停止設定が未設定: {m}" for m in settings.unsubscribe.missing()]
    return errors


def check_email_body(settings: Settings, subject: str, text: str) -> ComplianceResult:
    errors: list[str] = []
    warnings: list[str] = []
    sender = settings.sender

    if not subject.strip():
        errors.append("件名が空です")
    if len(subject) > 78:
        warnings.append("件名が長すぎます（78文字以内推奨）")

    if sender.name and sender.name not in text:
        errors.append("本文に送信者名（会社名）が含まれていません")
    if sender.address and sender.address not in text:
        errors.append("本文に送信者の住所が含まれていません")

    contactable = any(x and x in text for x in (sender.email, sender.phone, sender.url))
    if not contactable:
        errors.append("本文に問い合わせ先（メール・電話・URLのいずれか）が含まれていません")

    if not any(hint in text for hint in OPT_OUT_HINTS):
        errors.append("本文に配信停止（受信拒否）の案内が含まれていません")

    opt_out_target = settings.unsubscribe.base_url or settings.unsubscribe.email
    if opt_out_target and opt_out_target.split("?")[0] not in text and settings.unsubscribe.email not in text:
        errors.append("本文に配信停止用のURLまたはメールアドレスが含まれていません")

    if "{{" in text or "{%" in text:
        errors.append("未展開のテンプレート記法が本文に残っています")
    if "None" in text:
        warnings.append("本文に 'None' が含まれています（差し込み漏れの可能性）")

    return ComplianceResult(ok=not errors, errors=errors, warnings=warnings)


def check_form_body(settings: Settings, subject: str, body: str) -> ComplianceResult:
    errors: list[str] = []
    warnings: list[str] = []
    sender = settings.sender

    if sender.name and sender.name not in body:
        errors.append("本文に送信者名（会社名）が含まれていません")
    contactable = any(x and x in body for x in (sender.email, sender.phone, sender.url))
    if not contactable:
        errors.append("本文に返信先（メール・電話・URLのいずれか）が含まれていません")
    if not any(hint in body for hint in ("不要", "ご連絡は不要", "今後のご案内", "停止")):
        errors.append("本文に「今後の連絡が不要な場合の案内」が含まれていません")
    if "{{" in body or "{%" in body:
        errors.append("未展開のテンプレート記法が本文に残っています")
    if len(body) > 2000:
        warnings.append("本文が2000文字を超えています（フォームの文字数制限に注意）")
    if not subject.strip():
        warnings.append("件名が空です（件名欄のあるフォームでは空欄になります）")

    return ComplianceResult(ok=not errors, errors=errors, warnings=warnings)
