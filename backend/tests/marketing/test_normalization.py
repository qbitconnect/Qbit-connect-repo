"""Email/phone normalization tests (Phase 7 §15, §16)."""

from app.services.marketing.normalization import normalize_email, normalize_phone


class TestEmailNormalization:
    def test_valid_simple(self):
        r = normalize_email("user@example.com")
        assert r.valid and r.normalized == "user@example.com"

    def test_trim_whitespace(self):
        r = normalize_email("  user@example.com  ")
        assert r.valid and r.email == "user@example.com"

    def test_domain_lowercased_local_preserved(self):
        r = normalize_email("John.Doe@EXAMPLE.com")
        assert r.valid
        assert r.email == "John.Doe@EXAMPLE.com"
        assert r.normalized == "john.doe@example.com"

    def test_missing(self):
        assert normalize_email(None).reason == "MISSING_EMAIL"
        assert normalize_email("   ").reason == "MISSING_EMAIL"

    def test_invalid_no_at(self):
        assert normalize_email("userexample.com").valid is False

    def test_invalid_two_at(self):
        assert normalize_email("a@b@c.com").valid is False

    def test_invalid_space(self):
        assert normalize_email("user name@example.com").valid is False

    def test_invalid_double_dot(self):
        assert normalize_email("user..name@example.com").valid is False

    def test_invalid_no_tld(self):
        assert normalize_email("user@localhost").valid is False

    def test_no_transformation_of_valid(self):
        # valid local-part semantics are preserved (spec §16)
        raw = "Postmaster+QBIT@Example.COM"
        r = normalize_email(raw)
        assert r.valid and r.email == raw


class TestPhoneNormalization:
    def test_e164_valid(self):
        r = normalize_phone("+919876543210")
        assert r.valid and r.normalized == "+919876543210"

    def test_formatting_stripped(self):
        r = normalize_phone("+91 98765-43210")
        assert r.valid and r.normalized == "+919876543210"

    def test_00_international_prefix(self):
        r = normalize_phone("00 1 5551234567")
        assert r.valid and r.normalized == "+15551234567"

    def test_no_country_code_never_guessed(self):
        r = normalize_phone("9876543210")
        assert not r.valid and r.reason == "INVALID_PHONE"
        assert r.normalized is None

    def test_explicit_default_prefix(self):
        r = normalize_phone("9876543210", default_country_prefix="+91")
        assert r.valid and r.normalized == "+919876543210"

    def test_too_short(self):
        assert not normalize_phone("+123").valid

    def test_missing(self):
        assert normalize_phone(None).reason == "MISSING_PHONE"
