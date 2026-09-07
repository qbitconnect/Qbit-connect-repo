QBIT CONNECT — PHASE 9
AUTOMATION & WORKFLOW ENGINE
ENTERPRISE IMPLEMENTATION PROMPT

You are working on the existing QBIT Connect codebase.

IMPORTANT:
This is PHASE 9 ONLY.

PHASE 1–8 are already implemented.

PHASE 9 builds the Automation / Workflow Engine on top of:

- Scraping
- Lead Management
- Marketing Engine
- WhatsApp Business integration
- Email integration
- Unified Inbox
- Conversations
- Redis workers
- Existing RBAC
- Existing Audit Logs
- Existing Storage
- Existing Campaign/Event architecture

DO NOT rebuild previous phases.

DO NOT delete/reset existing data.

DO NOT introduce Node.js merely for convenience.

DO NOT introduce mandatory cloud storage.

DO NOT introduce a license/activation server.

DO NOT create fake automation execution.

--------------------------------------------------
PRIMARY OBJECTIVE
--------------------------------------------------

Build a modular enterprise Workflow Automation Engine.

The engine must allow:

TRIGGER
   ↓
CONDITION
   ↓
ACTION
   ↓
DELAY
   ↓
CONDITION
   ↓
ACTION
   ↓
END

Example:

New Lead
   ↓
Quality Score > 70?
   ↓ YES
Add Tag "High Quality"
   ↓
Wait 1 Day
   ↓
Has Email?
   ↓ YES
Send Email
   ↓
Wait 2 Days
   ↓
Has Customer Replied?
   ↓ YES
Assign Sales Team
   ↓
END

Another:

New WhatsApp Reply
   ↓
Conversation contains "price"
   ↓
Assign Sales Team
   ↓
Set Priority HIGH

The engine must be provider-independent.

--------------------------------------------------
STEP 0 — REPOSITORY AUDIT
--------------------------------------------------

Before coding inspect:

- Phase 4 Lead system
- Phase 5 Campaign engine
- Phase 6 WhatsApp
- Phase 7 Email
- Phase 8 Inbox
- Redis
- Workers
- Queue system
- Campaign events
- Conversation events
- Lead events
- RBAC
- Audit logging
- Existing scheduler
- Existing frontend component architecture

Create:

PHASE9_AUDIT.md

Document:

- reusable event architecture
- existing queues
- existing workers
- existing scheduling
- existing campaign actions
- existing conversation actions
- missing workflow functionality
- required database changes
- security risks
- implementation plan

Then implement Phase 9.

--------------------------------------------------
1. WORKFLOW DOMAIN
--------------------------------------------------

Create modular structure:

automation/
  core/
    workflow.py
    trigger.py
    condition.py
    action.py
    execution.py
    context.py
    schemas.py
    registry.py
    exceptions.py

  triggers/
  conditions/
  actions/
  services/
    workflow_service.py
    execution_service.py
    scheduler_service.py
    event_dispatcher.py

  workers/
    automation_worker.py

Do NOT create one giant automation.py file.

--------------------------------------------------
2. WORKFLOW MODEL
--------------------------------------------------

Create:

Workflow

Fields:

- id
- name
- description
- status
- version
- definition
- trigger_type
- created_by
- updated_by
- created_at
- updated_at
- published_at
- archived_at

Statuses:

DRAFT
ACTIVE
PAUSED
ARCHIVED

IMPORTANT:

Workflow versions must be immutable after publication.

If a workflow changes:

create a new version.

Do not silently change an active workflow's behavior.

--------------------------------------------------
3. WORKFLOW VERSION
--------------------------------------------------

Create:

WorkflowVersion

Fields:

- id
- workflow_id
- version
- definition
- checksum
- status
- created_by
- created_at
- published_at

Statuses:

DRAFT
PUBLISHED
RETIRED

A running execution must always reference the exact
workflow version that started it.

--------------------------------------------------
4. WORKFLOW DEFINITION
--------------------------------------------------

Represent workflow as a directed graph.

Example:

Trigger
  ↓
Condition
  ├── YES → Action
  │           ↓
  │         Delay
  │           ↓
  │         Action
  │
  └── NO → Action

Each node must have:

- id
- type
- configuration
- next_nodes

Do NOT execute arbitrary code from workflow definitions.

--------------------------------------------------
5. NODE TYPES
--------------------------------------------------

