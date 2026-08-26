"""問い合わせフォームへの自動入力・送信（Playwright）。

方針:
  - CAPTCHA が置かれているフォームには送信しない。回避も試みない。
    「自動送信を拒否している相手」と解釈し、スキップして人の判断に回す。
  - robots.txt で禁止されたパスには送信しない。
  - 判別できない必須項目があれば送信せず needs_review にする。
  - 送信前後のスクリーンショットとHTMLを証跡として保存する。
  - 既定は dry-run（入力するが送信ボタンは押さない）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import fieldmap, robots
from .config import Settings
from .normalize import host_of

# ページ内の入力欄を洗い出し、判定に使うヒントを集める。
# 要素には data-dm-field="<form>-<index>" を打ち、あとから確実に指し直せるようにする。
_COLLECT_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const nearbyText = (el) => {
    let node = el.parentElement;
    for (let i = 0; i < 5 && node; i++) {
      const text = clean(node.innerText || '');
      if (text && text.length <= 160) return text;
      node = node.parentElement;
    }
    return '';
  };
  const forms = Array.from(document.querySelectorAll('form'));
  return forms.map((form, fi) => {
    form.setAttribute('data-dm-form', String(fi));
    const fields = [];
    Array.from(form.querySelectorAll('input, textarea, select')).forEach((el, ei) => {
      const tag = el.tagName.toLowerCase();
      const type = (tag === 'input' ? (el.getAttribute('type') || 'text') : tag).toLowerCase();
      if (type === 'hidden' || type === 'image') return;
      const ref = fi + '-' + ei;
      el.setAttribute('data-dm-field', ref);
      let labelText = '';
      if (el.id) {
        try {
          const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
          if (lab) labelText = clean(lab.innerText);
        } catch (e) { /* 無効なidセレクタは無視 */ }
      }
      if (!labelText) {
        const parentLabel = el.closest('label');
        if (parentLabel) labelText = clean(parentLabel.innerText);
      }
      let options = [];
      if (tag === 'select') {
        options = Array.from(el.options).map(o => ({ value: o.value, text: clean(o.text) }));
      }
      const style = window.getComputedStyle(el);
      fields.push({
        ref, tag, type,
        name: el.getAttribute('name') || '',
        id: el.getAttribute('id') || '',
        placeholder: el.getAttribute('placeholder') || '',
        aria: el.getAttribute('aria-label') || '',
        title: el.getAttribute('title') || '',
        value: el.value || '',
        label: labelText,
        context: nearbyText(el),
        required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
        maxlength: el.getAttribute('maxlength') || '',
        visible: !!(el.offsetParent !== null || style.position === 'fixed'),
        checked: !!el.checked,
        options,
      });
    });
    return {
      index: fi,
      action: form.getAttribute('action') || '',
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      text: clean(form.innerText),
      fields,
    };
  });
}
"""

_BUTTON_JS = r"""
(formIndex) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const form = document.querySelector('form[data-dm-form="' + formIndex + '"]') || document;
  const nodes = Array.from(form.querySelectorAll(
    'button, input[type=submit], input[type=button], input[type=image], a[role=button]'
  ));
  return nodes.map((el, i) => {
    el.setAttribute('data-dm-button', String(i));
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    return {
      ref: String(i),
      type,
      text: clean(el.innerText || el.value || el.getAttribute('alt') || ''),
      name: el.getAttribute('name') || '',
      disabled: el.hasAttribute('disabled'),
    };
  });
}
"""


class PlaywrightUnavailable(RuntimeError):
    pass


@dataclass
class FormOutcome:
    status: str
    detail: str = ""
    evidence: str | None = None
    filled: dict[str, str] = field(default_factory=dict)
    unfilled_required: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("submitted", "dryrun")


def _hints(f: dict[str, Any]) -> tuple[str, ...]:
    return (f["label"], f["context"], f["placeholder"], f["aria"], f["title"], f["name"], f["id"])


