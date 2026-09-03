"""Phase 6 tests — phone normalization (§11) + credential vault (§4)."""

from __future__ import annotations

import pytest

from app.core.crypto import (
    SecretVaultError,
    decrypt_payload,
    encrypt_payload,
    mask_phone,
    secret_tail,
)
from app.services.marketing.phone import PhoneNormalizationService, normalize_recipient_phone

SECRET = "unit-test-secret-key-" + "b" * 40


# ------------------------------------------------------------------ §11 phone
@pytest.mark.parametrize("raw,expected", [
    ("+4915112345678", "+4915112345678"),
    ("+49 (151) 1234-5678", "+4915112345678"),
    ("+49.151.1234.5678", "+4915112345678"),
    ("004915112345678", "+4915112345678"),
    ("+91 98765 43210", "+919876543210"),
])
def test_valid_international_formats(raw, expected):
    ok, e164, reason = normalize_recipient_phone(raw)
    assert ok and e164 == expected and reason is None


def test_no_country_code_is_invalid_without_default():
    """Never guess country codes blindly (§11)."""
    ok, e164, reason = normalize_recipient_phone("9876543210")
    assert not ok and e164 is None and reason == "COUNTRY_CODE_REQUIRED"


def test_default_country_prefix_used_explicitly():
    ok, e164, _ = normalize_recipient_phone("9876543210", default_country_prefix="91")
    assert ok and e164 == "+919876543210"


def test_too_short_and_too_long_rejected():
    assert normalize_recipient_phone("+1234567")[0] is False        # 7 digits
    assert normalize_recipient_phone("+1234567890123456")[0] is False  # 16 digits
    ok, _, reason = normalize_recipient_phone("+12345678")
    assert ok  # 8 digits is the minimum


def test_letters_and_empty_rejected():
    assert normalize_recipient_phone("abc")[0] is False
    assert normalize_recipient_phone("")[0] is False
    assert normalize_recipient_phone(None)[0] is False
    assert normalize_recipient_phone(None)[2] == "MISSING_PHONE"


def test_unicode_digits_normalized():
    ok, e164, _ = normalize_recipient_phone("+٩٧١٥٠١٢٣٤٥٦٧")
    assert ok and e164 == "+971501234567"


def test_service_wrapper():
    svc = PhoneNormalizationService()
    ok, e164, _ = svc.normalize("+4915112345678")
    assert ok and e164 == "+4915112345678"
    assert svc.is_valid("+919876543210")
    assert not svc.is_valid("12345")
    assert svc.mask("+919876543210").startswith("+91")


# --------------------------------------------------------------- §4 crypto
def test_encrypt_decrypt_roundtrip():
    payload = {"access_token": "EAAG-super-secret", "app_secret": "abc123"}
    token = encrypt_payload(SECRET, payload)
    assert "EAAG" not in token  # no plaintext anywhere in the ciphertext
    assert decrypt_payload(SECRET, token) == payload


def test_wrong_key_fails_loudly():
    token = encrypt_payload(SECRET, {"access_token": "x" * 20})
    with pytest.raises(SecretVaultError):
        decrypt_payload("another-secret-key-" + "c" * 40, token)


def test_tampered_ciphertext_rejected():
    token = encrypt_payload(SECRET, {"access_token": "x" * 20})
    corrupted = token[:-4] + "AAAA"
    with pytest.raises(SecretVaultError):
        decrypt_payload(SECRET, corrupted)


def test_secret_tail_and_phone_mask():
    assert secret_tail("EAAG-super-secret-value").endswith("alue")
    assert secret_tail("EAAG-super-secret-value").startswith("\u2022")
    assert secret_tail("") == ""
    masked = mask_phone("4915112345678")
    assert masked.startswith("+49") and masked.endswith("678")
    assert "\u2022" in masked


# ------------------------------------------------- §4 vault service behavior
async def test_vault_store_rotate_resolve_and_hints(app, seeded_db):
    import uuid as uuid_mod

    from app.services.marketing.credentials import CredentialVault

    vault = CredentialVault(app.state.settings.QBIT_SECRET_KEY)
    row = await vault.store(
        seeded_db, name=f"whatsapp:{uuid_mod.uuid4().hex[:8]}", provider="whatsapp_cloud",
        payload={"access_token": "EAAG" + "t" * 30, "app_secret": "s3cr3t-value"},
    )
    assert row.ciphertext and "EAAG" not in row.ciphertext
    # hints show token tails only
    public = row.to_public_dict()
    assert "ciphertext" not in public
    assert public["hints"]["access_token_tail"].endswith(("t" * 4)[-4:])
    assert "s3cr3t" not in str(public)

    resolved = await vault.resolve(seeded_db, name=row.name)
    assert resolved["access_token"].startswith("EAAG")
    assert row.last_used_at is not None

    # rotate
    await vault.store(seeded_db, name=row.name, provider="whatsapp_cloud",
                      payload={"access_token": "NEW" + "u" * 30})
    resolved2 = await vault.resolve(seeded_db, name=row.name)
    assert resolved2["access_token"].startswith("NEW")

    await vault.delete(seeded_db, name=row.name)
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await vault.resolve(seeded_db, name=row.name)
