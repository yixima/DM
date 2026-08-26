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
from .selector import select
from .throttle import Pacer, in_quiet_hours

# 成功でも失敗でもなく、人が見て判断すべき結果
REVIEW_STATUSES = ("needs_review", "skipped_captcha", "skipped_no_form")


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
    if not campaign.enabled:
        raise SendAborted(f"キャンペーン {campaign.key} は enabled: false です")

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

    now = now or datetime.now(timezone.utc)
    report = select(conn, campaign, "form", settings, limit=limit, now=now)
    mode = "live" if not dry_run else "dry-run"
    result = SendResult(run_id=None, mode=mode, planned=len(report.plans), skip_reasons=dict(report.skipped))
    result.skipped = sum(report.skipped.values())

    if not report.plans:
        return result

    run_id = start_run(conn, campaign.key, "form", mode)
    result.run_id = run_id
    env = build_env(settings)
    pacer = Pacer(settings.form_limits.min_seconds_between_submits, settings.form_limits.jitter_seconds)

    try:
        with FormBrowser(settings, headless=headless) as browser:
            for plan in report.plans:
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
                    continue

                check = check_form_body(settings, rendered.subject, rendered.body)
                if not check.ok:
                    result.skipped += 1
                    reason = "本文チェック不合格"
                    result.skip_reasons[reason] = result.skip_reasons.get(reason, 0) + 1
                    result.errors.append(f"{plan.company}: {check.errors[0]}")
                    record_delivery(
                        conn, run_id=run_id, contact_id=plan.contact_id, campaign_key=campaign.key,
                        step_key=plan.step.key, channel="form", target=plan.target,
                        status="skipped_compliance", subject=rendered.subject,
                        error="; ".join(check.errors),
                    )
                    continue

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
    except PlaywrightUnavailable:
        finish_run(conn, run_id, planned=result.planned, sent=result.sent,
                   failed=result.failed, skipped=result.skipped, notes="playwright未導入")
        raise
    else:
        finish_run(
            conn, run_id,
            planned=result.planned, sent=result.sent, failed=result.failed, skipped=result.skipped,
            notes="; ".join(result.errors[:5]) or None,
        )
    return result
