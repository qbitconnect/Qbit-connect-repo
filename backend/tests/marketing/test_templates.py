"""Template engine tests (Phase 5 §9, §10, §42)."""

from __future__ import annotations

import pytest

from app.services.marketing.template import (
    TemplateService,
    extract_variables,
    render,
    render_from_lead,
)
from tests.marketing.conftest import make_lead


class TestSubstitution:
    def test_basic_substitution(self):
        out = render("Hello {{first_name}}, we work with {{business_name}}.",
                     {"first_name": "Aisha", "business_name": "Acme"})
        assert out == "Hello Aisha, we work with Acme."

    def test_missing_variable_renders_empty(self):
        out = render("Hi {{first_name}} {{never_heard_of_you}}", {"first_name": "Aisha"})
        assert out == "Hi Aisha "

    def test_no_expression_language(self):
        # non-identifier "expressions" stay literal text — nothing is ever
        # evaluated as code; identifier-shaped variables render EMPTY unless
        # the caller supplies them
        body = "{{ 7*6 }} {{ ''.__class__ }}"
        out = render(body, {})
        assert out == body
        out2 = render("{{ 7*6 }}", {"7*6": "42"})
        assert out2 == "{{ 7*6 }}"  # non-identifier keys can never inject
        assert render("{{ self }}", {}) == ""  # unknown var → empty, never code

    def test_html_is_not_executed_or_sanitized_away(self):
        body = "Hello {{first_name}}"
        out = render(body, {"first_name": "<b>Aisha</b>"})
        assert out == "Hello <b>Aisha</b>"  # engine substitutes verbatim; UI escapes on render

    def test_extract_variables(self):
        assert extract_variables("{{a}} {{ b_c }} plain") == {"a", "b_c"}
        assert extract_variables("no vars") == set()

    def test_render_from_lead(self):
        lead = make_lead(first_name="Ravi", business_name="Acme Pvt Ltd", city="Surat")
        out = render_from_lead("{{first_name}} @ {{business_name}} ({{city}})", lead)
        assert out == "Ravi @ Acme Pvt Ltd (Surat)"


class TestValidation:
    def setup_method(self):
        self.svc = TemplateService()

    def test_valid_whatsapp_template(self):
        report = self.svc.validate_template(
            channel="WHATSAPP", subject=None, body="Hi {{first_name}}",
            declared_variables=["first_name"],
        )
        assert report["valid"] is True

    def test_email_requires_subject(self):
        report = self.svc.validate_template(channel="EMAIL", subject=None, body="Hello")
        assert report["valid"] is False
        assert any("subject" in p.lower() for p in report["problems"])

    def test_whatsapp_rejects_subject(self):
        report = self.svc.validate_template(channel="WHATSAPP", subject="Subject", body="Hello")
        assert report["valid"] is False

    def test_unknown_variable_reported(self):
        report = self.svc.validate_template(
            channel="WHATSAPP", subject=None, body="Hi {{hacker_var}}",
        )
        assert report["valid"] is False
        assert any("hacker_var" in p for p in report["problems"])

    def test_sms_length_limit(self):
        report = self.svc.validate_template(channel="SMS", subject=None, body="x" * 1700)
        assert report["valid"] is False

    def test_unknown_channel(self):
        report = self.svc.validate_template(channel="PIGEON", subject=None, body="x")
        assert report["valid"] is False


class TestTemplateCrud:
    async def test_create_and_validation_errors(self, seeded_db):
        svc = TemplateService()
        template = await svc.create(
            seeded_db, name="Welcome", channel="WHATSAPP",
            body="Hi {{first_name}}", created_by=None,
        )
        assert template.status == "DRAFT"
        assert set(template.variables) == {"first_name"}

        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            await svc.create(
                seeded_db, name="Bad", channel="WHATSAPP",
                body="Hi {{bogus}}", created_by=None,
            )

    async def test_delete_archives_in_use(self, seeded_db):
        from app.models.marketing import Campaign, CampaignStatus

        svc = TemplateService()
        template = await svc.create(
            seeded_db, name="T", channel="EMAIL", subject="S", body="Body",
            created_by=None,
        )
        campaign = Campaign(
            name="C", channel="EMAIL", status=CampaignStatus.DRAFT,
            template_id=template.id,
        )
        seeded_db.add(campaign)
        await seeded_db.commit()

        await svc.delete(seeded_db, template.id)
        archived = await svc.get(seeded_db, template.id)
        assert archived.status == "ARCHIVED"  # evidence preserved, not destroyed
