#!/usr/bin/env python3
"""`.env` を用意し、まだ書かれていない設定名を追記する。

`cp .env.example .env` では、すでに `.env` がある場合に何もできない。
先に `DM_CONTACTS_DIR` だけを追記したあと、残りの設定を入れたいことがあるため、
「足りない設定名だけを足す」方式にしてある。

- 既にある行は書き換えない。ユーザーが入れた値を失わない
- 値は一切表示しない。パスワードが画面やスクリーンショットに残らないようにする
- 実行後、どの設定がまだ雛形のままかを一覧で示す
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
ENV = ROOT / ".env"

KEY_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")

# 送信に必須の設定。欠けていると送信は fail-closed で止まる。
REQUIRED = [
    "DM_SENDER_NAME", "DM_SENDER_PERSON", "DM_SENDER_EMAIL", "DM_SENDER_REPLY_TO",
    "DM_SENDER_PHONE", "DM_SENDER_ADDRESS", "DM_SENDER_URL",
    "DM_UNSUBSCRIBE_BASE_URL", "DM_UNSUBSCRIBE_EMAIL", "DM_UNSUBSCRIBE_SECRET",
    "DM_SMTP_HOST", "DM_SMTP_PORT", "DM_SMTP_USER", "DM_SMTP_PASSWORD", "DM_SMTP_STARTTLS",
]
# フォーム送信で必要。送信者情報からは補えない項目のみ。
FORM_REQUIRED = [
    "DM_FORM_PERSON_SEI", "DM_FORM_PERSON_MEI", "DM_FORM_KANA_SEI", "DM_FORM_KANA_MEI",
]
SECRET_KEYS = {"DM_SMTP_PASSWORD", "DM_IMAP_PASSWORD", "DM_UNSUBSCRIBE_SECRET"}


def parse(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        m = KEY_RE.match(line)
        if m:
            found[m.group(1)] = line.split("=", 1)[1].strip().strip('"').strip("'")
    return found


def main() -> int:
    if not EXAMPLE.exists():
        print(f"雛形が見つかりません: {EXAMPLE}", file=sys.stderr)
        return 2

    example_text = EXAMPLE.read_text(encoding="utf-8")
    example = parse(example_text)

    if not ENV.exists():
        shutil.copyfile(EXAMPLE, ENV)
        ENV.chmod(0o600)
        print(f".env を雛形から作成しました（{len(example)}項目）")
        current = dict(example)
    else:
        current = parse(ENV.read_text(encoding="utf-8"))
        missing = [k for k in example if k not in current]
        if missing:
            backup = ENV.parent / ".env.bak"
            shutil.copyfile(ENV, backup)
            backup.chmod(0o600)
            with ENV.open("a", encoding="utf-8") as fh:
                fh.write("\n# ---- 雛形から追記（未記入）----\n")
                for key in missing:
                    fh.write(f'{key}="{example[key]}"\n')
                    current[key] = example[key]
            print(f"足りない設定を {len(missing)}件 追記しました（元の内容は .env.bak に退避）")
        else:
            print("設定名はすべて揃っています。追記はありません")
        ENV.chmod(0o600)

    def unset(key: str) -> bool:
        value = current.get(key, "")
        return value == "" or value == example.get(key, "")

    todo = [k for k in REQUIRED if unset(k)]
    form_todo = [k for k in FORM_REQUIRED if unset(k)]
    done = [k for k in REQUIRED + FORM_REQUIRED if not unset(k)]

    print()
    print(f"記入済み: {len(done)}項目")
    if todo or form_todo:
        print(f"要記入: {len(todo) + len(form_todo)}項目（雛形の値のままです）")
        for key in todo + form_todo:
            mark = "  ※値はチャットに貼らないでください" if key in SECRET_KEYS else ""
            print(f"  - {key}{mark}")
    else:
        print("要記入: なし。`.venv/bin/python -m dm.cli doctor --dns` へ進めます")

    print()
    print("編集する:  open -e .env")
    print("※この一覧に値は含まれていません。そのままチャットに貼って構いません")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
