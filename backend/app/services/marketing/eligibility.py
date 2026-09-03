"""Eligibility engine (Phase 5 §13; Phase 6 §12 opt-in + §11 phone rules).

Runs BEFORE any recipient enters the send queue. Checks (in order):

 1. valid contact address for the channel (email/phone present + valid)
    — WhatsApp/SMS addresses must normalize to E.164; a number without a
    country code and without a usable default context is INVALID, never
    guessed (Phase 6 §11)
 2. channel availability (channel known + registered provider)
 3. suppression status (global list + opt-out evidence)
 4. opt-in / permission metadata where applicable — a scraped email/phone is
    NOT automatic consent (Phase 6 §12: never fabricate consent). Leads are
    opted-in when metadata carries EXPLICIT evidence, either:
      metadata.marketing_opt_in == true                      (Phase 5 form)
      metadata.opt_in_status in {"OPTED_IN", "OPT_IN", "GRANTED"}
    Optional enrichment (never required, never invented):
      opt_in_source, opt_in_timestamp, opt_in_notes
 5. lead usability (exists, not archived, not soft-merged away)

Result per recipient: (ELIGIBLE, None) or (INELIGIBLE, reason).
Reasons: MISSING_PHONE / MISSING_EMAIL / INVALID_ADDRESS / SUPPRESSED /
UNSUBSCRIBED / NO_OPT_IN / CHANNEL_UNAVAILABLE / LEAD_UNAVAILABLE.

All database work is batched per audience chunk — never per-lead queries.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.marketing import SuppressionType
from app.models.scrape import Lead
from app.services.marketing.channels import get_channel
from app.services.marketing.phone import normalize_recipient_phone
from app.services.marketing.providers import MarketingProviderRegistry
from app.services.marketing.suppression import SuppressionService

ELIGIBLE = "ELIGIBLE"
INELIGIBLE = "INELIGIBLE"

#: explicit opt-in evidence values accepted in metadata.opt_in_status (§12)
OPT_IN_STATUSES = {"OPTED_IN", "OPT_IN", "GRANTED", "SUBSCRIBED"}


def has_explicit_opt_in(lead: Lead) -> bool:
    """Consent decision from lead metadata ONLY — nothing is inferred from
    the mere existence of a phone number (Phase 6 §12)."""
    meta = lead.metadata_json if isinstance(lead.metadata_json, dict) else {}
    if bool(meta.get("marketing_opt_in")):
        return True
    status = str(meta.get("opt_in_status") or "").upper().strip()
    return status in OPT_IN_STATUSES


class EligibilityService:
    def __init__(self, suppression: SuppressionService | None = None) -> None:
        self.suppression = suppression or SuppressionService()

    # ----------------------------------------------------------- single lead
    def check_recipient(
        self, *,
        channel: str,
        lead: Lead | None,
        suppressed: bool,
        suppress_reason: str | None,
        opt_in_required: bool = True,
    ) -> tuple[str, str | None]:
        """Pure decision function (no I/O) so tests can drive every branch."""
        spec = get_channel(channel)
        if spec is None:
            return INELIGIBLE, "CHANNEL_UNAVAILABLE"
        if lead is None:
            return INELIGIBLE, "LEAD_UNAVAILABLE"
        if lead.merged_into_id is not None or lead.status == "ARCHIVED":
            return INELIGIBLE, "LEAD_UNAVAILABLE"

        address = self.address_for(spec, lead)
        if address is None:
            return INELIGIBLE, (
                "MISSING_EMAIL" if spec.address_kind == "email" else "MISSING_PHONE"
            )
        if not self._address_valid(spec, address):
            return INELIGIBLE, "INVALID_ADDRESS"

        if suppressed:
            return INELIGIBLE, (
                "UNSUBSCRIBED" if suppress_reason == "UNSUBSCRIBED" else "SUPPRESSED"
            )

        if opt_in_required and not has_explicit_opt_in(lead):
            return INELIGIBLE, "NO_OPT_IN"
        return ELIGIBLE, None

    # ------------------------------------------------------------- addresses
    def address_for(self, spec, lead: Lead) -> str | None:
        if spec.address_kind == "email":
            return (lead.email or "").strip() or None
        return (lead.phone or "").strip() or None

    def _address_valid(self, spec, address: str) -> bool:
        import re
        if spec.address_kind == "email":
            return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$", address))
        # Phase 6 §11: strict phone normalization — never guess country codes
        ok, _normalized, _reason = normalize_recipient_phone(address)
        return ok

    # --------------------------------------------------------------- batched
    async def check_batch(
        self, session: AsyncSession, *, channel: str,
        leads: list[Lead], provider_registry: MarketingProviderRegistry | None = None,
        opt_in_required: bool = True,
    ) -> dict[uuid.UUID, tuple[str, str | None]]:
        """Eligibility for a chunk of leads. Returns {lead_id: (status, reason)}."""
        spec = get_channel(channel)
        if spec is None:
            return {lead.id: (INELIGIBLE, "CHANNEL_UNAVAILABLE") for lead in leads}

        # channel availability: at least one registered provider must serve it
        # (the mock provider satisfies this only in test environments; the
        # deeper "is the account's provider configured" check happens at
        # campaign validation and again right before send)
        provider_ok = provider_registry is None or any(
            provider_registry.get(pid) is not None for pid in spec.providers
        )

        emails = [(lead.id, (lead.email or "").strip()) for lead in leads if (lead.email or "").strip()]
        phones = [(lead.id, (lead.phone or "").strip()) for lead in leads if (lead.phone or "").strip()]
        suppression = await self.suppression.check_batch(
            session, channel=channel,
            emails=[e for _, e in emails], phones=[p for _, p in phones],
            lead_ids=[lead.id for lead in leads],
        )
        email_by_id = {lead_id: email for lead_id, email in emails}
        phone_by_id = {lead_id: phone for lead_id, phone in phones}

        def _key(kind: str, value: str) -> str:
            from app.services.marketing.suppression import normalize_address
            entry_type = SuppressionType.EMAIL if kind == "email" else SuppressionType.PHONE
            try:
                return f"{kind}:{normalize_address(entry_type, value)}"
            except Exception:  # noqa: BLE001 — invalid address → plain key
                return f"{kind}:{value}"

        out: dict[uuid.UUID, tuple[str, str | None]] = {}
        for lead in leads:
            if not provider_ok:
                out[lead.id] = (INELIGIBLE, "CHANNEL_UNAVAILABLE")
                continue
            # the suppression key MUST use the channel's own address kind
            # (phone for WHATSAPP/SMS, email for EMAIL) — a Phase 5 bug here
            # checked phone-channel suppression against the email value
            address = self.address_for(spec, lead)
            key = _key(spec.address_kind, address or "")
            hit, reason = suppression["suppressed"].get(key, (False, None))
            status, why = self.check_recipient(
                channel=channel, lead=lead,
                suppressed=hit, suppress_reason=reason,
                opt_in_required=opt_in_required,
            )
            out[lead.id] = (status, why)
        return out

    # ---------------------------------------------------------- live leads
    async def load_leads(self, session: AsyncSession, lead_ids: list[uuid.UUID]) -> dict[uuid.UUID, Lead]:
        """Load a batch of leads by id (bounded by caller)."""
        if not lead_ids:
            return {}
        rows = await session.execute(select(Lead).where(Lead.id.in_(lead_ids)))
        return {lead.id: lead for lead in rows.scalars().all()}
