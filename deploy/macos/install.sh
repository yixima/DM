#!/usr/bin/env bash
# macOS で定期実行を有効にする。
#
#   bash deploy/macos/install.sh            登録する
#   bash deploy/macos/install.sh --uninstall 解除する
#
# 前提: このリポジトリのルートに .venv があること（README のセットアップ手順）。
#
# 注意: これは LaunchAgent です。ログイン中のユーザーとして動きます。
#       ログアウト中・Mac の電源が落ちている間は実行されません。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
JOBS=(refresh ingest email form)

if [[ "${1:-}" == "--uninstall" ]]; then
  for job in "${JOBS[@]}"; do
    label="jp.dm.${job}"
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
    rm -f "${AGENTS}/${label}.plist"
    echo "解除: ${label}"
  done
  echo "定期実行を解除しました。"
  exit 0
fi

if [[ ! -x "${ROOT}/.venv/bin/python" ]]; then
  echo "エラー: ${ROOT}/.venv/bin/python が見つかりません。" >&2
  echo "  先に README のセットアップ（python3 -m venv .venv && pip install -e '.[form,dns]'）を行ってください。" >&2
  exit 1
fi

mkdir -p "${AGENTS}" "${ROOT}/state/logs"

for job in "${JOBS[@]}"; do
  label="jp.dm.${job}"
  src="${ROOT}/deploy/macos/${label}.plist"
  dest="${AGENTS}/${label}.plist"

  # __DM_ROOT__ を実際のパスへ置き換えて配置する
  sed "s|__DM_ROOT__|${ROOT}|g" "${src}" > "${dest}"

  # 登録済みなら一度外してから入れ直す（設定変更を確実に反映するため）
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "${dest}"
  echo "登録: ${label}"
done

echo
echo "登録しました。状態の確認:"
echo "  launchctl list | grep jp.dm"
echo
echo "すぐに1回試すには（配信はされません。dry-run です）:"
echo "  bash ${ROOT}/scripts/periodic_run.sh refresh"
echo
echo "ログ: ${ROOT}/state/logs/"
