# Workflow Builder UI (Phase 9 §43–§48, §70–§72)

The builder lives at **`/automation`** in the operator UI (cookie-session
auth, permission-gated — the UI is never the security boundary).

## Pages

| Route | Permission | Purpose (§) |
|---|---|---|
| `/automation` | `automation.view` | workflow list: name, trigger, status, version, executions, success, failures, last run + starter templates (§47) |
| `/automation/new` | `automation.create` | builder (blank or from a template) (§43) |
| `/automation/{id}` | `automation.view` | detail: overview stats, validation report, versions, definition flow, recent executions; publish/pause/resume/duplicate/archive/delete (§48) |
| `/automation/{id}/edit` | `automation.edit` | edit the draft |
| `/automation/executions` | `automation.view_executions` | execution monitor with counters + filters (§49) |
| `/automation/executions/{id}` | `automation.view_executions` | step timeline with skip reasons + errors (§50) |

## Builder design (§43–§45)

A **lightweight, dependency-free** builder — per §43 the spec forbids adding
a heavy graph library unnecessarily. The builder renders the definition as a
vertical list of node cards:

- **Trigger** — type selector (all 21 trigger types).
- **Condition** — field (from the catalog), operator, JSON value, YES/NO
  target node selectors.
- **Action** — action key select + JSON config + next-node target.
- **Wait** — duration JSON + next-node target.
- **End** — terminal.

"Add node" / "Remove" mutate the list; every edit re-serializes the
definition JSON which is submitted with the form and validated server-side
(client state is never trusted — §70). Full branch graphs (BRANCH nodes,
nested AND/OR/NOT groups, trigger config filters) remain available through
the REST API — the builder covers the common linear flows honestly and the
API covers everything.

## Validation display (§46)

The detail page shows the live validation report:

- **PASS**: structure valid (reachable, acyclic, paths end at END), all
  actions/conditions/triggers configured.
- **ERROR**: specific issues (missing action config, broken connection,
  invalid condition, missing/INACTIVE template, unknown sending target…).

Invalid workflows can never be published — the API rejects them (§21, §46).

## Persistence (§70)

Everything persists through the API (`POST/PATCH /api/v1/automation/…`):
drafts survive refresh and browser crashes; nothing lives only in browser
state.

## Realtime (§71)

Execution pages poll on reload (consistent with the project's existing
badge-polling realtime mechanism); no separate websocket layer was added.
