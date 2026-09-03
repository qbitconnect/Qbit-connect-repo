"""SMTP provider adapter (Phase 7 §3, §4, §22, §23).

Official SMTP transport ONLY (aiosmtplib): TLS or STARTTLS, per-account
credentials from the vault. No port/relay trickery, no reputation evasion.

Config (non-secret, stored on the account):
    host, port, security ("TLS" implicit | "STARTTLS"), timeout_seconds
Credentials (vault):
    username, password
Sender (account columns): sender_name, sender_email, reply_to
"""

from __future__ import annotations

import email.utils
import re
from email.message import EmailMessage

import aiosmtplib

from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    HealthOutcome,
    OutboundMessage,
    SendResult,
    ValidationOutcome,
)
from app.services.marketing.providers.errors import classify_smtp_failure

CRLF_RE = re.compile(r"[\r\n]")

SECURITY_MODES = ("TLS", "STARTTLS")


def _addr(name: str | None, address: str) -> str:
    return email.utils.formataddr((name or "", address))


def _validate_header_value(value: str) -> bool:
    """Reject CR/LF injection (spec §39)."""
    return not CRLF_RE.search(value)


class SMTPProvider(BaseMarketingProvider):
    channel = "EMAIL"
    provider_id = "smtp"

    async def validate_configuration(self, config: dict, credentials: dict) -> ValidationOutcome:
        host = (config or {}).get("host", "").strip()
        if not host:
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "SMTP host is required")
        try:
            port = int((config or {}).get("port", 587))
        except (TypeError, ValueError):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "SMTP port must be an integer")
        if not (1 <= port <= 65535):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "SMTP port out of range")
        security = (config or {}).get("security", "STARTTLS")
        if security not in SECURITY_MODES:
            return ValidationOutcome(
                False, "CONFIGURATION_ERROR", f"security must be one of {SECURITY_MODES}"
            )
        if not (credentials or {}).get("username"):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "SMTP username is required")
        if not (credentials or {}).get("password"):
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "SMTP password is required")
        return ValidationOutcome(True)

    async def validate_sender(self, config: dict, credentials: dict, sender: dict) -> ValidationOutcome:
        address = (sender or {}).get("sender_email", "").strip()
        if not address:
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "Sender email is required")
        if CRLF_RE.search(address) or " " in address:
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "Invalid sender email")
        reply_to = (sender or {}).get("reply_to")
        if reply_to:
            if CRLF_RE.search(reply_to) or " " in reply_to:
                return ValidationOutcome(False, "CONFIGURATION_ERROR", "Invalid reply-to address")
        return ValidationOutcome(True)

    async def validate_recipient(self, recipient: str) -> ValidationOutcome:
        from app.services.marketing.normalization import normalize_email

        result = normalize_email(recipient)
        if not result.valid:
            return ValidationOutcome(False, result.reason, "Invalid recipient email")
        return ValidationOutcome(True)

    async def validate_message(self, message: OutboundMessage) -> ValidationOutcome:
        if message.channel != "EMAIL":
            return ValidationOutcome(False, "CONFIGURATION_ERROR", "Channel must be EMAIL")
        if not message.text and not message.html:
            return ValidationOutcome(False, "MESSAGE_REJECTED", "Message body is empty")
        if message.subject and not _validate_header_value(message.subject):
            return ValidationOutcome(False, "MESSAGE_REJECTED", "Subject contains forbidden characters")
        return ValidationOutcome(True)

    async def send(self, config: dict, credentials: dict, message: OutboundMessage) -> SendResult:
        outcome = await self.validate_configuration(config, credentials)
        if not outcome.ok:
            return SendResult(False, error_code=outcome.code, error_message=outcome.message)
        outcome = await self.validate_message(message)
        if not outcome.ok:
            return SendResult(False, error_code=outcome.code, error_message=outcome.message)

        mime = self._build_mime(message)
        try:
            await aiosmtplib.send(
                mime,
                sender=message.sender,
                recipients=[message.recipient],
                hostname=config.get("host"),
                port=int(config.get("port", 587)),
                username=credentials.get("username"),
                password=credentials.get("password"),
                starttls=config.get("security", "STARTTLS") == "STARTTLS",
                use_tls=config.get("security", "STARTTLS") == "TLS",
                timeout=float(config.get("timeout_seconds", 30)),
            )
        except aiosmtplib.SMTPAuthenticationError as exc:
            code, msg = classify_smtp_failure(exc)
            return SendResult(False, error_code=code, error_message=msg, retryable=False)
        except aiosmtplib.SMTPRecipientsRefused as exc:
            code, msg = classify_smtp_failure(exc)
            return SendResult(False, error_code=code, error_message=msg, retryable=False)
        except aiosmtplib.SMTPException as exc:
            code, msg = classify_smtp_failure(exc)
            from app.services.marketing.providers.errors import retry_class

            return SendResult(
                False,
                error_code=code,
                error_message=msg,
                retryable=retry_class(code) == "TRANSIENT",
            )
        except (OSError, TimeoutError) as exc:
            code, msg = classify_smtp_failure(exc)
            return SendResult(False, error_code=code, error_message=msg, retryable=True)

        # Standard SMTP DATA has no queue id contract; use the Message-ID header
        # as the provider message reference (stable, returned in bounces).
        message_id_header = mime.get("Message-ID", "")
        return SendResult(
            True,
            provider_message_id=message_id_header or None,
            provider_status="SENT",
            raw_metadata={"transport": "smtp", "host": config.get("host")},
        )

    async def get_status(
        self, config: dict, credentials: dict, provider_message_id: str
    ) -> SendResult:
        # Plain SMTP has no status API — delivery state arrives via events.
        return SendResult(True, provider_message_id=provider_message_id, provider_status="UNKNOWN")

    async def handle_event(self, payload, headers, credentials):
        # SMTP providers rarely push webhooks; bounce mail handling is future work.
        return []

    async def health_check(self, config: dict, credentials: dict) -> HealthOutcome:
        outcome = await self.validate_configuration(config, credentials)
        if not outcome.ok:
            return HealthOutcome(False, code=outcome.code, message=outcome.message)
        try:
            client = aiosmtplib.SMTP(
                hostname=config.get("host"),
                port=int(config.get("port", 587)),
                starttls=config.get("security", "STARTTLS") == "STARTTLS",
                use_tls=config.get("security", "STARTTLS") == "TLS",
                timeout=float(config.get("timeout_seconds", 15)),
            )
            await client.connect()
            try:
                await client.ehlo()
                # Verify AUTH cheaply when the server advertises it: perform full
                # login so credential problems surface during health checks.
                await client.login(
                    credentials.get("username"), credentials.get("password")
                )
            finally:
                await client.quit()
            return HealthOutcome(True)
        except aiosmtplib.SMTPAuthenticationError:
            return HealthOutcome(False, code="AUTHENTICATION_ERROR", message="Authentication failed")
        except aiosmtplib.SMTPException as exc:
            code, msg = classify_smtp_failure(exc)
            return HealthOutcome(False, degraded=code in ("CONNECTION_ERROR", "RATE_LIMITED"), code=code, message=msg)
        except (OSError, TimeoutError) as exc:
            code, msg = classify_smtp_failure(exc)
            return HealthOutcome(False, degraded=True, code=code, message=msg)

    # ------------------------------------------------------------------ mime
    def _build_mime(self, message: OutboundMessage) -> EmailMessage:
        mime = EmailMessage()
        mime["From"] = _addr(message.sender_name, message.sender or "")
        mime["To"] = message.recipient
        mime["Subject"] = message.subject or ""
        if message.reply_to:
            mime["Reply-To"] = message.reply_to
        for key, value in (message.headers or {}).items():
            if _validate_header_value(str(value)):
                mime[key] = str(value)
        mime.set_content(message.text or "")
        if message.html:
            mime.add_alternative(message.html, subtype="html")
        return mime
