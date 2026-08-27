"""マスターCSVを SQLite に取り込む。"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from .db import upsert_contacts, utcnow
from .normalize import normalize_row


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


def import_contacts(conn, csv_path: Path) -> dict[str, Any]:
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

    inserted, updated = upsert_contacts(conn, normalized.values())
    stats["新規登録"] = inserted
    stats["既存更新"] = updated

    missing = find_missing(conn, set(normalized))

    usable_email = sum(1 for r in normalized.values() if r["email_ok"])
    usable_form = sum(1 for r in normalized.values() if r["form_ok"])
    return {
        "csv_rows": len(raw_rows),
        "contacts": len(normalized),
        "email_targets": usable_email,
        "form_targets": usable_form,
        "stats": dict(stats),
        "missing": missing,
    }