Initial node types:

TRIGGER
CONDITION
ACTION
DELAY
BRANCH
END

Future:

LOOP
SUB_WORKFLOW

Do not implement unnecessary future nodes now.

--------------------------------------------------
6. TRIGGER SYSTEM
--------------------------------------------------

Create:

BaseTrigger

Methods:

validate()
register()
handle_event()
describe()

Triggers should be pluggable.

--------------------------------------------------
7. LEAD TRIGGERS
--------------------------------------------------

Implement:

LEAD_CREATED
LEAD_UPDATED
LEAD_STATUS_CHANGED
LEAD_TAG_ADDED
LEAD_TAG_REMOVED
LEAD_IMPORTED
LEAD_SCRAPED

Example:

LEAD_CREATED
 ↓
Workflow

--------------------------------------------------
8. CAMPAIGN TRIGGERS
--------------------------------------------------

Support:

CAMPAIGN_COMPLETED
CAMPAIGN_FAILED
CAMPAIGN_RECIPIENT_REPLIED
CAMPAIGN_RECIPIENT_FAILED

Use existing campaign events.

--------------------------------------------------
9. CONVERSATION TRIGGERS
--------------------------------------------------

Support:

INBOUND_MESSAGE
CONVERSATION_CREATED
CONVERSATION_ASSIGNED
CONVERSATION_STATUS_CHANGED
CONVERSATION_REOPENED

Example:

INBOUND_MESSAGE
 ↓
Condition:
message contains "price"
 ↓
Assign Sales

--------------------------------------------------
10. MESSAGE TRIGGERS
--------------------------------------------------

Support normalized events:

MESSAGE_RECEIVED
MESSAGE_SENT
MESSAGE_DELIVERED
MESSAGE_FAILED

Do not create provider-specific workflow logic.

Workflow engine receives normalized events.

--------------------------------------------------
11. SCHEDULE TRIGGER
--------------------------------------------------

Support:

SCHEDULED

Examples:

Every day at configured time
Every hour
One-time execution

Use existing scheduler/automation infrastructure.

Do not create a second scheduler.

Respect timezone.

--------------------------------------------------
12. TRIGGER EVENT BUS
--------------------------------------------------

Architecture:

System Event
 ↓
Event Dispatcher
 ↓
Matching Workflows
 ↓
Execution Instance
 ↓
Queue
 ↓
Automation Worker

Events may include:

lead.created
lead.updated
lead.status_changed
lead.tag_added
campaign.completed
conversation.inbound_message
conversation.reopened

Do not directly execute workflow inside event-producing
HTTP request handlers.

--------------------------------------------------
13. EVENT IDEMPOTENCY
--------------------------------------------------

Every event should have:

event_id

Workflow trigger processing must be idempotent.

Same event delivered twice:

must NOT create duplicate workflow executions.

Use database uniqueness where appropriate.

--------------------------------------------------
14. CONDITION ENGINE
--------------------------------------------------

Create:

ConditionEngine

Conditions must be declarative.

Do NOT execute arbitrary Python/JavaScript expressions.

Supported operators:

equals
not_equals
contains
not_contains
starts_with
ends_with
is_empty
is_not_empty
greater_than
less_than
greater_or_equal
less_or_equal
between
in
not_in

--------------------------------------------------
15. LEAD CONDITIONS
--------------------------------------------------

Support fields:

lead.status
lead.quality_score
lead.source
lead.category
lead.industry
lead.city
lead.state
lead.country
lead.email
lead.phone
lead.website

Example:

quality_score > 70

Example:

status == "QUALIFIED"

--------------------------------------------------
16. TAG CONDITIONS
--------------------------------------------------

Conditions:

has_tag
does_not_have_tag

Example:

Lead has "Hot"

or:

Lead does not have "Contacted"

--------------------------------------------------
17. MESSAGE CONDITIONS
--------------------------------------------------

Support:

message.body contains
message.channel equals
message.direction equals
message.type equals

Example:

message.body contains "pricing"

--------------------------------------------------
18. CONVERSATION CONDITIONS
--------------------------------------------------

Support:

conversation.status
conversation.priority
conversation.channel
conversation.assigned_user
conversation.assigned_team
conversation.unread_count

--------------------------------------------------
19. CAMPAIGN CONDITIONS
--------------------------------------------------

Support:

