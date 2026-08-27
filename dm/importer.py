"""マスターCSVを SQLite に取り込む。"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import upsert_contacts, utcnow
from .normalize import normalize_row


class ImportRefused(RuntimeError):
    """取り込むと危険な状態。黙って進めず止める。"""


def find_latest_csv(directory: Path, pattern: str) -> Path:
    """監視フォルダの中で最も新しいCSVを選ぶ。

    別セッションが書き出したファイルを自動で拾うための入口。
    更新時刻の新しい順、同着ならファイル名の大きい順（名前に日時が入る前提）。
    """
    if not directory.exists():
        raise ImportRefused(f"監視フォルダが見つかりません: {directory}")
    candidates = [p for p in directory.glob(pattern) if p.is_file()]
    if not candidates:
        raise ImportRefused(f"{directory} に {pattern} に一致するファイルがありません")
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return candidates[0]


def last_import(conn) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def check_shrink(conn, email_targets: int, form_targets: int, max_shrink_percent: float) -> str | None:
    """前回より宛先が大きく減っていないか。減っていれば理由の文字列を返す。

    別セッションでの書き出しが途中で失敗すると、小さなCSVができる。
    それを黙って取り込むと、リストが大量に消えたまま配信が続いてしまう。
    """
    previous = last_import(conn)
    if not previous or max_shrink_percent <= 0:
        return None

    for label, before, after in (
        ("メール送信可", previous["email_targets"], email_targets),
        ("フォーム送信可", previous["form_targets"], form_targets),
    ):
        if before <= 0:
            continue
        drop = (before - after) / before * 100
        if drop > max_shrink_percent:
            return (
                f"{label}の宛先が {before} → {after} と {drop:.1f}% 減っています"
                f"（許容 {max_shrink_percent:.0f}%）。"
                " 書き出しが途中で失敗した可能性があります。"
            )
    return None


def record_import(conn, source: Path, summary: dict[str, Any]) -> None:
    mtime = datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO imports
           (source_path, source_mtime, csv_rows, contacts, email_targets, form_targets,
            inserted, updated, missing, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            str(source), mtime, summary["csv_rows"], summary["contacts"],
            summary["email_targets"], summary["form_targets"],
            summary["stats"].get("新規登録", 0), summary["stats"].get("既存更新", 0),
            len(summary["missing"]),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def find_missing(conn, seen_keys: set[str]) -> list[dict[str, Any]]:
    """今回のCSVに現れなかった、DB上の宛先。

    リストは別途更新され続けるため、取り込みのたびに「消えた宛先」が生じる。
    黙って消すと送信履歴まで失われ、あとで同じ相手に送り直してしまう。
    削除はせず、見えるようにするだけにする。
    """
    rows = conn.execute(
        "SELECT id, dedupe_key, company_name, contact_email, status FROM contacts"
    ).fetchall()
    return [
        {"id": r["id"], "dedupe_key": r["dedupe_key"], "company_name": r["company_name"],
         "contact_email": r["contact_email"], "status": r["status"]}
        for r in rows if r["dedupe_key"] not in seen_keys
    ]


def deactivate(conn, contact_ids: list[int]) -> int:
    """宛先を送信対象から外す（削除はしない。履歴は残す）。"""
    if not contact_ids:
        return 0
    placeholders = ",".join("?" * len(contact_ids))
    cur = conn.execute(
        f"UPDATE contacts SET status='paused', updated_at=? WHERE id IN ({placeholders})",
        (utcnow(), *contact_ids),
    )
    conn.commit()
    return cur.rowcount


def import_contacts(
    conn,
    csv_path: Path,
    *,
    max_shrink_percent: float = 0.0,
    force: bool = False,
) -> dict[str, Any]:
    """CSVを取り込む。宛先が大きく減る場合は、書き込む前に中止する。"""
    raw_rows = read_csv(csv_path)
    normalized: dict[str, dict[str, Any]] = {}
    stats = Counter()

    for raw in raw_rows:
        row = normalize_row(raw)
        if not row["company_name"]:
            stats["社名なしで除外"] += 1
            continue
        if not row["email_ok"] and not row["form_ok"]:
            stats["メール・フォームとも使用不可で除外"] += 1
            continue
        key = row["dedupe_key"]
        if key in normalized:
            # 同一鍵の重複行はより情報量の多い方を残す
            prev = normalized[key]
            merged = dict(prev)
            for field in ("contact_email", "contact_form_url", "official_url", "evidence_url"):
                if not merged.get(field) and row.get(field):
                    merged[field] = row[field]
            merged["email_ok"] = max(prev["email_ok"], row["email_ok"])
            merged["form_ok"] = max(prev["form_ok"], row["form_ok"])
            merged["sources"] = ";".join(sorted(set(
                filter(None, (prev.get("sources", "") + ";" + row.get("sources", "")).split(";"))
            )))
            if prev.get("rank") == "B" and row.get("rank") == "A":
                merged["rank"] = "A"
            normalized[key] = merged
            stats["重複を統合"] += 1
            continue
        normalized[key] = row

    usable_email = sum(1 for r in normalized.values() if r["email_ok"])
    usable_form = sum(1 for r in normalized.values() if r["form_ok"])

    # ここまでは読み取りのみ。危険なら1件も書き込まずに止める。
    if not force:
        reason = check_shrink(conn, usable_email, usable_form, max_shrink_percent)
        if reason:
            raise ImportRefused(reason)

    inserted, updated = upsert_contacts(conn, normalized.values())
    stats["新規登録"] = inserted
    stats["既存更新"] = updated

    missing = find_missing(conn, set(normalized))
    summary = {
        "csv_rows": len(raw_rows),
        "contacts": len(normalized),
        "email_targets": usable_email,
        "form_targets": usable_form,
        "stats": dict(stats),
        "missing": missing,
    }
    record_import(conn, csv_path, summary)
    return summary
