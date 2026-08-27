"""進捗の可視化と、人が対応すべき残件の書き出し。"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .campaign import Campaign
from .db import sent_today
from .formrunner import REVIEW_STATUSES
from .selector import SUCCESS_STATUSES


def contact_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(email_ok) AS email_ok,
                  SUM(form_ok) AS form_ok,
                  SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
                  SUM(flag_freemail) AS freemail,
                  SUM(flag_domain_mismatch) AS mismatch
           FROM contacts"""
    ).fetchone()
    ranks = {r["rank"]: r["n"] for r in conn.execute("SELECT rank, COUNT(*) n FROM contacts GROUP BY rank")}
    suppressed = conn.execute("SELECT COUNT(*) n FROM suppressions").fetchone()["n"]
    return {
        "総件数": row["total"] or 0,
        "有効(active)": row["active"] or 0,
        "メール送信可": row["email_ok"] or 0,
        "フォーム送信可": row["form_ok"] or 0,
        "フリーメール": row["freemail"] or 0,
        "ドメイン不一致": row["mismatch"] or 0,
        "ランク別": ranks,
        "配信停止・除外": suppressed,
    }


def campaign_progress(conn: sqlite3.Connection, campaign: Campaign) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in campaign.steps:
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN status IN ('sent','submitted') THEN 1 ELSE 0 END) AS ok,
                 SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                 SUM(CASE WHEN status LIKE 'skipped%' OR status='needs_review' THEN 1 ELSE 0 END) AS review,
                 SUM(CASE WHEN status='dryrun' THEN 1 ELSE 0 END) AS dryrun
               FROM deliveries WHERE campaign_key=? AND step_key=?""",
            (campaign.key, step.key),
        ).fetchone()
        out.append({
            "step": step.key,
            "送信済": row["ok"] or 0,
            "失敗": row["failed"] or 0,
            "要確認": row["review"] or 0,
            "dry-run": row["dryrun"] or 0,
        })
    return out


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def channel_totals(conn: sqlite3.Connection, days: int = 30) -> dict[str, dict[str, int]]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    out: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        """SELECT channel, status, COUNT(*) n FROM deliveries
           WHERE created_at>=? GROUP BY channel, status""",
        (since,),
    ):
        out.setdefault(row["channel"], {})[row["status"]] = row["n"]
    return out


def export_needs_review(conn: sqlite3.Connection, path: Path, campaign_key: str | None = None) -> int:
    """自動送信できなかった先を、人が手で対応するためのCSVに書き出す。"""
    sql = """
        SELECT d.created_at, c.company_name, c.domain, d.target, d.status, d.error, d.evidence,
               d.campaign_key, d.step_key
        FROM deliveries d JOIN contacts c ON c.id = d.contact_id
        WHERE d.status IN ({})
    """.format(",".join("?" * len(REVIEW_STATUSES)))
    params: list[Any] = list(REVIEW_STATUSES)
    if campaign_key:
        sql += " AND d.campaign_key=?"
        params.append(campaign_key)
    sql += " ORDER BY d.created_at DESC"

    rows = conn.execute(sql, params).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["日時", "会社名", "ドメイン", "フォームURL", "結果", "詳細", "証跡", "キャンペーン", "ステップ"])
        for row in rows:
            writer.writerow([
                row["created_at"], row["company_name"], row["domain"], row["target"],
                row["status"], row["error"] or "", row["evidence"] or "",
                row["campaign_key"], row["step_key"],
            ])
    return len(rows)


def export_deliveries(conn: sqlite3.Connection, path: Path, campaign_key: str | None = None) -> int:
    sql = """
        SELECT d.created_at, c.company_name, d.channel, d.target, d.campaign_key, d.step_key,
               d.status, d.subject, d.error
        FROM deliveries d JOIN contacts c ON c.id = d.contact_id
    """
    params: list[Any] = []
    if campaign_key:
        sql += " WHERE d.campaign_key=?"
        params.append(campaign_key)
    sql += " ORDER BY d.created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["日時", "会社名", "チャネル", "宛先", "キャンペーン", "ステップ", "状態", "件名", "エラー"])
        for row in rows:
            writer.writerow([row[key] for key in row.keys()])
    return len(rows)


def format_stats(stats: dict[str, Any]) -> str:
    lines = []
    for key, value in stats.items():
        if isinstance(value, dict):
            inner = ", ".join(f"{k or '(なし)'}={v}" for k, v in sorted(value.items(), key=lambda kv: str(kv[0])))
            lines.append(f"  {key}: {inner}")
        else:
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)


__all__ = [
    "contact_stats", "campaign_progress", "recent_runs", "channel_totals", "sent_today",
    "export_needs_review", "export_deliveries", "format_stats", "SUCCESS_STATUSES",
]