campaign.status
recipient.status
delivery status
reply status

Example:

recipient replied = true

--------------------------------------------------
20. AND / OR GROUPS
--------------------------------------------------

Support:

AND
OR
NOT

Example:

quality_score > 70
AND
has_email = true
AND
status = NEW

Nested groups must be supported safely.

--------------------------------------------------
21. CONDITION VALIDATION
--------------------------------------------------

Workflow publish must validate:

- field exists
- operator supported
- value type correct
- node references valid
- no invalid branch
- no unreachable required node
- no circular execution unless explicitly supported
- action configuration valid

Invalid workflows cannot be activated.

--------------------------------------------------
22. ACTION SYSTEM
--------------------------------------------------

Create:

BaseAction

Methods:

validate()
execute()
describe()

Actions must be modular.

--------------------------------------------------
23. LEAD ACTIONS
--------------------------------------------------

Implement:

UPDATE_LEAD
CHANGE_STATUS
ADD_TAG
REMOVE_TAG
ADD_NOTE

Examples:

Change status → QUALIFIED

Add tag → HOT

Add note:

"Automatically qualified by workflow."

--------------------------------------------------
24. ASSIGNMENT ACTIONS
--------------------------------------------------

Implement:

ASSIGN_USER
ASSIGN_TEAM
UNASSIGN

Validate target exists.

Respect RBAC.

--------------------------------------------------
25. CONVERSATION ACTIONS
--------------------------------------------------

Implement:

CHANGE_CONVERSATION_STATUS
CHANGE_PRIORITY
ASSIGN_CONVERSATION
ADD_INTERNAL_NOTE

Example:

INBOUND_MESSAGE
 ↓
Contains "urgent"
 ↓
Set Priority URGENT
 ↓
Assign Support Team

--------------------------------------------------
26. COMMUNICATION ACTIONS
--------------------------------------------------

Implement architecture for:

SEND_WHATSAPP
SEND_EMAIL

These must reuse:

existing Campaign/Provider/Message architecture.

Do NOT duplicate provider logic inside automation.

Flow:

Automation
 ↓
Communication Action
 ↓
Eligibility
 ↓
Provider
 ↓
Queue
 ↓
Worker

--------------------------------------------------
27. COMMUNICATION SAFETY
--------------------------------------------------

Before communication action:

validate:

- recipient
- opt-in/eligibility
- suppression
- unsubscribe
- provider availability
- sending account
- template
- channel requirements

If invalid:

execution step = SKIPPED

with reason.

Never bypass campaign eligibility.

--------------------------------------------------
28. DELAY ACTION
--------------------------------------------------

Implement:

WAIT

Examples:

5 minutes
1 hour
1 day
3 days

Do NOT block workers during delay.

Bad:

sleep(86400)

Correct:

persist next_execution_at
↓
release worker
↓
scheduler requeues later

--------------------------------------------------
29. DELAY STATE
--------------------------------------------------

Execution:

RUNNING
 ↓
WAITING
 ↓
scheduled time
 ↓
QUEUED
 ↓
RUNNING

If application restarts:

workflow must resume.

Do not lose delayed executions.

--------------------------------------------------
30. BUSINESS HOURS
--------------------------------------------------

Prepare optional configuration:

respect_business_hours

If enabled:

action waits until configured business window.

Example:

Monday–Friday
09:00–18:00

Use configured timezone.

Do not assume India timezone globally.

--------------------------------------------------
31. BRANCHING
--------------------------------------------------

Implement:

IF / ELSE

Example:

IF email exists
    YES → Send Email
    NO  → Add Tag "No Email"

Support multiple condition branches.

--------------------------------------------------
32. EXECUTION MODEL
--------------------------------------------------

Create:

WorkflowExecution

Fields:

- id
- workflow_id
- workflow_version_id
- trigger_event_id
- entity_type
- entity_id
- status
- current_node_id
- context
- started_at
- completed_at
- next_execution_at
- error
- created_at
- updated_at

Statuses:

QUEUED
RUNNING
WAITING
COMPLETED
FAILED
CANCELLED
PAUSED

--------------------------------------------------
33. EXECUTION STEP
--------------------------------------------------

Create:

WorkflowExecutionStep

Fields:

- id
- execution_id
- node_id
- node_type
- status
- input_snapshot
- output_snapshot
- error
- started_at
- completed_at

Statuses:

