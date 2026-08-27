# 運用手引き

---

## 1. 立ち上げの手順

### ステップ1: 設定を埋める

```bash
cp .env.example .env && vi .env
chmod 600 .env
python -m dm.cli doctor
```

`doctor` が「問題は見つかりませんでした」と言うまで進まないでください。

### ステップ2: 文面を自社のものに差し替える

`templates/` に入っているのは**雛形**です。そのまま送っても意味がありません。
書き換えたら必ず:

```bash
python -m dm.cli preview --campaign intro_2026autumn --channel email --count 2
python -m dm.cli preview --campaign intro_2026autumn --channel form  --count 2
```

法令表示チェックが `OK` になることを確認します。
全体を通しで読みたいときは、確認用ページを書き出してブラウザで開きます。

```bash
python -m dm.cli preview --campaign intro_2026autumn --html state/preview.html
```

### ステップ2.5: 宛先ドメインを掃除する

```bash
python -m dm.cli verify
```

DNS を引いて、メールを受け取れないドメインを送信対象から外します。
2,518ドメインの判定に数分かかります。分割したい場合は `--limit 500` を付けて、
同じコマンドを繰り返してください（続きから再開します）。

**これを飛ばすとバウンス率が上がり、送信ドメインの評判を落とします。**
リストを更新したら、そのつど実行してください。

### ステップ3: .eml で最終確認

```bash
python -m dm.cli send --campaign intro_2026autumn --transport file --limit 5
ls state/outbox/
```

出力された `.eml` をメールソフトで開き、実際の見た目・文字化け・リンク切れを確認します。

### ステップ4: 少数で実送信

```bash
python -m dm.cli send --campaign intro_2026autumn --live --limit 10
```

自社の別アドレスをリストに1件入れておくと、着信を自分で確認できます。
迷惑メールフォルダに入っていないかも見てください。

### ステップ5: フォームを少数で試す

```bash
python -m dm.cli form --campaign intro_2026autumn --limit 5          # 入力のみ
ls state/evidence/            # スクリーンショットで入力内容を確認
python -m dm.cli form --campaign intro_2026autumn --live --limit 5
```

`state/evidence/<日付>/<ドメイン>/` に送信前後のスクリーンショットが残ります。
**最初の数十件は必ず目視してください。** 欄の対応づけが正しいかは、実物を見るのがいちばん早いです。

### ステップ6: 定期実行を有効化

```bash
sudo cp deploy/dm-*.service deploy/dm-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dm-ingest.timer dm-email.timer dm-form.timer
```

---

## 2. 毎週の点検

```bash
python -m dm.cli runs                    # 直近の実行結果
python -m dm.cli stats                   # 送信状況の集計
python -m dm.cli campaigns               # ステップごとの進捗
python -m dm.cli suppress --list         # 配信停止の増え方
```

見るべき数字:

| 兆候 | 意味 | 対応 |
|---|---|---|
| `failed` が急増 | SMTP の問題、または受信側のブロック | ログの SMTP エラーを確認。送信を止めて原因を潰す |
| 恒久バウンス率が 5% 超 | リストが古い／取得元の品質が低い | 一旦停止し、リストを精査。放置するとドメインの評判が落ちる |
| `skipped_captcha` が多い | 対象業種のサイトが自動送信を拒否している | 手動対応リストへ回すか、その層はメール中心に切り替える |
| `needs_review` が多い | 欄の対応づけが効いていないサイト群がある | `state/evidence/` を見て `dm/fieldmap.py` にパターンを追加 |
| 配信停止が急増 | 内容が刺さっていない、または頻度が高すぎる | 送信を止めて文面を見直す。頻度を上げて解決する問題ではない |

---

## 3. よくある操作

**特定の会社に今後送らない**

```bash
python -m dm.cli suppress --kind domain --value example.co.jp --reason "先方からの依頼"
```

**断りの連絡が来た宛先をまとめて登録する**

```bash
python -m dm.cli suppress --kind email --csv 断り一覧.csv --column メールアドレス --reason "先方からの依頼"
```

**今回だけ件数を絞る**

```bash
python -m dm.cli send --campaign intro_2026autumn --live --limit 30
```

**新しい案内シリーズを始める**

