"""
Idempotency, rate-limiting, and application lifecycle.

RULE (doc 02 + 05): the duplicate check fires BEFORE any form-filling work starts.
The DB UniqueConstraint (user_id, jd_hash, form_id) is the hard backstop; this module
is the soft pre-check that gives the user a friendly message.

A failed, cancelled or abandoned attempt never blocks a retry: only applications that
were actually submitted (or submitted without a confirmation page) count as duplicates.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from db.models import Application, ApplicationAnswer, User
from db.session import get_session

logger = logging.getLogger(__name__)

_DONE = ("submitted", "unconfirmed")      # statuses that mean "already applied"
_OPEN = ("drafted", "pending_approval")   # statuses a restart should clean up

# Query params that identify WHICH job a career-page URL points at.
_KEEP_PARAMS = {"gh_jid", "jid", "job_id", "jobid", "id", "req_id", "requisition", "lever-source"}


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def compute_jd_hash(text: str) -> str:
    return hashlib.sha256(_normalize_text(text).encode()).hexdigest()


def stable_jd_hash(company: str, role: str, text: str) -> str:
    """
    Same job -> same hash, even when the post is re-typed or re-screenshotted (OCR text
    differs every time). Falls back to hashing the full text when role/company are unknown.
    """
    if company.strip() and role.strip():
        return compute_jd_hash(f"{company}|{role}")
    return compute_jd_hash(text)


def _strip_url(url: str) -> str:
    p = urlparse(url)
    keep = [(k, v) for k, v in parse_qsl(p.query) if k.lower() in _KEEP_PARAMS]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", urlencode(keep), ""))


async def resolve_canonical_url(url: str) -> str:
    """Follow redirects (forms.gle -> docs.google.com) and drop tracking params."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
            resp = await client.head(url)
            if resp.status_code in (403, 405, 501):
                resp = await client.get(url)
            return _strip_url(str(resp.url))
    except Exception:
        return _strip_url(url)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def get_or_create_user(telegram_id: int) -> User:
    async with get_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


# ---------------------------------------------------------------------------
# Duplicate check + rate limit
# ---------------------------------------------------------------------------

async def check_duplicate(user_id: int, jd_hash: str, form_id: str, *, by_form_only: bool = False) -> Optional[Application]:
    """
    Existing finished application for this (user, job, target), else None.
    by_form_only=True also matches the same form URL under a different post text; use it for
    forms (one URL = one application) but not for email (one inbox, many roles).
    """
    async with get_session() as session:
        cond = [Application.user_id == user_id, Application.form_id == form_id, Application.status.in_(_DONE)]
        if not by_form_only:
            cond.append(Application.jd_hash == jd_hash)
        result = await session.execute(select(Application).where(*cond).limit(1))
        return result.scalars().first()


async def check_rate_limit(user_id: int) -> tuple[bool, int, int]:
    max_daily = int(os.getenv("MAX_DAILY_SUBMISSIONS", "20"))
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_session() as session:
        result = await session.execute(
            select(func.count(Application.id)).where(
                Application.user_id == user_id,
                Application.status.in_(_DONE),
                Application.submitted_at >= today_start,
            )
        )
        count = result.scalar() or 0
        return count >= max_daily, count, max_daily


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def create_application(
    user_id: int, jd_hash: str, form_id: str, platform: str, role: str = "", company: str = ""
) -> Optional[Application]:
    """
    Start (or restart) an application. Reuses a failed/cancelled/abandoned row because the
    unique constraint allows only one row per (user, job, target).
    Returns None if it was already submitted (or a concurrent insert won the race).
    """
    async with get_session() as session:
        result = await session.execute(
            select(Application).where(
                Application.user_id == user_id,
                Application.jd_hash == jd_hash,
                Application.form_id == form_id,
            )
        )
        app = result.scalar_one_or_none()
        if app is not None:
            if app.status in _DONE:
                return None
            app.status = "drafted"
            app.failure_reason = None
            app.platform, app.role, app.company = platform, role or None, company or None
            await session.commit()
            return app

        app = Application(
            user_id=user_id, jd_hash=jd_hash, form_id=form_id, platform=platform,
            status="drafted", role=role or None, company=company or None,
        )
        session.add(app)
        try:
            await session.commit()
            await session.refresh(app)
            return app
        except IntegrityError:
            await session.rollback()
            return None


async def _set_status(application_id: int, status: str, **extra) -> None:
    async with get_session() as session:
        app = (await session.execute(select(Application).where(Application.id == application_id))).scalar_one()
        app.status = status
        for k, v in extra.items():
            setattr(app, k, v)
        await session.commit()


async def mark_pending_approval(application_id: int) -> None:
    await _set_status(application_id, "pending_approval")


async def mark_submitted(application_id: int, receipt_path: str, confirmed: bool = True) -> None:
    await _set_status(
        application_id, "submitted" if confirmed else "unconfirmed",
        submitted_at=datetime.utcnow(), receipt_path=receipt_path,
    )


async def mark_failed(application_id: int, reason: str = "") -> None:
    await _set_status(application_id, "failed", failure_reason=reason[:1000] or None)


async def mark_cancelled(application_id: int) -> None:
    await _set_status(application_id, "cancelled")


async def cancel_stale_applications() -> int:
    """Called at startup: drafts left open by a crash/restart have no live page any more."""
    async with get_session() as session:
        result = await session.execute(
            update(Application).where(Application.status.in_(_OPEN)).values(status="cancelled")
        )
        await session.commit()
        return result.rowcount or 0


async def recent_applications(user_id: int, limit: int = 10) -> list[Application]:
    async with get_session() as session:
        result = await session.execute(
            select(Application).where(Application.user_id == user_id)
            .order_by(Application.id.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def save_application_answers(application_id: int, filled_fields: list[dict]) -> None:
    """Persist answers (what was actually in the form at submit time) for audit and learning."""
    import json
    async with get_session() as session:
        for f in filled_fields:
            session.add(ApplicationAnswer(
                application_id=application_id,
                question_text=f.get("question", ""),
                drafted_answer=f.get("value"),
                user_edited=bool(f.get("edited")),
                source=f.get("source"),
                context_keys_used=json.dumps(f.get("context_keys_used", [])),
            ))
        await session.commit()
        logger.info("Saved %d answer record(s) for app %d", len(filled_fields), application_id)
