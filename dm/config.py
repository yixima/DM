"""設定の読み込み。

優先順位: 環境変数 > config/settings.yaml > 既定値
.env ファイルがあれば（python-dotenv なしで）簡易的に読み込む。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"


def load_dotenv(path: Path | None = None) -> None:
    """.env を環境変数へ流し込む（既存の環境変数は上書きしない）。"""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Sender:
    """特定電子メール法で表示が必要な送信者情報。"""

    name: str = ""
    person: str = ""
    email: str = ""
    reply_to: str = ""
    phone: str = ""
    address: str = ""
    url: str = ""

    def missing(self) -> list[str]:
        required = {
            "name": "送信者（会社）名",
            "email": "送信元メールアドレス",
            "address": "住所",
            "url": "ウェブサイトURL",
        }
        return [label for key, label in required.items() if not getattr(self, key)]


@dataclass
class FormProfile:
    """問い合わせフォームの各欄に入れる送信者側の情報。"""

    company: str = ""
    person_sei: str = ""
    person_mei: str = ""
    kana_sei: str = ""
    kana_mei: str = ""
    department: str = ""
    email: str = ""
    phone: str = ""
    fax: str = ""
    zip: str = ""
    prefecture: str = ""
    address: str = ""
    url: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.person_sei} {self.person_mei}".strip()

    @property
    def full_kana(self) -> str:
        return f"{self.kana_sei} {self.kana_mei}".strip()

    def missing(self) -> list[str]:
        required = {"company": "会社名", "person_sei": "担当者の姓",
                    "email": "メールアドレス", "phone": "電話番号"}
        return [label for key, label in required.items() if not getattr(self, key)]


@dataclass
class Unsubscribe:
    base_url: str = ""
    email: str = ""
    secret: str = ""

    def missing(self) -> list[str]:
        out = []
        if not self.base_url and not self.email:
            out.append("配信停止URLまたは配信停止用メールアドレス")
        if not self.secret:
            out.append("配信停止トークンの秘密鍵 (DM_UNSUBSCRIBE_SECRET)")
        return out


@dataclass
class Smtp:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    starttls: bool = True


@dataclass
class Imap:
    host: str = ""
    port: int = 993
    user: str = ""
    password: str = ""


@dataclass
class EmailLimits:
    max_per_run: int = 200
    max_per_day: int = 1000
    max_per_hour: int = 120
    min_seconds_between_sends: float = 2.0
    jitter_seconds: float = 1.5
    max_per_domain_per_run: int = 1


@dataclass
class FormLimits:
    max_per_run: int = 60
    min_seconds_between_submits: float = 8.0
    jitter_seconds: float = 4.0
    max_per_domain_per_day: int = 1
    page_timeout_ms: int = 30000
    respect_robots: bool = True
    user_agent: str = ""


@dataclass
class Settings:
    contacts_csv: Path = ROOT / "data" / "master_contacts_20260825_181226.csv"
    db_path: Path = ROOT / "state" / "dm.sqlite3"
    outbox_dir: Path = ROOT / "state" / "outbox"
    evidence_dir: Path = ROOT / "state" / "evidence"
    log_dir: Path = ROOT / "state" / "logs"
    campaign_dir: Path = ROOT / "config" / "campaigns"
    template_dir: Path = ROOT / "templates"
    transport: str = "console"          # console | file | smtp
    global_min_interval_days: int = 14  # 同一相手への最短接触間隔（全チャネル横断）
    quiet_hours: tuple[int, int] = (21, 8)  # この時間帯は送らない (JST, 21時〜翌8時)
    timezone: str = "Asia/Tokyo"
    exclude_freemail: bool = False
    exclude_domain_mismatch: bool = False
    sender: Sender = field(default_factory=Sender)
    form_profile: FormProfile = field(default_factory=FormProfile)
    unsubscribe: Unsubscribe = field(default_factory=Unsubscribe)
    smtp: Smtp = field(default_factory=Smtp)
    imap: Imap = field(default_factory=Imap)
    email_limits: EmailLimits = field(default_factory=EmailLimits)
    form_limits: FormLimits = field(default_factory=FormLimits)

    def ensure_dirs(self) -> None:
        for path in (self.db_path.parent, self.outbox_dir, self.evidence_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


def _path(value: Any, default: Path) -> Path:
    if not value:
        return default
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (ROOT / path)


def load_settings(settings_path: Path | None = None) -> Settings:
    load_dotenv()
    path = settings_path or DEFAULT_SETTINGS
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    paths = raw.get("paths") or {}
    limits = raw.get("email_limits") or {}
    form = raw.get("form_limits") or {}
    sending = raw.get("sending") or {}
    quality = raw.get("quality") or {}
    sender_raw = raw.get("sender") or {}
    profile_raw = raw.get("form_profile") or {}
    unsub_raw = raw.get("unsubscribe") or {}

    quiet = sending.get("quiet_hours") or [21, 8]

    settings = Settings(
        contacts_csv=_path(paths.get("contacts_csv"), Settings.contacts_csv),
        db_path=_path(paths.get("db"), Settings.db_path),
        outbox_dir=_path(paths.get("outbox"), Settings.outbox_dir),
        evidence_dir=_path(paths.get("evidence"), Settings.evidence_dir),
        log_dir=_path(paths.get("logs"), Settings.log_dir),
        campaign_dir=_path(paths.get("campaigns"), Settings.campaign_dir),
        template_dir=_path(paths.get("templates"), Settings.template_dir),
        transport=_env("DM_TRANSPORT", str(sending.get("transport", "console"))),
        global_min_interval_days=int(sending.get("global_min_interval_days", 14)),
        quiet_hours=(int(quiet[0]), int(quiet[1])),
        timezone=str(sending.get("timezone", "Asia/Tokyo")),
        exclude_freemail=_as_bool(quality.get("exclude_freemail"), False),
        exclude_domain_mismatch=_as_bool(quality.get("exclude_domain_mismatch"), False),
        sender=Sender(
            name=_env("DM_SENDER_NAME", str(sender_raw.get("name", ""))),
            person=_env("DM_SENDER_PERSON", str(sender_raw.get("person", ""))),
            email=_env("DM_SENDER_EMAIL", str(sender_raw.get("email", ""))),
            reply_to=_env("DM_SENDER_REPLY_TO", str(sender_raw.get("reply_to", ""))),
            phone=_env("DM_SENDER_PHONE", str(sender_raw.get("phone", ""))),
            address=_env("DM_SENDER_ADDRESS", str(sender_raw.get("address", ""))),
            url=_env("DM_SENDER_URL", str(sender_raw.get("url", ""))),
        ),
        form_profile=FormProfile(
            company=_env("DM_FORM_COMPANY", str(profile_raw.get("company", ""))),
            person_sei=_env("DM_FORM_PERSON_SEI", str(profile_raw.get("person_sei", ""))),
            person_mei=_env("DM_FORM_PERSON_MEI", str(profile_raw.get("person_mei", ""))),
            kana_sei=_env("DM_FORM_KANA_SEI", str(profile_raw.get("kana_sei", ""))),
            kana_mei=_env("DM_FORM_KANA_MEI", str(profile_raw.get("kana_mei", ""))),
            department=_env("DM_FORM_DEPARTMENT", str(profile_raw.get("department", ""))),
            email=_env("DM_FORM_EMAIL", str(profile_raw.get("email", ""))),
            phone=_env("DM_FORM_PHONE", str(profile_raw.get("phone", ""))),
            fax=_env("DM_FORM_FAX", str(profile_raw.get("fax", ""))),
            zip=_env("DM_FORM_ZIP", str(profile_raw.get("zip", ""))),
            prefecture=_env("DM_FORM_PREFECTURE", str(profile_raw.get("prefecture", ""))),
            address=_env("DM_FORM_ADDRESS", str(profile_raw.get("address", ""))),
            url=_env("DM_FORM_URL", str(profile_raw.get("url", ""))),
        ),
        unsubscribe=Unsubscribe(
            base_url=_env("DM_UNSUBSCRIBE_BASE_URL", str(unsub_raw.get("base_url", ""))),
            email=_env("DM_UNSUBSCRIBE_EMAIL", str(unsub_raw.get("email", ""))),
            secret=_env("DM_UNSUBSCRIBE_SECRET", str(unsub_raw.get("secret", ""))),
        ),
        smtp=Smtp(
            host=_env("DM_SMTP_HOST"),
            port=int(_env("DM_SMTP_PORT", "587") or 587),
            user=_env("DM_SMTP_USER"),
            password=os.environ.get("DM_SMTP_PASSWORD", ""),
            starttls=_as_bool(_env("DM_SMTP_STARTTLS", "true"), True),
        ),
        imap=Imap(
            host=_env("DM_IMAP_HOST"),
            port=int(_env("DM_IMAP_PORT", "993") or 993),
            user=_env("DM_IMAP_USER"),
            password=os.environ.get("DM_IMAP_PASSWORD", ""),
        ),
        email_limits=EmailLimits(
            max_per_run=int(limits.get("max_per_run", 200)),
            max_per_day=int(limits.get("max_per_day", 1000)),
            max_per_hour=int(limits.get("max_per_hour", 120)),
            min_seconds_between_sends=float(limits.get("min_seconds_between_sends", 2.0)),
            jitter_seconds=float(limits.get("jitter_seconds", 1.5)),
            max_per_domain_per_run=int(limits.get("max_per_domain_per_run", 1)),
        ),
        form_limits=FormLimits(
            max_per_run=int(form.get("max_per_run", 60)),
            min_seconds_between_submits=float(form.get("min_seconds_between_submits", 8.0)),
            jitter_seconds=float(form.get("jitter_seconds", 4.0)),
            max_per_domain_per_day=int(form.get("max_per_domain_per_day", 1)),
            page_timeout_ms=int(form.get("page_timeout_ms", 30000)),
            respect_robots=_as_bool(form.get("respect_robots"), True),
            user_agent=str(form.get("user_agent", "")),
        ),
    )
    # フォーム用プロフィールが未設定の項目は送信者情報で補う
    profile = settings.form_profile
    for attr, fallback in (
        ("company", settings.sender.name),
        ("email", settings.sender.email),
        ("phone", settings.sender.phone),
        ("address", settings.sender.address),
        ("url", settings.sender.url),
    ):
        if not getattr(profile, attr):
            setattr(profile, attr, fallback)
    if not profile.person_sei and settings.sender.person:
        parts = settings.sender.person.replace("　", " ").split()
        if len(parts) >= 2:
            profile.person_sei, profile.person_mei = parts[-2], parts[-1]
        elif parts:
            profile.person_sei = parts[0]
    return settings