def _pick_form(forms: list[dict[str, Any]]) -> dict[str, Any] | None:
    """問い合わせフォームらしいものを1つ選ぶ。textarea があるものを最優先。"""
    scored: list[tuple[int, dict[str, Any]]] = []
    for form in forms:
        visible = [f for f in form["fields"] if f["visible"]]
        if not visible:
            continue
        score = 0
        if any(f["tag"] == "textarea" for f in visible):
            score += 50
        if any(f["type"] == "email" or fieldmap.classify(*_hints(f)) and
               fieldmap.classify(*_hints(f)).kind == "email" for f in visible):
            score += 20
        if form["method"] == "post":
            score += 10
        score += min(len(visible), 15)
        # 検索フォーム・ログインフォームは除外
        blob = " ".join([form["action"], form["text"][:80]])
        if re.search(r"(search|検索|login|ログイン|newsletter|メルマガ登録)", blob, re.I):
            score -= 40
        scored.append((score, form))
    if not scored:
        return None
    scored.sort(key=lambda item: -item[0])
    best_score, best = scored[0]
    return best if best_score > 0 else None


def _values_for(profile, subject: str, body: str) -> dict[str, str]:
    return {
        "company": profile.company,
        "department": profile.department,
        "name": profile.full_name or profile.company,
        "name_sei": profile.person_sei,
        "name_mei": profile.person_mei,
        "kana": profile.full_kana,
        "email": profile.email,
        "email_confirm": profile.email,
        "phone": profile.phone,
        "fax": profile.fax or profile.phone,
        "zip": profile.zip,
        "prefecture": profile.prefecture,
        "address": profile.address,
        "url": profile.url,
        "subject": subject,
        "message": body,
    }


def _split_kana(kind: str, profile) -> str | None:
    """ふりがな欄が姓・名に分かれている場合の値。"""
    return {"kana_sei": profile.kana_sei, "kana_mei": profile.kana_mei}.get(kind)


