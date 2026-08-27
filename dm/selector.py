"""「今回の実行で、誰に、どのコンテンツを送るか」を決める。

定期実行の要。ここで
  1. 配信停止・除外リストを外し
  2. セグメント条件で絞り
  3. 過剰接触にならない間隔を確認し
  4. 各宛先が次に受け取るべき step を1つだけ選ぶ
を行う。送信モジュールは、ここが出した計画をそのまま実行するだけ。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .campaign import Campaign, Step
from .config import Settings
from .db import load_suppressions
from .normalize import host_of

# 「実際に届いた」とみなすステータス。dry-run は履歴に残るが接触にはカウントしない。
SUCCESS_STATUSES = ("sent", "submitted")


@dataclass
class Plan:
    contact: sqlite3.Row
    step: Step
    channel: str
    target: str

    @property
    def contact_id(self) -> int:
        return int(self.contact["id"])

    @property
    def company(self) -> str:
        return str(self.contact["company_name"])


@dataclass
class SelectionReport:
    plans: list[Plan]
    skipped: dict[str, int]

    def summary(self) -> str:
        parts = [f"対象 {len(self.plans)}件"]
        parts += [f"{k} {v}件" for k, v in sorted(self.skipped.items(), key=lambda kv: -kv[1])]
        return " / ".join(parts)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _days_since(ts: str | None, now: datetime) -> float:
    parsed = _parse(ts)
    if parsed is None:
        return float("inf")
    return (now - parsed).total_seconds() / 86400.0


def _history(conn: sqlite3.Connection, campaign_key: str) -> dict[int, dict[str, str]]:
    """contact_id -> {step_key: 最終成功日時}"""
    out: dict[int, dict[str, str]] = {}
    placeholders = ",".join("?" * len(SUCCESS_STATUSES))
    rows = conn.execute(
        f"""SELECT contact_id, step_key, MAX(created_at) AS at
            FROM deliveries
            WHERE campaign_key=? AND status IN ({placeholders})
            GROUP BY contact_id, step_key""",
        (campaign_key, *SUCCESS_STATUSES),
    )
    for row in rows:
        out.setdefault(int(row["contact_id"]), {})[row["step_key"]] = row["at"]
    return out


def _last_touches(conn: sqlite3.Connection) -> dict[int, str]:
    placeholders = ",".join("?" * len(SUCCESS_STATUSES))
    rows = conn.execute(
        f"""SELECT contact_id, MAX(created_at) AS at FROM deliveries
            WHERE status IN ({placeholders}) GROUP BY contact_id""",
        SUCCESS_STATUSES,
    )
    return {int(r["contact_id"]): r["at"] for r in rows}


def _form_submits_today(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    since = (now - timedelta(days=1)).isoformat(timespec="seconds")
    counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT target FROM deliveries WHERE channel='form' AND status='submitted' AND created_at>=?",
        (since,),
    )
    for row in rows:
        host = host_of(row["target"])
        counts[host] = counts.get(host, 0) + 1
    return counts


def _matches_segment(row: sqlite3.Row, campaign: Campaign, settings: Settings) -> str | None:
    """除外理由を返す。通過したら None。"""
    seg = campaign.segment
    if seg.ranks and (row["rank"] or "") not in seg.ranks:
        return "セグメント対象外(ランク)"
    if seg.contact_types and (row["contact_type"] or "") not in seg.contact_types:
        return "セグメント対象外(接点種別)"
    domain = (row["domain"] or "").lower()
    if seg.include_domains and not any(domain.endswith(d) for d in seg.include_domains):
        return "セグメント対象外(ドメイン指定)"
    if any(domain.endswith(d) for d in seg.exclude_domains):
        return "セグメント除外ドメイン"
    if seg.company_name_like and seg.company_name_like not in (row["company_name"] or ""):
        return "セグメント対象外(社名条件)"
    if (seg.exclude_freemail or settings.exclude_freemail) and row["flag_freemail"]:
        return "フリーメール除外"
    if (seg.exclude_domain_mismatch or settings.exclude_domain_mismatch) and row["flag_domain_mismatch"]:
        return "ドメイン不一致除外"
    return None


def _suppressed(row: sqlite3.Row, channel: str, target: str, sup: dict[str, set[str]]) -> bool:
    if (row["company_name"] or "").lower() in sup.get("company", set()):
        return True
    domain = (row["domain"] or "").lower()
    if domain and domain in sup.get("domain", set()):
        return True
    if channel == "email":
        return (row["contact_email"] or "").lower() in sup.get("email", set())
    host = host_of(target)
    if host and host in sup.get("domain", set()):
        return True
    return target.lower().rstrip("/") in {v.rstrip("/") for v in sup.get("form_url", set())}


def _next_step(campaign: Campaign, done: dict[str, str], now: datetime) -> tuple[Step | None, str | None]:
    """次に送るべき step と、送れない場合の理由。"""
    previous_at: str | None = None
    for step in campaign.steps:
        if step.key in done:
            previous_at = done[step.key]
            continue
        if previous_at is None and done:
            # 途中の step だけ成功している場合も、直近の成功を基準にする
            previous_at = max(done.values())
        if previous_at is not None and _days_since(previous_at, now) < step.delay_days:
            return None, "次コンテンツの待機期間中"
        return step, None

    if not campaign.repeat_cycle:
        return None, "全コンテンツ配信済み"
    last = max(done.values()) if done else None
    if last and _days_since(last, now) < campaign.cycle_gap_days:
        return None, "サイクル待機中"
    return campaign.steps[0], None


def select(
    conn: sqlite3.Connection,
    campaign: Campaign,
    channel: str,
    settings: Settings,
    *,
    limit: int | None = None,
    now: datetime | None = None,
    only_contact_ids: list[int] | None = None,
) -> SelectionReport:
    if channel not in campaign.channels:
        raise ValueError(f"キャンペーン {campaign.key} はチャネル {channel} に対応していません")

    now = now or datetime.now(timezone.utc)
    target_column = "contact_email" if channel == "email" else "contact_form_url"
    ok_column = "email_ok" if channel == "email" else "form_ok"

    sql = f"SELECT * FROM contacts WHERE status='active' AND {ok_column}=1 AND {target_column} != ''"
    params: list[Any] = []
    if only_contact_ids:
        sql += f" AND id IN ({','.join('?' * len(only_contact_ids))})"
        params += only_contact_ids
    rows = conn.execute(sql, params).fetchall()

    suppressions = load_suppressions(conn)
    history = _history(conn, campaign.key)
    last_touch = _last_touches(conn)
    form_today = _form_submits_today(conn, now) if channel == "form" else {}

    # 全体の最短接触間隔は「下限」であり、キャンペーン側の設定で下回ることはできない。
    # キャンペーンはこれより長い間隔を要求できるが、短くはできない（過剰接触の安全弁）。
    min_interval = settings.global_min_interval_days
    if campaign.limits.min_interval_days_between_touches is not None:
        min_interval = max(min_interval, campaign.limits.min_interval_days_between_touches)
    per_domain_cap = (
        campaign.limits.max_per_domain_per_run
        if campaign.limits.max_per_domain_per_run is not None
        else (settings.email_limits.max_per_domain_per_run if channel == "email" else 1)
    )
    run_cap = limit or campaign.limits.max_per_run or (
        settings.email_limits.max_per_run if channel == "email" else settings.form_limits.max_per_run
    )

    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    candidates: list[tuple[int, float, int, sqlite3.Row, Step]] = []
    for row in rows:
        target = (row[target_column] or "").strip()
        if not target:
            skip("宛先なし")
            continue
        reason = _matches_segment(row, campaign, settings)
        if reason:
            skip(reason)
            continue
        if _suppressed(row, channel, target, suppressions):
            skip("配信停止・除外リスト")
            continue

        cid = int(row["id"])
        since_touch = _days_since(last_touch.get(cid), now)
        done = history.get(cid, {})
        step, why = _next_step(campaign, done, now)
        if step is None:
            skip(why or "対象外")
            continue
        # 既に接触済みの相手には最短間隔をあける（初回接触は待たない）
        if done and since_touch < min_interval:
            skip("最短接触間隔の待機中")
            continue
        if channel == "form" and form_today.get(host_of(target), 0) >= settings.form_limits.max_per_domain_per_day:
            skip("同一サイトへの1日上限")
            continue

        rank_order = 0 if (row["rank"] or "") == "A" else 1
        # 未接触(inf)を先に、次に最後の接触が古い順
        recency = -since_touch
        candidates.append((rank_order, recency, cid, row, step))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))

    plans: list[Plan] = []
    domain_used: dict[str, int] = {}
    for _, _, _, row, step in candidates:
        if len(plans) >= run_cap:
            skip("今回の上限に到達（次回に繰越）")
            continue
        domain = (row["domain"] or host_of(row[target_column] or "")).lower()
        if domain and domain_used.get(domain, 0) >= per_domain_cap:
            skip("同一ドメインの1回あたり上限")
            continue
        domain_used[domain] = domain_used.get(domain, 0) + 1
        plans.append(Plan(contact=row, step=step, channel=channel, target=str(row[target_column]).strip()))

    return SelectionReport(plans=plans, skipped=skipped)
