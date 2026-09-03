# WhatsApp Templates (Phase 6 §8, §9, §10)

WhatsApp business-initiated messaging requires **provider-approved templates**.
QBIT Connect keeps its platform template representation (`campaign_templates`)
and extends it with provider fields — one unified model for local and
provider-synced templates.

## Template fields (§8)

| Field | Meaning |
|---|---|
| `origin` | `LOCAL` (authored in QBIT) or `PROVIDER` (synced from WhatsApp) |
| `provider_template_id` | provider-side id, kept available across renames (§9) |
| `provider_status` | `PENDING / APPROVED / REJECTED / PAUSED / DISABLED` |
| `category` | provider category, e.g. MARKETING / UTILITY / AUTHENTICATION |
| `components` | normalized provider components incl. placeholder counts |
| `account_id` | the sending account this provider template belongs to |
| `last_synced_at` | last synchronization timestamp |
| `rejected_reason` | provider's rejection reason, when present |

Platform status stays `DRAFT / ACTIVE / ARCHIVED`. For provider templates the
platform status mirrors usability (ACTIVE ⇔ provider APPROVED) unless an
operator manually archived the row.

## Synchronization (§9)

`POST /api/v1/connections/whatsapp/{id}/sync-templates` fetches the full
template catalog of the account's WABA (paginated, bounded) and upserts rows
by `(account_id, provider_template_id)`:

- provider fields are refreshed (status, category, components, language, body, rejected_reason)
- **operator customizations are preserved**: the `variables` mapping (lead
  field → placeholder) is never overwritten, and a manually ARCHIVED template
  is never resurrected
- `last_synced_at` is tracked per template

## Variable mapping (§10)

Provider templates reference positional placeholders `{{1}}, {{2}}, …`.
QBIT maps them to lead fields via `template.variables` — an ORDERED list of
renderable lead fields (allowlist): `first_name, last_name, contact_name,
business_name, company_name, city, state, country, category, industry`.

Example: template body `Hello {{1}}, welcome to {{2}}!` with
`variables: ["contact_name", "business_name"]` renders body parameters
`[{text: "Ravi"}, {text: "Acme Pvt Ltd"}]` per recipient.

Mapping rules enforced before launch:

- `len(variables)` must equal the total placeholder count (body + header)
- a recipient whose lead is missing a mapped value is SKIPPED with
  `MISSING_TEMPLATE_VARIABLE` — incomplete templates are never sent
- header placeholders consume the first N variables, body the rest, in order

## Approval requirement (§8/§10)

`CampaignService.validate()` calls the provider's
`validate_send_requirements(template, account_config)`. For WhatsApp:

- the template MUST be `origin=PROVIDER` (synced) — local templates cannot be
  used for WhatsApp campaigns
- `provider_status` MUST be `APPROVED` (PENDING/REJECTED/PAUSED/DISABLED block
  the launch with the exact status and rejection reason)
- `provider_template_id` must exist
- placeholder/variable counts must match
- the sending account must have `phone_number_id` configured

Failing any check blocks the launch with a clear, actionable error —
the campaign can never send an unapproved or mismatched template.

## Usage in a campaign (§33)

Campaign wizard (channel WhatsApp):

1. **Sending account** — select an ACTIVE WhatsApp account
2. **Template** — only templates synced to THAT account appear; the wizard
   labels each with its provider status (only APPROVED ones validate)
3. **Audience** — leads / saved view / tags / selection
4. **Eligibility preview** — Total / Eligible / Suppressed / Missing phone /
   No opt-in / Invalid phone / other restrictions (all real counts)
5. Review → Launch

Campaign detail (§34) then shows Account, Template, Audience, Total, Eligible,
Queued, Sent, Delivered, Read, Failed, Replies — every value computed from
actual recipient rows and provider events.
