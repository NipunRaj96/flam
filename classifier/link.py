"""
Link & Channel Classifier — extracts application channels from pasted job postings.

Classifies incoming text into:
1. Known form platforms (loaded dynamically from /form_knowledge_base/*.json):
   - google_forms, ms_forms, typeform, notion_forms
2. Email-based applications (mailto: or recruitment email addresses like jobs@, careers@, apply@)
3. Custom career pages (Greenhouse, Lever, Workday, company ATS pages)
"""
from __future__ import annotations

import re
from typing import Optional

from executor.knowledge_base import discover_platform_patterns, get_supported_platforms

_URL_RE = re.compile(
    r"https?://"
    r"[^\s<>\"{}|\\^`\[\]]+"
)

_MAILTO_RE = re.compile(r"mailto:([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b")

_RECRUITMENT_KEYWORDS = {
    "apply", "resume", "cv", "job", "career", "hiring", "position", "candidate", "role"
}


def extract_urls(text: str) -> list[str]:
    """Return all http/https URLs found in the text."""
    return _URL_RE.findall(text)


def classify_url(url: str) -> Optional[str]:
    """
    Map a URL to a known platform identifier using dynamic patterns from form_knowledge_base/.
    Returns None if the URL does not match any known platform.
    """
    patterns = discover_platform_patterns()
    for platform, pats in patterns.items():
        for pattern in pats:
            if re.search(pattern, url, re.IGNORECASE):
                return platform
    return None


def find_application_channel(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Identify the application channel from pasted text.

    Returns:
        (channel_type, target):
          - ('google_forms' | 'ms_forms' | 'typeform' | 'notion_forms', form_url)
          - ('email_application', email_address)
          - ('custom_career_page', page_url)
          - (None, None) if no application target found.
    """
    urls = extract_urls(text)

    # 1. Check if any URL matches a known platform in form_knowledge_base
    for url in urls:
        platform = classify_url(url)
        if platform:
            return platform, url

    # 2. Check for mailto: or recruitment email address
    mailto_match = _MAILTO_RE.search(text)
    if mailto_match:
        return "email_application", mailto_match.group(1)

    text_lower = text.lower()
    has_recruitment_kw = any(kw in text_lower for kw in _RECRUITMENT_KEYWORDS)

    if has_recruitment_kw:
        emails = _EMAIL_RE.findall(text)
        if emails:
            # Prefer emails with apply/careers/jobs in username
            for email in emails:
                local_part = email.split("@")[0].lower()
                if any(k in local_part for k in ("apply", "career", "job", "talent", "hr")):
                    return "email_application", email
            return "email_application", emails[0]

    # 3. If there are other URLs and recruitment context, treat as custom career page
    if urls:
        # Ignore social links or generic domains if possible
        for url in urls:
            u_lower = url.lower()
            if not any(skip in u_lower for skip in ("linkedin.com/in/", "github.com/", "twitter.com/", "x.com/")):
                return "custom_career_page", url
        return "custom_career_page", urls[0]

    return None, None


# Backward compatibility helpers
def find_form_url(text: str) -> tuple[Optional[str], Optional[str]]:
    """Legacy helper for finding form URLs."""
    channel, target = find_application_channel(text)
    if channel and channel != "email_application":
        return channel, target
    return None, None


def is_supported(platform: Optional[str]) -> bool:
    """Return True if the platform is supported in form_knowledge_base or custom page / email."""
    if not platform:
        return False
    if platform in ("custom_career_page", "email_application"):
        return True
    return platform in get_supported_platforms()