PENDING
RUNNING
COMPLETED
SKIPPED
FAILED

Do not store secrets in snapshots.

--------------------------------------------------
34. WORKFLOW CONTEXT
--------------------------------------------------

Execution context may include:

lead
conversation
message
campaign
trigger event
variables

Example:

{
  "lead_id": "...",
  "conversation_id": "...",
  "trigger": "...",
  "variables": {}
}

Use references rather than copying huge objects.

--------------------------------------------------
35. VARIABLE SYSTEM
--------------------------------------------------

Support safe variables:

{{lead.first_name}}
{{lead.business_name}}
{{lead.email}}
{{lead.phone}}
{{lead.city}}

{{conversation.status}}

{{message.body}}

Do NOT allow:

arbitrary expressions
Python execution
JavaScript execution
shell commands

--------------------------------------------------
36. ACTION OUTPUT
--------------------------------------------------

Each action returns structured output.

Example:

{
  "status": "success",
  "result": {
    "tag_id": "..."
  }
}

Do not put sensitive provider responses into execution logs.

--------------------------------------------------
37. ERROR HANDLING
--------------------------------------------------

Each node must classify errors:

TRANSIENT
PERMANENT
CONFIGURATION
PERMISSION

Transient:

retry

Permanent:

fail/skip

Configuration:

workflow should require correction.

Permission:

fail safely and audit.

--------------------------------------------------
38. RETRY
--------------------------------------------------

Use existing Redis/job retry infrastructure.

Do not retry:

invalid lead
invalid email
unsubscribed
suppressed
invalid workflow configuration

Retry:

temporary provider failure
temporary database/network failure where safe

Use exponential backoff.

--------------------------------------------------
39. EXECUTION IDEMPOTENCY
--------------------------------------------------

Actions must be idempotent where practical.

Examples:

ADD_TAG twice
→ only one assignment

ASSIGN_TEAM twice
→ no duplicate assignment record

SEND_EMAIL:
must use existing messaging idempotency.

Do not send duplicate messages.

--------------------------------------------------
40. LOOP PROTECTION
--------------------------------------------------

Prevent infinite automation loops.

Example:

Lead updated
 ↓
Workflow
 ↓
Update Lead
 ↓
Lead updated
 ↓
Workflow
 ↓
...

Implement safeguards:

- workflow execution depth
- event causation ID
- max executions per entity/workflow/time window
- duplicate event detection

Do NOT disable legitimate events globally.

--------------------------------------------------
41. CAUSATION / CORRELATION
--------------------------------------------------

Track:

event_id
causation_id
correlation_id
workflow_execution_id

Example:

Lead Created
 ↓ event A
Workflow Execution
 ↓
Add Tag
 ↓ event B
B.causation_id = execution A

This makes debugging possible.

--------------------------------------------------
42. WORKFLOW LIMITS
--------------------------------------------------

Configure safe limits:

max_nodes_per_workflow
max_execution_time
max_steps_per_execution
max_retries
max_concurrent_executions
max_wait_duration

Prevent accidental infinite resource consumption.

--------------------------------------------------
43. WORKFLOW BUILDER UI
--------------------------------------------------

Create:

/automation

and:

/automation/new

Use visual node-based builder only if an existing frontend
graph library is already present or a lightweight safe
implementation is practical.

DO NOT add a huge dependency unnecessarily.

Builder:

TRIGGER
  ↓
CONDITION
  ↓
ACTION
  ↓
WAIT
  ↓
CONDITION
  ↓
ACTION

Node palette:

Triggers
Conditions
Actions
Delay
Branch
End

--------------------------------------------------
44. WORKFLOW BUILDER
--------------------------------------------------

User should be able to:

- add node
- delete node
- connect nodes
- configure node
- rename workflow
- validate workflow
- save draft
- publish
- pause
- duplicate
- archive

Prevent invalid connections.

--------------------------------------------------
45. NODE CONFIGURATION
--------------------------------------------------

Selecting a node opens configuration panel.

Example:

TRIGGER

Lead Created

CONDITION

Field:
Quality Score

Operator:
Greater Than

Value:
70

ACTION

Add Tag

Tag:
High Quality

DELAY

Duration:
1 Day

--------------------------------------------------
46. VISUAL VALIDATION
--------------------------------------------------

Before publish show:

PASS:

Trigger configured
All nodes reachable
All branches valid
All actions configured