`config/campaigns/` に YAML を追加し、`templates/` に本文を置くだけです。
既存キャンペーンは `enabled: false` にすれば止まります。

**既存シリーズに割り込ませる**

新しい YAML に `priority:` を既存より大きい値（例 `90`）で書き、`--campaign all` で回します。
割り込み側が先に宛先を確保し、既存シリーズはその相手を次回に回します。
既存シリーズも毎回いくらか進めたい場合は、割り込み側に `max_per_run` を設定して取り分を制限します。

```bash
python -m dm.cli plan --campaign all --channel email    # 取り分を事前に確認
python -m dm.cli send --campaign all --live
```

**リストを更新する**

新しい CSV を `data/` に置いて `dm import --csv data/新しいファイル.csv`。
同じ宛先は更新され、送信履歴は保持されます（＝二重送信になりません）。

**手動対応リストを出す**

```bash
python -m dm.cli export --needs-review state/needs_review.csv
```

自動送信できなかった先が、理由とスクリーンショットのパス付きで出ます。

---

## 4. 送信量の考え方

既定値は控えめです。以下は上げる前に読んでください。

- **新しい送信ドメインでいきなり大量に送らないこと。** 1日30〜50通から始め、2〜3週間かけて
  段階的に増やします（ウォームアップ）。急に数千通送ると、ほぼ確実に迷惑メール扱いになります。
- `email_limits.max_per_hour`（既定120）は受信側のレート制限に引っかからないための値です。
- フォームは1件あたり10〜30秒かかります。150件で1時間前後が目安。`form_limits.max_per_run`
  を上げるときは `TimeoutStartSec` も合わせて見直してください。
- **同一サイトへの1日1回制限（`max_per_domain_per_day`）は下げないでください。**
  ここを緩めると相手の業務妨害になり得ます。
- 同一相手への最短接触間隔は3日（週2回まで）に設定しています。内容が違う場合を想定した値です。
  同じ内容を週2回送ることは、この設定では防げません。**内容が違うことは運用側で担保してください。**

4,568件のリストを、週150件のペースで一巡させると約30週です。急ぐ理由がなければ、
この程度の速度が最も安全で、結果的に到達率も高くなります。

---

## 5. トラブルシューティング

**`中止しました: 送信できません` と出る**
→ `.env` の必須項目が欠けています。`dm doctor` が具体的な不足項目を出します。

**`中止しました: 送信抑止時間帯です`**
→ 21時〜翌8時（JST）です。急ぐ場合のみ `--ignore-quiet-hours`。ただし深夜のDMは印象を損ねます。

**`plan` の対象が0件になる**
→ 全員が「待機期間中」か「配信済み」です。`dm campaigns` で進捗を確認してください。
ステップの `delay_days` を満たすまでは対象になりません。

**フォーム送信が `needs_review` ばかりになる**
→ `state/evidence/` のスクリーンショットを見てください。多くは
（a）JavaScript でフォームを後から描画している、（b）ラベルが画像になっている、
（c）独自の必須項目がある、のいずれかです。同じパターンが多い場合は
`dm/fieldmap.py` の `PATTERNS` に正規表現を足すと一気に改善します。

**メールが届かない／迷惑メールに入る**
→ まず SPF・DKIM・DMARC を確認してください。次に、送信ドメインと `From` のドメインが
一致しているか。HTML を使わず本文をテキストのみにしているのも、この対策の一部です。

**同じ相手に2回送ってしまった**
→ 起きないはずですが、`state/dm.sqlite3` を消して作り直した場合は履歴が失われます。
このファイルは**バックアップ対象**です。日次でコピーを取ってください。

```bash
sqlite3 state/dm.sqlite3 ".backup 'state/backup/dm-$(date +%F).sqlite3'"
```

---

## 6. このシステムを変更するとき

- `dm/selector.py` が配信ルールの中心です。ここを変えるときは `tests/test_selector.py` を
  必ず一緒に更新してください（二重送信・過剰接触を防いでいるのはこのテストです）。
- `dm/compliance.py` のチェックは**緩めないでください**。緩めると、法令表示の欠けた
  メールが送れてしまいます。
- `dm/formbot.py` に CAPTCHA 回避を足さないでください。方針として実装しません。
