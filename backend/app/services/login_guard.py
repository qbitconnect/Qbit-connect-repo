"""Account lockout guard (Phase 12 audit M11, brief §5 brute-force protection).

Both login surfaces (API bearer login + UI cookie login) route their failure
accounting through this module so a distributed password-guessing attempt
against one surface cannot bypass the other.

Design notes:
- Only failures where the account EXISTS and the password is wrong increment
  the counter (unknown emails reveal nothing and cannot lock a victim).
- Lockout state is constant-shaped everywhere: a locked account returns the
  same "invalid credentials" response as a wrong password, so an attacker
  cannot distinguish lockout from failure (and cannot use lockout as an
  oracle to confirm a guessed password).
- Counters reset on any successful password verification.
- Knobs: QBIT_LOGIN_MAX_FAILED_ATTEMPTS (0 disables lockout),
  QBIT_LOGIN_LOCKOUT_MINUTES. Both login endpoints stay per-IP rate limited
  independently (first line of defense); this is the per-ACCOUNT second line.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger, log_with
from app.models.user import User

logger = get_logger("qbit.login_guard")


def is_locked(user: User, *, now: datetime | None = None) -> bool:
    """True while the account is under a temporary login lockout."""
    if user.locked_until is None:
        return False
    current = now or datetime.now(timezone.utc)
    locked_until = user.locked_until
    if locked_until.tzinfo is None:  # defensive: naive DB values
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return current < locked_until


async def register_failure(
    session: AsyncSession, user: User, settings: Settings
) -> None:
    """Count a failed password attempt; lock the account at the threshold."""
    if settings.QBIT_LOGIN_MAX_FAILED_ATTEMPTS <= 0:
        return
    user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
    if user.failed_login_attempts >= settings.QBIT_LOGIN_MAX_FAILED_ATTEMPTS:
        user.locked_until = datetime.now(timezone.utc) + timedelta(
            minutes=settings.QBIT_LOGIN_LOCKOUT_MINUTES
        )
        user.failed_login_attempts = 0  # window restarts after the lock expires
        log_with(
            logger, 30, "Account locked after repeated failed logins",
            user_id=str(user.id),
            lockout_minutes=settings.QBIT_LOGIN_LOCKOUT_MINUTES,
        )
    await session.commit()


async def register_success(session: AsyncSession, user: User) -> None:
    """Reset the failure counter and any lingering lockout on success."""
    if user.failed_login_attempts or user.locked_until:
        user.failed_login_attempts = 0
        user.locked_until = None
        await session.commit()
