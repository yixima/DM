"""SQLite ラッパー。状態（誰に・いつ・何を送ったか）はすべてここに残す。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def start_run(conn: sqlite3.Connection, campaign_key: str, channel: str, mode: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (campaign_key, channel, mode, started_at) VALUES (?,?,?,?)",
        (campaign_key, channel, mode, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, **counts: Any) -> None:
    conn.execute(
        """UPDATE runs SET finished_at=?, planned=?, sent=?, failed=?, skipped=?, notes=?
           WHERE id=?""",
        (
            utcnow(),
            counts.get("planned", 0),
            counts.get("sent", 0),
            counts.get("failed", 0),
            counts.get("skipped", 0),
            counts.get("notes"),
            run_id,
        ),
    )
    conn.commit()


def record_delivery(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    contact_id: int,
    campaign_key: str,
    step_key: str,
    channel: str,
    target: str,
    status: str,
    subject: str | None = None,
    body_hash: str | None = None,
    error: str | None = None,
    evidence: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO deliveries
           (run_id, contact_id, campaign_key, step_key, channel, target, status,
            subject, body_hash, error, evidence, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, contact_id, campaign_key, step_key, channel, target, status,
         subject, body_hash, error, evidence, utcnow()),
    )
    conn.commit()


def add_event(
    conn: sqlite3.Connection,
    *,
    contact_id: int | None,
    type: str,
    detail: str | None = None,
    campaign_key: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (contact_id, campaign_key, type, detail, created_at) VALUES (?,?,?,?,?)",
        (contact_id, campaign_key, type, detail, utcnow()),
    )
    conn.commit()


def add_suppression(
    conn: sqlite3.Connection, kind: str, value: str, reason: str, source: str = "manual"
) -> bool:
    """戻り値: 新規に追加されたら True。"""
    value = (value or "").strip().lower()
    if not value:
        return False
    cur = conn.execute(
        """INSERT OR IGNORE INTO suppressions (kind, value, reason, source, created_at)
           VALUES (?,?,?,?,?)""",
        (kind, value, reason, source, utcnow()),
    )
    conn.commit()
    return cur.rowcount > 0


def load_suppressions(conn: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"email": set(), "domain": set(), "company": set(), "form_url": set()}
    for row in conn.execute("SELECT kind, value FROM suppressions"):
        out.setdefault(row["kind"], set()).add(row["value"])
    return out


def upsert_contacts(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """戻り値: (新規件数, 更新件数)。dedupe_key で同一性を判定する。"""
    inserted = updated = 0
    now = utcnow()
    for row in rows:
        existing = conn.execute(
            "SELECT id FROM contacts WHERE dedupe_key=?", (row["dedupe_key"],)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE contacts SET company_name=?, official_url=?, domain=?, contact_email=?,
                       contact_form_url=?, contact_type=?, rank=?, sources=?, evidence_url=?,
                       flag_freemail=?, flag_domain_mismatch=?, email_ok=?, form_ok=?,
                       quality_notes=?, updated_at=?
                   WHERE id=?""",
                (
                    row["company_name"], row["official_url"], row["domain"], row["contact_email"],
                    row["contact_form_url"], row["contact_type"], row["rank"], row["sources"],
                    row["evidence_url"], row["flag_freemail"], row["flag_domain_mismatch"],
                    row["email_ok"], row["form_ok"], row["quality_notes"], now, existing["id"],
                ),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO contacts
                   (dedupe_key, company_name, official_url, domain, contact_email, contact_form_url,
                    contact_type, rank, sources, evidence_url, flag_freemail, flag_domain_mismatch,
                    email_ok, form_ok, quality_notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["dedupe_key"], row["company_name"], row["official_url"], row["domain"],
                    row["contact_email"], row["contact_form_url"], row["contact_type"], row["rank"],
                    row["sources"], row["evidence_url"], row["flag_freemail"],
                    row["flag_domain_mismatch"], row["email_ok"], row["form_ok"],
                    row["quality_notes"], now, now,
                ),
            )
            inserted += 1
    conn.commit()
    return inserted, updated
