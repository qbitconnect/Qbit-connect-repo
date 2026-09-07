"""Filter allowlist + report config validation (spec §5, §32)."""

from __future__ import annotations

import pytest

from app.analytics.core.exceptions import (
    AnalyticsValidationError,
    ReportConfigError,
)
from app.analytics.core.filters import AnalyticsFilters, filters_from_params
from app.analytics.reports.schemas import validate_report_config, validate_report_name
from app.analytics.core.time import resolve_period


class TestFilters:
    def test_allowlisted_values_parse(self):
        period = resolve_period(period="30d")
        filters = filters_from_params(
            {"source": "google_maps,website", "city": "Pune"}, period,
        )
        assert filters.source == ["google_maps", "website"]
        assert filters.city == ["Pune"]
        assert filters.period is period

    def test_unknown_filter_rejected(self):
        period = resolve_period(period="30d")
        with pytest.raises(AnalyticsValidationError):
            filters_from_params({"raw_sql": "1=1; DROP TABLE leads"}, period)

    def test_injection_value_is_just_a_bound_value(self):
        """A hostile 'value' never becomes SQL — it is data, applied as a
        parameter; it simply matches nothing (spec §32)."""
        period = resolve_period(period="30d")
        filters = filters_from_params(
            {"city": "x' OR '1'='1"}, period,
        )
        assert filters.city == ["x' OR '1'='1"]

    def test_invalid_uuid_filter_rejected(self):
        period = resolve_period(period="30d")
        with pytest.raises(AnalyticsValidationError):
            filters_from_params({"campaign_id": "not-a-uuid"}, period)

    def test_value_length_cap(self):
        period = resolve_period(period="30d")
        with pytest.raises(AnalyticsValidationError):
            filters_from_params({"city": "x" * 500}, period)

    def test_multi_value_cap(self):
        period = resolve_period(period="30d")
        filters = filters_from_params(
            {"source": [f"s{i}" for i in range(100)]}, period,
        )
        assert len(filters.source) <= 25

    def test_to_payload_deterministic(self):
        period = resolve_period(period="7d")
        a = AnalyticsFilters(period=period, source=["b", "a"], city=["Pune"])
        b = AnalyticsFilters(period=period, source=["a", "b"], city=["Pune"])
        assert a.to_payload() == b.to_payload()

    def test_filters_from_payload_validates(self):
        from app.analytics.core.filters import filters_from_payload

        filters = filters_from_payload({"source": "import"}, None)
        assert filters.source == ["import"]
        with pytest.raises(AnalyticsValidationError):
            filters_from_payload({"evil": "1"}, None)


class TestReportConfig:
    def test_valid_config_normalizes(self):
        config = validate_report_config({
            "domain": "leads",
            "metrics": ["total", "converted"],
            "dimensions": ["source"],
            "filters": {"city": "Pune"},
            "period": "30d",
            "visualization": "bar",
        })
        assert config["domain"] == "LEADS"
        assert config["metrics"] == ["total", "converted"]
        assert config["filters"] == {"city": ["Pune"]}

    def test_unknown_config_key_rejected(self):
        with pytest.raises(ReportConfigError):
            validate_report_config({"domain": "LEADS", "metrics": ["total"],
                                    "raw_sql": "SELECT 1"})

    def test_unknown_metric_rejected(self):
        with pytest.raises(ReportConfigError):
            validate_report_config({"domain": "LEADS", "metrics": ["fabricated_rate"]})

    def test_metric_domain_mismatch_rejected(self):
        with pytest.raises(ReportConfigError):
            validate_report_config({"domain": "SCRAPING", "metrics": ["total"]})

    def test_unknown_dimension_rejected(self):
        with pytest.raises(ReportConfigError):
            validate_report_config({"domain": "OVERVIEW", "metrics": ["total_leads"],
                                    "dimensions": ["source"]})

    def test_unknown_filter_key_rejected(self):
        with pytest.raises(ReportConfigError):
            validate_report_config({"domain": "LEADS", "metrics": ["total"],
                                    "filters": {"sql": "drop"}})

    def test_funnel_only_for_leads(self):
        with pytest.raises(ReportConfigError):
            validate_report_config({"domain": "MARKETING", "metrics": ["messages_sent"],
                                    "visualization": "funnel"})

    def test_custom_period_requires_dates(self):
        with pytest.raises(ReportConfigError):
            validate_report_config({"domain": "LEADS", "metrics": ["total"],
                                    "period": "custom"})

    def test_limit_bounds(self):
        with pytest.raises(ReportConfigError):
            validate_report_config({"domain": "LEADS", "metrics": ["total"],
                                    "limit": 100000})

    def test_name_validation(self):
        assert validate_report_name("Weekly leads 2026") == "Weekly leads 2026"
        with pytest.raises(ReportConfigError):
            validate_report_name("")
        with pytest.raises(ReportConfigError):
            validate_report_name("x" * 300)
