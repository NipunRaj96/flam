"""
Link & channel classifier — finds how to apply from pasted (or screenshot-read) text.

Channels, in priority order:
1. Known form platform URL (loaded from /form_knowledge_base/*.json)
2. An email address (any email in the post: the user sent it to apply)
3. Any other apply-looking URL -> custom career page
"""
from __future__ import annotations

import re
from typing import Optional

from executor.knowledge_base import discover_platform_patterns, get_supported_platforms

_URL_RE = re.compile(r"https?://[^\s<>\"{}|\\^`\[\]]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_MAILTO_RE = re.compile(r"mailto:([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)", re.IGNORECASE)

# "name [at] company [dot] com", "name (at) company.com"
_AT_RE = re.compile(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", re.IGNORECASE)
_DOT_RE = re.compile(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", re.IGNORECASE)
# OCR / formatting often puts spaces around the @:  "name @ company.com"
_SPACED_AT_RE = re.compile(r"([A-Za-z0-9._%+-]+)\s+@\s+([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")

_HINT_WORDS = ("mail", "email", "e-mail", "send", "apply", "share", "drop", "reach", "forward",
               "resume", "cv", "contact", "write to", "ping", "dm")
_HIRING_LOCALS = ("career", "job", "hr", "talent", "recruit", "hiring", "apply", "people")
_NOISE_LOCALS = ("noreply", "no-reply", "donotreply", "do-not-reply", "unsubscribe")

# Never an apply target: social/login-walled pages.
_SKIP_DOMAINS = (
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "t.me",
    "wa.me", "youtube.com", "youtu.be", "github.com", "medium.com", "discord.gg",
)


def deobfuscate(text: str) -> str:
    text = _AT_RE.sub("@", text)
    text = _DOT_RE.sub(".", text)
    return _SPACED_AT_RE.sub(r"\1@\2", text)


def extract_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:!?)'\"]") for u in _URL_RE.findall(text)]


def extract_emails(text: str) -> list[str]:
    text = deobfuscate(text)
    seen: list[str] = []
    for e in _MAILTO_RE.findall(text) + _EMAIL_RE.findall(text):
        e = e.strip(".").lower()
        if e not in seen:
            seen.append(e)
    return seen


def pick_apply_email(text: str, emails: list[str]) -> Optional[str]:
    """Choose the address the post tells you to apply to."""
    if not emails:
        return None
    flat = deobfuscate(text).lower()

    def score(email: str) -> int:
        s = 0
        i = flat.find(email)
        window = flat[max(0, i - 90): i] if i >= 0 else ""
        if any(w in window for w in _HINT_WORDS):
            s += 2
        local = email.split("@")[0]
        if any(k in local for k in _HIRING_LOCALS):
            s += 1
        if any(k in local for k in _NOISE_LOCALS):
            s -= 3
        return s

    return max(emails, key=score)  # max() keeps the first of equal scores


def classify_url(url: str) -> Optional[str]:
    for platform, pats in discover_platform_patterns().items():
        if any(re.search(p, url, re.IGNORECASE) for p in pats):
            return platform
    return None


def find_application_channel(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (channel, target):
      ('google_forms'|'ms_forms'|'typeform'|'notion_forms', url)
      ('email_application', address)
      ('custom_career_page', url)
      (None, None) when there is nothing to apply to.
    """
    urls = extract_urls(text)

    for url in urls:
        platform = classify_url(url)
        if platform:
            return platform, url

    email = pick_apply_email(text, extract_emails(text))
    if email:
        return "email_application", email

    for url in urls:
        if not any(d in url.lower() for d in _SKIP_DOMAINS):
            return "custom_career_page", url

    return None, None


def is_supported(platform: Optional[str]) -> bool:
    if not platform:
        return False
    return platform in ("custom_career_page", "email_application") or platform in get_supported_platforms()
