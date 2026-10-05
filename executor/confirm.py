"""
Decide whether a submit really went through.

A click is not a submission. After clicking we watch for any of: the platform's
confirmation element, a URL that looks like a thank-you/response page (Google Forms
goes to .../formResponse), or confirmation wording in the page text.
"""
from __future__ import annotations

from typing import Optional

from playwright.async_api import Page

_PHRASES = (
    "response has been recorded", "your response was submitted", "response was recorded",
    "thanks for submitting", "thank you for submitting", "thank you for applying",
    "thanks for applying", "application has been submitted", "application was submitted",
    "application submitted", "received your application", "submission has been received",
    "successfully submitted", "we'll be in touch", "we will be in touch",
)
_URL_HINTS = ("formresponse", "thank", "confirm", "success", "submitted")


async def wait_for_confirmation(page: Page, selector: Optional[str] = None, timeout_ms: int = 15_000) -> bool:
    start_url = page.url.lower()
    waited = 0
    while waited <= timeout_ms:
        if selector:
            try:
                if await page.locator(selector).first.is_visible():
                    return True
            except Exception:
                pass
        url = page.url.lower()
        if url != start_url and any(h in url for h in _URL_HINTS):
            return True
        try:
            body = (await page.inner_text("body")).lower()
            if any(p in body for p in _PHRASES):
                return True
        except Exception:
            pass  # page is mid-navigation
        await page.wait_for_timeout(500)
        waited += 500
    return False
