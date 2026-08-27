# DM — 定期メール配信 + 問い合わせフォーム自動送信システム

`master_contacts_20260825_181226.csv`（5,278行）を元に、

1. **送信先へ一括・定期的にメールで案内を送る**
2. **各社の問い合わせフォームへ自動・一斉・定期的に案内を送る**

の2つを、同じ宛先リスト・同じ配信ルール・同じ履歴の上で回すためのシステムです。

「1回送って終わり」ではなく**続けられること**を前提に作ってあります。誰にいつ何を送ったかを
すべて記録し、次に送るべき人と内容を自動で決め、配信停止とバウンスを自動で取り込みます。

---

## 1. 何がどう動くか

```
data/master_contacts_*.csv
        │  dm import      … 正規化・重複統合・不正データの除外
        ▼
   state/dm.sqlite3  ←──────────────┐
   （宛先 / 送信履歴 / 配信停止 / 実行ログ）│
        │                          │
        │  dm plan / run           │ dm ingest
        ▼                          │（バウンス・配信停止の取り込み）
   ┌─────────────┐           ┌──────┴──────┐
   │ selector    │ 誰に何を   │  受信箱      │
   │ (配信ルール) │ 送るか決定 │  (IMAP)     │
   └──────┬──────┘           └─────────────┘
          │
     ┌────┴────┐
     ▼         ▼
  mailer    formrunner
 (SMTP)    (Playwright)
     │         │
     └────┬────┘
          ▼
    法令表示チェック → 送信 → 履歴・証跡を記録
```

宛先リストの取り込み結果（実データ）:

| | 件数 |
|---|---|
| CSV 行数 | 5,278 |
| 登録された宛先（重複統合後） | 4,568 |
| メール送信可 | 2,520 |
| フォーム送信可 | 3,772 |
| 除外（メール・フォームとも使用不可） | 706 |

除外されたのは、`info@domain.com` のようなサンプルアドレス、形式不正のアドレス、
Yahoo!ファイナンスや検索ページなど**その企業のサイトではない**フォームURLです。
そのまま送るとバウンスや誤送信になるため、取り込み時点で落としています。

---

## 2. 「定期的に案内する」の仕組み

キャンペーンは**順番に届ける複数のコンテンツ（steps）**として定義します。
定期実行のたびに、各宛先は**次に受け取るべきコンテンツを1つだけ**受け取ります。

```yaml
# config/campaigns/intro_2026autumn.yaml
steps:
  - key: s1_intro    delay_days: 0    # 初回のご挨拶
  - key: s2_cases    delay_days: 21   # 前回から21日空けて、導入事例
  - key: s3_seminar  delay_days: 28   # さらに28日空けて、説明会の案内
```

つまり週次で実行しておけば、

- まだ誰にも送っていない企業には `s1_intro` が届く
- `s1_intro` を21日以上前に受け取った企業には `s2_cases` が届く
- 全部受け取り終えた企業には、もう送らない（`repeat_cycle: true` なら1年後に再開）

同一相手への最短接触間隔は **3日**（`settings.yaml` の `global_min_interval_days`）です。
これは**下限**で、キャンペーン側でこれより短い値を書いても3日で頭打ちになります。
内容の違うものであれば、同じ相手に週2回まで届けられます。

### 複数シリーズを並行して回す

シリーズ（キャンペーン）は同時に複数走らせられます。例えば「定期ニュースレター」を
低い優先度で回しつつ、時期が来たら「個別キャンペーン」を高い優先度で割り込ませる、という形です。

```yaml
# config/campaigns/newsletter.yaml
key: newsletter
priority: 20        # 数字が大きいほど先に宛先を確保する（既定 50）
```

```bash
python -m dm.cli plan --campaign all --channel email   # 全シリーズまとめて計画
python -m dm.cli send --campaign all --live            # 全シリーズまとめて配信
```

同じ相手が2つのシリーズの対象になったときは、**優先度の高い方だけが今回送り**、
もう一方はその相手を次回に回します。1回の実行で同じ相手に2通届くことはありません。

**注意**: 優先度の高いシリーズは、自分の `max_per_run` に達するまで宛先を取り切ります。
低い側にも毎回いくらか回したい場合は、高い側に `max_per_run` を設定してください。

という配信が、リストの端から順に自動で回り続けます。1回の実行で送る件数には上限があるので
（既定150件）、4,568件のリストは数週間かけて消化されていきます。

