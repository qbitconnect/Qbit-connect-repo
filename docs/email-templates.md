# Email Templates (Phase 7 §10–§12, §37, §38, §46)

## Anatomy

| Channel | Parts |
|---|---|
| EMAIL | `subject` + `html_body` + optional `text_body` (derived from HTML if absent) |
| WHATSAPP | `body` + provider approval state (`provider_status`) |

## Variables (§11)

`{{variable}}` placeholders only — **no code execution, no Jinja, no eval**.

Whitelist:

```
first_name, last_name, business_name, email, phone, city, state, country,
website,                              ← lead-derived
unsubscribe_url, company_name, company_address   ← system
```

Unknown variables fail template creation/update with a 422 listing them.
Missing values render as empty strings and are reported as preview warnings.

## Unsubscribe variable (§12–§13)

Every sent email receives a REAL `{{unsubscribe_url}}` built from a one-time
token (32 random bytes, SHA-256 hash stored, no ids encoded, single-use,
expiry-tracked). The platform never fabricates links. If the template does not
place `{{unsubscribe_url}}` explicitly, append it before launch — the wizard
warns when it is missing.

## HTML sanitization (§38)

`sanitize_html()` runs on every save/update:

- allowed tags: p, div, span, a, img, b/strong/i/em/u/s, h1–h6, ul/ol/li,
  table/thead/tbody/tr/td/th, hr, br, blockquote, pre, code, font, center, small
- removed: script, style, iframe, object, embed, form, input, link, meta, svg …
- removed: all `on*` event handlers, comments
- `href`/`src`: only `http(s):`, `mailto:` (a), `#` — `javascript:`, `data:`,
  protocol-relative are stripped
- `style` attribute: `expression()`, `javascript:`, `vbscript:`, `@import`,
  non-http `url()` are stripped

## Header injection (§39)

Subject/From/To/Reply-To are CRLF-validated at template validation AND provider
level; CR/LF in header positions is rejected (`MESSAGE_REJECTED`).

## Preview (§37)

`POST /api/v1/templates/{id}/preview` renders with a sample lead (Sagar /
Example Company) — never production recipient data — and returns warnings:
`EMPTY_SUBJECT`, `EMPTY_BODY`, `MISSING_VARIABLES:…`.

## API

```
GET    /api/v1/templates?channel=EMAIL
POST   /api/v1/templates
PATCH  /api/v1/templates/{id}
DELETE /api/v1/templates/{id}
POST   /api/v1/templates/{id}/preview
```

Permissions: `email.templates.view/manage` (channel-scoped, backend-enforced).
