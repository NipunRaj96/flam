"""
In-memory state for active Telegram conversations.

Tracks:
1. Pending applications waiting for /approve or /cancel (one per chat).
2. Multi-step prompts (waiting for resume text, LinkedIn text, template text).

Pending applications hold a live browser page, so they cannot survive a restart;
main.py cancels any leftover DB drafts at startup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from playwright.async_api import Page


@dataclass
class PendingApplication:
    application_id: int
    form_url: str
    platform: str
    filled_fields: list[dict]
    page: Optional[Page] = None          # live Playwright page; None for email
    jd_hash: str = ""
    form_id: str = ""
    email_draft: Optional[dict] = None
    post: Any = None                     # intake.post.PostInfo
    notes: list[str] = field(default_factory=list)
    warned: bool = False                 # user has seen the 'some answers are empty' warning


_pending: dict[int, PendingApplication] = {}
_waiting_action: dict[int, str] = {}


def store(chat_id: int, pending: PendingApplication) -> None:
    _pending[chat_id] = pending


def get(chat_id: int) -> Optional[PendingApplication]:
    return _pending.get(chat_id)


def remove(chat_id: int) -> Optional[PendingApplication]:
    return _pending.pop(chat_id, None)


def has_pending(chat_id: int) -> bool:
    return chat_id in _pending


def all_pending() -> list[PendingApplication]:
    return list(_pending.values())


def set_waiting(chat_id: int, action: str) -> None:
    _waiting_action[chat_id] = action


def get_waiting(chat_id: int) -> Optional[str]:
    return _waiting_action.get(chat_id)


def clear_waiting(chat_id: int) -> Optional[str]:
    return _waiting_action.pop(chat_id, None)
