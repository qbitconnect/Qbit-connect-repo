"""SMTPProvider — real SMTP email delivery (Phase 7 §3, §4).

Responsibilities (and NOTHING else — campaign logic stays in CampaignService):
- configuration validation (structural, never echoes secrets)
- recipient validation (email format)
- message validation (subject required, size limits)
- send via SMTP with TLS / STARTTLS / none, per the account configuration
- response normalization into SendResult (no secrets in result or logs)
- health probe (connect + authenticate + QUIT — never sends mail)
- account validation flow (§7): config → credentials → connectivity → auth

Security rules enforced here:
- credentials arrive per-call via `credentials` (decrypted vault payload) and
  are never logged, persisted, or echoed into errors
- the From/To/Reply-To/Subject header values are validated against CR/LF
  injection (§39) before any bytes reach the wire
- provider errors are normalized + sanitized (§23); restrictions are never
  bypassed — SMTP rejected operations surface their real (sanitized) error

Delivery-timeouts (§20): if the connection times out after the message data
phase the acceptance state is unknown; the adapter classifies the result as
DELIVERY_STATE_UNKNOWN (PERMANENT) so the queue never auto-resends — a
duplicate email is worse than an honest failure.
"""

from __future__ import annotations

import asyncio
import email.utils
import re
import smtplib
import socket
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage

from app.core.logging import get_logger
from app.services.marketing.providers.base import (
    BaseMarketingProvider,
    ErrorClass,
    SendResult,
)
from app.services.marketing.providers.email.errors import (
    AUTHENTICATION_ERROR,
    CONFIGURATION_ERROR,
    EmailErrorNormalizer,
)
from app.services.marketing.providers.interfaces import EMAIL_RE

logger = get_logger("qbit.marketing.email_smtp")

#: header values containing CR/LF are always injection attempts (§39)
_CRLF_RE = re.compile(r"[\r\n]")

SECURITY_MODES = ("TLS", "STARTTLS", "NONE")
DEFAULT_SMTP_PORT = {  # per security mode
    "TLS": 465,
    "STARTTLS": 587,
    "NONE": 25,
}
SEND_TIMEOUT_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 15


def header_safe(value: str | None) -> bool:
    """True when a header value is free of CR/LF injection (§39)."""
    if value is None:
        return True
    return not bool(_CRLF_RE.search(str(value)))


def build_message_id(sender_domain: str) -> str:
    """Generate a standards-compliant Message-ID (also our provider_message_id)."""
    domain = re.sub(r"[^a-zA-Z0-9.-]", "", sender_domain) or "qbit.local"
    return f"<{uuid.uuid4().hex}@{domain}>"


