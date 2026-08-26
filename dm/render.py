"""テンプレート描画。差し込み変数はここで一元的に組み立てる。"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from .config import Settings

# 「（株）〇〇」等の法人格表記から敬称付きの呼びかけを作るための整形
_HONORIFIC = "御中"


class RenderError(RuntimeError):
    pass


def unsubscribe_token(secret: str, address: str) -> str:
    digest = hmac.new(secret.encode(), address.lower().encode(), hashlib.sha256).hexdigest()
    return digest[:32]


def verify_unsubscribe_token(secret: str, address: str, token: str) -> bool:
    return hmac.compare_digest(unsubscribe_token(secret, address), (token or "").strip())


def unsubscribe_url(settings: Settings, address: str) -> str:
    if not settings.unsubscribe.base_url:
        return ""
    query = urlencode({"e": address, "t": unsubscribe_token(settings.unsubscribe.secret, address)})
    joiner = "&" if "?" in settings.unsubscribe.base_url else "?"
    return f"{settings.unsubscribe.base_url}{joiner}{query}"


def unsubscribe_mailto(settings: Settings, address: str) -> str:
    if not settings.unsubscribe.email:
        return ""
    subject = quote(f"配信停止希望 {address}")
    return f"mailto:{settings.unsubscribe.email}?subject={subject}"


def build_env(settings: Settings) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(settings.template_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


@dataclass
class Context:
    values: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return self.values


def company_salutation(company_name: str) -> str:
    name = (company_name or "").strip()
    if not name:
        return "ご担当者様"
    if name.endswith(("様", "御中", "殿")):
        return name
    return f"{name} {_HONORIFIC}"


def build_context(settings: Settings, contact: Any, campaign: Any, step: Any, channel: str) -> dict[str, Any]:
    address = str(contact["contact_email"] or "")
    sender = settings.sender
    return {
        "company_name": str(contact["company_name"] or ""),
        "salutation": company_salutation(str(contact["company_name"] or "")),
        "domain": str(contact["domain"] or ""),
        "official_url": str(contact["official_url"] or ""),
        "contact_email": address,
        "contact_form_url": str(contact["contact_form_url"] or ""),
        "rank": str(contact["rank"] or ""),
        "channel": channel,
        "campaign_key": campaign.key,
        "campaign_name": campaign.name,
        "step_key": step.key,
        "sender": {
            "name": sender.name,
            "person": sender.person,
            "email": sender.email,
            "reply_to": sender.reply_to or sender.email,
            "phone": sender.phone,
            "address": sender.address,
            "url": sender.url,
        },
        "unsubscribe_url": unsubscribe_url(settings, address) if channel == "email" else "",
        "unsubscribe_mailto": unsubscribe_mailto(settings, address) if channel == "email" else "",
        "unsubscribe_email": settings.unsubscribe.email,
    }


def render_string(env: Environment, source: str, context: dict[str, Any]) -> str:
    return env.from_string(source).render(**context)


def render_template(env: Environment, name: str, context: dict[str, Any]) -> str:
    try:
        return env.get_template(name).render(**context)
    except TemplateNotFound as exc:
        raise RenderError(f"テンプレートが見つかりません: {name}") from exc


@dataclass
class RenderedEmail:
    subject: str
    text: str
    html: str | None

    def body_hash(self) -> str:
        return hashlib.sha256((self.subject + "\n" + self.text).encode()).hexdigest()[:16]


@dataclass
class RenderedForm:
    subject: str
    body: str

    def body_hash(self) -> str:
        return hashlib.sha256((self.subject + "\n" + self.body).encode()).hexdigest()[:16]


def render_email(settings: Settings, env: Environment, contact: Any, campaign: Any, step: Any) -> RenderedEmail:
    context = build_context(settings, contact, campaign, step, "email")
    subject = render_string(env, step.subject, context).strip().replace("\n", " ")
    text = render_template(env, step.body_text, context)
    html = render_template(env, step.body_html, context) if step.body_html else None
    return RenderedEmail(subject=subject, text=text, html=html)


def render_form(settings: Settings, env: Environment, contact: Any, campaign: Any, step: Any) -> RenderedForm:
    context = build_context(settings, contact, campaign, step, "form")
    subject = render_string(env, step.form_subject or step.subject, context).strip().replace("\n", " ")
    body = render_template(env, step.form_body, context)
    return RenderedForm(subject=subject, body=body)
