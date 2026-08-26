"""キャンペーン定義（YAML）の読み込みと検証。

1つのキャンペーンは「順番に届ける複数のコンテンツ（steps）」で構成される。
定期実行のたびに、各宛先が次に受け取るべき step を1つだけ進める。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VALID_CHANNELS = {"email", "form"}


class CampaignError(ValueError):
    pass


@dataclass
class Step:
    key: str
    delay_days: int = 0                 # 直前の step からの最短待機日数
    subject: str = ""                   # メール件名（Jinja2 可）
    body_text: str = ""                 # テンプレートファイル名 (templates/ からの相対)
    body_html: str = ""
    form_subject: str = ""              # フォームの件名欄
    form_body: str = ""                 # フォーム本文テンプレート


@dataclass
class Segment:
    ranks: list[str] = field(default_factory=lambda: ["A", "B"])
    include_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    exclude_freemail: bool = False
    exclude_domain_mismatch: bool = False
    contact_types: list[str] = field(default_factory=list)
    company_name_like: str = ""


@dataclass
class Limits:
    max_per_run: int | None = None
    max_per_domain_per_run: int | None = None
    min_interval_days_between_touches: int | None = None


@dataclass
class Campaign:
    key: str
    name: str
    channels: list[str]
    steps: list[Step]
    segment: Segment = field(default_factory=Segment)
    limits: Limits = field(default_factory=Limits)
    enabled: bool = True
    repeat_cycle: bool = False          # 最終 step の後、先頭に戻すか
    cycle_gap_days: int = 180
    path: Path | None = None

    def step_by_key(self, key: str) -> Step | None:
        return next((s for s in self.steps if s.key == key), None)

    def step_index(self, key: str) -> int:
        for i, step in enumerate(self.steps):
            if step.key == key:
                return i
        return -1


def _parse_step(raw: dict[str, Any], index: int) -> Step:
    key = str(raw.get("key") or f"step{index + 1}")
    return Step(
        key=key,
        delay_days=int(raw.get("delay_days", 0)),
        subject=str(raw.get("subject", "")),
        body_text=str(raw.get("body_text", "")),
        body_html=str(raw.get("body_html", "")),
        form_subject=str(raw.get("form_subject", "")),
        form_body=str(raw.get("form_body", "")),
    )


def load_campaign(path: Path) -> Campaign:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    key = str(raw.get("key") or path.stem)
    channels = [str(c).strip() for c in (raw.get("channels") or ["email"])]
    bad = set(channels) - VALID_CHANNELS
    if bad:
        raise CampaignError(f"{path.name}: 未知のチャネル {sorted(bad)}")

    steps_raw = raw.get("steps") or []
    if not steps_raw:
        raise CampaignError(f"{path.name}: steps が空です")
    steps = [_parse_step(s, i) for i, s in enumerate(steps_raw)]
    if len({s.key for s in steps}) != len(steps):
        raise CampaignError(f"{path.name}: step の key が重複しています")

    for step in steps:
        if "email" in channels and not step.body_text:
            raise CampaignError(f"{path.name}/{step.key}: email チャネルには body_text が必要です")
        if "email" in channels and not step.subject:
            raise CampaignError(f"{path.name}/{step.key}: email チャネルには subject が必要です")
        if "form" in channels and not step.form_body:
            raise CampaignError(f"{path.name}/{step.key}: form チャネルには form_body が必要です")

    seg_raw = raw.get("segment") or {}
    lim_raw = raw.get("limits") or {}

    return Campaign(
        key=key,
        name=str(raw.get("name") or key),
        channels=channels,
        steps=steps,
        enabled=bool(raw.get("enabled", True)),
        repeat_cycle=bool(raw.get("repeat_cycle", False)),
        cycle_gap_days=int(raw.get("cycle_gap_days", 180)),
        segment=Segment(
            ranks=[str(r) for r in (seg_raw.get("ranks") or ["A", "B"])],
            include_domains=[str(d).lower() for d in (seg_raw.get("include_domains") or [])],
            exclude_domains=[str(d).lower() for d in (seg_raw.get("exclude_domains") or [])],
            exclude_freemail=bool(seg_raw.get("exclude_freemail", False)),
            exclude_domain_mismatch=bool(seg_raw.get("exclude_domain_mismatch", False)),
            contact_types=[str(t) for t in (seg_raw.get("contact_types") or [])],
            company_name_like=str(seg_raw.get("company_name_like", "")),
        ),
        limits=Limits(
            max_per_run=lim_raw.get("max_per_run"),
            max_per_domain_per_run=lim_raw.get("max_per_domain_per_run"),
            min_interval_days_between_touches=lim_raw.get("min_interval_days_between_touches"),
        ),
        path=path,
    )


def load_campaigns(directory: Path) -> dict[str, Campaign]:
    campaigns: dict[str, Campaign] = {}
    for path in sorted(directory.glob("*.y*ml")):
        campaign = load_campaign(path)
        if campaign.key in campaigns:
            raise CampaignError(f"キャンペーンキーの重複: {campaign.key}")
        campaigns[campaign.key] = campaign
    return campaigns


def get_campaign(directory: Path, key: str) -> Campaign:
    campaigns = load_campaigns(directory)
    if key not in campaigns:
        known = ", ".join(sorted(campaigns)) or "(なし)"
        raise CampaignError(f"キャンペーン '{key}' が見つかりません。定義済み: {known}")
    return campaigns[key]
