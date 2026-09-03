# Email Templates (Phase 7)

EMAIL templates reuse the Phase 5 template architecture (`CampaignTemplate`) with email-specific extensions.

## Structure

| Part | Storage | Notes |
|---|---|---|
| Subject | `subject` column | required for EMAIL; CRLF-guarded (§39) |
| HTML body | `body` column | **sanitized** with an email-safe allowlist at save time and again at render time (§38) |
| Plain-text body | `components.text` | optional; derived from the HTML when omitted (§10) |
| Variables | `variables` column | the `{{names}}` actually used |

## Variables (§11, §12)

Allowlist only — unknown variables **fail validation**; there is no expression language, no loops, no filters, and template content is never executed as code.

```
{{first_name}} {{last_name}} {{business_name}} {{city}} {{state}}
{{country}} {{category}} {{industry}} {{contact_name}}
{{email}} {{phone}} {{website}}
{{unsubscribe_url}}       ← REAL one-time opt-out link (never fake)
{{company_name}} {{company_address}}   ← from campaign metadata
```

- substitution into HTML is **HTML-escaped**; into plain text it is raw
- a recipient missing a field simply renders it as an empty string (free-form email, unlike provider templates) — missing values surface as PREVIEW warnings (§37), never as silent skips

## Example

Subject:
```
Hello {{first_name}} — partnership opportunity
```
HTML:
```html
<p>Hello {{first_name}},</p>
<p>We would like to discuss an opportunity with {{business_name}}.</p>
<p>Unsubscribe: {{unsubscribe_url}}</p>
```
Plain text:
```
Hello {{first_name}},

We would like to discuss an opportunity with {{business_name}}.

Unsubscribe: {{unsubscribe_url}}
```

## Unsubscribe guarantee (§12, §14)

- `{{unsubscribe_url}}` always resolves through the platform's REAL `/unsubscribe/{token}` endpoint with a cryptographically secure one-time token
- if the template lacks the variable, an honest footer (HTML + text) with the real link is appended automatically (campaign setting `append_unsubscribe_footer`, default on)
- launching an EMAIL campaign without `QBIT_EMAIL_UNSUBSCRIBE_BASE_URL` configured is **blocked** — the platform never generates fake unsubscribe links

## HTML sanitization (§38)

Stripped: `script`/`iframe`/`object`/`embed`, inline event handlers (`onclick`…), `javascript:`/`data:` URLs, unsafe CSS (e.g. `position:fixed`). Kept: normal email-safe HTML — tables, headings, links, images (http/https), inline styles within the allowlist.

## Header injection (§39)

`From` / `To` / `Reply-To` / `Subject` values are CR/LF-guarded at validation, render and send time; injection attempts are rejected (`MESSAGE_REJECTED`) before any bytes reach a provider.

## API (§45, §46)

```
GET    /api/v1/templates?channel=EMAIL
POST   /api/v1/templates            (channel=EMAIL, body=HTML, text_body=…)
PATCH  /api/v1/templates/{id}
DELETE /api/v1/templates/{id}       (in-use templates are archived, never destroyed)
POST   /api/v1/templates/{id}/preview   → html + text + §37 warnings
```

Preview warnings cover: unrendered variables, missing unsubscribe plan, empty subject, plain-text-only fallback. A sample lead (`lead_id`) or explicit `sample` values can be supplied — production recipient data is never used unnecessarily.
