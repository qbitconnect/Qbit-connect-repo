"""Email composer (Phase 7 §10–§12, §29–§31, §37–§40).

Turns a rendered template + recipient context into the final email payload:

    subject (CRLF-guarded)          §39
    html body  (sanitized + escaped substitution)   §38
    plain-text body (raw substitution)
    unsubscribe link (REAL endpoint + real token)   §12, §13
    open-tracking pixel (campaign opt-in)           §29
    click-tracking links (signed, http/https only)  §30

Hard rules:
- template HTML is sanitized with an email-safe allowlist (nh3); javascript:
  URLs, inline event handlers, script/style tricks and dangerous embeds never
  survive (§38)
- variable substitution into HTML is HTML-ESCAPED; into plain text it is raw
- tracking is optional and controlled per campaign (§31); nothing is claimed
  to be perfectly accurate — clients block/prefetch pixels
- the unsubscribe link is generated from QBIT_EMAIL_UNSUBSCRIBE_BASE_URL and a
  real one-time token — a fake link is NEVER generated (§12)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html as html_mod
import re
from urllib.parse import quote, urlsplit

import nh3

from app.services.marketing.providers.email.smtp import header_safe
#: NOTE: `render` is imported lazily inside render_parts — template.py imports
#: the sanitizer from this module, so a top-level import would be circular.

#: email-safe HTML allowlist (§38: "Allow normal email-safe HTML")
ALLOWED_TAGS = {
    "a", "abbr", "b", "bdo", "blockquote", "br", "caption", "center", "code",
    "div", "em", "figcaption", "figure", "font", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "i", "img", "li", "ol", "p", "pre", "q", "s", "small", "span",
    "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "tr", "u", "ul",
}
ALLOWED_ATTRIBUTES = {
    "*": {"class", "dir", "height", "width", "align", "valign", "style"},
    "a": {"href", "title", "target", "rel"},
    "img": {"src", "alt", "title"},
    "font": {"color", "face", "size"},
    "table": {"border", "cellpadding", "cellspacing", "bgcolor"},
    "td": {"bgcolor", "colspan", "rowspan"},
    "th": {"bgcolor", "colspan", "rowspan"},
}
ALLOWED_CSS_PROPERTIES = {
    "background-color", "border", "border-radius", "color", "display",
    "font-family", "font-size", "font-weight", "height", "line-height",
    "margin", "max-height", "max-width", "min-height", "min-width",
    "padding", "padding-bottom", "padding-left", "padding-right",
    "padding-top", "text-align", "text-decoration", "vertical-align",
    "white-space", "width",
}
ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}

#: only http/https links are click-rewritten (§30: no javascript:, no data:)
_REWRITABLE_SCHEMES = {"http", "https"}

_HREF_RE = re.compile(r'href\s*=\s*"([^"]*)"', re.IGNORECASE)
_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc\s*=\s*")([^"]*)(")', re.IGNORECASE)


def sanitize_html(raw_html: str) -> str:
    """Email-safe sanitization (§38). Never executes; strips scripts, inline
    handlers, unsafe embeds and dangerous redirects. link_rel is managed by
    nh3 (noopener noreferrer is added to links automatically)."""
    return nh3.clean(
        raw_html or "",
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        filter_style_properties=ALLOWED_CSS_PROPERTIES,
        link_rel=None,  # <a rel> is allowlisted; nh3 refuses defaults + rel together
    )


def escape_html_value(value: str | None) -> str:
    """HTML-escape a substitution value for safe interpolation into HTML."""
    return html_mod.escape(str(value if value is not None else ""), quote=True)


def sign_tracking(secret: str, *parts: str) -> str:
    """HMAC-SHA256 signature over the tracking URL parts (hex, trimmed)."""
    message = ":".join(parts).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()[:32]


def verify_tracking_signature(secret: str, signature: str, *parts: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign_tracking(secret, *parts), signature.strip().lower())


def safe_destination_url(raw_url: str) -> str | None:
    """Validate a redirect destination (§30): http/https only — no javascript:,
    no data:, no open-redirect tricks via scheme confusion."""
    try:
        parts = urlsplit(str(raw_url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in _REWRITABLE_SCHEMES or not parts.netloc:
        return None
    if any(ch in parts.scheme + parts.netloc for ch in ("\\", '"', " ", "<", ">")):
        return None
    return urlsplit(str(raw_url).strip()).geturl()


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</(p|div|tr|h[1-6]|li|table|blockquote)>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_ENTITY_RE = re.compile(r"&(#x?[0-9a-fA-F]+|[a-zA-Z]+);")


def html_to_text(raw_html: str) -> str:
    """Derive the plain-text fallback from an HTML body (§10).

    Deliberately simple + safe: block tags become line breaks, everything else
    is stripped, entities decoded, whitespace collapsed. Never executes
    anything; used only when the template has no explicit text body.
    """
    if not raw_html:
        return ""
    text = _BR_RE.sub("\n", raw_html)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)

    def _unescape(match: re.Match) -> str:
        import html as _html

        return _html.unescape(match.group(0))

    text = _ENTITY_RE.sub(_unescape, text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


class EmailComposer:
    """Builds final email payloads for one recipient (stateless)."""

    def __init__(self, *, secret_key: str, unsubscribe_base_url: str | None) -> None:
        self.secret_key = secret_key
        self.unsubscribe_base_url = (unsubscribe_base_url or "").rstrip("/")

    # -------------------------------------------------------------- rendering
    def render_parts(
        self, *, subject: str | None, html_body: str | None, text_body: str,
        values: dict[str, str | None],
    ) -> dict:
        """Render subject/html/text with safe substitution.

        `values` must already contain the per-recipient extras (unsubscribe_url
        etc.). HTML context gets escaped values; text context gets raw values.
        """
        from app.services.marketing.template import render

        subject_out = render(subject or "", values)
        if not header_safe(subject_out):
            # strip control characters instead of sending an injectable header
            subject_out = re.sub(r"[\r\n]+", " ", subject_out)
        html_out = None
        if html_body:
            escaped_values = {
                key: escape_html_value(value) for key, value in values.items()
            }
            html_out = sanitize_html(render(html_body, escaped_values))
        text_out = render(text_body or "", values)
        return {"subject": subject_out, "html": html_out, "text": text_out}

    # ------------------------------------------------------------ unsubscribe
    def unsubscribe_url_for(self, raw_token: str) -> str | None:
        """REAL unsubscribe link (§12) — None when no public base URL is
        configured (validation then reports the problem; no fake link)."""
        if not self.unsubscribe_base_url or not raw_token:
            return None
        return f"{self.unsubscribe_base_url}/unsubscribe/{quote(raw_token, safe='')}"

    def ensure_unsubscribe(
        self, *, html: str | None, text: str,
        unsubscribe_url: str | None,
    ) -> tuple[str | None, str, bool]:
        """Guarantee a functional unsubscribe mechanism where required (§12).

        If the rendered bodies already contain the link, nothing is appended.
        Otherwise a plain, honest footer is appended (HTML + text).
        Returns (html, text, appended).
        """
        if not unsubscribe_url:
            return html, text, False
        marker = unsubscribe_url.rstrip("/")
        already = bool(html and marker in html) or marker in text
        if already:
            return html, text, False
        html = (html or "") + (
            f'<div style="margin-top:24px;padding-top:12px;border-top:1px solid #ccc;'
            f'font-size:12px;color:#666;">'
            f'Don&#39;t want these emails? <a href="{unsubscribe_url}">Unsubscribe</a></div>'
        )
        text = text.rstrip() + f"\n\n---\nDon't want these emails? Unsubscribe: {unsubscribe_url}\n"
        return html, text, True

    # --------------------------------------------------------------- tracking
    def wrap_links(
        self, *, html: str, base_url: str, tracking_key: str,
        campaign_id: str, enabled: bool,
    ) -> tuple[str, int]:
        """Rewrite http/https links to signed tracking redirects (§30).

        Returns (html, rewritten_count). Non-http(s) schemes are left alone
        and are never redirectable (the redirect endpoint re-validates).
        """
        if not enabled or not base_url:
            return html, 0
        count = 0

        def _repl(match: re.Match) -> str:
            nonlocal count
            original = match.group(1)
            destination = safe_destination_url(original)
            if destination is None:
                return match.group(0)
            wrapped = self.click_url(
                base_url=base_url, tracking_key=tracking_key,
                campaign_id=campaign_id, destination=destination,
            )
            if wrapped is None:
                return match.group(0)
            count += 1
            return f'{match.group(0)[:match.group(0).lower().index("href")]}href="{wrapped}"'

        wrapped = _HREF_RE.sub(_repl, html)
        return wrapped, count

    def click_url(
        self, *, base_url: str, tracking_key: str,
        campaign_id: str, destination: str,
    ) -> str | None:
        destination = safe_destination_url(destination)
        if destination is None:
            return None
        encoded = base64.urlsafe_b64encode(destination.encode("utf-8")).decode("ascii").rstrip("=")
        signature = sign_tracking(self.secret_key, "click", tracking_key, encoded)
        return (
            f"{base_url}/api/v1/email/track/click/{quote(tracking_key, safe='')}"
            f"?u={encoded}&s={signature}"
        )

    def resolve_click(self, *, tracking_key: str, encoded: str,
                      signature: str) -> str | None:
        """Verify the signed click URL and return the destination (§30).
        Scheme is re-validated at redirect time — forged URLs never redirect."""
        if not verify_tracking_signature(
            self.secret_key, signature or "", "click", tracking_key or "", encoded or "",
        ):
            return None
        try:
            pad = "=" * (-len(encoded) % 4)
            destination = base64.urlsafe_b64decode((encoded + pad).encode("ascii")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        return safe_destination_url(destination)

    def open_pixel_url(self, *, base_url: str, tracking_key: str) -> str | None:
        if not base_url or not tracking_key:
            return None
        signature = sign_tracking(self.secret_key, "open", tracking_key)
        return f"{base_url}/api/v1/email/track/open/{quote(tracking_key, safe='')}?s={signature}"

    def inject_open_pixel(self, *, html: str, base_url: str,
                          tracking_key: str, enabled: bool) -> str:
        """Append the 1x1 tracking pixel when open tracking is on (§29)."""
        if not enabled or not html:
            return html
        pixel = self.open_pixel_url(base_url=base_url, tracking_key=tracking_key)
        if not pixel:
            return html
        return html + f'<img src="{pixel}" width="1" height="1" alt="" style="border:0;" />'

    # ---------------------------------------------------------------- preview
    def preview_warnings(self, *, subject: str | None, html: str | None,
                         text: str, unsubscribe_url: str | None,
                         used_variables: set[str], account_ready: bool) -> list[str]:
        """§37 preview warnings (honest, actionable)."""
        warnings: list[str] = []
        if not (subject or "").strip():
            warnings.append("Empty subject — the campaign will fail validation")
        if "{{" in (html or "") + text:
            warnings.append("Unrendered {{variables}} remain — check for missing values")
        if unsubscribe_url is None and self.unsubscribe_base_url:
            warnings.append("Missing unsubscribe link — a footer will be appended automatically")
        if unsubscribe_url is None and not self.unsubscribe_base_url:
            warnings.append(
                "QBIT_EMAIL_UNSUBSCRIBE_BASE_URL is not configured — "
                "no unsubscribe link can be generated (launch will be blocked)"
            )
        if html is None:
            warnings.append("No HTML body — recipients receive plain text only")
        if not account_ready:
            warnings.append("Sending account is not ACTIVE/HEALTHY")
        return warnings