ERROR:

Missing action configuration
Broken connection
Invalid condition
Missing template
Missing sending account

Do not publish invalid workflow.

--------------------------------------------------
47. WORKFLOW LIST
--------------------------------------------------

/automation

Show:

Name
Trigger
Status
Version
Executions
Last Run
Created
Updated

Actions:

Open
Duplicate
Pause
Resume
Archive

Do not show fake execution counts.

--------------------------------------------------
48. WORKFLOW DETAIL
--------------------------------------------------

/automation/{id}

Sections:

Overview
Workflow
Versions
Executions
Analytics
Activity

Actions:

Edit Draft
Publish
Pause
Resume
Duplicate
Archive

Published versions must remain viewable.

--------------------------------------------------
49. EXECUTION MONITOR
--------------------------------------------------

/automation/executions

Show:

Workflow
Lead
Trigger
Status
Current Step
Started
Duration
Error

Filters:

Workflow
Status
Date
Trigger
Lead

--------------------------------------------------
50. EXECUTION DETAIL
--------------------------------------------------

/automation/executions/{id}

Timeline:

Trigger
 ↓
Condition PASS
 ↓
Add Tag SUCCESS
 ↓
Wait 1 Day
 ↓
Email SKIPPED
Reason:
UNSUBSCRIBED

Show:

Input
Output
Status
Duration
Error

Never expose secrets.

--------------------------------------------------
51. AUTOMATION ANALYTICS FOUNDATION
--------------------------------------------------

Track:

Total executions
Successful
Failed
Waiting
Cancelled
Skipped

Per workflow:

execution count
success rate
failure rate
average duration
last execution

All data must be real.

Detailed analytics will be expanded in Phase 10.

--------------------------------------------------
52. AUTOMATION TEMPLATES
--------------------------------------------------

Provide optional starter workflow templates.

Examples:

1. New Lead Qualification
2. New WhatsApp Reply Assignment
3. New Email Reply Assignment
4. High Quality Lead Tagging
5. Unresponsive Lead Follow-up

IMPORTANT:

These are workflow definitions/templates.

Do NOT automatically activate them.

User must configure and publish.

--------------------------------------------------
53. COMMUNICATION FOLLOW-UP
--------------------------------------------------

Support architecture:

Wait 2 days
 ↓
Condition:
No reply
 ↓
Send Email

But enforce:

eligibility
suppression
unsubscribe
provider requirements

Never send to an opted-out recipient.

--------------------------------------------------
54. AUTOMATION + CAMPAIGN
--------------------------------------------------

Allow workflow action:

START_CAMPAIGN

BUT:

Do not create duplicate recipients.

Reuse existing campaign audience/recipient logic.

Before implementation verify whether starting a campaign
from automation is safe.

If not safely implementable:

leave action disabled and report it.

--------------------------------------------------
55. AUTOMATION + SCRAPING
--------------------------------------------------

Prepare event:

SCRAPE_JOB_COMPLETED

Workflow may:

Add tags
Update status
Create notes
Start campaign

Do not automatically launch campaigns without eligibility.

--------------------------------------------------
56. AUTOMATION + INBOX
--------------------------------------------------

Examples:

Inbound WhatsApp
 ↓
Condition message contains "price"
 ↓
Assign Sales
 ↓
Set priority HIGH
 ↓
Add note

Inbound Email
 ↓
Condition subject contains "quotation"
 ↓
Assign Sales Team

Do not auto-reply unless an explicit communication action
is configured.

--------------------------------------------------
57. SCHEDULER
--------------------------------------------------

Use existing scheduler infrastructure if available.

Support:

scheduled workflow trigger
delayed nodes
business-hour waits

Do not create multiple independent scheduler systems.

Delayed jobs must survive:

restart
worker crash
deployment

--------------------------------------------------
58. BACKGROUND WORKER
--------------------------------------------------

Automation worker:

Queue
 ↓
Load execution
 ↓
Lock execution
 ↓
Load workflow version
 ↓
Execute current node
 ↓
Persist result
 ↓
Schedule next node
 ↓
Release lock

Use safe locking.

Prevent two workers executing the same step concurrently.

--------------------------------------------------
59. DATABASE LOCKING
--------------------------------------------------

Implement safe execution claiming.

Example:

execution
status = QUEUED

Worker A claims

Worker B must not also execute.

Use appropriate:

