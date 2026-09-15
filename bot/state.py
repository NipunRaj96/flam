"""
In-memory state management for active Telegram conversations.

Tracks:
1. Pending applications waiting for /approve or /cancel.
2. Multi-step command prompts (e.g. waiting for resume PDF/text or LinkedIn text).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import Page


@dataclass
class PendingApplication:
    application_id: int
    form_url: str
    platform: str
    filled_fields: list[dict]
    page: Optional[Page] = None   # Playwright page — kept open until submit/cancel (None for email)
    jd_hash: str = ""             # stored so we can show in duplicate message
    form_id: str = ""             # canonical form URL or email target
    user_template: Optional[str] = None
    email_draft: Optional[dict] = None


# chat_id → PendingApplication
_pending: dict[int, PendingApplication] = {}

# chat_id → action awaiting user input ("waiting_resume", "waiting_linkedin", "waiting_template", etc.)
_waiting_action: dict[int, str] = {}


def store(chat_id: int, pending: PendingApplication) -> None:
    _pending[chat_id] = pending


def get(chat_id: int) -> PendingApplication | None:
    return _pending.get(chat_id)


def remove(chat_id: int) -> PendingApplication | None:
    return _pending.pop(chat_id, None)


def has_pending(chat_id: int) -> bool:
    return chat_id in _pending


def set_waiting(chat_id: int, action: str) -> None:
    _waiting_action[chat_id] = action


def get_waiting(chat_id: int) -> Optional[str]:
    return _waiting_action.get(chat_id)


def clear_waiting(chat_id: int) -> Optional[str]:
    return _waiting_action.pop(chat_id, None)
