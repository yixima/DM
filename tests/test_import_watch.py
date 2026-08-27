"""監視フォルダからの自動取り込みと、宛先が急減したときの安全弁。"""
from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest

from dm.importer import ImportRefused, find_latest_csv, import_contacts, last_import

HEADER = ("company_name,official_url,domain,contact_email,contact_form_url,"
          "contact_type,rank,sources,evidence_url,flag_freemail,flag_domain_mismatch")


def make_csv(path: Path, count: int) -> Path:
    rows = [HEADER]
    for i in range(count):
        rows.append(
            f"会社{i},https://c{i}.example.jp/,c{i}.example.jp,a@c{i}.example.jp,"
            f"https://c{i}.example.jp/contact/,both,A,test,,,"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_latest_file_in_the_folder_is_chosen(tmp_path):
    folder = tmp_path / "lists"
    old = make_csv(folder / "master_contacts_20260101_090000.csv", 3)
    time.sleep(0.01)
    new = make_csv(folder / "master_contacts_20260201_090000.csv", 3)
    # 更新時刻を明示的にずらす
    import os
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))

    assert find_latest_csv(folder, "master_contacts_*.csv") == new


def test_missing_folder_is_reported(tmp_path):
    with pytest.raises(ImportRefused) as exc:
        find_latest_csv(tmp_path / "nope", "*.csv")
    assert "見つかりません" in str(exc.value)


def test_no_matching_file_is_reported(tmp_path):
    (tmp_path / "lists").mkdir()
    with pytest.raises(ImportRefused) as exc:
        find_latest_csv(tmp_path / "lists", "master_contacts_*.csv")
    assert "一致するファイルがありません" in str(exc.value)


def test_import_is_recorded(conn, tmp_path):
    path = make_csv(tmp_path / "a.csv", 5)
    import_contacts(conn, path)
    record = last_import(conn)
    assert record["contacts"] == 5
    assert record["email_targets"] == 5
    assert record["source_path"] == str(path)


def test_large_shrink_is_refused_without_writing_anything(conn, tmp_path):
    import_contacts(conn, make_csv(tmp_path / "full.csv", 100), max_shrink_percent=20)
    before = conn.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
    assert before == 100

    with pytest.raises(ImportRefused) as exc:
        import_contacts(conn, make_csv(tmp_path / "broken.csv", 10), max_shrink_percent=20)
    assert "90.0% 減っています" in str(exc.value)

    # 1件も書き換わっていないこと
    assert conn.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"] == before
    assert last_import(conn)["contacts"] == 100


def test_small_shrink_is_allowed(conn, tmp_path):
    import_contacts(conn, make_csv(tmp_path / "full.csv", 100), max_shrink_percent=20)
    summary = import_contacts(conn, make_csv(tmp_path / "slightly_less.csv", 90),
                              max_shrink_percent=20)
    assert summary["email_targets"] == 90


def test_force_overrides_the_guard(conn, tmp_path):
    import_contacts(conn, make_csv(tmp_path / "full.csv", 100), max_shrink_percent=20)
    summary = import_contacts(conn, make_csv(tmp_path / "broken.csv", 10),
                              max_shrink_percent=20, force=True)
    assert summary["email_targets"] == 10


def test_growth_is_never_refused(conn, tmp_path):
    import_contacts(conn, make_csv(tmp_path / "small.csv", 10), max_shrink_percent=20)
    summary = import_contacts(conn, make_csv(tmp_path / "grown.csv", 500), max_shrink_percent=20)
    assert summary["email_targets"] == 500


def test_first_import_has_nothing_to_compare(conn, tmp_path):
    summary = import_contacts(conn, make_csv(tmp_path / "first.csv", 3), max_shrink_percent=20)
    assert summary["contacts"] == 3


def test_contacts_dir_can_come_from_the_environment(monkeypatch, tmp_path):
    """.env に1行足すだけで監視フォルダを指定できる（設定ファイルの書き換え不要）。"""
    from dm.config import load_settings

    folder = tmp_path / "shared lists"
    folder.mkdir()
    monkeypatch.setenv("DM_CONTACTS_DIR", str(folder))
    monkeypatch.setenv("DM_CONTACTS_GLOB", "list_*.csv")

    settings = load_settings()
    assert settings.contacts_dir == folder
    assert settings.contacts_glob == "list_*.csv"


def test_contacts_dir_is_none_when_unset(monkeypatch):
    from dm.config import load_settings

    monkeypatch.delenv("DM_CONTACTS_DIR", raising=False)
    settings = load_settings()
    assert settings.contacts_dir is None


def test_tilde_in_the_path_is_expanded(monkeypatch):
    from dm.config import load_settings

    monkeypatch.setenv("DM_CONTACTS_DIR", "~/Shared/dm-lists")
    settings = load_settings()
    assert settings.contacts_dir is not None
    assert "~" not in str(settings.contacts_dir)
    assert settings.contacts_dir.is_absolute()