row locks
state transition
lease/lock strategy

Do not rely only on Redis locks.

--------------------------------------------------
60. WORKFLOW VERSIONING
--------------------------------------------------

Important:

Workflow v1:

Lead Created
→ Add Tag

Later edit:

Lead Created
→ Condition
→ Send Email

This becomes:

Workflow v2

Existing executions continue using v1.

New executions use v2.

Never change behavior of a running execution.

--------------------------------------------------
61. RBAC
--------------------------------------------------

Add/use:

automation.view
automation.create
automation.edit
automation.publish
automation.pause
automation.resume
automation.execute
automation.delete
automation.view_executions

Only authorized users may publish workflows.

--------------------------------------------------
62. AUDIT LOG
--------------------------------------------------

Record:

workflow created
workflow edited
workflow published
workflow paused
workflow resumed
workflow archived
workflow duplicated
workflow execution started
workflow execution failed
workflow execution completed

Do not log secrets.

--------------------------------------------------
63. SECURITY
--------------------------------------------------

Critical:

DO NOT allow workflow definitions to execute:

Python
JavaScript
shell
SQL
arbitrary HTTP requests

No:

eval()
exec()
subprocess
dynamic code execution

Workflow engine must be declarative.

--------------------------------------------------
64. HTTP ACTION
--------------------------------------------------

DO NOT implement arbitrary HTTP request action in Phase 9.

If future external webhook action is required:

create a tightly controlled provider/integration system
with:

allowlisted domains
SSRF protection
timeouts
authentication abstraction
audit logs

Leave it for a future phase.

--------------------------------------------------
65. SECURITY TESTING
--------------------------------------------------

Test:

workflow injection
template injection
XSS
SQL injection
IDOR
RBAC bypass
unauthorized execution
unauthorized publishing
infinite loops
duplicate execution
duplicate action
duplicate email
duplicate WhatsApp message
invalid workflow graph
invalid node reference
malicious variable
oversized workflow
excessive execution depth
resource exhaustion

--------------------------------------------------
66. PERFORMANCE
--------------------------------------------------

Test:

10,000 workflows
100,000 executions

Verify:

- workflow lookup indexed
- event dispatch efficient
- execution claiming safe
- delayed jobs efficient
- no N+1 queries
- execution history pagination
- worker throughput reasonable

Do not run synthetic load against production.

--------------------------------------------------
67. DATABASE
--------------------------------------------------

Use Alembic.

Potential tables:

workflows
workflow_versions
workflow_executions
workflow_execution_steps
workflow_events
workflow_schedules

Only create tables that do not already exist.

Do not duplicate existing event tables unnecessarily.

--------------------------------------------------
68. INDEXES
--------------------------------------------------

Consider:

workflows.status
workflows.trigger_type

workflow_versions.workflow_id
workflow_versions.version

workflow_executions.workflow_id
workflow_executions.status
workflow_executions.entity_type
workflow_executions.entity_id
workflow_executions.next_execution_at
workflow_executions.created_at

execution_steps.execution_id
execution_steps.status

workflow_events.event_id
workflow_events.created_at

Use uniqueness constraints for idempotency.

--------------------------------------------------
69. API
--------------------------------------------------

Implement:

GET
/api/v1/automation/workflows

POST
/api/v1/automation/workflows

GET
/api/v1/automation/workflows/{id}

PATCH
/api/v1/automation/workflows/{id}

POST
/api/v1/automation/workflows/{id}/validate

POST
/api/v1/automation/workflows/{id}/publish

POST
/api/v1/automation/workflows/{id}/pause

POST
/api/v1/automation/workflows/{id}/resume

POST
/api/v1/automation/workflows/{id}/duplicate

POST
/api/v1/automation/workflows/{id}/archive

GET
/api/v1/automation/workflows/{id}/versions

GET
/api/v1/automation/executions

GET
/api/v1/automation/executions/{id}

POST
/api/v1/automation/executions/{id}/cancel

GET
/api/v1/automation/templates

--------------------------------------------------
70. UI API INTEGRATION
--------------------------------------------------

Builder must persist workflow definition through API.

Do not store workflow only in browser state.

Drafts must survive refresh.

Autosave can be added if reliable.

--------------------------------------------------
71. REALTIME EXECUTION
--------------------------------------------------

Use existing WebSocket/SSE mechanism.

Execution updates:

