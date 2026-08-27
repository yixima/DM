"""送信前のリスト検証。

自動収集のリストには、すでに存在しないドメインが混ざる。そのまま送るとバウンスになり、
バウンス率が上がると送信ドメインの評判が落ちて、正常な宛先にも届かなくなる。
送る前に DNS を引いて、確実に届かない宛先を落としておく。

判定は3値で扱う（§3-15）:
  True  … MX または A がある。受け取れる可能性がある
  False … どちらも無い。送っても届かない
  None  … DNS に到達できず判定不能。**「無い」とは断定せず、宛先はそのまま残す**
"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .db import utcnow
from .deliverability import has_mail_exchanger


@dataclass
class VerifyResult:
    total_domains: int = 0      # メール宛先のユニークドメイン数
    queried: int = 0            # 今回 DNS を引いた数
    cached: int = 0             # 判定済みで引き直さなかった数
    reachable: int = 0
    unreachable: int = 0
    undetermined: int = 0       # 引いたが DNS に到達できなかった
    not_checked: int = 0        # まだ引いていない（--limit で今回の対象外）
    contacts_disabled: int = 0
    disabled_examples: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"ユニークドメイン {self.total_domains}件"
            f"（今回問い合わせ {self.queried}件 / 判定済み流用 {self.cached}件）",
            f"  到達可 {self.reachable} / 到達不可 {self.unreachable}"
            f" / 判定不能 {self.undetermined} / 未判定 {self.not_checked}",
            f"  送信対象から外した宛先: {self.contacts_disabled}件",
        ]
        if self.total_domains and self.reachable + self.unreachable:
            judged = self.reachable + self.unreachable
            rate = self.unreachable / judged * 100
            lines.insert(2, f"  判定できたうち到達不可の割合: {rate:.1f}%（{self.unreachable}/{judged}）")
        return "\n".join(lines)


def _load_cache(conn: sqlite3.Connection) -> dict[str, bool | None]:
    rows = conn.execute("SELECT domain, has_mx FROM domain_mx").fetchall()
    return {r["domain"]: (None if r["has_mx"] is None else bool(r["has_mx"])) for r in rows}


def _save(conn: sqlite3.Connection, domain: str, has_mx: bool | None) -> None:
    detail = {True: "MXまたはAあり", False: "MX・Aとも無し", None: "DNSに到達できず判定不能"}[has_mx]
    conn.execute(
        """INSERT INTO domain_mx (domain, has_mx, detail, checked_at) VALUES (?,?,?,?)
           ON CONFLICT(domain) DO UPDATE SET has_mx=excluded.has_mx,
               detail=excluded.detail, checked_at=excluded.checked_at""",
        (domain, None if has_mx is None else int(has_mx), detail, utcnow()),
    )


def email_domains(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT substr(contact_email, instr(contact_email,'@')+1) AS d
           FROM contacts WHERE email_ok=1 AND contact_email != ''"""
    ).fetchall()
    return sorted({(r["d"] or "").lower() for r in rows if r["d"]})


def apply_cached_verdicts(conn: sqlite3.Connection) -> int:
    """記録済みの判定結果を宛先へ反映する。DNS は引かない。

    `dm import` の直後に呼ぶ。これが無いと、リストを再取り込みするたびに
    検証で外したはずの宛先が復活してしまう。
    """
    dead = [
        r["domain"] for r in
        conn.execute("SELECT domain FROM domain_mx WHERE has_mx=0").fetchall()
    ]
    return _disable_contacts(conn, dead)[0]


def _disable_contacts(conn: sqlite3.Connection, domains: list[str]) -> tuple[int, list[str]]:
    """指定ドメインの宛先をメール送信対象から外す。(件数, 例)。"""
    disabled = 0
    examples: list[str] = []
    for domain in domains:
        rows = conn.execute(
            """SELECT id, company_name, contact_email, quality_notes FROM contacts
               WHERE email_ok=1 AND lower(contact_email) LIKE ?""",
            (f"%@{domain}",),
        ).fetchall()
        for row in rows:
            notes = [n for n in (row["quality_notes"] or "").split("; ") if n]
            note = "email:メールを受け取れないドメイン"
            if note not in notes:
                notes.append(note)
            conn.execute(
                "UPDATE contacts SET email_ok=0, quality_notes=?, updated_at=? WHERE id=?",
                ("; ".join(notes), utcnow(), row["id"]),
            )
            disabled += 1
            if len(examples) < 10:
                examples.append(f"{row['company_name']} <{row['contact_email']}>")
    conn.commit()
    return disabled, examples


def verify_email_domains(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    recheck: bool = False,
    workers: int = 8,
    timeout: float = 8.0,
    apply: bool = True,
) -> VerifyResult:
    """メール宛先のドメインを実測し、届かないものを送信対象から外す。"""
    result = VerifyResult()
    cache = {} if recheck else _load_cache(conn)
    domains = email_domains(conn)

    result.total_domains = len(domains)
    unresolved = [d for d in domains if d not in cache or cache[d] is None]
    pending = unresolved[:limit] if limit else unresolved
    result.queried = len(pending)
    result.cached = len(domains) - len(unresolved)
    result.not_checked = len(unresolved) - len(pending)

    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            verdicts = list(pool.map(lambda d: has_mail_exchanger(d, timeout), pending))
        for domain, verdict in zip(pending, verdicts):
            cache[domain] = verdict
            _save(conn, domain, verdict)
        conn.commit()

    queried_or_cached = set(cache)
    for domain in domains:
        if domain not in queried_or_cached:
            continue                      # 今回は引いていない（--limit の対象外）
        verdict = cache[domain]
        if verdict is True:
            result.reachable += 1
        elif verdict is False:
            result.unreachable += 1
        else:
            result.undetermined += 1

    if not apply:
        return result

    dead = [d for d, verdict in cache.items() if verdict is False]
    result.contacts_disabled, result.disabled_examples = _disable_contacts(conn, dead)
    return result
