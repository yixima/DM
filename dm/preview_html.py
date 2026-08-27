"""確認用プレビューHTMLの生成。

`dm preview --html <出力先>` から呼ばれる。実際に送られる文面をそのまま流し込み、
ブラウザで（あるいは共有ページとして）読める形にする。
文面を書き換えたら、これを作り直して目で確認する運用を想定している。
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .campaign import Campaign
from .config import Settings
from .render import build_env, render_email, render_form

EMAIL_RULE = "━" * 10
FORM_RULE = "─" * 10


def _split_footer(text: str, rule: str) -> tuple[str, str]:
    """本文と、自動生成される法令表示・署名ブロックを分ける。"""
    lines = text.rstrip().split("\n")
    for i, line in enumerate(lines):
        if rule in line:
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).rstrip()
    return text.rstrip(), ""


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _timing(delay_days: int, index: int) -> str:
    if index == 0:
        return "リスト登録後すぐ"
    if delay_days == 0:
        return "前の通知と同じ実行でも可"
    return f"前の通知から {delay_days} 日後"


STYLE = """
  :root {
    --ground:#faf8f4; --surface:#ffffff; --surface-alt:#f3f0e9;
    --ink:#221f1a; --ink-soft:#4b463d; --muted:#7d766a;
    --rule:#e2ddd2; --rule-strong:#cdc6b8;
    --accent:#1c4f70; --accent-soft:#e8f0f5;
    --warn:#8a5316; --warn-soft:#fbf1e2;
    --mail:#1c4f70; --form:#5c6b2f;
    --step: clamp(1.05rem, 0.9rem + 0.6vw, 1.3rem);
    --measure: 68ch;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground:#161513; --surface:#1e1d1a; --surface-alt:#262421;
      --ink:#ece8e0; --ink-soft:#c8c2b7; --muted:#948d81;
      --rule:#33302b; --rule-strong:#4a463f;
      --accent:#82b6d8; --accent-soft:#1b2b36;
      --warn:#d9a866; --warn-soft:#2e2618;
      --mail:#82b6d8; --form:#a8bd6e;
    }
  }
  :root[data-theme="dark"] {
    --ground:#161513; --surface:#1e1d1a; --surface-alt:#262421;
    --ink:#ece8e0; --ink-soft:#c8c2b7; --muted:#948d81;
    --rule:#33302b; --rule-strong:#4a463f;
    --accent:#82b6d8; --accent-soft:#1b2b36;
    --warn:#d9a866; --warn-soft:#2e2618;
    --mail:#82b6d8; --form:#a8bd6e;
  }
  * { box-sizing: border-box; }
  body {
    margin:0; background:var(--ground); color:var(--ink);
    font-family:"Zen Kaku Gothic New","Hiragino Kaku Gothic ProN","Yu Gothic",system-ui,sans-serif;
    font-size:16px; line-height:1.85; -webkit-font-smoothing:antialiased;
  }
  .wrap {
    max-width:1180px; margin:0 auto;
    padding: clamp(2rem,5vw,4.5rem) clamp(1rem,4vw,3rem) 5rem;
    display:flex; flex-direction:column; gap: clamp(2.5rem,5vw,4rem);
  }
  .masthead { display:flex; flex-direction:column; gap:1.25rem; }
  .eyebrow {
    font-size:.75rem; letter-spacing:.18em; text-transform:uppercase;
    color:var(--muted); font-weight:500; margin:0;
  }
  .masthead h1 {
    font-family:"Shippori Mincho B1","Hiragino Mincho ProN",serif; font-weight:700;
    font-size: clamp(1.9rem,1.4rem + 2.2vw,3rem); line-height:1.35; margin:0;
    text-wrap:balance; letter-spacing:.01em;
  }
  .lede { margin:0; max-width:var(--measure); color:var(--ink-soft); font-size:1.02rem; }
  .notice {
    background:var(--warn-soft);
    border:1px solid color-mix(in srgb, var(--warn) 28%, transparent);
    border-radius:3px; padding:1rem 1.15rem; max-width:var(--measure);
  }
  .notice strong { color:var(--warn); font-weight:700; }
  .notice p { margin:0; font-size:.95rem; color:var(--ink-soft); }
  .legend {
    display:grid; gap:1px; grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
    background:var(--rule); border:1px solid var(--rule); border-radius:3px; overflow:hidden;
  }
  .legend div { background:var(--surface); padding:1.1rem 1.25rem; }
  .legend dt { font-size:.72rem; letter-spacing:.14em; color:var(--muted); font-weight:700; margin:0 0 .4rem; }
  .legend dd { margin:0; font-size:.93rem; color:var(--ink-soft); line-height:1.7; }
  .legend code, footer.page-foot code {
    font-family:"M PLUS 1 Code",ui-monospace,monospace; font-size:.86em;
    color:var(--accent); background:var(--accent-soft); padding:.05em .35em; border-radius:2px;
  }
  .step { display:flex; flex-direction:column; gap:1.4rem; }
  .step-head {
    display:flex; align-items:baseline; gap:1.15rem;
    padding-bottom:.85rem; border-bottom:2px solid var(--rule-strong);
  }
  .step-mark { display:flex; flex-direction:column; gap:.2rem; flex:none; }
  .step-num {
    font-family:"Shippori Mincho B1",serif; font-weight:700;
    font-size:1.5rem; color:var(--accent); line-height:1;
  }
  .step-key { font-family:"M PLUS 1 Code",ui-monospace,monospace; font-size:.7rem; color:var(--muted); }
  .step-title { display:flex; flex-direction:column; gap:.15rem; }
  .step-title h2 {
    font-family:"Shippori Mincho B1","Hiragino Mincho ProN",serif;
    font-weight:500; font-size:var(--step); margin:0; line-height:1.4;
  }
  .timing { margin:0; font-size:.83rem; color:var(--muted); }
  .pair {
    display:grid; gap:1.5rem;
    grid-template-columns:repeat(auto-fit,minmax(370px,1fr)); align-items:start;
  }
  .doc {
    background:var(--surface); border:1px solid var(--rule); border-radius:3px;
    display:flex; flex-direction:column; overflow:hidden;
  }
  .doc-head {
    display:flex; flex-direction:column; gap:.75rem; padding:1.15rem 1.35rem;
    background:var(--surface-alt); border-bottom:1px solid var(--rule);
  }
  .chan {
    align-self:flex-start; font-size:.72rem; font-weight:700; letter-spacing:.12em;
    padding:.2rem .6rem; border-radius:2px; border:1px solid currentColor;
  }
  .chan-mail { color:var(--mail); }
  .chan-form { color:var(--form); }
  .subject { display:flex; flex-direction:column; gap:.25rem; }
  .field-label { font-size:.68rem; letter-spacing:.14em; color:var(--muted); font-weight:700; }
  .subject p { margin:0; font-weight:500; font-size:.97rem; line-height:1.6; color:var(--ink); text-wrap:balance; }
  .doc-body, .doc-foot { padding:1.35rem; overflow-x:auto; }
  .doc-foot {
    background:var(--accent-soft); border-top:1px dashed var(--rule-strong);
    display:flex; flex-direction:column; gap:.6rem;
  }
  .foot-label { font-size:.68rem; letter-spacing:.1em; color:var(--accent); font-weight:700; }
  pre {
    margin:0; font-family:"M PLUS 1 Code",ui-monospace,"SFMono-Regular",monospace;
    font-size:.83rem; line-height:1.95; white-space:pre-wrap; word-break:break-word;
    color:var(--ink-soft);
  }
  .doc-foot pre { font-size:.76rem; line-height:1.8; }
  .sample {
    display:flex; flex-wrap:wrap; gap:.4rem 1.5rem; font-size:.85rem; color:var(--muted);
    padding:.9rem 1.15rem; border:1px dashed var(--rule-strong); border-radius:3px;
    max-width:var(--measure);
  }
  .sample b { color:var(--ink-soft); font-weight:500; }
  footer.page-foot {
    border-top:1px solid var(--rule); padding-top:1.5rem;
    font-size:.85rem; color:var(--muted); display:flex; flex-direction:column; gap:.4rem;
  }
  footer.page-foot p { margin:0; }
  @media (max-width:640px) {
    .step-head { flex-direction:column; gap:.5rem; }
    .step-mark { flex-direction:row; align-items:baseline; gap:.6rem; }
  }