---

## 3. セットアップ

```bash
git clone <this repo> /opt/dm && cd /opt/dm
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[form,dev]"
python -m playwright install chromium        # フォーム送信を使う場合のみ

cp .env.example .env && vi .env              # ★ 必須。詳細は次節
chmod 600 .env

python -m dm.cli init
python -m dm.cli import
python -m dm.cli doctor                      # 設定に不足がないか確認
```

### `.env` の必須項目

法令上の表示義務があるため、**ここが埋まっていないと `--live` 送信は実行できません**
（dry-run は可能）。fail-closed です。

| 変数 | 用途 |
|---|---|
| `DM_SENDER_NAME` | 送信者（会社）名。本文への表示が必須 |
| `DM_SENDER_ADDRESS` | 住所。本文への表示が必須 |
| `DM_SENDER_EMAIL` / `DM_SENDER_PHONE` / `DM_SENDER_URL` | 問い合わせ先。いずれかの表示が必須 |
| `DM_UNSUBSCRIBE_BASE_URL` / `DM_UNSUBSCRIBE_EMAIL` | 配信停止の受付先。表示が必須 |
| `DM_UNSUBSCRIBE_SECRET` | 配信停止リンクの改ざん防止用。長いランダム文字列を設定 |
| `DM_SMTP_*` | 実送信に使う SMTP |
| `DM_FORM_*` | 問い合わせフォームの各欄に入力する自社情報 |
| `DM_IMAP_*` | バウンス・配信停止の自動取り込み（強く推奨） |

---

## 4. 日常のコマンド

```bash
# 今回の実行で誰に何が送られるか（送信しない）
python -m dm.cli plan --campaign intro_2026autumn --channel email
python -m dm.cli plan --campaign intro_2026autumn --channel form

# 実際の文面を目で確認する（法令表示チェック付き）
python -m dm.cli preview --campaign intro_2026autumn --channel email --count 2

# 全ステップをまとめた確認用ページを書き出す（ブラウザで読む・共有する）
python -m dm.cli preview --campaign intro_2026autumn --html state/preview.html

# メール配信（既定は dry-run。--live で実送信）
python -m dm.cli send --campaign intro_2026autumn                 # dry-run
python -m dm.cli send --campaign intro_2026autumn --transport file  # .eml を outbox に出力
python -m dm.cli send --campaign intro_2026autumn --live

# フォーム送信（既定は「入力するが送信ボタンは押さない」dry-run）
python -m dm.cli form --campaign intro_2026autumn
python -m dm.cli form --campaign intro_2026autumn --live

# メールとフォームをまとめて（定期実行の入口）
python -m dm.cli run --campaign intro_2026autumn --live

# バウンス・配信停止の取り込み（毎日回すこと）
python -m dm.cli ingest --imap --days 7

# 状況確認・書き出し
python -m dm.cli stats
python -m dm.cli campaigns
python -m dm.cli runs
python -m dm.cli export --needs-review state/needs_review.csv

# 手動での送信停止登録
python -m dm.cli suppress --kind email  --value a@example.jp --reason "先方からの依頼"
python -m dm.cli suppress --kind domain --value example.jp
python -m dm.cli suppress --list
```

**推奨の進め方**: `plan` → `preview` → `--transport file` で .eml を検品 → 少数で `--live --limit 10`
→ 問題なければ通常運用。

---

## 5. 定期実行の設定

`scripts/periodic_run.sh` が入口です（多重起動を `flock` で防ぎ、ログと要対応CSVを毎回出力）。

**systemd（推奨）**

```bash
sudo cp deploy/dm-*.service deploy/dm-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dm-ingest.timer dm-email.timer dm-form.timer
systemctl list-timers 'dm-*'
```

既定のスケジュール:

| | タイミング | 内容 |
|---|---|---|
| `dm-ingest` | 毎日 8:00 | バウンス・配信停止の取り込み |
| `dm-email` | 毎週火曜 10:00 | メール配信 |
| `dm-form` | 毎週水・金 11:00 | フォーム送信 |

cron を使う場合は `deploy/crontab.example` を参照してください。

> **GitHub Actions では回さないでください。** 送信履歴（`state/dm.sqlite3`）が実行ごとに消えるため
> 二重送信になり、Actions の IP からのフォーム送信は多くのサイトで弾かれます。
> リポジトリの Actions はテストと文面検証のみに使っています。