queued
running
waiting
completed
failed

Builder/detail page can show live execution status.

Do not introduce another realtime architecture.

--------------------------------------------------
72. NOTIFICATIONS
--------------------------------------------------

Optional in-app notifications:

workflow failed
workflow completed
high failure rate

Do not send external notifications yet unless existing
notification architecture supports them.

--------------------------------------------------
73. DATA PRIVACY
--------------------------------------------------

Workflow context may reference:

lead
phone
email
messages

Do not copy unnecessary personal data into execution logs.

Prefer IDs/references.

Mask sensitive fields where appropriate.

--------------------------------------------------
74. TESTING
--------------------------------------------------

Write tests for:

Workflow creation
Workflow editing
Workflow versioning
Workflow validation
Workflow publishing
Workflow pause
Workflow resume
Workflow archive
Trigger dispatch
Lead trigger
Campaign trigger
Conversation trigger
Message trigger
Scheduled trigger
Conditions
AND
OR
NOT
Branching
Actions
Lead actions
Assignment
Conversation actions
WhatsApp action
Email action
Delay
Business hours
Retry
Idempotency
Loop protection
Execution locking
Worker restart
Workflow version behavior
RBAC
Audit
Security

--------------------------------------------------
75. IMPORTANT TEST CASES
--------------------------------------------------

TEST 1:

Lead Created
 ↓
Add Tag
 ↓
Complete

Expected:
one execution.

TEST 2:

Lead Created twice with same event_id.

Expected:
one execution.

TEST 3:

Workflow v1 running.

User publishes v2.

Expected:
existing execution continues on v1.

TEST 4:

Workflow:

Lead Updated
 ↓
Update Lead

Expected:
loop protection prevents infinite recursion.

TEST 5:

Send Email action
+
Unsubscribed lead

Expected:
SKIPPED
reason = UNSUBSCRIBED

No email sent.

TEST 6:

Wait 1 day.

Restart application.

Expected:
execution remains WAITING and resumes correctly.

TEST 7:

Two workers claim same execution.

Expected:
only one executes step.

TEST 8:

Condition:

quality_score > 70
AND
has_email = true

Expected:
correct branching.

TEST 9:

Unauthorized operator attempts publish.

Expected:
403/permission denial.

--------------------------------------------------
76. OBSERVABILITY
--------------------------------------------------

Structured logs:

workflow_id
workflow_version_id
execution_id
execution_step_id
trigger_event_id
entity_id
node_id
action
status

Never log:

API keys
provider tokens
passwords
cookies
message secrets

--------------------------------------------------
77. FAILURE RECOVERY
--------------------------------------------------

If worker crashes:

Execution must be recoverable.

If action state is uncertain:

do not blindly repeat external communication.

Use idempotency and persisted state.

Provider communication actions must reuse existing
message idempotency.

--------------------------------------------------
78. DATA SAFETY
--------------------------------------------------

CRITICAL:

DO NOT:

DROP DATABASE
TRUNCATE workflows
DELETE campaigns
DELETE leads
DELETE conversations
DELETE messages
RESET migrations
DELETE users
DELETE settings
DELETE existing connections

If destructive migration is required:

STOP and report:

BLOCKED — destructive migration required

--------------------------------------------------
79. DOCUMENTATION
--------------------------------------------------

Create/update:

docs/automation.md
docs/workflow-builder.md
docs/workflow-triggers.md
docs/workflow-conditions.md
docs/workflow-actions.md
docs/workflow-execution.md
docs/automation-security.md

Document:

- architecture
- workflow lifecycle
- triggers
- conditions
- actions
- delays
- versioning
- idempotency
- loop prevention
- retry
- RBAC
- troubleshooting

--------------------------------------------------
80. VERSION
--------------------------------------------------

Use the existing project versioning system.

Increment appropriately for Phase 9.

Do NOT create another version source.

--------------------------------------------------
81. GIT
--------------------------------------------------

Before commit:

git status

Check:

- .env
- credentials
- tokens
- API keys
- cookies
- local DB
- temporary files
- build artifacts

Run:

backend tests
frontend tests
lint
type checks
migration checks

Then:

git add .

git commit -m "feat(qbit-connect): add workflow automation engine"

If intended remote/branch is configured:

git push

Never force push.

If push fails:

report exact reason.

--------------------------------------------------
82. FINAL VERIFICATION
--------------------------------------------------