class SMTPProvider(BaseMarketingProvider):
    """SMTP adapter (multi-account: ALL account context arrives per call)."""

    provider_id = "smtp"
    channel = "EMAIL"
    interface_only = False

    def __init__(self, *, error_normalizer: EmailErrorNormalizer | None = None,
                 connect_factory=None) -> None:
        self.errors = error_normalizer or EmailErrorNormalizer()
        # connect_factory(config, credentials) -> object with .send (TEST HOOK)
        self._connect_factory = connect_factory

    # ------------------------------------------------------- configuration
    async def validate_configuration(self, config: dict) -> list[str]:
        config = config if isinstance(config, dict) else {}
        if not config.get("configured"):
            return ["Account is not configured — complete the connection wizard"]
        problems: list[str] = []
        if not str(config.get("smtp_host") or "").strip():
            problems.append("smtp_host is required")
        port = config.get("smtp_port")
        if port is not None and not str(port).strip().isdigit():
            problems.append("smtp_port must be numeric")
        security = str(config.get("smtp_security") or "STARTTLS").upper()
        if security not in SECURITY_MODES:
            problems.append(f"smtp_security must be one of {', '.join(SECURITY_MODES)}")
        sender = str(config.get("sender_email") or "").strip()
        if not sender:
            problems.append("sender_email is required")
        elif not EMAIL_RE.match(sender):
            problems.append("sender_email is not a valid address")
        reply_to = str(config.get("reply_to") or "").strip()
        if reply_to and not EMAIL_RE.match(reply_to):
            problems.append("reply_to is not a valid address")
        if not str(config.get("credential_ref") or "").strip():
            problems.append(
                "No credential reference — store the SMTP password in the encrypted vault"
            )
        return problems

    async def validate_recipient(self, address: str) -> bool:
        return bool(EMAIL_RE.match((address or "").strip()))

    async def validate_message(self, *, subject: str | None, body: str) -> list[str]:
        problems: list[str] = []
        if not (subject or "").strip():
            problems.append("Email requires a subject")
        if header_safe(subject) is False:
            problems.append("Subject contains forbidden control characters")
        if len(body or "") > 200_000:
            problems.append("Email body exceeds 200,000 characters")
        return problems

    async def validate_send_requirements(self, *, template, account_config: dict) -> list[str]:
        """Email launch requirements (§12/§37): subject + unsubscribe plan."""
        problems: list[str] = []
        sender = str((account_config or {}).get("sender_email") or "").strip()
        if not sender or not EMAIL_RE.match(sender):
            problems.append("Sending account has no valid sender_email configured")
        return problems

    # ------------------------------------------------------------------ send
    async def send(
        self, *, account_config: dict, recipient_address: str,
        subject: str | None, body: str, idempotency_key: str,
        metadata: dict | None = None, credentials: dict | None = None,
        template: dict | None = None,
    ) -> SendResult:
        config = account_config if isinstance(account_config, dict) else {}
        sender = str(config.get("sender_email") or "").strip()
        sender_name = str(config.get("sender_name") or "").strip()
        reply_to = str(config.get("reply_to") or "").strip()
        host = str(config.get("smtp_host") or "").strip()
        if not host or not sender:
            return SendResult.failure(
                "SMTP account is not fully configured (smtp_host / sender_email)",
                code=CONFIGURATION_ERROR, error_class=ErrorClass.CONFIGURATION,
            )
        if not EMAIL_RE.match(recipient_address.strip()):
            return SendResult.failure(
                "Recipient address is invalid",
                code="INVALID_RECIPIENT", error_class=ErrorClass.PERMANENT,
            )
        # §39 header-injection guard — reject before anything is sent
        for label, value in (("subject", subject), ("sender", sender),
                             ("reply_to", reply_to),
                             ("sender_name", sender_name)):
            if not header_safe(value):
                return SendResult.failure(
                    f"{label} contains forbidden control characters (header injection rejected)",
                    code="MESSAGE_REJECTED", error_class=ErrorClass.PERMANENT,
                )

        creds = credentials if isinstance(credentials, dict) else {}
        username = str(creds.get("smtp_username") or "").strip()
        password = str(creds.get("smtp_password") or "").strip()

        security = str(config.get("smtp_security") or "STARTTLS").upper()
        if security not in SECURITY_MODES:
            security = "STARTTLS"
        try:
            port = int(str(config.get("smtp_port") or "").strip() or DEFAULT_SMTP_PORT[security])
        except (TypeError, ValueError):
            port = DEFAULT_SMTP_PORT[security]

        # message payload: template carries html/text parts; body is plain text
        template = template if isinstance(template, dict) else {}
        html = str(template.get("html") or "") or None
        text = str(template.get("text") or body or "")
        headers = template.get("headers") if isinstance(template.get("headers"), dict) else {}

        sender_domain = sender.rsplit("@", 1)[-1]
        message_id = build_message_id(sender_domain)

        message = EmailMessage()
        # header injection is impossible here: every value passed the CRLF guard
        message["From"] = f"{sender_name} <{sender}>" if sender_name else sender
        message["To"] = recipient_address.strip()
        if reply_to:
            message["Reply-To"] = reply_to
        message["Subject"] = str(subject or "")
        message["Date"] = email.utils.formatdate(localtime=False)
        message["Message-ID"] = message_id
        message["X-QBIT-Idempotency-Key"] = idempotency_key[:200]
        for key, value in (headers or {}).items():
            if header_safe(key) and header_safe(value) and str(key).lower() not in (
                "from", "to", "cc", "bcc", "content-type",
            ):
                message[str(key)] = str(value)
        message.set_content(text or " ")
        if html:
            message.add_alternative(html, subtype="html")

        try:
            await self._deliver(
                config=config, host=host, port=port, security=security,
                username=username, password=password,
                sender=sender, recipient=recipient_address.strip(), message=message,
            )
        except (smtplib.SMTPResponseException,) as exc:
            normalized = self.errors.normalize_smtp(
                smtp_code=exc.smtp_code, message=exc.smtp_error.decode(errors="replace")
                if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error),
            )
            return self._failure(normalized)
        except smtplib.SMTPRecipientsRefused:
            return SendResult(
                ok=False,
                error="Recipient rejected by SMTP server",
                error_code="INVALID_RECIPIENT",
                error_class=ErrorClass.PERMANENT,
                status="FAILED",
                metadata={"provider": self.provider_id},
            )
        except (smtplib.SMTPAuthenticationError,) as exc:
            normalized = self.errors.normalize_smtp(exception=exc)
            return self._failure(normalized)
        except (smtplib.SMTPException,) as exc:
            normalized = self.errors.normalize_smtp(exception=exc)
            return self._failure(normalized)
        except (TimeoutError, socket.timeout, asyncio.TimeoutError) as exc:
            # §20: uncertain delivery — NEVER auto-retried (duplicate risk)
            normalized = self.errors.normalize_smtp(exception=exc)
            return self._failure(normalized)
        except (OSError,) as exc:
            normalized = self.errors.normalize_smtp(exception=exc)
            return self._failure(normalized)

        logger.info(
            "email_smtp_send_ok",
            extra={"extra_fields": {"message_id": message_id}},
        )
        return SendResult.success(
            message_id,
            status="SENT",
            metadata={
                "provider": self.provider_id,
                "transport": f"smtp:{security}",
                "smtp_host": host,
            },
        )

    # ---------------------------------------------------------------- health
    async def health_check(self, account_config: dict, credentials: dict | None = None) -> dict:
        """§7 probe: connect (+authenticate) + QUIT — never sends mail."""
        config = account_config if isinstance(account_config, dict) else {}
        checked_at = datetime.now(timezone.utc).isoformat()
        host = str(config.get("smtp_host") or "").strip()
        if not host:
            return {"health": "UNHEALTHY", "checked_at": checked_at,
                    "detail": "smtp_host is not configured"}
        creds = credentials if isinstance(credentials, dict) else {}
        username = str(creds.get("smtp_username") or "").strip()
        password = str(creds.get("smtp_password") or "").strip()
        security = str(config.get("smtp_security") or "STARTTLS").upper()
        try:
            port = int(str(config.get("smtp_port") or "").strip() or DEFAULT_SMTP_PORT.get(security, 587))
        except (TypeError, ValueError):
            port = 587

        try:
            await asyncio.to_thread(
                self._probe_sync, host, port, security, username, password,
            )
        except smtplib.SMTPAuthenticationError as exc:
            return {"health": "UNHEALTHY", "checked_at": checked_at,
                    "detail": f"{AUTHENTICATION_ERROR}: credentials rejected"}
        except smtplib.SMTPException as exc:
            normalized = self.errors.normalize_smtp(exception=exc)
            return {"health": "UNHEALTHY", "checked_at": checked_at,
                    "detail": f"{normalized.code}: {normalized.message[:300]}"}
        except (TimeoutError, socket.timeout, OSError) as exc:
            normalized = self.errors.normalize_smtp(exception=exc)
            return {"health": "UNHEALTHY", "checked_at": checked_at,
                    "detail": f"{normalized.code}: {normalized.message[:300]}"}
        return {"health": "HEALTHY", "checked_at": checked_at,
                "detail": {"smtp_host": host, "smtp_port": port, "security": security}}

    # ---------------------------------------------------- account validation
    async def validate_account(self, account_config: dict, credentials: dict | None = None) -> dict:
        """§7 connection flow: configuration → sender → connectivity → auth.
        Never marks anything ACTIVE — the caller decides from the steps."""
        config = account_config if isinstance(account_config, dict) else {}
        checked_at = datetime.now(timezone.utc).isoformat()
        steps: dict[str, dict] = {}

        host = str(config.get("smtp_host") or "").strip()
        sender = str(config.get("sender_email") or "").strip()
        reply_to = str(config.get("reply_to") or "").strip()
        steps["configuration"] = {
            "ok": bool(host),
            "detail": host or "smtp_host missing",
        }
        steps["sender_email"] = {
            "ok": bool(sender and EMAIL_RE.match(sender)),
            "detail": sender or "sender_email missing or invalid",
        }
        if reply_to:
            steps["reply_to"] = {
                "ok": bool(EMAIL_RE.match(reply_to)),
                "detail": reply_to if EMAIL_RE.match(reply_to) else "reply_to invalid",
            }
        creds = credentials if isinstance(credentials, dict) else {}
        username = str(creds.get("smtp_username") or "").strip()
        password = str(creds.get("smtp_password") or "").strip()
        steps["credentials"] = {
            "ok": bool(password),
            "detail": "smtp password present" if password else "smtp_password missing",
        }
        if not (host and sender and password):
            return {"ok": False, "checked_at": checked_at, "steps": steps}

        security = str(config.get("smtp_security") or "STARTTLS").upper()
        try:
            port = int(str(config.get("smtp_port") or "").strip() or DEFAULT_SMTP_PORT.get(security, 587))
        except (TypeError, ValueError):
            port = 587
        try:
            await asyncio.to_thread(
                self._probe_sync, host, port, security, username, password,
            )
            steps["connectivity"] = {"ok": True, "detail": f"{host}:{port} ({security})"}
            steps["authentication"] = {"ok": True, "detail": "credentials accepted by server"}
        except smtplib.SMTPAuthenticationError:
            steps["connectivity"] = {"ok": True, "detail": f"{host}:{port} ({security})"}
            steps["authentication"] = {"ok": False, "detail": "credentials rejected by server"}
        except (TimeoutError, socket.timeout, smtplib.SMTPException, OSError) as exc:
            normalized = self.errors.normalize_smtp(exception=exc)
            steps["connectivity"] = {
                "ok": False, "detail": f"{normalized.code}: {normalized.message[:300]}",
            }
        return {
            "ok": all(step.get("ok") for step in steps.values()),
            "checked_at": checked_at, "steps": steps,
        }

    # ---------------------------------------------------------------- events
    async def handle_event(self, payload: dict) -> dict:
        """Normalize a pre-extracted provider event (internal ingestion path)."""
        return {
            "event_type": str(payload.get("event_type") or "").upper(),
            "provider_message_id": payload.get("provider_message_id"),
            "metadata": dict(payload.get("metadata") or {}),
        }

    # --------------------------------------------------------------- helpers
    def _failure(self, normalized) -> SendResult:
        return SendResult(
            ok=False, error=normalized.message[:500], error_code=normalized.code,
            error_class=normalized.error_class, status="FAILED",
            metadata={
                "provider": self.provider_id,
                "retry_after_seconds": normalized.retry_after_seconds,
            },
        )

    @staticmethod
    def _open_server(host: str, port: int, security: str):
        """Open an SMTP session with the configured transport security."""
        timeout = CONNECT_TIMEOUT_SECONDS
        if security == "TLS":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            if security == "STARTTLS":
                server.starttls()
        return server

    @classmethod
    def _probe_sync(cls, host: str, port: int, security: str,
                    username: str, password: str) -> None:
        """Connect (+auth) + QUIT. Raises on any failure. Never sends mail."""
        server = cls._open_server(host, port, security)
        try:
            server.ehlo()
            if username and password:
                server.login(username, password)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001 — QUIT best-effort
                pass

    async def _deliver(self, *, config: dict, host: str, port: int, security: str,
                       username: str, password: str, sender: str,
                       recipient: str, message: EmailMessage) -> None:
        """Deliver one message over SMTP. Individual recipient delivery ONLY
        (§40 — the recipient is the single To: address; never CC/BCC)."""
        if self._connect_factory is not None:
            # test hook: synchronous object exposing .send
            session = self._connect_factory(
                {"host": host, "port": port, "security": security,
                 "username": username, "password": password},
            )
            await asyncio.to_thread(session.send, sender, recipient, message)
            return

        def _run() -> None:
            server = self._open_server(host, port, security)
            try:
                server.ehlo()
                if username and password:
                    server.login(username, password)
                server.send_message(message, from_addr=sender, to_addrs=[recipient])
            finally:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.wait_for(asyncio.to_thread(_run), timeout=SEND_TIMEOUT_SECONDS)
