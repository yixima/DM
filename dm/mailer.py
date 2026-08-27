"""メール配信の実行。selector が出した計画を1件ずつ処理する。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .campaign import Campaign
from .compliance import check_email_body, check_settings
from .config import Settings
from .db import finish_run, record_delivery, start_run
from .render import build_env, render_email, unsubscribe_mailto, unsubscribe_url
from .selector import Plan, SelectionReport, select, select_across
from .throttle import Pacer, in_quiet_hours
from .transports import TransportError, build_message, get_transport


class SendAborted(RuntimeError):
    """設定不備など、1件ずつではなく実行全体を止めるべき問題。"""


@dataclass
class SendResult:
    run_id: int | None
    planned: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    mode: str = "dry-run"
    skip_reasons: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # 複数シリーズをまとめて実行したときの、シリーズごとの内訳
    per_campaign: dict[str, int] = field(default_factory=dict)

    def merge(self, other: "SendResult") -> None:
        self.planned += other.planned
        self.sent += other.sent
        self.failed += other.failed
        self.skipped += other.skipped
        self.errors += other.errors
        for key, value in other.skip_reasons.items():
            self.skip_reasons[key] = self.skip_reasons.get(key, 0) + value
        for key, value in other.per_campaign.items():
            self.per_campaign[key] = self.per_campaign.get(key, 0) + value

    def summary(self) -> str:
        head = f"[{self.mode}] 計画 {self.planned} / 送信 {self.sent} / 失敗 {self.failed} / スキップ {self.skipped}"
        if len(self.per_campaign) > 1:
            head += "\n  シリーズ別: " + ", ".join(
                f"{k}={v}" for k, v in sorted(self.per_campaign.items(), key=lambda kv: -kv[1])
            )
        if self.skip_reasons:
            head += "\n  スキップ内訳: " + ", ".join(
                f"{k}={v}" for k, v in sorted(self.skip_reasons.items(), key=lambda kv: -kv[1])
            )
        return head


def _preflight(settings: Settings, dry_run: bool, ignore_quiet_hours: bool) -> None:
    setting_errors = check_settings(settings, "email")
    if setting_errors and not dry_run:
        raise SendAborted("送信できません:\n  - " + "\n  - ".join(setting_errors))
    if not dry_run and not ignore_quiet_hours and in_quiet_hours(settings.quiet_hours, settings.timezone):
        raise SendAborted(
            f"送信抑止時間帯です（{settings.quiet_hours[0]}時〜{settings.quiet_hours[1]}時, {settings.timezone}）。"
            " どうしても実行する場合は --ignore-quiet-hours を付けてください。"
        )


def _execute(
    conn: sqlite3.Connection,
    planned: list[tuple[Campaign, SelectionReport]],
    settings: Settings,
    *,
    dry_run: bool,
    transport_override: str | None,
) -> SendResult:
    mode = "dry-run" if dry_run else "live"
    total = SendResult(run_id=None, mode=mode)

    for _, report in planned:
        total.skipped += sum(report.skipped.values())
        for reason, count in report.skipped.items():
            total.skip_reasons[reason] = total.skip_reasons.get(reason, 0) + count
        total.planned += len(report.plans)

    if not any(report.plans for _, report in planned):
        return total

    env = build_env(settings)
    transport = get_transport(settings, transport_override if not dry_run else (transport_override or "console"))
    pacer = Pacer(
        settings.email_limits.min_seconds_between_sends,
        settings.email_limits.jitter_seconds,
        settings.email_limits.max_per_hour,
    )

    try:
        for campaign, report in planned:
            if not report.plans:
                continue
            run_id = start_run(conn, campaign.key, "email", mode)
            total.run_id = total.run_id or run_id
            per_run = SendResult(run_id=run_id, mode=mode, planned=len(report.plans))

            for plan in report.plans:
                _process_one(conn, plan, campaign, settings, env, transport, pacer, run_id, dry_run, per_run)

            finish_run(
                conn, run_id,
                planned=per_run.planned, sent=per_run.sent,
                failed=per_run.failed, skipped=per_run.skipped,
                notes="; ".join(per_run.errors[:5]) or None,
            )
            total.sent += per_run.sent
            total.failed += per_run.failed
            total.errors += per_run.errors
            total.skipped += per_run.skipped
            for reason, count in per_run.skip_reasons.items():
                total.skip_reasons[reason] = total.skip_reasons.get(reason, 0) + count
            total.per_campaign[campaign.key] = len(report.plans)
    finally:
        transport.close()

    return total


def run_email_campaign(
    conn: sqlite3.Connection,
    campaign: Campaign,
    settings: Settings,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    transport_override: str | None = None,
    ignore_quiet_hours: bool = False,
    now: datetime | None = None,
) -> SendResult:
    """1つのシリーズだけを配信する。"""
    if not campaign.enabled:
        raise SendAborted(f"キャンペーン {campaign.key} は enabled: false です")
    _preflight(settings, dry_run, ignore_quiet_hours)

    now = now or datetime.now(timezone.utc)
    report = select(conn, campaign, "email", settings, limit=limit, now=now)
    result = _execute(conn, [(campaign, report)], settings,
                      dry_run=dry_run, transport_override=transport_override)
    result.per_campaign.setdefault(campaign.key, len(report.plans))
    return result


def run_email_campaigns(
    conn: sqlite3.Connection,
    campaigns: list[Campaign],
    settings: Settings,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    transport_override: str | None = None,
    ignore_quiet_hours: bool = False,
    now: datetime | None = None,
) -> SendResult:
    """有効なシリーズをまとめて配信する。優先度の高い順に宛先を確保する。"""
    _preflight(settings, dry_run, ignore_quiet_hours)
    now = now or datetime.now(timezone.utc)
    planned = select_across(conn, campaigns, "email", settings, limit=limit, now=now)
    return _execute(conn, planned, settings, dry_run=dry_run, transport_override=transport_override)


def _process_one(
    conn: sqlite3.Connection,
    plan: Plan,
    campaign: Campaign,
    settings: Settings,
    env,
    transport,
    pacer: Pacer,
    run_id: int,
    dry_run: bool,
    result: SendResult,
) -> None:
    contact = plan.contact
    try:
        rendered = render_email(settings, env, contact, campaign, plan.step)
    except Exception as exc:  # テンプレート不備は記録して次へ
        result.failed += 1
        result.errors.append(f"{plan.company}: 描画失敗 {exc}")
        record_delivery(
            conn, run_id=run_id, contact_id=plan.contact_id, campaign_key=campaign.key,
            step_key=plan.step.key, channel="email", target=plan.target, status="failed",
            error=f"render: {exc}",
        )
        return

    check = check_email_body(settings, rendered.subject, rendered.text)
    if not check.ok:
        result.skipped += 1
        reason = "法令表示チェック不合格"
        result.skip_reasons[reason] = result.skip_reasons.get(reason, 0) + 1
        result.errors.append(f"{plan.company}: {check.errors[0]}")
        record_delivery(
            conn, run_id=run_id, contact_id=plan.contact_id, campaign_key=campaign.key,
            step_key=plan.step.key, channel="email", target=plan.target, status="skipped_compliance",
            subject=rendered.subject, error="; ".join(check.errors),
        )
        return

    msg = build_message(
        settings,
        to_address=plan.target,
        subject=rendered.subject,
        text=rendered.text,
        html=rendered.html,
        unsubscribe_url=unsubscribe_url(settings, plan.target),
        unsubscribe_mailto=unsubscribe_mailto(settings, plan.target),
    )

    if dry_run:
        record_delivery(
            conn, run_id=run_id, contact_id=plan.contact_id, campaign_key=campaign.key,
            step_key=plan.step.key, channel="email", target=plan.target, status="dryrun",
            subject=rendered.subject, body_hash=rendered.body_hash(),
        )
        return

    pacer.wait()
    try:
        reference = transport.send(msg)
    except TransportError as exc:
        result.failed += 1
        result.errors.append(f"{plan.target}: {exc}")
        record_delivery(
            conn, run_id=run_id, contact_id=plan.contact_id, campaign_key=campaign.key,
            step_key=plan.step.key, channel="email", target=plan.target, status="failed",
            subject=rendered.subject, body_hash=rendered.body_hash(), error=str(exc),
        )
        return

    result.sent += 1
    record_delivery(
        conn, run_id=run_id, contact_id=plan.contact_id, campaign_key=campaign.key,
        step_key=plan.step.key, channel="email", target=plan.target, status="sent",
        subject=rendered.subject, body_hash=rendered.body_hash(), evidence=reference,
    )