Verify:

1. Application starts.
2. Authentication works.
3. RBAC works.
4. Leads work.
5. Campaigns work.
6. WhatsApp works.
7. Email works.
8. Inbox works.
9. Workflow creation works.
10. Workflow validation works.
11. Workflow publishing works.
12. Workflow versioning works.
13. Lead triggers work.
14. Campaign triggers work.
15. Conversation triggers work.
16. Message triggers work.
17. Scheduled triggers work.
18. Conditions work.
19. AND/OR/NOT works.
20. Branching works.
21. Lead actions work.
22. Assignment works.
23. Conversation actions work.
24. WhatsApp action uses existing provider architecture.
25. Email action uses existing provider architecture.
26. Eligibility is enforced.
27. Suppression is enforced.
28. Unsubscribe is enforced.
29. Delay survives restart.
30. Retry works.
31. Idempotency works.
32. Loop protection works.
33. Worker locking works.
34. Execution history works.
35. Realtime updates work if existing realtime system exists.
36. Audit logs work.
37. Security tests pass.
38. No secrets are exposed.
39. No production data was deleted.
40. Tests pass.
41. Version updated.
42. Git commit created.
43. Git push succeeds if configured.

--------------------------------------------------
83. NON-GOALS
--------------------------------------------------

DO NOT implement:

- arbitrary code execution
- Python execution
- JavaScript execution
- shell execution
- arbitrary HTTP request nodes
- stealth automation
- anti-ban systems
- CAPTCHA bypass
- provider restriction bypass
- spam-filter bypass
- automatic consent generation
- unauthorized messaging
- AI autonomous agent behavior

--------------------------------------------------
84. TARGET ARCHITECTURE
--------------------------------------------------

Final:

                         QBIT CONNECT
                              |
                         EVENT BUS
                              |
          +-------------------+-------------------+
          |                   |                   |
        LEADS             CAMPAIGNS           INBOX
          |                   |                   |
          +-------------------+-------------------+
                              |
                       TRIGGER ENGINE
                              |
                         WORKFLOW
                              |
                    WORKFLOW VERSION
                              |
                    +---------+---------+
                    |                   |
                CONDITION            ACTION
                    |                   |
                 BRANCH              LEAD
                    |              CONVERSATION
                    |              ASSIGNMENT
                    |              WHATSAPP
                    |              EMAIL
                    |              CAMPAIGN
                    |
                   WAIT
                    |
                 SCHEDULER
                    |
                  REDIS
                    |
                 WORKER
                    |
              EXECUTION STATE
                    |
             EXECUTION HISTORY
                    |
                ANALYTICS

Example:

LEAD CREATED
     ↓
WORKFLOW
     ↓
QUALITY > 70?
   /       \
 YES       NO
  ↓         ↓
TAG HOT   TAG REVIEW
  ↓
WAIT 1 DAY
  ↓
HAS EMAIL?
 /       \
YES      NO
 ↓        ↓
EMAIL    END
 ↓
WAIT 2 DAYS
 ↓
REPLIED?
 /       \
YES      NO
 ↓        ↓
ASSIGN   FOLLOW-UP
SALES    EMAIL
 ↓
END

--------------------------------------------------
85. FINAL REPORT
--------------------------------------------------

Return:

# QBIT CONNECT — PHASE 9 FINAL REPORT

## Status
PASS / BLOCKED

## Repository Audit
...

## Workflow Engine
...

## Triggers
...

## Conditions
...

## Actions
...

## Delays
...

## Scheduler
...

## Versioning
...

## Execution Engine
...

## Idempotency
...

## Loop Protection
...

## WhatsApp Integration
...

## Email Integration
...

## Inbox Integration
...

## UI / Workflow Builder
...

## APIs
...

## Security
...

## Performance
...

## Tests
Passed:
Failed:

## Migration
...

## Data Safety
Confirm:
"No production data was deleted/reset."

## Git
Commit:
...

Push:
SUCCESS / FAILED / NOT CONFIGURED

## Version
...

## Known Limitations
...

## Next Phase
PHASE 10 — ANALYTICS & REPORTING ENGINE

IMPORTANT:
Do not claim PASS unless implementation and verification
actually succeeded.

START NOW.

First audit the repository and Phase 1–8 implementation.
Then implement Phase 9 completely.
Do not stop at analysis.