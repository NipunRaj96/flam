"""
Idempotency, rate-limiting, and application lifecycle.

RULE (doc 02 + 05): The duplicate check fires BEFORE any form-filling work
starts. The application row is created (status=drafted) immediately after the
check passes. The DB UniqueConstraint is the hard backstop; this module is
the soft pre-check that gives the user a friendly message.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime
from urllib.parse import urlparse, urlunparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from db.models import Application, ApplicationAnswer, User
from db.session import get_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    """Collapse whitespace and lowercase so trivial rephrasing doesn't bypass dedup."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def compute_jd_hash(text: str) -> str:
    """SHA-256 of normalised job-post text."""
    return hashlib.sha256(_normalize_text(text).encode()).hexdigest()


def _strip_url(url: str) -> str:
    """Remove query params and fragment; keep scheme + host + path."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))


async def resolve_canonical_url(url: str) -> str:
    """
    Follow HTTP redirects to get the canonical URL (handles forms.gle → docs.google.com).
    Falls back to the stripped input URL on any network error.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            resp = await client.head(url)
            return _strip_url(str(resp.url))
    except Exception:
        return _strip_url(url)


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

async def get_or_create_user(telegram_id: int) -> User:
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


# ---------------------------------------------------------------------------
# Duplicate check
# ---------------------------------------------------------------------------

async def check_duplicate(
    user_id: int, jd_hash: str, form_id: str
) -> Application | None:
    """
    Returns the existing Application if one exists for this (user, jd, form),
    else None. Called BEFORE any Playwright work.
    """
    async with get_session() as session:
        result = await session.execute(
            select(Application).where(
                Application.user_id == user_id,
                Application.jd_hash == jd_hash,
                Application.form_id == form_id,
            )
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

async def check_rate_limit(user_id: int) -> tuple[bool, int, int]:
    """
    Returns (is_limited, today_count, max_daily).
    is_limited is True if the user has reached/exceeded their daily submission cap.
    Cap is read from MAX_DAILY_SUBMISSIONS env var (default 20).
    """
    max_daily = int(os.getenv("MAX_DAILY_SUBMISSIONS", "20"))
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        result = await session.execute(
            select(func.count(Application.id)).where(
                Application.user_id == user_id,
                Application.status == "submitted",
                Application.submitted_at >= today_start,
            )
        )
        count = result.scalar() or 0
        is_limited = count >= max_daily
        return is_limited, count, max_daily


# ---------------------------------------------------------------------------
# Application creation
# ---------------------------------------------------------------------------

async def create_application(
    user_id: int, jd_hash: str, form_id: str, platform: str
) -> Application | None:
    """
    Insert a new application row (status=drafted).
    Returns None if the DB unique constraint fires (race-condition backstop).
    The caller should treat None as a duplicate, same as check_duplicate().
    """
    async with get_session() as session:
        app = Application(
            user_id=user_id,
            jd_hash=jd_hash,
            form_id=form_id,
            platform=platform,
            status="drafted",
        )
        session.add(app)
        try:
            await session.commit()
            await session.refresh(app)
            return app
        except IntegrityError:
            await session.rollback()
            return None


# ---------------------------------------------------------------------------
# Status updates
# ---------------------------------------------------------------------------

async def mark_pending_approval(application_id: int) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Application).where(Application.id == application_id)
        )
        app = result.scalar_one()
        app.status = "pending_approval"
        await session.commit()


async def mark_submitted(application_id: int, receipt_path: str) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Application).where(Application.id == application_id)
        )
        app = result.scalar_one()
        app.status = "submitted"
        app.submitted_at = datetime.utcnow()
        app.receipt_path = receipt_path
        await session.commit()


async def mark_failed(application_id: int, reason: str = "") -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Application).where(Application.id == application_id)
        )
        app = result.scalar_one()
        app.status = "failed"
        await session.commit()


async def mark_cancelled(application_id: int) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Application).where(Application.id == application_id)
        )
        app = result.scalar_one()
        app.status = "cancelled"
        await session.commit()


async def save_application_answers(
    application_id: int,
    filled_fields: list[dict],
) -> None:
    """
    Persist drafted answers for this application into the application_answers table.
    Enables post-submission review, auditing, and doc 04 context traceability.
    """
    async with get_session() as session:
        for field in filled_fields:
            ans = ApplicationAnswer(
                application_id=application_id,
                question_text=field.get("question", ""),
                drafted_answer=field.get("value"),
                user_edited=False,
                source=field.get("source"),
                context_keys_used=json.dumps(field.get("context_keys_used", [])),
            )
            session.add(ans)
        await session.commit()
        logger.info("Saved %d answer record(s) for app %d", len(filled_fields), application_id)