---

## 6. 送りすぎ・送り間違いを防ぐ設計

このシステムが**送らない**と判断する条件:

| 条件 | 挙動 |
|---|---|
| 配信停止・バウンス・苦情のあった宛先 | 恒久的に除外 |
| 同じ相手に3日以内に接触済み（メール・フォーム横断） | 今回は見送り（＝同一相手へは週2回まで） |
| 同一ドメインへ1回の実行で2件目 | 次回に繰り越し（シリーズをまたいで共有） |
| 同じ相手が同一実行で2つのシリーズの対象 | 優先度の高い方のみ送信 |
| 同一サイトへ24時間以内に2回目のフォーム送信 | 見送り |
| 21時〜翌8時（JST） | 実行を中止 |
| 送信者名・住所・配信停止先が本文にない | **送信せず** `skipped_compliance` として記録 |
| 未展開の `{{ }}` が本文に残っている | **送信せず** 記録 |
| フォームに CAPTCHA がある | **送信せず** 手動対応リストへ（回避はしません） |
| `robots.txt` が禁止しているパス | 送信せず記録 |
| フォームの必須項目を埋められなかった | 送信せず `needs_review` として記録 |
| フォームの本文欄を特定できなかった | 送信せず記録 |

送信ペースは1通ごとに2秒＋ゆらぎ、毎時120通まで、フォームは1件ごとに8秒＋ゆらぎ。
急いで送ってドメインの評判を落とすと、その後どこにも届かなくなるためです。

自動で送れなかった先は `state/needs_review.csv` に、スクリーンショット付きで溜まります。
件数の多い先だけ人が手で対応する、という運用を想定しています。

---

## 7. 案内する内容を変える

文面は `templates/` にあります（**現在入っているのは雛形です。自社の内容に差し替えてください**）。

```
templates/email/_footer.txt.j2     法令表示のフッタ（原則そのまま使う）
templates/email/intro_s1.txt.j2    1通目: ご挨拶
templates/email/intro_s2.txt.j2    2通目: 導入事例
templates/email/intro_s3.txt.j2    3通目: 説明会の案内
templates/form/*.txt.j2            フォーム用（同じ3本）
```

差し込める変数: `{{ company_name }}` `{{ salutation }}`（＝「〇〇 御中」）`{{ domain }}`
`{{ official_url }}` `{{ sender.name }}` `{{ sender.person }}` `{{ sender.address }}`
`{{ sender.phone }}` `{{ sender.email }}` `{{ sender.url }}` `{{ unsubscribe_url }}`

書き換えたら必ず `dm preview` で法令表示チェックを通してください。
`--html` を付けると全ステップをまとめた確認用ページになり、ブラウザでそのまま読めます。

各ステップには `title:`（例: `はじめのご挨拶`）を付けられます。進捗表示とプレビューに使われます。

新しいシリーズを始めるときは `config/campaigns/` に YAML を1本足すだけです。
セグメント（`ranks`, `exclude_domains`, `exclude_freemail` など）で宛先を絞れます。

---

## 8. 配信停止リンクの受け口

メール本文の配信停止URLは `https://.../unsubscribe?e=<アドレス>&t=<トークン>` の形です。
トークンは `DM_UNSUBSCRIBE_SECRET` による HMAC なので、他人のアドレスを勝手に停止させられません。

Web 側で受けたら、次を実行すれば登録されます（トークン検証込み）:

```bash
python -m dm.cli unsubscribe --email "$E" --token "$T"
```

メールでの配信停止依頼は `dm ingest --imap` が自動で拾います。

---

## 9. 注意点

- **`data/*.csv` には取引先候補の連絡先が入っています。** このリポジトリを公開する場合は、
  CSV と `state/` をリポジトリ外へ移し、`paths.contacts_csv` で参照してください。
- `state/` と `.env` は `.gitignore` 済みです（送信履歴・秘密情報をコミットしないため）。
- 法令面の要点は [`docs/COMPLIANCE.md`](docs/COMPLIANCE.md)、日々の運用は
  [`docs/OPERATIONS.md`](docs/OPERATIONS.md) にまとめています。**運用開始前に両方読んでください。**

## テスト

```bash
python -m pytest -q      # 77件。フォーム送信はローカルHTTPサーバに実物のフォームを立てて検証
```