class FormBrowser:
    """Playwright のブラウザを1つ開き、複数フォームを順に処理する。"""

    def __init__(self, settings: Settings, *, headless: bool = True) -> None:
        self.settings = settings
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None

    def __enter__(self) -> "FormBrowser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 実行環境依存
            raise PlaywrightUnavailable(
                "playwright が未インストールです。`pip install playwright` の後に "
                "`playwright install chromium` を実行してください。"
            ) from exc
        self._playwright = sync_playwright().start()
        # 環境によっては Playwright 同梱版と別の Chromium を使う必要がある
        executable = os.environ.get("DM_CHROMIUM_PATH") or None
        launch_args: dict[str, Any] = {"headless": self.headless}
        if executable:
            launch_args["executable_path"] = executable
        self._browser = self._playwright.chromium.launch(**launch_args)
        ua = self.settings.form_limits.user_agent or None
        self._context = self._browser.new_context(
            user_agent=ua,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1280, "height": 1600},
        )
        self._context.set_default_timeout(self.settings.form_limits.page_timeout_ms)
        return self

    def __exit__(self, *exc: Any) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # pragma: no cover
                pass
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------ 内部

    def _evidence_dir(self, url: str) -> Path:
        day = datetime.now().strftime("%Y%m%d")
        host = host_of(url).replace(":", "_") or "unknown"
        path = self.settings.evidence_dir / day / host
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _snapshot(self, page, directory: Path, label: str) -> str:
        stamp = datetime.now().strftime("%H%M%S")
        shot = directory / f"{stamp}_{label}.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            try:
                page.screenshot(path=str(shot))
            except Exception:
                return ""
        try:
            (directory / f"{stamp}_{label}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        return str(shot)

    def _has_captcha(self, page) -> bool:
        for selector in fieldmap.CAPTCHA_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _fill_form(self, page, form: dict[str, Any], subject: str, body: str) -> tuple[dict[str, str], list[str]]:
        profile = self.settings.form_profile
        values = _values_for(profile, subject, body)
        filled: dict[str, str] = {}
        unfilled_required: list[str] = []
        used_kinds: set[str] = set()
        radio_groups_done: set[str] = set()

        for f in form["fields"]:
            if not f["visible"]:
                continue
            selector = f'[data-dm-field="{f["ref"]}"]'
            locator = page.locator(selector).first
            hints = _hints(f)
            guess = fieldmap.classify(*hints)
            kind = guess.kind if guess else None

            if f["type"] in ("checkbox", "radio"):
                self._handle_choice(page, f, locator, hints, radio_groups_done, filled, unfilled_required)
                continue

            if f["tag"] == "select":
                self._handle_select(page, f, locator, filled, unfilled_required)
                continue

            if kind == "skip":
                if f["required"]:
                    unfilled_required.append(f["label"] or f["name"] or f["ref"])
                continue

            # ふりがなが姓・名に分かれているケース
            if kind == "kana" and "kana" in used_kinds:
                kind = "kana_mei"
            elif kind == "kana" and re.search(r"(姓|セイ|せい|last|sei)", " ".join(hints), re.I):
                kind = "kana_sei"

            value = values.get(kind or "") or _split_kana(kind or "", profile)

            # 種別が読めないが textarea なら本文とみなす
            if value is None and f["tag"] == "textarea" and "message" not in used_kinds:
                kind, value = "message", body

            if not value:
                if f["required"]:
                    unfilled_required.append(f["label"] or f["context"] or f["name"] or f["ref"])
                continue

            maxlength = int(f["maxlength"]) if str(f["maxlength"]).isdigit() else 0
            if maxlength and len(value) > maxlength:
                value = value[:maxlength]
            try:
                locator.fill(value)
            except Exception as exc:
                if f["required"]:
                    unfilled_required.append(f"{f['label'] or f['name']}（入力失敗: {exc}）")
                continue
            used_kinds.add(kind or "")
            filled[f["ref"]] = f"{kind}: {value[:40]}"

        return filled, unfilled_required

    def _handle_choice(self, page, f, locator, hints, radio_groups_done, filled, unfilled_required) -> None:
        blob = " ".join(hints)
        if f["type"] == "checkbox":
            # 同意チェックのみ操作する。それ以外の任意チェックには触れない。
            if fieldmap.is_agreement(blob):
                try:
                    if not f["checked"]:
                        locator.check()
                    filled[f["ref"]] = "agree: checked"
                except Exception:
                    if f["required"]:
                        unfilled_required.append(f["label"] or "同意チェック")
            elif f["required"] and not f["checked"]:
                unfilled_required.append(f["label"] or f["name"] or "必須チェック")
            return

        group = f["name"] or f["ref"]
        if group in radio_groups_done:
            return
        # ラジオは「その他」「ご提案」など、営業連絡に妥当な選択肢のみ選ぶ
        option_text = f["label"] or f["context"]
        if fieldmap.preferred_option([option_text or ""]) or fieldmap.is_agreement(blob):
            try:
                locator.check()
                radio_groups_done.add(group)
                filled[f["ref"]] = f"radio: {option_text[:30]}"
            except Exception:
                pass

    def _handle_select(self, page, f, locator, filled, unfilled_required) -> None:
        options = [o for o in f["options"] if o["value"] not in ("", None)]
        texts = [o["text"] for o in options]
        choice = fieldmap.preferred_option(texts)
        if choice is None:
            if f["required"] and options:
                unfilled_required.append(f["label"] or f["name"] or "選択項目")
            return
        target = next(o for o in options if o["text"] == choice)
        try:
            locator.select_option(value=target["value"])
            filled[f["ref"]] = f"select: {choice[:30]}"
        except Exception:
            if f["required"]:
                unfilled_required.append(f["label"] or f["name"] or "選択項目")

    def _click_submit(self, page, form_index: int, *, allow_confirm: bool) -> str | None:
        """押した本文を返す。押せる送信ボタンがなければ None。"""
        buttons = page.evaluate(_BUTTON_JS, str(form_index))
        candidates = [
            b for b in buttons
            if not b["disabled"]
            and (b["type"] in ("submit", "image") or fieldmap.SUBMIT_TEXT_RE.search(b["text"] or ""))
            and not fieldmap.NON_SUBMIT_TEXT_RE.search(b["text"] or "")
        ]
        if not candidates:
            return None
        # 「確認」より「送信」を優先。確認画面経由の場合のみ「確認」を許す。
        def rank(b: dict[str, Any]) -> int:
            text = b["text"] or ""
            if re.search(r"送信|送る|申し込|submit|send", text, re.I):
                return 0
            if allow_confirm and re.search(r"確認|次へ|confirm", text, re.I):
                return 1
            return 2

        candidates.sort(key=rank)
        button = candidates[0]
        locator = page.locator(f'[data-dm-button="{button["ref"]}"]').first
        try:
            locator.click()
        except Exception:
            return None
        try:
            page.wait_for_load_state("networkidle", timeout=self.settings.form_limits.page_timeout_ms)
        except Exception:
            pass
        return button["text"] or button["type"]

    # ------------------------------------------------------------------ 公開

    def process(self, url: str, subject: str, body: str, *, dry_run: bool = True) -> FormOutcome:
        limits = self.settings.form_limits
        if limits.respect_robots:
            allowed, reason = robots.allowed(url, robots.DEFAULT_UA)
            if not allowed:
                return FormOutcome(status="skipped_robots", detail=reason)

        page = self._context.new_page()
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=limits.page_timeout_ms)
            except Exception as exc:
                return FormOutcome(status="failed", detail=f"ページを開けません: {exc}")

            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            evidence_dir = self._evidence_dir(url)

            if self._has_captcha(page):
                shot = self._snapshot(page, evidence_dir, "captcha")
                return FormOutcome(
                    status="skipped_captcha",
                    detail="CAPTCHAが設置されているため自動送信しません（手動対応リストへ）",
                    evidence=shot,
                )

            forms = page.evaluate(_COLLECT_JS)
            form = _pick_form(forms)
            if form is None:
                shot = self._snapshot(page, evidence_dir, "noform")
                return FormOutcome(status="skipped_no_form", detail="入力可能なフォームが見つかりません", evidence=shot)

            filled, unfilled = self._fill_form(page, form, subject, body)
            if "message" not in " ".join(filled.values()):
                shot = self._snapshot(page, evidence_dir, "nomessage")
                return FormOutcome(
                    status="needs_review",
                    detail="本文を入れる欄を特定できませんでした",
                    evidence=shot,
                    filled=filled,
                )
            if unfilled:
                shot = self._snapshot(page, evidence_dir, "incomplete")
                return FormOutcome(
                    status="needs_review",
                    detail="必須項目を埋められませんでした: " + ", ".join(unfilled[:5]),
                    evidence=shot,
                    filled=filled,
                    unfilled_required=unfilled,
                )

            filled_shot = self._snapshot(page, evidence_dir, "filled")
            (evidence_dir / "filled.json").write_text(
                json.dumps({"url": url, "filled": filled}, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            if dry_run:
                return FormOutcome(
                    status="dryrun",
                    detail=f"入力のみ実施（{len(filled)}項目）。送信ボタンは押していません。",
                    evidence=filled_shot,
                    filled=filled,
                )

            clicked = self._click_submit(page, form["index"], allow_confirm=True)
            if clicked is None:
                return FormOutcome(status="needs_review", detail="送信ボタンを特定できません",
                                   evidence=filled_shot, filled=filled)

            page_text = page.inner_text("body")[:8000]

            # 確認画面が挟まる場合は、もう一度「送信」を押す
            if fieldmap.CONFIRM_PAGE_RE.search(page_text) and not fieldmap.SUCCESS_TEXT_RE.search(page_text):
                self._snapshot(page, evidence_dir, "confirm")
                if self._has_captcha(page):
                    return FormOutcome(status="skipped_captcha", detail="確認画面にCAPTCHAがあるため中止",
                                       evidence=filled_shot, filled=filled)
                page.evaluate(_COLLECT_JS)  # 確認画面のフォームに data 属性を打ち直す
                again = self._click_submit(page, 0, allow_confirm=False)
                if again is None:
                    return FormOutcome(status="needs_review", detail="確認画面の送信ボタンを特定できません",
                                       evidence=filled_shot, filled=filled)
                page_text = page.inner_text("body")[:8000]

            result_shot = self._snapshot(page, evidence_dir, "result")
            if fieldmap.SUCCESS_TEXT_RE.search(page_text):
                return FormOutcome(status="submitted", detail="送信完了を確認", evidence=result_shot, filled=filled)
            if fieldmap.ERROR_TEXT_RE.search(page_text):
                return FormOutcome(status="needs_review", detail="送信後にエラー表示を検出",
                                   evidence=result_shot, filled=filled)
            return FormOutcome(
                status="needs_review",
                detail="送信は行われたが完了表示を確認できません（証跡を確認してください）",
                evidence=result_shot,
                filled=filled,
            )
        finally:
            try:
                page.close()
            except Exception:
                pass
