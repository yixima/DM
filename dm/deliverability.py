"""到達性（迷惑メール判定を避けるための条件）の検査。

送信ドメインの認証設定が欠けていると、内容がどれだけ良くても大半が迷惑メール扱いになる。
Gmail は 1日5,000通を超える送信者に SPF・DKIM・DMARC の設定と、迷惑メール報告率
0.30%未満を求めている（下回っても 0.1% 未満が推奨）。
  https://support.google.com/a/answer/81126

ここでは実際に DNS を引いて、設定の有無を機械的に確かめる。
重要な原則として、**「確認できない」と「設定されていない」を区別する**。
DNS に到達できない環境では unknown を返し、NG とは言わない。
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Literal

from .config import Settings

Status = Literal["ok", "warn", "ng", "unknown"]

# DKIM は「セレクタ」を知らないと引けない。よく使われるものを順に試す。
COMMON_DKIM_SELECTORS = (
    "default", "google", "selector1", "selector2", "s1", "s2",
    "mail", "dkim", "k1", "smtp", "sendgrid", "mandrill", "zoho", "amazonses",
)

DOH_ENDPOINT = "https://dns.google/resolve"


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""

    @property
    def symbol(self) -> str:
        return {"ok": "OK", "warn": "注意", "ng": "NG", "unknown": "確認不可"}[self.status]


@dataclass
class DeliverabilityReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == "ng"]

    @property
    def unknowns(self) -> list[Check]:
        return [c for c in self.checks if c.status == "unknown"]

    def render(self) -> str:
        lines = []
        for check in self.checks:
            lines.append(f"  {check.symbol:6s} {check.name}: {check.detail}")
            if check.fix and check.status in ("ng", "warn"):
                lines.append(f"         → {check.fix}")
        return "\n".join(lines)


class DnsUnavailable(RuntimeError):
    """DNS そのものに到達できない。設定の有無は判断できない。"""


def _resolve_system(name: str, rtype: str, timeout: float) -> list[str] | None:
    """OS の DNS で引く。dnspython が無い／引けない場合は None。"""
    try:
        import dns.resolver  # type: ignore
    except ImportError:
        return None
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        answers = resolver.resolve(name, rtype)
    except Exception as exc:
        # 「レコードが無い」は答えが返ってきているので、空リストとして扱う
        name_error = type(exc).__name__ in ("NXDOMAIN", "NoAnswer")
        return [] if name_error else None
    records = []
    for answer in answers:
        # TXT は 255 バイトごとに分割されることがあるため連結する
        parts = getattr(answer, "strings", None)
        if parts:
            records.append(b"".join(parts).decode("utf-8", "replace"))
        else:
            records.append(str(answer).strip('"'))
    return records


def _resolve_https(name: str, rtype: str, timeout: float) -> list[str] | None:
    """UDP/53 が塞がれた環境向けに、HTTPS 経由（DoH）で引く。"""
    url = f"{DOH_ENDPOINT}?{urllib.parse.urlencode({'name': name, 'type': rtype})}"
    request = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if payload.get("Status") not in (0, 3):  # 0=NOERROR, 3=NXDOMAIN（レコード無し）
        return None
    return [a.get("data", "").strip('"').replace('" "', "") for a in payload.get("Answer", [])]


def resolve(name: str, rtype: str = "TXT", timeout: float = 8.0) -> list[str]:
    """DNS レコードを引く。到達できないときは DnsUnavailable。

    「レコードが無い」（空リスト）と「引けない」（例外）を区別する。
    """
    for resolver in (_resolve_system, _resolve_https):
        records = resolver(name, rtype, timeout)
        if records is not None:
            return records
    raise DnsUnavailable(f"{name} の DNS 問い合わせに失敗しました（{rtype}）")


def resolve_txt(name: str, timeout: float = 8.0) -> list[str]:
    return resolve(name, "TXT", timeout)


def has_mail_exchanger(domain: str, timeout: float = 8.0) -> bool | None:
    """そのドメインがメールを受け取れるか。

    True  = MX または A レコードがある（受け取れる可能性がある）
    False = どちらも無い（送っても確実に届かない）
    None  = DNS に到達できず判定不能（§3-15: 「無い」と断定しない）
    """
    try:
        if resolve(domain, "MX", timeout):
            return True
    except DnsUnavailable:
        return None
    # MX が無くても A があれば、そのホストが受け取る決まりになっている
    try:
        return bool(resolve(domain, "A", timeout))
    except DnsUnavailable:
        return None


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""


def check_spf(domain: str, timeout: float = 8.0) -> Check:
    name = "SPF（送信元サーバの許可リスト）"
    try:
        records = resolve_txt(domain, timeout)
    except DnsUnavailable:
        return Check(name, "unknown", f"{domain} の DNS を引けませんでした（設定の有無は不明）")

    spf = [r for r in records if r.lower().startswith("v=spf1")]
    if not spf:
        return Check(
            name, "ng", f"{domain} に SPF レコードがありません",
            "DNS に TXT レコードを追加してください。例: v=spf1 include:<送信サーバ> ~all",
        )
    if len(spf) > 1:
        return Check(
            name, "ng", f"SPF レコードが {len(spf)} 件あります（複数あると無効になります）",
            "1件にまとめてください。",
        )
    record = spf[0]
    if re.search(r"\+all", record):
        return Check(
            name, "ng", "SPF が +all（誰でも詐称可）になっています",
            "~all または -all に変更してください。",
        )
    if not re.search(r"[~\-?]all", record):
        return Check(name, "warn", f"all 指定がありません: {record[:80]}",
                     "末尾に ~all を付けてください。")
    return Check(name, "ok", record[:100])


def check_dkim(domain: str, selectors: tuple[str, ...] = COMMON_DKIM_SELECTORS,
               timeout: float = 8.0) -> Check:
    name = "DKIM（本文の電子署名）"
    unreachable = 0
    for selector in selectors:
        try:
            records = resolve_txt(f"{selector}._domainkey.{domain}", timeout)
        except DnsUnavailable:
            unreachable += 1
            continue
        for record in records:
            if "p=" in record and ("v=DKIM1" in record or "k=rsa" in record):
                return Check(name, "ok", f"セレクタ '{selector}' に鍵があります")
    if unreachable == len(selectors):
        return Check(name, "unknown", f"{domain} の DNS を引けませんでした（設定の有無は不明）")
    return Check(
        name, "ng",
        f"よく使われるセレクタ（{', '.join(selectors[:5])} 等）に鍵が見つかりません",
        "送信サービスの管理画面で DKIM を有効にし、指示された TXT を DNS に登録してください。"
        " 独自セレクタを使っている場合は --dkim-selector で指定してください。",
    )


def check_dmarc(domain: str, timeout: float = 8.0) -> Check:
    name = "DMARC（認証失敗時の扱いの宣言）"
    try:
        records = resolve_txt(f"_dmarc.{domain}", timeout)
    except DnsUnavailable:
        return Check(name, "unknown", f"_dmarc.{domain} の DNS を引けませんでした（設定の有無は不明）")

    dmarc = [r for r in records if r.lower().startswith("v=dmarc1")]
    if not dmarc:
        return Check(
            name, "ng", f"_dmarc.{domain} にレコードがありません",
            'まず "v=DMARC1; p=none; rua=mailto:dmarc@<自社ドメイン>" から始めてください。',
        )
    record = dmarc[0]
    policy = re.search(r"\bp\s*=\s*(none|quarantine|reject)", record, re.I)
    if not policy:
        return Check(name, "ng", f"p= の指定がありません: {record[:80]}", "p=none を追加してください。")
    if "rua=" not in record.lower():
        return Check(name, "warn", f"レポート送付先(rua)がありません: {record[:80]}",
                     "rua=mailto:... を足すと、認証失敗の状況を把握できます。")
    return Check(name, "ok", record[:100])


def check_reverse_dns(host: str, timeout: float = 5.0) -> Check:
    """送信サーバの逆引き（PTR）。Gmail が要求する項目のひとつ。"""
    name = "逆引き（PTRレコード）"
    if not host:
        return Check(name, "unknown", "送信サーバのホスト名が未設定です（DM_SMTP_HOST）")
    socket.setdefaulttimeout(timeout)
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, OSError):
        return Check(name, "unknown", f"{host} の名前解決ができませんでした（設定の有無は不明）")

    for address in sorted(addresses):
        try:
            pointer, _, _ = socket.gethostbyaddr(address)
        except (socket.herror, OSError):
            return Check(
                name, "warn", f"{address} の逆引きが引けません",
                "送信サーバの提供元に PTR レコードの設定を依頼してください。",
            )
        return Check(name, "ok", f"{address} → {pointer}")
    return Check(name, "unknown", f"{host} のIPアドレスを取得できませんでした")


def check_from_alignment(settings: Settings) -> Check:
    """From のドメインと、SMTP・返信先のドメインが揃っているか。

    Gmail は From ドメインが SPF か DKIM のドメインと一致していることを求める。
    """
    name = "From ドメインの整合"
    sender_domain = _domain_of(settings.sender.email)
    if not sender_domain:
        return Check(name, "ng", "送信元メールアドレスが未設定です", "DM_SENDER_EMAIL を設定してください。")

    problems = []
    reply_domain = _domain_of(settings.sender.reply_to or settings.sender.email)
    if reply_domain and reply_domain != sender_domain:
        problems.append(f"返信先が別ドメイン（{reply_domain}）")

    unsub_host = ""
    if settings.unsubscribe.email:
        unsub_host = _domain_of(settings.unsubscribe.email)
    if unsub_host and unsub_host != sender_domain:
        problems.append(f"配信停止用アドレスが別ドメイン（{unsub_host}）")

    if problems:
        return Check(
            name, "warn", "; ".join(problems),
            f"できるだけ {sender_domain} に揃えてください。ドメインが分かれると認証が通りにくくなります。",
        )
    return Check(name, "ok", f"すべて {sender_domain} で揃っています")


def check_daily_volume(sent_today: int, settings: Settings) -> Check:
    """1日あたりの送信量。5,000通を超えると Gmail の要求水準が上がる。"""
    name = "1日あたりの送信量"
    cap = settings.email_limits.max_per_day
    if cap and cap > 5000:
        return Check(
            name, "warn", f"上限が {cap} 通/日 に設定されています",
            "5,000通/日を超えると Gmail の要求が厳しくなります。認証設定が万全でなければ下げてください。",
        )
    return Check(name, "ok", f"本日 {sent_today} 通 / 上限 {cap} 通")


def run_checks(
    settings: Settings,
    *,
    sent_today: int = 0,
    dkim_selectors: tuple[str, ...] | None = None,
    timeout: float = 8.0,
) -> DeliverabilityReport:
    domain = _domain_of(settings.sender.email)
    report = DeliverabilityReport()
    report.checks.append(check_from_alignment(settings))
    if domain:
        report.checks.append(check_spf(domain, timeout))
        report.checks.append(check_dkim(domain, dkim_selectors or COMMON_DKIM_SELECTORS, timeout))
        report.checks.append(check_dmarc(domain, timeout))
    report.checks.append(check_reverse_dns(settings.smtp.host, min(timeout, 5.0)))
    report.checks.append(check_daily_volume(sent_today, settings))
    return report
