"""マスターCSVを SQLite に取り込む。"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from .db import upsert_contacts
from .normalize import normalize_row


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


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

    usable_email = sum(1 for r in normalized.values() if r["email_ok"])
    usable_form = sum(1 for r in normalized.values() if r["form_ok"])
    return {
        "csv_rows": len(raw_rows),
        "contacts": len(normalized),
        "email_targets": usable_email,
        "form_targets": usable_form,
        "stats": dict(stats),
    }
