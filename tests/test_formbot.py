"""フォーム自動送信の結合テスト。ローカルHTTPサーバに実物のフォームを立てて検証する。"""
from __future__ import annotations

import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright")

from dm.formbot import FormBrowser  # noqa: E402

BASIC_FORM = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>お問い合わせ</title></head>
<body><h1>お問い合わせ</h1>
<form method="post" action="/submit">
  <table>
    <tr><th><label for="c">会社名</label></th><td><input type="text" id="c" name="company" required></td></tr>
    <tr><th><label for="d">部署名</label></th><td><input type="text" id="d" name="busho"></td></tr>
    <tr><th><label for="n">お名前</label></th><td><input type="text" id="n" name="your_name" required></td></tr>
    <tr><th><label for="k">フリガナ</label></th><td><input type="text" id="k" name="kana"></td></tr>
    <tr><th><label for="e">メールアドレス</label></th><td><input type="email" id="e" name="email" required></td></tr>
    <tr><th><label for="e2">メールアドレス（確認用）</label></th>
        <td><input type="email" id="e2" name="email_confirm" required></td></tr>
    <tr><th><label for="t">電話番号</label></th><td><input type="tel" id="t" name="tel" required></td></tr>
    <tr><th><label for="z">郵便番号</label></th><td><input type="text" id="z" name="zip"></td></tr>
    <tr><th><label for="a">ご住所</label></th><td><input type="text" id="a" name="address"></td></tr>
    <tr><th><label for="s">件名</label></th><td><input type="text" id="s" name="subject"></td></tr>
    <tr><th>お問い合わせ種別</th><td>
      <select name="category" required>
        <option value="">選択してください</option>
        <option value="1">ご購入について</option>
        <option value="2">採用について</option>
        <option value="9">その他</option>
      </select></td></tr>
    <tr><th><label for="m">お問い合わせ内容</label></th>
        <td><textarea id="m" name="message" required></textarea></td></tr>
  </table>
  <p><label><input type="checkbox" name="agree" required> 個人情報の取り扱いに同意する</label></p>
  <p><input type="hidden" name="csrf" value="xyz">
     <button type="submit">送信する</button>
     <button type="reset">リセット</button></p>
</form></body></html>"""

CONFIRM_FORM = BASIC_FORM.replace('action="/submit"', 'action="/confirm2"').replace(
    "<button type=\"submit\">送信する</button>", "<button type=\"submit\">確認画面へ</button>"
)

CAPTCHA_FORM = BASIC_FORM.replace(
    "<p><input type=\"hidden\"",
    '<div class="g-recaptcha" data-sitekey="abc"></div><p><input type="hidden"',
)

NO_FORM = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>会社概要</title></head>
<body><h1>会社概要</h1><p>お問い合わせは電話でお願いします。</p></body></html>"""

SEARCH_ONLY = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>検索</title></head>
<body><form method="get" action="/search"><input type="text" name="q" placeholder="サイト内検索">
<button type="submit">検索</button></form></body></html>"""

received: list[dict[str, list[str]]] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # テスト出力を汚さない
        pass

    def _html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/robots.txt":
            body = "User-agent: *\nDisallow: /blocked/\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/contact", "/blocked/contact"):
            self._html(BASIC_FORM)
        elif path == "/confirm":
            self._html(CONFIRM_FORM)
        elif path == "/captcha":
            self._html(CAPTCHA_FORM)
        elif path == "/empty":
            self._html(NO_FORM)
        elif path == "/searchonly":
            self._html(SEARCH_ONLY)
        else:
            self._html("<html><body>not found</body></html>", 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        fields = urllib.parse.parse_qs(raw)
        path = urllib.parse.urlparse(self.path).path
        if path == "/confirm2":
            hidden = "".join(
                f'<input type="hidden" name="{k}" value="{v[0]}">' for k, v in fields.items()
            )
            self._html(
                '<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>確認</title></head><body>'
                "<h1>入力内容の確認</h1><p>以下の内容でよろしければ送信してください。</p>"
                f'<form method="post" action="/submit">{hidden}'
                '<button type="submit">送信する</button>'
                '<button type="submit" name="back" formaction="/confirm">戻る</button>'
                "</form></body></html>"
            )
            return
        received.append(fields)
        self._html(
            '<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>完了</title></head>'
            "<body><h1>送信完了</h1><p>お問い合わせありがとうございました。</p></body></html>"
        )


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def browser(settings):
    settings.ensure_dirs()
    with FormBrowser(settings, headless=True) as instance:
        yield instance


@pytest.fixture(autouse=True)
def _clear_received():
    received.clear()
    from dm import robots

    robots.clear_cache()


def test_dry_run_fills_without_submitting(browser, server):
    outcome = browser.process(f"{server}/contact", "ご案内", "本文です。", dry_run=True)
    assert outcome.status == "dryrun", outcome.detail
    assert received == []
    kinds = {v.split(":")[0] for v in outcome.filled.values()}
    assert {"company", "name", "email", "email_confirm", "phone", "message", "agree"} <= kinds


def test_live_submit_delivers_expected_values(browser, server, settings):
    outcome = browser.process(f"{server}/contact", "サービスのご案内", "本文です。", dry_run=False)
    assert outcome.status == "submitted", outcome.detail
    assert len(received) == 1
    posted = {k: v[0] for k, v in received[0].items()}
    assert posted["company"] == settings.form_profile.company
    assert posted["email"] == settings.form_profile.email
    assert posted["email_confirm"] == settings.form_profile.email
    assert posted["message"] == "本文です。"
    assert posted["subject"] == "サービスのご案内"
    assert posted["category"] == "9"        # 「その他」を選ぶ
    assert posted["agree"] == "on"
    assert posted["csrf"] == "xyz"          # hidden はそのまま送られる
    assert outcome.evidence and outcome.evidence.endswith(".png")


def test_confirmation_page_is_followed_through(browser, server):
    outcome = browser.process(f"{server}/confirm", "ご案内", "本文です。", dry_run=False)
    assert outcome.status == "submitted", outcome.detail
    assert len(received) == 1
    assert received[0]["message"][0] == "本文です。"


def test_captcha_page_is_skipped_not_solved(browser, server):
    outcome = browser.process(f"{server}/captcha", "ご案内", "本文です。", dry_run=False)
    assert outcome.status == "skipped_captcha"
    assert received == []


def test_page_without_form_is_skipped(browser, server):
    outcome = browser.process(f"{server}/empty", "ご案内", "本文です。", dry_run=False)
    assert outcome.status == "skipped_no_form"
    assert received == []


def test_search_only_page_is_not_treated_as_contact_form(browser, server):
    outcome = browser.process(f"{server}/searchonly", "ご案内", "本文です。", dry_run=False)
    assert outcome.status in ("skipped_no_form", "needs_review")
    assert received == []


def test_robots_disallowed_path_is_skipped(browser, server):
    outcome = browser.process(f"{server}/blocked/contact", "ご案内", "本文です。", dry_run=False)
    assert outcome.status == "skipped_robots"
    assert received == []


def test_unreachable_url_is_reported_as_failed(browser, server):
    outcome = browser.process(f"{server}/missing-page-xyz", "ご案内", "本文です。", dry_run=False)
    assert outcome.status in ("skipped_no_form", "failed")
    assert received == []