"""


def build_preview_html(
    settings: Settings,
    campaign: Campaign,
    contact: Any,
    *,
    channels: tuple[str, ...] = ("email", "form"),
) -> str:
    """1宛先ぶんの全ステップを、送信される文面そのままで組んだHTMLを返す。"""
    env = build_env(settings)
    sections: list[str] = []

    for index, step in enumerate(campaign.steps):
        panels: list[str] = []

        if "email" in channels and "email" in campaign.channels:
            rendered = render_email(settings, env, contact, campaign, step)
            body, footer = _split_footer(rendered.text, EMAIL_RULE)
            panels.append(
                f'<article class="doc">'
                f'<div class="doc-head"><span class="chan chan-mail">メール</span>'
                f'<div class="subject"><span class="field-label">件名</span>'
                f"<p>{_esc(rendered.subject)}</p></div></div>"
                f'<div class="doc-body"><pre>{_esc(body)}</pre></div>'
                f'<div class="doc-foot"><span class="foot-label">'
                f"法令表示ブロック（自動生成・原則そのまま）</span>"
                f"<pre>{_esc(footer)}</pre></div></article>"
            )

        if "form" in channels and "form" in campaign.channels:
            rendered = render_form(settings, env, contact, campaign, step)
            body, footer = _split_footer(rendered.body, FORM_RULE)
            panels.append(
                f'<article class="doc">'
                f'<div class="doc-head"><span class="chan chan-form">フォーム</span>'
                f'<div class="subject"><span class="field-label">件名欄</span>'
                f"<p>{_esc(rendered.subject)}</p></div></div>"
                f'<div class="doc-body"><pre>{_esc(body)}</pre></div>'
                f'<div class="doc-foot"><span class="foot-label">'
                f"署名ブロック（自動生成・原則そのまま）</span>"
                f"<pre>{_esc(footer)}</pre></div></article>"
            )

        if not panels:
            continue

        sections.append(
            f'<section class="step" id="{_esc(step.key)}">'
            f'<header class="step-head">'
            f'<div class="step-mark"><span class="step-num">{index + 1}通目</span>'
            f'<span class="step-key">{_esc(step.key)}</span></div>'
            f'<div class="step-title"><h2>{_esc(step.title or step.key)}</h2>'
            f'<p class="timing">{_esc(_timing(step.delay_days, index))}</p></div>'
            f"</header>"
            f'<div class="pair">{"".join(panels)}</div>'
            f"</section>"
        )

    sender_ready = not settings.sender.missing()
    sender_note = (
        "送信者情報は設定済みです。この署名がそのまま送られます。"
        if sender_ready
        else "送信者情報が未設定のため、署名は仮の値です。設定すると全通の署名が自動で入れ替わります。"
    )

    return f"""<title>{_esc(campaign.name)} 文面</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Shippori+Mincho+B1:wght@500;700&family=Zen+Kaku+Gothic+New:wght@400;500;700&family=M+PLUS+1+Code:wght@400;500&display=swap">
