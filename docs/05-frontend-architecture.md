# 05 — Frontend Architecture & UI Sitemap

> Covers required architecture docs: **Frontend Architecture** + **UI Sitemap**
> (Final Output S) + Design System (Brief §30)

## 1. Approach — SSR + HTMX (no Node toolchain)

The brief permits plain HTML/CSS/JS and forbids introducing Node.js without
architectural justification. QBIT therefore uses **server-rendered Jinja2 templates +
HTMX + Alpine.js + precompiled Tailwind CSS**:

- The FastAPI app serves full pages for navigation and **HTML fragments** (`/ui/...`)
  that HTMX swaps into tables, cards, drawers, and modals.
- Alpine.js handles small client state (dropdowns, confirm dialogs, tabs).
- Server-Sent Events (SSE) stream job/campaign progress into live-updating fragments;
  polling fallback at 5 s.
- No SPA build server, no Node runtime, no CDN dependency (vendor JS files locally).
- The JSON API (`/api/v1`) remains first-class, so a JS SPA can be added later without
  backend changes if ever justified.

## 2. UI Sitemap (Final Output S)

```
/login                              Login (dark, centered, QBIT branding)

/dashboard                          QBIT Control Center (widgets, KPIs, system health)

/scraping                           Scraper catalog (search + cards)
/scraping/:scraperId                Scraper detail → input form → validate → run
/scraping/jobs                      All scrape jobs (filters: status/source/date)
/scraping/jobs/:jobId               Job detail: progress, logs, results, cancel

/leads                              Lead database (dense table, filters, search)
/leads/:id                          Lead detail: contact, source, timeline, tags
/leads/import                       CSV/XLSX import wizard
/leads/export                       Export builder (selection → format)

/marketing                          Marketing home (channel cards)
/marketing/whatsapp                 Accounts health, templates, quick campaign
/marketing/email                    Email accounts, templates, quick campaign
/marketing/sms                      Future-ready placeholder (disabled state)

/campaigns                          Campaign list (status pipeline view)
/campaigns/:id                      Detail: audience, template, sends, analytics, controls

/connections                        Connection Center (categories)
/connections/whatsapp               Connect/test/disconnect WhatsApp accounts (multi)
/connections/email                  Connect/test/disconnect email accounts
/connections/storage                Storage backend status (local default)

/inbox                              Unified inbox (filters: all/WA/email/unread/assigned)
/inbox/:conversationId              Thread view + reply + assignment + close

/exports                            Export/files browser (size, source, date, download)

/analytics                          Event-based analytics (scraping, WA, email, campaigns)

/settings
/settings/users                     Users + roles assignment
/settings/security                  Password policy, sessions, audit log viewer
/settings/storage                   Data dir usage, retention, backup trigger
/settings/system                    System config, feature flags, health checks
```

## 3. Layout Shell

```
┌────────────────────────────────────────────────────────────┐
│ Topbar: QBIT ◆ global search · env badge · alerts · user   │
├──────────┬─────────────────────────────────────────────────┤
│ Sidebar  │  Page header (title + primary action)           │
│ Dashboard│  KPI strip (dense stat cards)                   │
│ Scraping │  Filters bar (search, status, date, source)     │
│ Leads    │  Content: dense tables / card grids             │
│ Marketing│  Drawers & modals for create/edit flows         │
│  ▸WA     │  Sticky job/campaign progress strip when active │
│  ▸Email  │                                                 │
│ Campaigns│                                                 │
│ Connections                                              │
│ Inbox    │                                                 │
│ Exports  │                                                 │
│ Analytics│                                                 │
│ Settings │                                                 │
└──────────┴─────────────────────────────────────────────────┘
```

## 4. QBIT Design System (Brief §30)

**Visual direction:** black/dark, premium, minimal, developer-tool aesthetic. Dense but
readable; card-based; subtle borders; strong typography; clear status indicators;
responsive down to tablet.

**Tokens**

| Token | Value | Usage |
|---|---|---|
| `--bg` | `#0A0A0B` | App background |
| `--surface` | `#121214` | Cards, panels |
| `--surface-2` | `#1A1A1E` | Hover, raised |
| `--border` | `#26262B` | Subtle 1px borders |
| `--text` | `#E7E7EA` | Primary text |
| `--muted` | `#9A9AA3` | Secondary text |
| `--accent` | `#3B82F6` | Primary actions, links |
| `--ok` | `#22C55E` | Completed / healthy |
| `--warn` | `#F59E0B` | Paused / attention |
| `--err` | `#EF4444` | Failed / critical |
| Radius | `8px` cards, `6px` inputs/buttons | |
| Font | Inter (vendored), `ui-monospace` for logs/IDs | |
| Spacing | 4px base scale; tables 12px cell padding | |

**Status indicators (consistent everywhere):** colored dot + label —
QUEUED (muted) · RUNNING (accent, pulse) · PAUSED (warn) · COMPLETED (ok) · FAILED
(err) · CANCELLED (muted strike).

**Component inventory (single source in `templates/partials/`):** cards, dense data
tables, filter bars, badges, buttons (primary/ghost/danger), modals, right-side
drawers, forms with inline validation, empty states, loading skeletons (HTMX
`htmx-indicator`), error banners, confirmation dialogs, toast stack, progress bars,
log viewers (monospace), and stat/KPI tiles. Every list view implements identical
interaction grammar: search → filter → sort → paginate → row action → detail.

**States mandated for every view:** loading, empty ("No jobs yet — run your first
scraper"), error (retry affordance), and permission-denied.

**Branding rule:** QBIT's own logo, wordmark, and palette only. Apify-style density is
the inspiration; Apify assets are never copied.
