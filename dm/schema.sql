PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS contacts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key           TEXT NOT NULL UNIQUE,
    company_name         TEXT NOT NULL,
    official_url         TEXT,
    domain               TEXT,
    contact_email        TEXT,
    contact_form_url     TEXT,
    contact_type         TEXT,
    rank                 TEXT,
    sources              TEXT,
    evidence_url         TEXT,
    flag_freemail        INTEGER NOT NULL DEFAULT 0,
    flag_domain_mismatch INTEGER NOT NULL DEFAULT 0,
    email_ok             INTEGER NOT NULL DEFAULT 0,
    form_ok              INTEGER NOT NULL DEFAULT 0,
    quality_notes        TEXT,
    status               TEXT NOT NULL DEFAULT 'active',  -- active | paused | excluded
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_domain ON contacts(domain);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(contact_email);
CREATE INDEX IF NOT EXISTS idx_contacts_rank ON contacts(rank);

-- 配信停止 / 送信禁止リスト。kind: email | domain | company | form_url
CREATE TABLE IF NOT EXISTS suppressions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    source     TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(kind, value)
);

-- キャンペーンの1回の実行
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_key TEXT NOT NULL,
    channel      TEXT NOT NULL,               -- email | form
    mode         TEXT NOT NULL,               -- dry-run | live
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    planned      INTEGER NOT NULL DEFAULT 0,
    sent         INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    notes        TEXT
);

-- 1件ごとの送信記録。定期実行の重複防止はこのテーブルが真実の源。
CREATE TABLE IF NOT EXISTS deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES runs(id),
    contact_id   INTEGER NOT NULL REFERENCES contacts(id),
    campaign_key TEXT NOT NULL,
    step_key     TEXT NOT NULL,
    channel      TEXT NOT NULL,
    target       TEXT NOT NULL,               -- 宛先メール or フォームURL
    status       TEXT NOT NULL,               -- sent | dryrun | failed | skipped_* | bounced
    subject      TEXT,
    body_hash    TEXT,
    error        TEXT,
    evidence     TEXT,                        -- スクリーンショット等のパス
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_contact ON deliveries(contact_id, channel, created_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_campaign ON deliveries(campaign_key, step_key, status);
CREATE INDEX IF NOT EXISTS idx_deliveries_target ON deliveries(target);

-- 反応・イベント（配信停止申出、バウンス、返信など）
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id   INTEGER REFERENCES contacts(id),
    campaign_key TEXT,
    type         TEXT NOT NULL,               -- unsubscribe | bounce_hard | bounce_soft | reply | complaint
    detail       TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, created_at);
