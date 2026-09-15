"""
Context store — CRUD for context_versions and templates tables.

Design:
- Context types: "resume", "github", "linkedin", "portfolio"
- Each upload appends a new version row (audit trail, latest-wins).
- get_context() returns the latest version of each type, merged into one dict.
- get_context_for_jd() additionally filters github repos to the top-N most
  relevant for the given JD text (keeps context window small per doc spec).
- Templates are stored one-per-user (upsert on write).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import func, select

from db.models import ContextVersion, Template
from db.session import get_session

logger = logging.getLogger(__name__)

# Context type constants
TYPE_RESUME    = "resume"
TYPE_GITHUB    = "github"
TYPE_LINKEDIN  = "linkedin"
TYPE_PORTFOLIO = "portfolio"

ALL_TYPES = (TYPE_RESUME, TYPE_GITHUB, TYPE_LINKEDIN, TYPE_PORTFOLIO)

# How many GitHub repos to inject per JD (keeps context window tight)
DEFAULT_TOP_N_REPOS = 3


# ---------------------------------------------------------------------------
# Context version CRUD
# ---------------------------------------------------------------------------

async def upsert_context(user_id: int, type: str, content: str) -> ContextVersion:
    """
    Append a new version row for this (user, type).
    Latest-version-wins on read; old versions kept for audit.
    """
    async with get_session() as session:
        result = await session.execute(
            select(func.max(ContextVersion.version)).where(
                ContextVersion.user_id == user_id,
                ContextVersion.type == type,
            )
        )
        max_version: int = result.scalar() or 0
        cv = ContextVersion(
            user_id=user_id,
            type=type,
            content=content,
            version=max_version + 1,
        )
        session.add(cv)
        await session.commit()
        await session.refresh(cv)
        logger.info(
            "Context upserted: user=%d type=%s version=%d (%d chars)",
            user_id, type, cv.version, len(content),
        )
        return cv


async def get_context(user_id: int) -> dict[str, str]:
    """
    Return the latest version of each context type as a flat dict.
    Keys: "resume", "github", "linkedin", "portfolio".
    Missing types are simply absent from the dict.
    """
    async with get_session() as session:
        # Subquery: max version per type for this user
        subq = (
            select(
                ContextVersion.type,
                func.max(ContextVersion.version).label("max_ver"),
            )
            .where(ContextVersion.user_id == user_id)
            .group_by(ContextVersion.type)
            .subquery()
        )
        result = await session.execute(
            select(ContextVersion).join(
                subq,
                (ContextVersion.type == subq.c.type)
                & (ContextVersion.version == subq.c.max_ver)
                & (ContextVersion.user_id == user_id),
            )
        )
        rows = result.scalars().all()
        return {row.type: row.content for row in rows}


async def has_context(user_id: int) -> bool:
    """Return True if the user has at least one context item (any type)."""
    async with get_session() as session:
        result = await session.execute(
            select(func.count()).where(ContextVersion.user_id == user_id)
        )
        return (result.scalar() or 0) > 0


async def get_context_for_jd(
    user_id: int,
    jd_text: str,
    top_n_repos: int = DEFAULT_TOP_N_REPOS,
) -> dict[str, str]:
    """
    Like get_context(), but the github entry is filtered to the top-N repos
    most relevant to the JD text (keyword overlap). Keeps the context window
    tight during generation per Phase 2 spec.
    """
    ctx = await get_context(user_id)
    if TYPE_GITHUB in ctx:
        ctx[TYPE_GITHUB] = _filter_relevant_repos(
            ctx[TYPE_GITHUB], jd_text, top_n_repos
        )
    return ctx


def _filter_relevant_repos(
    github_content: str,
    jd_text: str,
    n: int,
) -> str:
    """
    Parse the stored JSON repo list, score each repo by keyword overlap with
    the JD text, and return the top-N as a formatted string.

    github_content format (set by context/github.py):
    JSON array of {"name", "description", "language", "readme_summary"} dicts.

    Falls back to raw content if JSON parse fails (e.g., legacy plain text).
    """
    try:
        repos: list[dict] = json.loads(github_content)
    except (json.JSONDecodeError, ValueError):
        # Legacy plain text — return as-is (no filtering possible)
        return github_content

    if not repos:
        return ""

    jd_lower = jd_text.lower()
    jd_words = set(w for w in jd_lower.split() if len(w) > 3)

    def _score(repo: dict) -> int:
        text = " ".join(filter(None, [
            repo.get("name", ""),
            repo.get("description", ""),
            repo.get("language", ""),
            repo.get("readme_summary", ""),
        ])).lower()
        return sum(1 for w in jd_words if w in text)

    scored = sorted(repos, key=_score, reverse=True)
    top = scored[:n]

    lines = []
    for r in top:
        name    = r.get("name", "?")
        desc    = r.get("description") or "No description"
        lang    = r.get("language") or "unknown"
        summary = r.get("readme_summary", "")
        lines.append(f"• {name} ({lang}): {desc}")
        if summary:
            lines.append(f"  Summary: {summary}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Template CRUD
# ---------------------------------------------------------------------------

async def get_template(user_id: int) -> Optional[str]:
    """Return the user's style template text, or None if not set."""
    async with get_session() as session:
        result = await session.execute(
            select(Template).where(Template.user_id == user_id)
        )
        tmpl = result.scalar_one_or_none()
        return tmpl.template_text if tmpl else None


async def set_template(user_id: int, template_text: str) -> Template:
    """Upsert the user's style template (one row per user)."""
    async with get_session() as session:
        result = await session.execute(
            select(Template).where(Template.user_id == user_id)
        )
        tmpl = result.scalar_one_or_none()
        if tmpl:
            tmpl.template_text = template_text
        else:
            tmpl = Template(user_id=user_id, template_text=template_text)
            session.add(tmpl)
        await session.commit()
        await session.refresh(tmpl)
        logger.info("Template updated for user %d", user_id)
        return tmpl
