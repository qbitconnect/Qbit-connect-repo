"""Email/WhatsApp template engine (Phase 7 §10–§12, §37, §38).

Security model:
- {{variable}} placeholders only — NO code execution, NO eval, NO Jinja.
- Variables must come from the declared whitelist (template.variables ∪
  SYSTEM_VARIABLES ∪ LEAD_VARIABLES); unknown placeholders fail validation.
- User HTML is sanitized through a BeautifulSoup allow-list sanitizer:
  script/style/iframe/event handlers/javascript: URLs are removed (§38).
- Unsubscribe links are REAL endpoints built from one-time tokens (§12–§13) —
  never fabricated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from bs4 import BeautifulSoup, Comment

from app.core.errors import ValidationError

VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

#: Always-available system variables (spec §11–§12)
SYSTEM_VARIABLES = (
    "unsubscribe_url",
    "company_name",
    "company_address",
)
#: Lead-derived personalization variables (spec §11)
LEAD_VARIABLES = (
    "first_name",
    "last_name",
    "business_name",
    "email",
    "phone",
    "city",
    "state",
    "country",
    "website",
)

ALLOWED_VARIABLES = frozenset(SYSTEM_VARIABLES) | frozenset(LEAD_VARIABLES)

# --- HTML sanitizer allow-lists (email-safe subset) --------------------------
ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "center", "code", "div", "em", "font", "h1",
    "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "s",
    "small", "span", "strong", "table", "tbody", "td", "th", "thead", "tr", "u", "ul",
}
DROP_TAGS = {
    "script", "style", "iframe", "object", "embed", "form", "input", "button",
    "link", "meta", "base", "svg", "video", "audio", "applet", "frame",
    "frameset", "noscript", "textarea", "select", "option",
}
ALLOWED_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "width", "height"},
    "td": {"colspan", "rowspan", "width", "bgcolor", "align", "valign"},
    "th": {"colspan", "rowspan", "width", "bgcolor", "align", "valign"},
    "table": {"width", "border", "cellpadding", "cellspacing", "bgcolor", "align"},
    "font": {"color", "size", "face"},
    "div": {"align"},
    "p": {"align"},
}
_GLOBAL_ATTRS = {"style"}
SAFE_URL_RE = re.compile(r"^(https?://|mailto:|#)", re.IGNORECASE)
UNSAFE_STYLE_RE = re.compile(
    r"(expression\s*\(|javascript\s*:|vbscript\s*:|@import|behavior\s*:|url\s*\(\s*['\"]?\s*(?!https?://|#|/))",
    re.IGNORECASE,
)


@dataclass
class RenderResult:
    subject: str
    html: str | None
    text: str | None
    missing_variables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def extract_variables(*parts: str | None) -> list[str]:
    """Ordered unique {{variable}} names used across the given parts."""
    seen: list[str] = []
    for part in parts:
        if not part:
            continue
        for match in VARIABLE_RE.finditer(part):
            name = match.group(1)
            if name not in seen:
                seen.append(name)
    return seen


def validate_variable_usage(*parts: str | None) -> list[str]:
    """Return unknown (non-whitelisted) variables used in the parts (§11)."""
    unknown = [v for v in extract_variables(*parts) if v not in ALLOWED_VARIABLES]
    return unknown


def substitute(text: str, values: dict) -> tuple[str, list[str]]:
    """Replace {{var}} with values. Missing values render as empty string and
    are reported as missing (spec §37 warnings). Any leftover brace sequences
    (non-whitelist syntax, expressions, class access) are scrubbed from the
    output — they can never be executed or rendered (spec §11, §38)."""
    missing: list[str] = []

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in values or values[name] is None:
            missing.append(name)
            return ""
        return str(values[name])

    replaced = VARIABLE_RE.sub(_replace, text)
    # defense-in-depth: neutralize any brace sequences the whitelist pass
    # could not interpret ({{ self.__class__ }}, {{ 7*7 }}, …)
    scrubbed = re.sub(r"\{\{.*?\}\}", "", replaced, flags=re.DOTALL)
    return scrubbed, missing


# ------------------------------------------------------------------ sanitizer
def sanitize_html(raw_html: str) -> str:
    """Allow-list sanitizer for user-provided email HTML (spec §38)."""
    soup = BeautifulSoup(raw_html or "", "html.parser")

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()

    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()

    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()  # keep text children of unknown tags
            continue
        allowed = ALLOWED_ATTRS.get(tag.name, set()) | _GLOBAL_ATTRS
        for attr_name in list(tag.attrs.keys()):
            if attr_name not in allowed:
                del tag.attrs[attr_name]
                continue
            value = tag.attrs[attr_name]
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            value = str(value)
            if attr_name in ("href", "src"):
                stripped = value.strip().replace("\n", "").replace("\r", "").replace("\t", "")
                if not SAFE_URL_RE.match(stripped):
                    del tag.attrs[attr_name]
                    continue
                tag.attrs[attr_name] = stripped
            elif attr_name == "style":
                if UNSAFE_STYLE_RE.search(value):
                    del tag.attrs[attr_name]
                    continue
                tag.attrs[attr_name] = value
    return str(soup)


def html_to_text(raw_html: str) -> str:
    """Plain-text fallback derivation (spec §10) — used when no text body given."""
    soup = BeautifulSoup(raw_html or "", "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"]):
        block.append("\n")
    text = soup.get_text()
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# ----------------------------------------------------------------- renderer
class TemplateRenderer:
    """Renders channel templates with personalization + unsubscribe links."""

    def __init__(self, *, unsubscribe_url: str | None, company_name: str, company_address: str) -> None:
        self.unsubscribe_url = unsubscribe_url
        self.company_name = company_name
        self.company_address = company_address

    def base_values(self) -> dict:
        return {
            "unsubscribe_url": self.unsubscribe_url or "",
            "company_name": self.company_name or "",
            "company_address": self.company_address or "",
        }

    def render(
        self,
        *,
        channel: str,
        subject: str | None,
        html_body: str | None,
        text_body: str | None,
        body: str | None,
        values: dict,
        sanitize: bool = True,
    ) -> RenderResult:
        merged = {**self.base_values(), **{k: v for k, v in (values or {}).items()}}
        warnings: list[str] = []

        if channel == "EMAIL":
            if not subject or not subject.strip():
                warnings.append("EMPTY_SUBJECT")
            rendered_subject, missing_sub = substitute(subject or "", merged)
            rendered_html = None
            rendered_text = None
            missing: list[str] = list(missing_sub)
            if html_body:
                raw_html, missing_html = substitute(html_body, merged)
                missing.extend(m for m in missing_html if m not in missing)
                rendered_html = sanitize_html(raw_html) if sanitize else raw_html
            if text_body:
                rendered_text, missing_txt = substitute(text_body, merged)
                missing.extend(m for m in missing_txt if m not in missing)
            elif rendered_html and not text_body:
                rendered_text, missing_txt = substitute(html_to_text(html_body), merged)
                missing.extend(m for m in missing_txt if m not in missing)
            if not rendered_text and not rendered_html:
                warnings.append("EMPTY_BODY")
            if missing:
                warnings.append("MISSING_VARIABLES:" + ",".join(sorted(set(missing))))
            return RenderResult(
                subject=rendered_subject,
                html=rendered_html,
                text=rendered_text or rendered_subject,
                missing_variables=sorted(set(missing)),
                warnings=warnings,
            )

        # WHATSAPP / SMS: single body, no sanitization needed
        rendered_body, missing = substitute(body or "", merged)
        if missing:
            warnings.append("MISSING_VARIABLES:" + ",".join(sorted(set(missing))))
        return RenderResult(subject="", html=None, text=rendered_body, missing_variables=sorted(set(missing)), warnings=warnings)


def ensure_template_variables(template_variables: Iterable[str]) -> None:
    """Raise when a template declares non-whitelisted variables (§11)."""
    unknown = [v for v in template_variables if v not in ALLOWED_VARIABLES]
    if unknown:
        raise ValidationError(
            f"Unknown template variables: {', '.join(unknown)}. "
            f"Allowed: {sorted(ALLOWED_VARIABLES)}"
        )
