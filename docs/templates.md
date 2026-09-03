# Templates

Marketing templates are per-channel message blueprints with strictly safe
variable substitution.

## Model

| Field | Notes |
|---|---|
| `name` | unique-ish label |
| `channel` | `WHATSAPP` \| `EMAIL` \| `SMS` (must match the campaign) |
| `subject` | required for EMAIL, forbidden elsewhere |
| `body` | message body with `{{variable}}` placeholders |
| `status` | `DRAFT` \| `ACTIVE` \| `ARCHIVED` |
| `language` | informational |
| `variables` | auto-derived list of variables used |

## Substitution rules (§9, §10)

1. **Identifiers only** — a placeholder must match `{{ identifier }}`
   (`[a-zA-Z_][a-zA-Z0-9_]*`). Anything else inside `{{ }}` (expressions,
   method chains, filters) is rejected at validation as *invalid template
   syntax* — template content is never executed as code.
2. **Allowlisted variables** — values come only from lead fields:

   `first_name`, `last_name`, `contact_name`, `business_name`,
   `company_name` (alias), `city`, `state`, `country`, `category`,
   `industry`.

   Unknown variables are reported as validation problems.
3. **Missing values render empty** — never an error mid-send, never a leak
   of the variable name or value map.
4. **Channel limits** — WHATSAPP ≤ 4096 chars / no subject; EMAIL requires a
   subject, ≤ 200k chars; SMS ≤ 1600 chars / no subject.

## API

```
GET    /api/v1/templates                  list (channel/status filters)
POST   /api/v1/templates                  create (validated)
GET    /api/v1/templates/{id}             detail
PATCH  /api/v1/templates/{id}             edit (re-validated)
DELETE /api/v1/templates/{id}             remove; ARCHIVED if referenced by a campaign
POST   /api/v1/templates/{id}/preview     render with a lead_id or inline sample
```

## Rendering at send time

For each queue item the worker renders the template against the recipient's
lead row (`render_from_lead`) and passes the composed message to the
provider. The render happens right before the send so the freshest lead data
is used while the recipient snapshot stays fixed.

## Deletion semantics

Templates referenced by any campaign are ARCHIVED instead of deleted —
launched campaigns must remain reproducible. Unreferenced templates are
removed outright.
