"""問い合わせフォーム送信の実行。selector が出した計画を1件ずつ処理する。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .campaign import Campaign
from .compliance import check_form_body, check_settings
from .config import Settings
from .db import finish_run, record_delivery, start_run
from .formbot import FormBrowser, PlaywrightUnavailable
from .mailer import SendAborted, SendResult
from .render import build_env, render_form
from .selector import SelectionReport, select, select_across
from .throttle import Pacer, in_quiet_hours

# 成功でも失敗でもなく、人が見て判断すべき結果
REVIEW_STATUSES = ("needs_review", "skipped_captcha", "skipped_no_form")


def _preflight(settings: Settings, dry_run: bool, ignore_quiet_hours: bool) -> None:
    setting_errors = check_settings(settings, "form")
    profile_missing = settings.form_profile.missing()
    if profile_missing:
        setting_errors.append("フォーム入力用プロフィールが未設定: " + ", ".join(profile_missing))
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
    headless: bool,
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
    pacer = Pacer(settings.form_limits.min_seconds_between_submits, settings.form_limits.jitter_seconds)

    with FormBrowser(settings, headless=headless) as browser:
        for campaign, report in planned:
            if not report.plans:
                continue
            run_id = start_run(conn, campaign.key, "form", mode)
            total.run_id = total.run_id or run_id
            per_run = SendResult(run_id=run_id, mode=mode, planned=len(report.plans))

            try:
                for plan in report.plans:
                    _process_one(conn, plan, campaign, settings, env, browser, pacer, run_id, dry_run, per_run)
            finally:
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

    return total


def _process_one(conn, plan, campaign, settings, env, browser, pacer, run_id, dry_run, result) -> None:
    try:
        rendered = render_form(settings, env, plan.contact, campaign, plan.step)
    except Exception as exc:
        result.failed += 1
        result.errors.append(f"{plan.company}: 描画失敗 {exc}")
        record_delivery(
            conn, run_id=run_id, contact_id=plan.contact_id, campaign_key=campaign.key,
            step_key=plan.step.key, channel="form", target=plan.target, status="failed",
            error=f"render: {exc}",
        )
        return

    check = check_form_body(settings, rendered.subject, rendered.body)
    if not check.ok:
        result.skipped += 1
        reason = "本文チェック不合格"
        result.skip_reasons[reason] = result.skip_reasons.get(reason, 0) + 1
        result.errors.append(f"{plan.company}: {check.errors[0]}")
        record_delivery(
            conn, run_id=run_id, contact_id=plan.contact_id, campaign_key=campaign.key,
            step_key=plan.step.key, channel="form", target=plan.target,
            status="skipped_compliance", subject=rendered.subject, error="; ".join(check.errors),
        )
        return

    pacer.wait()
    outcome = browser.process(plan.target, rendered.subject, rendered.body, dry_run=dry_run)

    if outcome.status == "submitted":
        result.sent += 1
    elif outcome.status == "dryrun":
        pass
    elif outcome.status == "failed":
        result.failed += 1
        result.errors.append(f"{plan.target}: {outcome.detail}")
    else:
        result.skipped += 1
        result.skip_reasons[outcome.status] = result.skip_reasons.get(outcome.status, 0) + 1

    record_delivery(
        conn, run_id=run_id, contact_id=plan.contact_id, campaign_key=campaign.key,
        step_key=plan.step.key, channel="form", target=plan.target, status=outcome.status,
        subject=rendered.subject, body_hash=rendered.body_hash(),
        error=outcome.detail if outcome.status not in ("submitted", "dryrun") else None,
        evidence=outcome.evidence,
    )


def run_form_campaign(
    conn: sqlite3.Connection,
    campaign: Campaign,
    settings: Settings,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    headless: bool = True,
    ignore_quiet_hours: bool = False,
    now: datetime | None = None,
) -> SendResult:
    """1つのシリーズだけをフォーム送信する。"""
    if not campaign.enabled:
        raise SendAborted(f"キャンペーン {campaign.key} は enabled: false です")
    _preflight(settings, dry_run, ignore_quiet_hours)

    now = now or datetime.now(timezone.utc)
    report = select(conn, campaign, "form", settings, limit=limit, now=now)
    result = _execute(conn, [(campaign, report)], settings, dry_run=dry_run, headless=headless)
    result.per_campaign.setdefault(campaign.key, len(report.plans))
    return result


def run_form_campaigns(
    conn: sqlite3.Connection,
    campaigns: list[Campaign],
    settings: Settings,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    headless: bool = True,
    ignore_quiet_hours: bool = False,
    now: datetime | None = None,
) -> SendResult:
    """有効なシリーズをまとめてフォーム送信する。優先度の高い順に宛先を確保する。"""
    _preflight(settings, dry_run, ignore_quiet_hours)
    now = now or datetime.now(timezone.utc)
    planned = select_across(conn, campaigns, "form", settings, limit=limit, now=now)
    return _execute(conn, planned, settings, dry_run=dry_run, headless=headless)


__all__ = [
    "REVIEW_STATUSES", "run_form_campaign", "run_form_campaigns",
    "PlaywrightUnavailable", "SendAborted",
]
