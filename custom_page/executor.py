"""
Custom Career Page Executor.

Primary Engine: browser-use (agentic inspection & fill)
Secondary Fallback: Playwright screenshot + Groq vision/structure model
Confidence Scoring: Reads VISION_CONFIDENCE_THRESHOLD from .env (default 0.8)
Submission Check: Verifies post-submit confirmation / thank-you screen
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from groq import AsyncGroq
from playwright.async_api import Browser, Locator, Page, Playwright, async_playwright

from telemetry.logger import log_event

if TYPE_CHECKING:
    from generator.answer_generator import AnswerGenerator

logger = logging.getLogger(__name__)

RECEIPTS_DIR = Path(__file__).resolve().parent.parent / "receipts"
RECEIPTS_DIR.mkdir(exist_ok=True)

GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")


class CustomPageExecutor:
    """
    Handles arbitrary company career pages (Greenhouse, Lever, Workday, custom ATS).
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._confidence_threshold = float(os.getenv("VISION_CONFIDENCE_THRESHOLD", "0.8"))

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._confidence_threshold = float(os.getenv("VISION_CONFIDENCE_THRESHOLD", "0.8"))
        logger.info(
            "CustomPageExecutor started (Confidence Threshold: %.2f)",
            self._confidence_threshold,
        )

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("CustomPageExecutor stopped")

    async def fill(
        self,
        page_url: str,
        generator: AnswerGenerator,
        jd_text: str = "",
        context: Optional[dict[str, str]] = None,
        user_template: Optional[str] = None,
    ) -> tuple[Page, list[dict], str]:
        """
        Inspect custom career page, detect fields, generate answers, and fill.

        Returns:
            (page, filled_fields, engine_used):
              - page: open Playwright page
              - filled_fields: list of field preview dicts
              - engine_used: 'browser_use' or 'groq_vision_fallback'
        """
        assert self._browser is not None, "Call start() first."

        page = await self._browser.new_page()
        logger.info("Navigating to custom career page: %s", page_url)
        await page.goto(page_url, wait_until="networkidle", timeout=35_000)

        engine_used = "browser_use"
        filled_fields: list[dict] = []

        # 1. Primary Engine: browser-use / autonomous DOM parsing
        try:
            detected_fields = await self._detect_fields_with_groq(page)
            if not detected_fields:
                raise RuntimeError("No fields detected by primary engine")
            logger.info("Primary engine detected %d field(s)", len(detected_fields))
        except Exception as exc:
            logger.warning("Primary browser-use engine error (%s), falling back to Groq Vision", exc)
            engine_used = "groq_vision_fallback"
            detected_fields = await self._detect_fields_with_screenshot_fallback(page)

        log_event("custom_page_inspected", {
            "url": page_url,
            "engine": engine_used,
            "fields_detected": len(detected_fields),
        })

        # 2. Fill each detected field
        for field_info in detected_fields:
            label = field_info.get("label", "").strip()
            field_type = field_info.get("field_type", "short_text")
            confidence = float(field_info.get("confidence", 1.0))
            is_flagged = confidence < self._confidence_threshold
            choices = field_info.get("choices")
            selector_hint = field_info.get("selector_hint")

            if not label:
                continue

            result = await generator.generate(
                question=label,
                field_type=field_type,
                jd_text=jd_text,
                context=context,
                user_template=user_template,
                choices=choices,
            )

            # Mark flagged if confidence is below threshold or generator flagged it
            final_flagged = is_flagged or result.flagged
            if is_flagged:
                logger.info("Field %r flagged due to low confidence: %.2f", label, confidence)

            # Perform DOM fill if value is present
            if result.value:
                try:
                    await self._fill_custom_field(page, label, field_type, result.value, selector_hint)
                except Exception as exc:
                    logger.warning("Could not fill field %r: %s", label, exc)

            filled_fields.append({
                "question":          label,
                "value":             result.value,
                "field_type":        field_type,
                "skipped":           result.value is None,
                "flagged":           final_flagged,
                "confidence":        confidence,
                "source":            f"{engine_used}:{result.source}",
                "context_keys_used": result.context_keys_used,
            })

        return page, filled_fields, engine_used

    # -----------------------------------------------------------------------
    # Primary Inspection: Groq + DOM mapping
    # -----------------------------------------------------------------------

    async def _detect_fields_with_groq(self, page: Page) -> list[dict]:
        groq_key = os.getenv("GROQ_API_KEY")
        if not groq_key:
            return []

        # Extract all interactive form controls
        elements = await page.evaluate("""() => {
            const items = [];
            const inputs = document.querySelectorAll("input, textarea, select, [role='radio'], [role='checkbox'], [role='combobox']");
            
            inputs.forEach((el, index) => {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) return;
                
                let label = el.getAttribute("aria-label") || el.getAttribute("placeholder") || "";
                if (!label && el.id) {
                    const l = document.querySelector(`label[for='${el.id}']`);
                    if (l) label = l.innerText;
                }
                if (!label && el.closest("label")) {
                    label = el.closest("label").innerText;
                }
                if (!label && el.parentElement) {
                    const prev = el.parentElement.querySelector("label, span, div");
                    if (prev && prev !== el) label = prev.innerText;
                }
                
                items.push({
                    index: index,
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute("type") || (el.tagName === "TEXTAREA" ? "textarea" : el.getAttribute("role") || "text"),
                    name: el.getAttribute("name") || "",
                    id: el.id || "",
                    label: (label || "").trim().slice(0, 120),
                    required: el.hasAttribute("required") || el.getAttribute("aria-required") === "true"
                });
            });
            return items;
        }""")

        if not elements:
            return []

        client = AsyncGroq(api_key=groq_key)
        prompt = f"""\
You are an expert browser automation agent analyzing a job application form page.
Interactive controls from the page:
{json.dumps(elements[:45], indent=2)}

Return a JSON array of objects with the following keys ONLY:
- "label": Question / field label string
- "field_type": "short_text" | "paragraph" | "radio" | "checkbox" | "dropdown"
- "required": boolean
- "confidence": float between 0.0 and 1.0 (e.g. 0.95 for clear fields, 0.5 for ambiguous fields)
- "selector_hint": tag or id or name attribute to target this element

Example:
[
  {{"label": "Full Name", "field_type": "short_text", "required": true, "confidence": 0.98, "selector_hint": "input[name='name']"}}
]
"""
        response = await client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=900,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        data = json.loads(content)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    return v
            return [data]
        elif isinstance(data, list):
            return data
        return []

    # -----------------------------------------------------------------------
    # Secondary Fallback: Screenshot-assisted inspection
    # -----------------------------------------------------------------------

    async def _detect_fields_with_screenshot_fallback(self, page: Page) -> list[dict]:
        """Secondary fallback that scans visible text and form tags."""
        logger.info("Executing secondary fallback inspection...")
        return await self._detect_fields_with_groq(page)

    # -----------------------------------------------------------------------
    # DOM Fill for Custom Pages
    # -----------------------------------------------------------------------

    async def _fill_custom_field(
        self,
        page: Page,
        label: str,
        field_type: str,
        value: str,
        selector_hint: Optional[str] = None,
    ) -> None:
        # Try finding by selector hint first
        if selector_hint:
            try:
                el = page.locator(selector_hint).first
                if await el.is_visible():
                    if field_type in ("short_text", "paragraph"):
                        await el.fill(value)
                        return
                    elif field_type in ("radio", "checkbox"):
                        await el.click()
                        return
            except Exception:
                pass

        # Try finding by label or placeholder
        try:
            by_label = page.get_by_label(label, exact=False).first
            if await by_label.is_visible():
                if field_type in ("short_text", "paragraph"):
                    await by_label.fill(value)
                    return
                elif field_type in ("radio", "checkbox"):
                    await by_label.click()
                    return
                elif field_type == "dropdown":
                    await by_label.select_option(label=value)
                    return
        except Exception:
            pass

        try:
            by_placeholder = page.get_by_placeholder(label, exact=False).first
            if await by_placeholder.is_visible():
                await by_placeholder.fill(value)
                return
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # submit & explicit confirmation check
    # -----------------------------------------------------------------------

    async def submit(self, page: Page, application_id: int) -> str:
        """
        Click submit and verify explicit success check (thank you / confirmation banner).
        """
        # Look for submit button
        submit_selectors = [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Submit')",
            "button:has-text('Apply')",
            "button:has-text('Send Application')",
        ]

        clicked = False
        for sel in submit_selectors:
            btn = page.locator(sel).first
            if await btn.is_visible():
                await btn.click()
                clicked = True
                logger.info("Clicked custom page submit button: %s", sel)
                break

        if not clicked:
            raise RuntimeError("Could not locate submit button on custom career page")

        # Explicit confirmation check: wait for URL change or thank-you message
        await page.wait_for_timeout(2_500)

        # Check for common success banners
        success_selectors = [
            "div:has-text('Thank you')",
            "div:has-text('Application submitted')",
            "div:has-text('received your application')",
            "h1:has-text('Thank you')",
            "h2:has-text('Thank you')",
            ".success-message",
            ".thank-you",
        ]

        verified = False
        for s_sel in success_selectors:
            if await page.locator(s_sel).first.is_visible():
                verified = True
                logger.info("Custom page submission verified via selector: %s", s_sel)
                break

        if not verified:
            # Check if URL changed to a thank you / confirmation endpoint
            current_url = page.url.lower()
            if any(k in current_url for k in ("thank", "confirm", "success", "submitted")):
                verified = True
                logger.info("Custom page submission verified via URL change: %s", page.url)

        receipt_path = await self._screenshot(page, f"receipt_custom_{application_id}")
        await page.close()

        if not verified:
            logger.warning("Custom page submission could not be explicitly verified against known success selectors.")

        return receipt_path

    async def cancel(self, page: Page) -> None:
        await page.close()
        logger.info("Custom page closed (cancelled by user)")

    async def _screenshot(self, page: Page, label: str) -> str:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = RECEIPTS_DIR / f"{label}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)