<style>{STYLE}</style>
<div class="wrap">
  <header class="masthead">
    <p class="eyebrow">{_esc(campaign.key)} ／ 現行テンプレート</p>
    <h1>{_esc(campaign.name)}</h1>
    <p class="lede">
      いまシステムに入っている文面の全文です。1宛先につき、上から順に1通ずつ届きます。
      メールとフォームで同じ内容を、それぞれの体裁に合わせて出し分けています。
    </p>
    <div class="notice"><p><strong>送信前の確認用です。</strong>
      ここに出ているのは、実際に送信される文字列そのものです。
      白い部分がテンプレート、青地の部分は送信者情報から自動生成される定型ブロックです。</p></div>
  </header>

  <dl class="legend">
    <div><dt>差し替えるところ</dt>
      <dd>各通の白い部分（本文）。<code>templates/</code> にあります。</dd></div>
    <div><dt>触らなくてよいところ</dt>
      <dd>青地の法令表示・署名ブロック。送信者情報から自動生成されます。</dd></div>
    <div><dt>宛先ごとに変わるところ</dt>
      <dd>社名などの差し込み。リストから自動で入ります。</dd></div>
    <div><dt>送信の間隔</dt>
      <dd>各通の見出しの下に表示。この日数は変更できます。</dd></div>
  </dl>

  <p class="sample">
    <span><b>差し込み例に使った宛先</b></span>
    <span>社名：{_esc(contact["company_name"])}</span>
    <span>メール：{_esc(contact["contact_email"]) or "（なし）"}</span>
    <span>フォーム：{_esc(contact["contact_form_url"]) or "（なし）"}</span>
  </p>

  {"".join(sections)}

  <footer class="page-foot">
    <p>{_esc(sender_note)}</p>
    <p>再生成：<code>dm preview --campaign {_esc(campaign.key)} --html &lt;出力先&gt;</code></p>
  </footer>
</div>
"""


def write_preview_html(
    path: Path,
    settings: Settings,
    campaign: Campaign,
    contact: Any,
    *,
    channels: tuple[str, ...] = ("email", "form"),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_preview_html(settings, campaign, contact, channels=channels), encoding="utf-8")
    return path
