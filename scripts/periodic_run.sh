#!/usr/bin/env bash
# 定期実行の入口。cron / systemd timer から呼ぶ。
#
#   scripts/periodic_run.sh refresh          リストの取り込み＋ドメイン検証
#   scripts/periodic_run.sh email            メール配信（dry-run）
#   scripts/periodic_run.sh email --live     メール配信（実送信）
#   scripts/periodic_run.sh form  --live     フォーム送信（実送信）
#   scripts/periodic_run.sh ingest           バウンス・配信停止の取り込み
#
# refresh は配信より先に回すこと。順序を逆にすると、古いリストで送ることになる。
#
# 環境変数:
#   DM_CAMPAIGN  対象キャンペーン（既定: intro_2026autumn）
#   DM_LIMIT     1回の上限（省略時はキャンペーン設定に従う）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHANNEL="${1:-email}"
shift || true

CAMPAIGN="${DM_CAMPAIGN:-intro_2026autumn}"
PYTHON="${DM_PYTHON:-python3}"
LOG_DIR="$ROOT/state/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/${CHANNEL}-${STAMP}.log"

# 同時実行を防ぐ（前回の実行が終わっていなければ何もしない）
LOCK="$ROOT/state/.${CHANNEL}.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "前回の ${CHANNEL} 実行が終了していないため、今回はスキップします" | tee -a "$LOG"
  exit 0
fi

LIMIT_ARG=()
if [[ -n "${DM_LIMIT:-}" ]]; then
  LIMIT_ARG=(--limit "$DM_LIMIT")
fi

{
  echo "===== $(date '+%F %T') ${CHANNEL} / ${CAMPAIGN} ====="
  case "$CHANNEL" in
    refresh)
      # 監視フォルダの最新CSVを取り込む。宛先が大きく減っていれば取り込まずに止まる。
      if ! "$PYTHON" -m dm.cli import --deactivate-missing "$@"; then
        echo "!! リストの取り込みに失敗しました。配信は行いません。" >&2
        exit 1
      fi
      # 新しく増えたドメインだけ判定される（判定済みは引き直さない）
      "$PYTHON" -m dm.cli verify
      ;;
    email)  "$PYTHON" -m dm.cli send   --campaign "$CAMPAIGN" "${LIMIT_ARG[@]}" "$@" ;;
    form)   "$PYTHON" -m dm.cli form   --campaign "$CAMPAIGN" "${LIMIT_ARG[@]}" "$@" ;;
    both)   "$PYTHON" -m dm.cli run    --campaign "$CAMPAIGN" "${LIMIT_ARG[@]}" "$@" ;;
    ingest) "$PYTHON" -m dm.cli ingest --imap "$@" ;;
    *) echo "不明なチャネル: $CHANNEL" >&2; exit 1 ;;
  esac
  echo "----- 完了 $(date '+%F %T') -----"
} 2>&1 | tee -a "$LOG"

# 手動対応が必要な残件を毎回書き出す
"$PYTHON" -m dm.cli export --needs-review "$ROOT/state/needs_review.csv" --campaign "$CAMPAIGN" >>"$LOG" 2>&1 || true

# 30日より古いログは捨てる
find "$LOG_DIR" -name '*.log' -mtime +30 -delete 2>/dev/null || true
