# Providers

The provider layer is the ONLY component that talks to external messaging
platforms. CampaignService resolves providers through the registry — it is
never coupled to a specific provider.

## Contract

```python
class BaseMarketingProvider:
    provider_id: str; channel: str
    interface_only: bool   # architecture-only (no real integration yet)
    test_only: bool        # MOCK / TEST ONLY

    async def validate_configuration(config) -> list[str]   # problems (never echoes secrets)
    async def validate_recipient(address) -> bool
    async def validate_message(*, subject, body) -> list[str]
    async def send(*, account_config, recipient_address, subject, body,
                   idempotency_key, metadata) -> SendResult
    async def get_status(*, account_config, provider_message_id) -> dict
    async def handle_event(payload) -> dict                 # normalize → campaign event
    async def health_check(account_config) -> dict
```

`SendResult` carries `provider_message_id`, a normalized status, and an error
classification `TRANSIENT | PERMANENT | CONFIGURATION` that drives the retry
policy. Providers never raise for provider-side failures — they return
`SendResult.failure`. Secrets never appear in results, events, or logs.

## Registered providers (Phase 5)

| provider_id | Channel | Kind | Behavior |
|---|---|---|---|
| `whatsapp_cloud` | WHATSAPP | interface | all sends fail `PROVIDER_NOT_IMPLEMENTED`/`NOT_CONFIGURED`; real WhatsApp Business API integration is Phase 6 |
| `email` | EMAIL | interface | SMTP/transactional adapter arrives later |
| `sms` | SMS | interface | SMS integration arrives later |
| `mock` | MOCK | **TEST ONLY** | deterministic test double |

### Mock provider (§36)

- Deterministic: addresses containing `flaky` fail TRANSIENT, addresses
  containing `fail` fail PERMANENT, everything else succeeds with a
  synthetic `mock-…` message id.
- Registered ONLY when `QBIT_ENV == "test"` or
  `QBIT_MARKETING_ALLOW_MOCK_PROVIDER` is set — and never in production,
  regardless of flags.
- Every UI/API surface that can expose it labels it `MOCK / TEST ONLY`, and
  send results carry `metadata.mock = true` so the UI can never present a
  mock send as a real delivery.

## Event interface (§37)

```
Provider Event → Event Normalizer (PROVIDER_EVENT_MAP) → Campaign Event
              → Recipient status (forward-only) → Analytics
```

Provider-specific webhook shapes are normalized inside each provider's
`handle_event`; CampaignService sees only canonical event types
(`MESSAGE_SENT`, `MESSAGE_DELIVERED`, `MESSAGE_READ`, `MESSAGE_REPLIED`,
`MESSAGE_FAILED`). The generic ingestion endpoint
`POST /api/v1/campaigns/events/provider` exposes the pipeline; dedicated
per-provider webhook endpoints arrive with the real integrations.

## Sending accounts

Multiple accounts per channel are first-class (e.g. WhatsApp numbers 1–N,
email accounts 1–M). Each account stores only NON-SECRET configuration
(`from_address`, `phone_number_id`, `rate_policy`, credential *references*);
secret-like keys are rejected at the API because the encrypted credential
vault is a later phase (architecture doc 17). Account lifecycle: PENDING →
ACTIVE / ERROR / SUSPENDED / INACTIVE / DISCONNECTED, with health
UNKNOWN → HEALTHY / DEGRADED / UNHEALTHY refreshed by `health_check`.

## Future phases

- **Phase 6 — WhatsApp Business API**: approved Cloud API adapter,
  template management, delivery/read/reply webhooks. No WhatsApp Web
  automation, no anti-ban, no bulk unsanctioned messaging — these are
  permanent non-goals.
- **Email**: SMTP / transactional API adapter behind the same interface.
- **SMS**: any approved provider behind the same interface.
