"""Lead contact-enrichment layer (spec §20, §QBIT DIFFERENTIATION #3/#4).

Works across ALL source actors: given a lead with a website, crawl its
public contact pages (same compliant HTTP stack as the actors — SSRF
guard, robots.txt, size caps, honest failures) and fill EMPTY contact
fields only. Existing data is never overwritten.

Honesty rules:
- consent is NEVER implied: scraped contacts stay marketing-ineligible
  unless the operator records explicit opt-in (suppression/opt-out layer)
- every enriched field is traceable (metadata_json.enrichment: pages,
  timestamps, evidence URLs)
- enrichment_status lifecycle: UNRICHED → RUNNING → ENRICHED | FAILED | SKIPPED
- last_verified_at is set ONLY here (and by future verification passes) —
  it means "this lead's public contact data was re-checked at ..."
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone as dt_timezone

from bs4 import BeautifulSoup

from app.core.logging import get_logger
from app.models.scrape import EnrichmentStatus, Lead
from app.scrapers.actors.email_finder.actor import (
    _CONTACT_HINTS,
    _EMAIL_RE,
    classify_email,
)
from app.scrapers.actors.website.parser import (
    extract_emails,
    extract_phones,
    extract_social_links,
    page_title,
)
from app.scrapers.core.http import HttpPolicy, PolicyHttpClient
from app.scrapers.core.netguard import canonical_url, in_same_domain, validate_url_async

logger = get_logger(__name__)

_MAX_PAGES_DEFAULT = 12
#: deterministic 0-100 evidence confidence for the enriched record
_CONFIDENCE = {"own-domain": 85, "contact-page": 70, "any-page": 55}
#: explicit role-based inboxes outrank generic ones on equal evidence strength
_TYPE_RANK = {"sales": 3, "support": 2, "contact": 2, "info": 1}


class LeadEnrichmentService:
    """Single-lead public contact enrichment (no job machinery needed).

    `transport`/`url_policy` are test hooks (httpx.MockTransport) — the
    same convention as PolicyHttpClient; production never passes them.
    """

    def __init__(self, session, settings, *, transport=None, url_policy=None) -> None:
        self.session = session
        self.settings = settings
        self._transport = transport
        self._url_policy = url_policy

    async def enrich_lead(
        self, lead: Lead, *, max_pages: int = _MAX_PAGES_DEFAULT
    ) -> Lead:
        if not lead.website:
            lead.enrichment_status = EnrichmentStatus.SKIPPED.value
            lead.updated_at = datetime.now(dt_timezone.utc)
            await self.session.commit()
            return lead
        if lead.enrichment_status == EnrichmentStatus.RUNNING.value:
            from app.core.errors import ConflictError

            raise ConflictError("Enrichment already running for this lead")

        website = lead.website.strip()
        if not website.startswith(("http://", "https://")):
            website = f"https://{website}"
        site_domain = website.split("//", 1)[-1].split("/", 1)[0].lower()

        policy = HttpPolicy.from_settings(self.settings)
        from app.scrapers.core.netguard import UrlPolicy

        url_policy = self._url_policy or UrlPolicy(
            allowed_ports=self.settings.scraper_allowed_ports(),
            allow_private_targets=self.settings.QBIT_SCRAPER_ALLOW_PRIVATE_TARGETS,
        )

        lead.enrichment_status = EnrichmentStatus.RUNNING.value
        await self.session.commit()

        evidence: list[dict] = []
        pages_scanned = 0
        found_emails: dict[str, dict] = {}
        phones: list[str] = []
        socials: dict[str, str] = {}
        try:
            start = await validate_url_async(website, url_policy)
            base = f"{start.split('//', 1)[0]}//{site_domain}"
            queued: list[str] = [start]
            for hint in _CONTACT_HINTS:
                queued.append(f"{base}{hint}")
            fetched: set[str] = set()
            http = PolicyHttpClient(policy, url_policy, transport=self._transport)
            try:
                while queued and pages_scanned < max_pages:
                    url = queued.pop(0)
                    cu = canonical_url(url)
                    if cu in fetched:
                        continue
                    fetched.add(cu)
                    try:
                        checked = await validate_url_async(url, url_policy)
                        if not (
                            in_same_domain(checked, start)
                            or canonical_url(checked) == canonical_url(start)
                        ):
                            continue
                        resp = await http.get_html(checked)
                    except Exception:  # noqa: BLE001 — one bad page never stops enrichment
                        continue
                    pages_scanned += 1
                    if resp.status_code >= 400:
                        continue
                    html = resp.text
                    soup = BeautifulSoup(html, "html.parser")
                    is_contact = any(h in str(resp.url) for h in ("/contact", "/impressum", "/about"))
                    for email in extract_emails(html, soup):
                        if not _EMAIL_RE.fullmatch(email) or email in found_emails:
                            continue
                        email_type, confidence = classify_email(email, str(resp.url), site_domain)
                        bonus = "contact-page" if is_contact else "any-page"
                        host = email.split("@")[-1].lower()
                        if host.endswith(site_domain) or site_domain.endswith(host):
                            bonus = "own-domain"
                        found_emails[email] = {
                            "email_type": email_type,
                            "confidence": confidence,
                            "evidence": str(resp.url),
                            "strength": bonus,
                        }
                    if not phones:
                        phones = extract_phones(soup)
                    for platform, link in extract_social_links(soup).items():
                        socials.setdefault(platform, link)
                    evidence.append({"url": str(resp.url), "title": page_title(soup)})
            finally:
                await http.aclose()
        except Exception as exc:  # noqa: BLE001 — honest FAILED, never silent
            lead.enrichment_status = EnrichmentStatus.FAILED.value
            meta = dict(lead.metadata_json or {})
            meta["enrichment"] = {
                "at": datetime.now(dt_timezone.utc).isoformat(),
                "error": str(exc)[:500],
                "pages_scanned": pages_scanned,
            }
            lead.metadata_json = meta
            lead.updated_at = datetime.now(dt_timezone.utc)
            await self.session.commit()
            logger.warning(
                "Lead enrichment failed",
                extra={"extra_fields": {"lead_id": str(lead.id), "error": str(exc)[:200]}},
            )
            return lead

        # ---- merge results: fill EMPTY fields only (never overwrite) --------
        updated_fields: list[str] = []
        if found_emails and not lead.email:
            best = max(
                found_emails.items(),
                key=lambda kv: (
                    _CONFIDENCE.get(kv[1]["strength"], 0),
                    _TYPE_RANK.get(kv[1]["email_type"], 0),
                    kv[1]["confidence"] == "HIGH",
                ),
            )
            lead.email = best[0]
            updated_fields.append("email")
        if phones and not lead.phone:
            lead.phone = phones[0][:40]
            updated_fields.append("phone")
        if socials:
            merged_social = dict(lead.social_links or {})
            new_social = {k: v for k, v in socials.items() if not merged_social.get(k)}
            if new_social:
                merged_social.update(new_social)
                lead.social_links = merged_social
                updated_fields.append("social_links")
        if not lead.business_name and evidence:
            title = next((e["title"] for e in evidence if e.get("title")), None)
            if title:
                lead.business_name = title[:300]
                updated_fields.append("business_name")

        meta = dict(lead.metadata_json or {})
        meta["enrichment"] = {
            "at": datetime.now(dt_timezone.utc).isoformat(),
            "pages_scanned": pages_scanned,
            "emails_found": sorted(found_emails.keys())[:20],
            "emails_detail": {k: v for k, v in list(found_emails.items())[:20]},
            "phones_found": phones[:10],
            "social_found": socials,
            "fields_updated": updated_fields,
            "evidence_urls": [e["url"] for e in evidence[:20]],
            "consent": "not_implied",
        }
        lead.metadata_json = meta
        lead.enrichment_status = (
            EnrichmentStatus.ENRICHED.value if updated_fields else EnrichmentStatus.UNRICHED.value
        )
        # deterministic evidence confidence: own-domain contact data scores high
        if updated_fields:
            strengths = [
                _CONFIDENCE.get(found_emails[email]["strength"], 40)
                for email in found_emails
            ] if lead.email in found_emails else []
            lead.confidence = max(strengths) if strengths else 60
            lead.last_verified_at = datetime.now(dt_timezone.utc)
        lead.updated_at = datetime.now(dt_timezone.utc)
        await self.session.commit()
        await self.session.refresh(lead)
        # activity trail (same convention as lead.scraped / lead.imported)
        try:
            from app.services.leads.activity import LeadActivityService

            await LeadActivityService().log(
                self.session,
                lead.id,
                "lead.enriched",
                message=(
                    f"Enriched from public website ({pages_scanned} pages): "
                    f"updated {', '.join(updated_fields) or 'nothing new'}"
                    if updated_fields
                    else f"Enrichment scan found nothing new ({pages_scanned} pages)"
                ),
                metadata={"pages_scanned": pages_scanned, "fields_updated": updated_fields},
            )
            await self.session.commit()
        except Exception:  # noqa: BLE001 — activity log must never fail enrichment
            logger.exception("Failed to write lead enrichment activity")
        return lead
