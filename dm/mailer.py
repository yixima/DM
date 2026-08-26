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
from .selector import Plan, select
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

    def summary(self) -> str:
        head = f"[{self.mode}] 計画 {self.planned} / 送信 {self.sent} / 失敗 {self.failed} / スキップ {self.skipped}"
        if self.skip_reasons:
            head += "\n  スキップ内訳: " + ", ".join(f"{k}={v}" for k, v in sorted(self.skip_reasons.items()))
        return head


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
    if not campaign.enabled:
        raise SendAborted(f"キャンペーン {campaign.key} は enabled: false です")

    setting_errors = check_settings(settings, "email")
    if setting_errors and not dry_run:
        raise SendAborted("送信できません:\n  - " + "\n  - ".join(setting_errors))

    if not dry_run and not ignore_quiet_hours and in_quiet_hours(settings.quiet_hours, settings.timezone):
        raise SendAborted(
            f"送信抑止時間帯です（{settings.quiet_hours[0]}時〜{settings.quiet_hours[1]}時, {settings.timezone}）。"
            " どうしても実行する場合は --ignore-quiet-hours を付けてください。"
        )

    now = now or datetime.now(timezone.utc)
    report = select(conn, campaign, "email", settings, limit=limit, now=now)
    mode = "live" if not dry_run else "dry-run"
    result = SendResult(run_id=None, mode=mode, planned=len(report.plans), skip_reasons=dict(report.skipped))
    result.skipped = sum(report.skipped.values())

    if not report.plans:
        return result

    run_id = start_run(conn, campaign.key, "email", mode)
    result.run_id = run_id

    env = build_env(settings)
    transport = get_transport(settings, transport_override if not dry_run else (transport_override or "console"))
    pacer = Pacer(
        settings.email_limits.min_seconds_between_sends,
        settings.email_limits.jitter_seconds,
        settings.email_limits.max_per_hour,
    )

    try:
        for plan in report.plans:
            _process_one(conn, plan, campaign, settings, env, transport, pacer, run_id, dry_run, result)
    finally:
        transport.close()
        finish_run(
            conn,
            run_id,
            planned=result.planned,
            sent=result.sent,
            failed=result.failed,
            skipped=result.skipped,
            notes="; ".join(result.errors[:5]) or None,
        )
    return result


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
    except Exception as exc:  # テンプレート不備は1件だけの問題ではないことが多いが、記録して続行
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
