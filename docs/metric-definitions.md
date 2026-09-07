# Metric Definitions (Phase 10 spec §28)

Every metric below documents its source table, calculation, filters,
timezone behaviour, denominator and missing-data behaviour. The SAME
definitions power the dashboard, the domain pages and saved reports — a
number can never mean two things in different places.

Global rules:
- All windows are `[start, end)` UTC instants derived from wall-clock dates
  in the report timezone (see `docs/analytics.md`).
- All filters from the request apply identically to every metric in the
  same response.
- A zero/empty denominator yields `None` (rendered "—"/"not available") —
  never a fabricated percentage. `0.0` is used only where the Phase 5
  service already established that convention (campaign rates).

## Leads

| Metric | Source | Definition |
|---|---|---|
| Total leads | `leads` | count of leads with `created_at` in period |
| New | `leads.status` | current status NEW |
| Valid (actionable) | `leads` | has `email_norm` OR `phone_norm` OR `website_norm` |
| Verified→Converted (reached) | `leads.status` | current status at-or-past the stage in the canonical order NEW→VERIFIED→QUALIFIED→CONTACTED→REPLIED→INTERESTED→CONVERTED (point-in-time facts; no invented transition history) |
| Lost / Archived | `leads.status` | current status |
| Duplicates merged | `leads.merged_into_id` | count not null |
| Avg quality | `leads.quality_score` | mean over non-null scores; None when no scores exist |
| Valid rate | derived | valid / total |

Funnel: stage counts are "currently at or past stage"; step conversion =
stage / previous stage; share of total = stage / total leads. LOST/
NOT_INTERESTED/ARCHIVED sit outside the funnel.

## Lead sources (spec §7)

| Metric | Source | Definition |
|---|---|---|
| Leads | `leads` | grouped by `source` (+ `source_type`) |
| Actionable | `leads` | count with email_norm or phone_norm |
| Duplicate rate | `leads.merged_into_id` | merged / total |
| Avg quality | `leads.quality_score` | mean of non-null |
| Qualified/Interested/Converted | `leads.status` | exact current-status counts |
| Scrape block | `scrape_jobs` | records found/accepted/duplicates/rejected + duplicate & rejection rates grouped by actor — present ONLY when the source maps to an actor; otherwise `null` ("not available") |

## Scraping (spec §8)

| Metric | Source | Definition |
|---|---|---|
| Jobs by status | `scrape_jobs.status` | counts in period |
| Running | status | RUNNING + PAUSED |
| Records collected/accepted | `records_saved` | sum |
| Duplicates / Rejected | `records_duplicate` / `records_failed` | sum |
| Success rate | derived | completed / (completed + failed) — cancelled excluded |
| Acceptance rate | derived | saved / found |
| Avg duration | derived | avg(completed_at − started_at) over jobs with both timestamps |
| Per-scraper rows | grouped by (`actor_id`, `actor_version`) | version performance is never blended |

## Marketing (spec §9–§10)

Event-stream metrics come from the immutable `campaign_events` table in the
period; recipient-status metrics from `campaign_recipients`.

| Metric | Source | Definition |
|---|---|---|
| Campaigns by status | `campaigns.status` | counts in period (created_at) |
| Recipients targeted | `campaign_recipients` | count in period |
| Sent/Delivered/Failed/Replied/Unsubscribed | `campaign_events` | count of MESSAGE_* events in period |
| Delivery rate | derived | delivered events / sent events |
| Failure rate | derived | failed / (sent + failed) |
| Reply rate | derived | replied / delivered |
| Unsubscribe rate | derived | unsubscribed / sent |

Campaign detail REUSES the Phase 5/7 service definitions (recipient-status
based) plus an event timeline — the campaign page and the analytics API
cannot disagree.

## WhatsApp (spec §11)

| Metric | Source | Definition |
|---|---|---|
| Sent/failed | `messages` (direction OUT, channel WHATSAPP) | `sent_at` / `failed_at` counts |
| Delivered | `messages.delivered_at` | count not null |
| **Read** | `messages.read_at` | count not null — written only by REAL read-event webhooks; if the provider never sent read events, reads are 0 and read_rate is None ("not available"), never inferred |
| Incoming/Outgoing | `messages.direction` | IN/OUT counts |
| Active conversations | `conversations.status` | OPEN/PENDING/WAITING |
| Per-account rates | joins `sending_accounts` | delivery = delivered/sent; failure = failed/(sent+failed); read_rate only when reads exist |

## Email (spec §12)

| Metric | Source | Definition |
|---|---|---|
| Sent / Delivered | `campaign_recipients.sent_at` / `.delivered_at` | counts (EMAIL channel) |
| Bounced / Complained | `.bounced_at` / `.complained_at` | counts |
| Hard/soft split | `campaign_events` MESSAGE_BOUNCED metadata | `bounce_type == HARD_BOUNCE` / `SOFT_BOUNCE` (same definition as the Phase 7 service) |
| **Opens / Clicks** | `.opened_at` / `.clicked_at` | EXIST only where real tracking events were recorded (opt-in per campaign). No tracking → rate None ("not available"), never estimated |
| Unsubscribed | `campaign_events` MESSAGE_UNSUBSCRIBED | count |
| Rates | derived | delivery = delivered/sent; bounce = bounced/sent; complaint = complained/sent; reply = replied/delivered; open = opened/delivered (only when opens > 0) |

## Inbox (spec §13)

| Metric | Source | Definition |
|---|---|---|
| Conversations by status/priority/channel/assignee | `conversations` | counts in period |
| Reopened | `conversation_events` CONVERSATION_REOPENED | count in period |
| Unread / Assigned / Unassigned | `conversations` | unread_count > 0 / assigned_user_id set or not |
| First response time | `messages` per conversation | min(OUT created_at) − min(IN created_at), ONLY over conversations having both; conversations missing either side are excluded, never imputed |
| Resolution time | `conversations` | closed_at − created_at over conversations with closed_at |
| Avg messages/conversation | `messages` | total messages / conversations (None when no conversations) |

## Automation (spec §15)

| Metric | Source | Definition |
|---|---|---|
| Executions by status | `workflow_executions` | counts in period |
| Success/failure rate | derived | completed or failed / total |
| Avg duration | derived | avg(completed_at − started_at) where both exist |
| Action steps | `workflow_execution_steps` | count of ACTION steps joined to in-period executions |
| Failure reasons | `workflow_executions.error` | first line, truncated to 120 chars, top-N |
| Read-only | — | analytics never executes or modifies workflows |

## Team (spec §14)

Attributed activity only — the caller passes the visibility-scoped user set
(ALL/ASSIGNED_ONLY per the established rules); an empty set yields an empty
table, never other users' data.

| Metric | Source | Definition |
|---|---|---|
| Leads created | `leads.created_by` | count in period |
| Leads updated / archived | `lead_activities` (user_id) | event_type updated/status / archived |
| Replies sent | `conversation_events` MESSAGE_SENT by actor | count (messages are account-attributed; user attribution uses the event trail only) |
| Conversations assigned | `conversation_events` ASSIGNED | count |
| Conversations resolved | `conversation_events` STATUS_CHANGED where `new_value.status` ∈ {RESOLVED, CLOSED} | count (payload matched in Python for portability) |

## Overview comparison (spec §4)

Every overview KPI carries `{current, previous, difference, change_pct}`.
`change_pct` is None when the previous period had zero events (undefined
division) — the UI renders "—". 'all' periods have no previous window.
