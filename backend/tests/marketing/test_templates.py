"""Template engine tests (Phase 7 §10–§12, §37, §38)."""

import pytest

from app.core.errors import ValidationError
from app.services.marketing import templates as T


class TestVariables:
    def test_extract_variables(self):
        used = T.extract_variables("Hi {{first_name}}, {{ unknown_one }}", None, "x {{email}}")
        assert used == ["first_name", "unknown_one", "email"]

    def test_unknown_variable_fails_whitelist(self):
        assert T.validate_variable_usage("Hi {{first_name}} {{evil_code}}") == ["evil_code"]

    def test_known_system_variables(self):
        assert T.validate_variable_usage("{{unsubscribe_url}} {{company_name}}") == []

    def test_ensure_template_variables_raises(self):
        with pytest.raises(ValidationError):
            T.ensure_template_variables(["first_name", "__import__"])


class TestRendering:
    def _renderer(self):
        return T.TemplateRenderer(
            unsubscribe_url="https://app.example.com/unsubscribe/abc",
            company_name="QBIT",
            company_address="1 Road",
        )

    def test_email_render_html_and_text(self):
        out = self._renderer().render(
            channel="EMAIL",
            subject="Hello {{first_name}}",
            html_body="<p>Hi {{first_name}} of {{business_name}}</p>",
            text_body=None,
            body=None,
            values={"first_name": "Sagar", "business_name": "Example Co"},
        )
        assert out.subject == "Hello Sagar"
        assert "Sagar" in out.html and "Example Co" in out.html
        assert out.text  # derived from html

    def test_missing_variable_reported(self):
        out = self._renderer().render(
            channel="EMAIL",
            subject="Hi {{first_name}} {{city}}",
            html_body="<p>x</p>",
            text_body=None,
            body=None,
            values={"first_name": "A"},
        )
        assert "MISSING_VARIABLES:city" in out.warnings
        assert out.missing_variables == ["city"]

    def test_empty_subject_warns(self):
        out = self._renderer().render(
            channel="EMAIL", subject="", html_body="<p>x</p>", text_body=None, body=None, values={}
        )
        assert "EMPTY_SUBJECT" in out.warnings

    def test_whatsapp_body_render(self):
        out = self._renderer().render(
            channel="WHATSAPP", subject=None, html_body=None, text_body=None,
            body="Hi {{first_name}}", values={"first_name": "Sam"},
        )
        assert out.text == "Hi Sam"

    def test_no_code_execution(self):
        # {{...}} placeholders are plain substitution — expressions stay inert
        out = self._renderer().render(
            channel="EMAIL",
            subject="{{ 7*7 }}",
            html_body="<p>{{ self.__class__ }}</p>",
            text_body=None,
            body=None,
            values={},
        )
        assert "49" not in out.subject
        assert "class" not in (out.html or "")


class TestSanitizer:
    def test_script_removed(self):
        out = T.sanitize_html("<p>ok</p><script>alert(1)</script>")
        assert "<script" not in out and "alert" not in out

    def test_event_handlers_removed(self):
        out = T.sanitize_html('<p onclick="evil()">x</p><img src="https://x/y.png" onerror="evil()">')
        assert "onclick" not in out and "onerror" not in out

    def test_javascript_url_removed(self):
        out = T.sanitize_html('<a href="javascript:alert(1)">c</a>')
        assert "javascript:" not in out

    def test_data_iframe_removed(self):
        out = T.sanitize_html('<iframe src="data:text/html,<script>x</script>"></iframe>')
        assert "<iframe" not in out

    def test_style_expression_removed(self):
        out = T.sanitize_html('<p style="width: expression(alert(1))">x</p>')
        assert "expression" not in out

    def test_safe_html_preserved(self):
        html = '<table><tr><td><a href="https://ok.example.com">site</a></td></tr></table>'
        out = T.sanitize_html(html)
        assert "https://ok.example.com" in out

    def test_protocol_relative_blocked(self):
        out = T.sanitize_html('<a href="//evil.example.com">x</a>')
        assert "evil.example.com" not in out

    def test_html_to_text(self):
        text = T.html_to_text("<p>Hello</p><p>World</p>")
        assert "Hello" in text and "World" in text
