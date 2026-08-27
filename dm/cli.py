"""コマンドラインインターフェース。

日常運用はこの4つで足りる:
  dm import            リストの取り込み・更新
  dm plan   --campaign K --channel email    今回誰に何が送られるかの確認
  dm run    --campaign K --live             定期実行（メール＋フォーム）
  dm ingest --imap                          バウンス・配信停止の取り込み
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from . import report as report_mod
from .campaign import CampaignError, get_campaign, load_campaigns
from .compliance import check_email_body, check_form_body, check_settings
from .config import Settings, load_settings
from .db import add_suppression, init_db, load_suppressions
from .importer import import_contacts
from .mailer import SendAborted, run_email_campaign, run_email_campaigns
from .preview_html import write_preview_html
from .render import build_env, render_email, render_form, verify_unsubscribe_token
from .selector import select, select_across


def _open(settings: Settings) -> sqlite3.Connection:
    settings.ensure_dirs()
    return init_db(settings.db_path)


def _is_all(value: str) -> bool:
    """--campaign all で、有効なシリーズをまとめて対象にする。"""
    return (value or "").strip().lower() == "all"


def _print_result(result) -> None:
    print(result.summary())
    if result.errors:
        print("  主なエラー:")
        for line in result.errors[:10]:
            print(f"    - {line}")


# --------------------------------------------------------------------- 各コマンド


def cmd_init(args, settings: Settings) -> int:
    conn = _open(settings)
    print(f"データベースを初期化しました: {settings.db_path}")
    conn.close()
    return 0


def cmd_import(args, settings: Settings) -> int:
    csv_path = Path(args.csv) if args.csv else settings.contacts_csv
    if not csv_path.exists():
        print(f"CSVが見つかりません: {csv_path}", file=sys.stderr)
        return 1
    conn = _open(settings)
    summary = import_contacts(conn, csv_path)
    print(f"取り込み元: {csv_path}")
    print(f"  CSV行数           : {summary['csv_rows']}")
    print(f"  登録された宛先     : {summary['contacts']}")
    print(f"  メール送信可       : {summary['email_targets']}")
    print(f"  フォーム送信可     : {summary['form_targets']}")
    for key, value in summary["stats"].items():
        print(f"  {key}: {value}")
    conn.close()
    return 0


def cmd_stats(args, settings: Settings) -> int:
    conn = _open(settings)
    print("■ 宛先リスト")
    print(report_mod.format_stats(report_mod.contact_stats(conn)))
    totals = report_mod.channel_totals(conn, days=args.days)
    if totals:
        print(f"\n■ 直近{args.days}日の送信結果")
        for channel, statuses in totals.items():
            inner = ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
            print(f"  {channel}: {inner}")
    conn.close()
    return 0


def cmd_campaigns(args, settings: Settings) -> int:
    campaigns = load_campaigns(settings.campaign_dir)
    if not campaigns:
        print(f"キャンペーン定義がありません: {settings.campaign_dir}")
        return 1
    conn = _open(settings)
    for key, campaign in campaigns.items():
        state = "有効" if campaign.enabled else "停止中"
        print(f"\n● {key}  [{state}]  {campaign.name}")
        print(f"  チャネル: {', '.join(campaign.channels)} / ステップ数: {len(campaign.steps)}")
        titles = {s.key: (s.title or s.key) for s in campaign.steps}
        for row in report_mod.campaign_progress(conn, campaign):
            print(
                f"    - {titles.get(row['step'], row['step'])}（{row['step']}）:"
                f" 送信済 {row['送信済']} / 失敗 {row['失敗']}"
                f" / 要確認 {row['要確認']} / dry-run {row['dry-run']}"
            )
    conn.close()
    return 0


def cmd_doctor(args, settings: Settings) -> int:
    problems: list[str] = []
    for channel in ("email", "form"):
        for error in check_settings(settings, channel):
            problems.append(f"[{channel}] {error}")
    for missing in settings.form_profile.missing():
        problems.append(f"[form] フォーム入力用プロフィールが未設定: {missing}")

    conn = _open(settings)
    contacts = report_mod.contact_stats(conn)
    if contacts["総件数"] == 0:
        problems.append("[data] 宛先が0件です。`dm import` を実行してください。")

    try:
        campaigns = load_campaigns(settings.campaign_dir)
    except CampaignError as exc:
        problems.append(f"[campaign] {exc}")
        campaigns = {}
    if not campaigns:
        problems.append("[campaign] 有効なキャンペーン定義がありません。")

    print("■ 設定チェック")
    if problems:
        for problem in problems:
            print(f"  NG {problem}")
        print("\n上記を解消するまで --live での送信はできません（dry-run は可能です）。")
    else:
        print("  問題は見つかりませんでした。")
    print(f"\n  transport            : {settings.transport}")
    print(f"  送信抑止時間帯       : {settings.quiet_hours[0]}時〜{settings.quiet_hours[1]}時 ({settings.timezone})")
    print(f"  最短接触間隔         : {settings.global_min_interval_days}日")
    print(f"  メール1回あたり上限  : {settings.email_limits.max_per_run}件")
    print(f"  フォーム1回あたり上限: {settings.form_limits.max_per_run}件")
    print(f"  robots.txt 尊重      : {'はい' if settings.form_limits.respect_robots else 'いいえ'}")
    conn.close()
    return 1 if problems else 0


def cmd_preview(args, settings: Settings) -> int:
    conn = _open(settings)
    campaign = get_campaign(settings.campaign_dir, args.campaign)
    channel = args.channel
    if channel not in campaign.channels:
        print(f"キャンペーン {campaign.key} はチャネル {channel} に対応していません", file=sys.stderr)
        return 1
    steps = [s for s in campaign.steps if not args.step or s.key == args.step]
    if not steps:
        print(f"ステップ {args.step} が見つかりません", file=sys.stderr)
        return 1

    ok_column = "email_ok" if channel == "email" else "form_ok"
    rows = conn.execute(
        f"SELECT * FROM contacts WHERE {ok_column}=1 ORDER BY id LIMIT ?", (args.count,)
    ).fetchall()
    if not rows:
        print("プレビュー対象の宛先がありません。`dm import` を先に実行してください。", file=sys.stderr)
        return 1

    if args.html:
        target = Path(args.html)
        write_preview_html(target, settings, campaign, rows[0],
                           channels=(channel,) if args.channel_only else ("email", "form"))
        print(f"プレビューを書き出しました: {target}")
        print("  ブラウザで開くか、共有ページとして提示してください。")
        conn.close()
        return 0

    env = build_env(settings)
    failures = 0
    for step in steps:
        for contact in rows:
            print("=" * 76)
            print(f"[{campaign.key} / {step.key} / {channel}] {contact['company_name']}")
            if channel == "email":
                rendered = render_email(settings, env, contact, campaign, step)
                check = check_email_body(settings, rendered.subject, rendered.text)
                print(f"To: {contact['contact_email']}")
                print(f"件名: {rendered.subject}")
                print("-" * 76)
                print(rendered.text)
            else:
                rendered = render_form(settings, env, contact, campaign, step)
                check = check_form_body(settings, rendered.subject, rendered.body)
                print(f"フォーム: {contact['contact_form_url']}")
                print(f"件名欄: {rendered.subject}")
                print("-" * 76)
                print(rendered.body)
            print("-" * 76)
            print(f"チェック: {check.report()}")
            if not check.ok:
                failures += 1
    conn.close()
    if failures:
        print(f"\n{failures}件が法令表示チェックに不合格です。テンプレートと .env を修正してください。", file=sys.stderr)
        return 1
    return 0


def _show_plan(campaign_key: str, channel: str, result, show: int) -> None:
    print(f"[{campaign_key} / {channel}] {result.summary()}")
    for plan in result.plans[:show]:
        print(f"  {plan.step.key:12s} {plan.company[:28]:30s} {plan.target}")
    if len(result.plans) > show:
        print(f"  ... 他 {len(result.plans) - show} 件")


def cmd_plan(args, settings: Settings) -> int:
    conn = _open(settings)
    if _is_all(args.campaign):
        campaigns = list(load_campaigns(settings.campaign_dir).values())
        planned = select_across(conn, campaigns, args.channel, settings, limit=args.limit)
        if not planned:
            print(f"チャネル {args.channel} に対応した有効なシリーズがありません")
        total = 0
        for campaign, result in planned:
            _show_plan(campaign.key, args.channel, result, args.show)
            total += len(result.plans)
            print()
        print(f"合計 {total} 件（優先度の高いシリーズから順に確保）")
        conn.close()
        return 0

    campaign = get_campaign(settings.campaign_dir, args.campaign)
    result = select(conn, campaign, args.channel, settings, limit=args.limit)
    _show_plan(campaign.key, args.channel, result, args.show)
    conn.close()
    return 0


def cmd_send(args, settings: Settings) -> int:
    conn = _open(settings)
    common = dict(
        dry_run=not args.live,
        limit=args.limit,
        transport_override=args.transport,
        ignore_quiet_hours=args.ignore_quiet_hours,
    )
    try:
        if _is_all(args.campaign):
            campaigns = list(load_campaigns(settings.campaign_dir).values())
            result = run_email_campaigns(conn, campaigns, settings, **common)
        else:
            campaign = get_campaign(settings.campaign_dir, args.campaign)
            result = run_email_campaign(conn, campaign, settings, **common)
    except SendAborted as exc:
        print(f"中止しました: {exc}", file=sys.stderr)
        conn.close()
        return 2
    _print_result(result)
    conn.close()
    return 0


def cmd_form(args, settings: Settings) -> int:
    from .formbot import PlaywrightUnavailable
    from .formrunner import run_form_campaign, run_form_campaigns

    conn = _open(settings)
    common = dict(
        dry_run=not args.live,
        limit=args.limit,
        headless=not args.no_headless,
        ignore_quiet_hours=args.ignore_quiet_hours,
    )
    try:
        if _is_all(args.campaign):
            campaigns = list(load_campaigns(settings.campaign_dir).values())
            result = run_form_campaigns(conn, campaigns, settings, **common)
        else:
            campaign = get_campaign(settings.campaign_dir, args.campaign)
            result = run_form_campaign(conn, campaign, settings, **common)
    except SendAborted as exc:
        print(f"中止しました: {exc}", file=sys.stderr)
        conn.close()
        return 2
    except PlaywrightUnavailable as exc:
        print(f"中止しました: {exc}", file=sys.stderr)
        conn.close()
        return 3
    _print_result(result)
    conn.close()
    return 0


def cmd_run(args, settings: Settings) -> int:
    """定期実行の入口。設定されたチャネルを順に回す。"""
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    campaign = None if _is_all(args.campaign) else get_campaign(settings.campaign_dir, args.campaign)
    exit_code = 0
    for channel in channels:
        if campaign is not None and channel not in campaign.channels:
            print(f"[{channel}] キャンペーン {campaign.key} は未対応のためスキップします")
            continue
        label = campaign.key if campaign else "全シリーズ"
        print(f"\n===== {label} / {channel} =====")
        sub = argparse.Namespace(
            campaign=args.campaign, limit=args.limit, live=args.live,
            transport=args.transport, ignore_quiet_hours=args.ignore_quiet_hours,
            no_headless=False,
        )
        code = cmd_send(sub, settings) if channel == "email" else cmd_form(sub, settings)
        exit_code = exit_code or code
    return exit_code


def cmd_suppress(args, settings: Settings) -> int:
    conn = _open(settings)
    if args.list:
        rows = conn.execute(
            "SELECT kind, value, reason, source, created_at FROM suppressions ORDER BY created_at DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        print(f"送信禁止リスト（新しい順、最大{args.limit}件）")
        for row in rows:
            print(f"  {row['kind']:9s} {row['value']:45s} {row['reason']} ({row['source']})")
        counts = load_suppressions(conn)
        print("  合計: " + ", ".join(f"{k}={len(v)}" for k, v in counts.items()))
        conn.close()
        return 0

    added = 0
    if args.csv:
        path = Path(args.csv)
        if not path.exists():
            print(f"CSVが見つかりません: {path}", file=sys.stderr)
            return 1
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                value = (row.get(args.column) or "").strip()
                if value and add_suppression(conn, args.kind, value, args.reason, f"csv:{path.name}"):
                    added += 1
    for value in args.value or []:
        if add_suppression(conn, args.kind, value, args.reason, "manual"):
            added += 1

    if not added and not args.value and not args.csv:
        print("追加する値を指定してください（--value / --csv）", file=sys.stderr)
        conn.close()
        return 1
    print(f"送信禁止リストに {added} 件追加しました（種別: {args.kind}）")
    conn.close()
    return 0


def cmd_unsubscribe(args, settings: Settings) -> int:
    """配信停止URLのバックエンド処理。トークンを検証してから登録する。"""
    if not args.force and not verify_unsubscribe_token(settings.unsubscribe.secret, args.email, args.token):
        print("トークンが一致しません。配信停止を登録しませんでした。", file=sys.stderr)
        return 1
    conn = _open(settings)
    added = add_suppression(conn, "email", args.email, "配信停止の申し出（本人操作）", "unsubscribe-link")
    print(f"配信停止を登録しました: {args.email}" if added else f"すでに登録済みです: {args.email}")
    conn.close()
    return 0


def cmd_ingest(args, settings: Settings) -> int:
    from .inbox import ingest_eml_dir, ingest_imap

    conn = _open(settings)
    try:
        if args.eml_dir:
            result = ingest_eml_dir(conn, args.eml_dir)
        else:
            result = ingest_imap(conn, settings, days=args.days, mailbox=args.mailbox, mark_seen=args.mark_seen)
    except RuntimeError as exc:
        print(f"取り込みに失敗しました: {exc}", file=sys.stderr)
        conn.close()
        return 1
    print(result.summary())
    for line in result.details[:20]:
        print(f"  - {line}")
    conn.close()
    return 0


def cmd_export(args, settings: Settings) -> int:
    conn = _open(settings)
    if args.needs_review:
        count = report_mod.export_needs_review(conn, Path(args.needs_review), args.campaign)
        print(f"要対応リストを書き出しました: {args.needs_review}（{count}件）")
    if args.deliveries:
        count = report_mod.export_deliveries(conn, Path(args.deliveries), args.campaign)
        print(f"送信履歴を書き出しました: {args.deliveries}（{count}件）")
    if not args.needs_review and not args.deliveries:
        print("--needs-review か --deliveries のいずれかを指定してください", file=sys.stderr)
        conn.close()
        return 1
    conn.close()
    return 0


def cmd_runs(args, settings: Settings) -> int:
    conn = _open(settings)
    print("実行履歴（新しい順）")
    for row in report_mod.recent_runs(conn, args.limit):
        print(
            f"  #{row['id']:<4d} {row['started_at']} {row['campaign_key']:22s} {row['channel']:5s}"
            f" {row['mode']:8s} 計画{row['planned']:4d} 送信{row['sent']:4d}"
            f" 失敗{row['failed']:3d} スキップ{row['skipped']:4d}"
        )
    conn.close()
    return 0


# --------------------------------------------------------------------- パーサ


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dm", description="定期メール配信 + 問い合わせフォーム自動送信システム"
    )
    parser.add_argument("--settings", help="設定ファイルのパス（既定: config/settings.yaml）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="データベースを作成する").set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="マスターCSVを取り込む")
    p.add_argument("--csv", help="取り込むCSV（既定: settings の contacts_csv）")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("stats", help="宛先リストと送信結果の集計")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_stats)

    sub.add_parser("campaigns", help="キャンペーン一覧と進捗").set_defaults(func=cmd_campaigns)
    sub.add_parser("doctor", help="送信前の設定チェック").set_defaults(func=cmd_doctor)

    p = sub.add_parser("preview", help="実際に送られる文面を確認する")
    p.add_argument("--campaign", required=True)
    p.add_argument("--channel", choices=["email", "form"], default="email")
    p.add_argument("--step")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--html", help="全ステップをまとめたHTMLを書き出す（画面で確認・共有する用）")
    p.add_argument("--channel-only", action="store_true",
                   help="--html のとき、--channel で指定した片方だけを出力する")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("plan", help="今回の実行で誰に何が送られるかを表示する（送信しない）")
    p.add_argument("--campaign", required=True, help="キャンペーンキー、または all（有効な全シリーズ）")
    p.add_argument("--channel", choices=["email", "form"], default="email")
    p.add_argument("--limit", type=int)
    p.add_argument("--show", type=int, default=20)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("send", help="メールを配信する（既定は dry-run）")
    p.add_argument("--campaign", required=True, help="キャンペーンキー、または all（有効な全シリーズ）")
    p.add_argument("--limit", type=int)
    p.add_argument("--live", action="store_true", help="実際に送信する")
    p.add_argument("--transport", choices=["console", "file", "smtp"])
    p.add_argument("--ignore-quiet-hours", action="store_true")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("form", help="問い合わせフォームへ送信する（既定は入力のみの dry-run）")
    p.add_argument("--campaign", required=True, help="キャンペーンキー、または all（有効な全シリーズ）")
    p.add_argument("--limit", type=int)
    p.add_argument("--live", action="store_true", help="実際に送信ボタンを押す")
    p.add_argument("--no-headless", action="store_true", help="ブラウザを画面表示する（デバッグ用）")
    p.add_argument("--ignore-quiet-hours", action="store_true")
    p.set_defaults(func=cmd_form)

    p = sub.add_parser("run", help="定期実行（メール・フォームをまとめて）")
    p.add_argument("--campaign", required=True, help="キャンペーンキー、または all（有効な全シリーズ）")
    p.add_argument("--channels", default="email,form")
    p.add_argument("--limit", type=int)
    p.add_argument("--live", action="store_true")
    p.add_argument("--transport", choices=["console", "file", "smtp"])
    p.add_argument("--ignore-quiet-hours", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("suppress", help="送信禁止リストの管理")
    p.add_argument("--kind", choices=["email", "domain", "company", "form_url"], default="email")
    p.add_argument("--value", action="append", help="追加する値（複数可）")
    p.add_argument("--csv", help="CSVから一括追加")
    p.add_argument("--column", default="email", help="CSVの対象列名")
    p.add_argument("--reason", default="手動登録")
    p.add_argument("--list", action="store_true", help="現在の一覧を表示")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_suppress)

    p = sub.add_parser("unsubscribe", help="配信停止リンクからの登録（トークン検証あり）")
    p.add_argument("--email", required=True)
    p.add_argument("--token", default="")
    p.add_argument("--force", action="store_true", help="トークン検証を省略して登録する")
    p.set_defaults(func=cmd_unsubscribe)

    p = sub.add_parser("ingest", help="受信箱からバウンス・配信停止を取り込む")
    p.add_argument("--imap", action="store_true", help="IMAPで取り込む（既定）")
    p.add_argument("--eml-dir", help="保存済み .eml ディレクトリから取り込む")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--mailbox", default="INBOX")
    p.add_argument("--mark-seen", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("export", help="CSVへ書き出す")
    p.add_argument("--needs-review", help="人が対応すべき残件の出力先")
    p.add_argument("--deliveries", help="送信履歴の出力先")
    p.add_argument("--campaign")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("runs", help="実行履歴")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_runs)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings(Path(args.settings) if args.settings else None)
    try:
        return int(args.func(args, settings) or 0)
    except CampaignError as exc:
        print(f"キャンペーン定義のエラー: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n中断しました", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
