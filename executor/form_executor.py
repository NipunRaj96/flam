"""
Unified Multi-Platform Form Executor.

Drives Playwright using on-demand JSON configs loaded from /form_knowledge_base/.
Handles:
1. Standard single-page forms (Google Forms, MS Forms, Notion Forms).
2. Multi-page pagination loops (detects Next button vs Submit button, advances through sections).
3. Conversational step-by-step forms (Typeform: fills active block, clicks OK/Enter, advances).
4. All field types: short_text, paragraph, radio, checkbox, dropdown.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from playwright.async_api import Browser, Locator, Page, Playwright, async_playwright

from executor.knowledge_base import load_platform_config

if TYPE_CHECKING:
    from generator.answer_generator import AnswerGenerator

logger = logging.getLogger(__name__)

RECEIPTS_DIR = Path(__file__).resolve().parent.parent / "receipts"
RECEIPTS_DIR.mkdir(exist_ok=True)


class FormExecutor:
    """
    Long-lived Playwright executor capable of driving any platform defined in /form_knowledge_base/.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        logger.info("FormExecutor started (browser launched)")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("FormExecutor stopped")

    # -----------------------------------------------------------------------
    # fill — universal dispatch by platform_id
    # -----------------------------------------------------------------------

    async def fill(
        self,
        platform_id: str,
        form_url: str,
        generator: AnswerGenerator,
        jd_text: str = "",
        context: Optional[dict[str, str]] = None,
        user_template: Optional[str] = None,
    ) -> tuple[Page, list[dict]]:
        """
        Navigate to form_url and execute the platform-specific fill routine.
        Leaves page open for submit() or cancel().
        """
        assert self._browser is not None, "Call start() first."
        config = load_platform_config(platform_id)
        nav_type = config.get("navigation", {}).get("type", "standard")

        page = await self._browser.new_page()
        logger.info("Navigating to %s (%s)", form_url, platform_id)
        await page.goto(form_url, wait_until="networkidle", timeout=35_000)

        if nav_type == "conversational":
            filled_fields = await self._fill_conversational(page, config, generator, jd_text, context, user_template)
        else:
            filled_fields = await self._fill_standard_or_paginated(page, config, generator, jd_text, context, user_template)

        filled_count = sum(1 for f in filled_fields if not f["skipped"])
        skipped_count = sum(1 for f in filled_fields if f["skipped"])
        logger.info(
            "Fill complete on %s: %d field(s) filled, %d skipped",
            platform_id, filled_count, skipped_count
        )
        return page, filled_fields

    # -----------------------------------------------------------------------
    # 1. Standard & Paginated Form Fill
    # -----------------------------------------------------------------------

    async def _fill_standard_or_paginated(
        self,
        page: Page,
        config: dict[str, Any],
        generator: AnswerGenerator,
        jd_text: str,
        context: Optional[dict[str, str]],
        user_template: Optional[str],
    ) -> list[dict]:
        selectors = config.get("selectors", {})
        nav = config.get("navigation", {})
        next_btn_sel = nav.get("next_button_selector")
        filled_fields: list[dict] = []
        max_pages = 8
        current_page = 1

        while current_page <= max_pages:
            logger.info("Scanning page %d for question items...", current_page)
            # Wait briefly for items to be present
            try:
                await page.wait_for_selector(selectors["question_selector"], timeout=8_000)
            except Exception:
                logger.debug("No question selector found on current view.")

            items = await page.locator(selectors["item_selector"]).all()
            logger.info("Found %d question block(s) on page %d", len(items), current_page)

            for item in items:
                # 1. Extract question text
                headings = await item.locator(selectors["question_selector"]).all()
                if not headings:
                    continue
                try:
                    question = (await headings[0].text_content(timeout=1_500) or "").strip()
                except Exception:
                    continue
                if not question:
                    continue

                # Skip if already answered on previous page
                if any(f["question"] == question for f in filled_fields):
                    continue

                # 2. Detect field type & available choices
                field_type, choices, target_inputs = await self._detect_field_type(page, item, selectors)
                if field_type == "unknown":
                    continue

                # 3. Generate answer
                result = await generator.generate(
                    question=question,
                    field_type=field_type,
                    jd_text=jd_text,
                    context=context,
                    user_template=user_template,
                    choices=choices,
                )

                # 4. Perform Playwright fill action
                if result.value:
                    try:
                        await self._perform_fill_action(page, item, field_type, result.value, target_inputs, selectors)
                    except Exception as exc:
                        logger.warning("Error filling %r (%s): %s", question, field_type, exc)

                filled_fields.append({
                    "question":          question,
                    "value":             result.value,
                    "field_type":        field_type,
                    "skipped":           result.value is None,
                    "flagged":           result.flagged,
                    "source":            result.source,
                    "context_keys_used": result.context_keys_used,
                })

            # Check for pagination (Next button)
            if next_btn_sel:
                next_btns = await page.locator(next_btn_sel).all()
                visible_next = [b for b in next_btns if await b.is_visible()]
                # Ensure it's not the submit button
                submit_sel = selectors.get("submit_selector")
                is_submit = False
                if visible_next and submit_sel:
                    try:
                        txt = (await visible_next[0].text_content() or "").lower()
                        if "submit" in txt:
                            is_submit = True
                    except Exception:
                        pass

                if visible_next and not is_submit:
                    logger.info("Next page button found. Navigating to page %d...", current_page + 1)
                    await visible_next[0].click()
                    await page.wait_for_timeout(1_000)
                    current_page += 1
                    continue

            # No further pages
            break

        return filled_fields

    # -----------------------------------------------------------------------
    # 2. Conversational (Typeform) Form Fill
    # -----------------------------------------------------------------------

    async def _fill_conversational(
        self,
        page: Page,
        config: dict[str, Any],
        generator: AnswerGenerator,
        jd_text: str,
        context: Optional[dict[str, str]],
        user_template: Optional[str],
    ) -> list[dict]:
        selectors = config.get("selectors", {})
        nav = config.get("navigation", {})
        step_delay = nav.get("step_delay_ms", 500)
        filled_fields: list[dict] = []
        max_steps = 25
        step = 0

        logger.info("Starting conversational form flow...")

        while step < max_steps:
            step += 1
            await page.wait_for_timeout(step_delay)

            # Check if reached submit or thank you
            conf_sel = selectors.get("confirmation_selector")
            if conf_sel and await page.locator(conf_sel).is_visible():
                logger.info("Reached confirmation screen in conversational flow.")
                break

            submit_btn = page.locator(selectors.get("submit_selector", "button:has-text('Submit')")).first
            if await submit_btn.is_visible():
                logger.info("Submit button visible in conversational flow.")
                break

            # Find active question block
            active_blocks = await page.locator(selectors.get("item_selector", "[data-qa='block-wrapper']")).all()
            if not active_blocks:
                break

            target_block = active_blocks[0]
            for b in active_blocks:
                if await b.is_visible():
                    target_block = b
                    break

            headings = await target_block.locator(selectors["question_selector"]).all()
            if not headings:
                break
            question = (await headings[0].text_content() or "").strip()
            if not question:
                break

            # Check if this question was already answered to avoid infinite loops
            if any(f["question"] == question for f in filled_fields):
                # Press Enter or click OK to advance
                ok_btn = target_block.locator(nav.get("next_button_selector", "button:has-text('OK')")).first
                if await ok_btn.is_visible():
                    await ok_btn.click()
                else:
                    await page.keyboard.press("Enter")
                await page.wait_for_timeout(step_delay)
                continue

            field_type, choices, target_inputs = await self._detect_field_type(page, target_block, selectors)
            result = await generator.generate(
                question=question,
                field_type=field_type,
                jd_text=jd_text,
                context=context,
                user_template=user_template,
                choices=choices,
            )

            if result.value:
                try:
                    await self._perform_fill_action(page, target_block, field_type, result.value, target_inputs, selectors)
                except Exception as exc:
                    logger.warning("Error in conversational step: %s", exc)

            filled_fields.append({
                "question":          question,
                "value":             result.value,
                "field_type":        field_type,
                "skipped":           result.value is None,
                "flagged":           result.flagged,
                "source":            result.source,
                "context_keys_used": result.context_keys_used,
            })

            # Advance to next block: click OK button or press Enter
            ok_btns = await target_block.locator(nav.get("next_button_selector", "button:has-text('OK')")).all()
            if ok_btns and await ok_btns[0].is_visible():
                await ok_btns[0].click()
            else:
                await page.keyboard.press("Enter")

            await page.wait_for_timeout(step_delay)

        return filled_fields

    # -----------------------------------------------------------------------
    # Field detection and fill action helpers
    # -----------------------------------------------------------------------

    async def _detect_field_type(
        self,
        page: Page,
        item: Locator,
        selectors: dict[str, str],
    ) -> tuple[str, Optional[list[str]], list[Locator]]:
        short_inputs = await item.locator(selectors.get("short_text_selector", "input[type='text']")).all()
        paras = await item.locator(selectors.get("paragraph_selector", "textarea")).all()
        radios = await item.locator(selectors.get("radio_selector", "[role='radio']")).all()
        checkboxes = await item.locator(selectors.get("checkbox_selector", "[role='checkbox']")).all()
        dropdowns = await item.locator(selectors.get("dropdown_selector", "select")).all()

        if short_inputs and await short_inputs[0].is_visible():
            return "short_text", None, short_inputs[:1]
        elif paras and await paras[0].is_visible():
            return "paragraph", None, paras[:1]
        elif radios and len(radios) > 0:
            choices = await self._extract_choice_labels(radios)
            return "radio", choices, radios
        elif checkboxes and len(checkboxes) > 0:
            choices = await self._extract_choice_labels(checkboxes)
            return "checkbox", choices, checkboxes
        elif dropdowns and len(dropdowns) > 0:
            choices = await self._extract_dropdown_choices(page, item, selectors)
            return "dropdown", choices, dropdowns

        return "unknown", None, []

    async def _extract_choice_labels(self, locators: list[Locator]) -> list[str]:
        choices = []
        for loc in locators:
            val = (
                await loc.get_attribute("data-value")
                or await loc.get_attribute("aria-label")
                or (await loc.text_content())
                or ""
            ).strip()
            if val and val not in choices:
                choices.append(val)
        return choices

    async def _extract_dropdown_choices(self, page: Page, item: Locator, selectors: dict[str, str]) -> list[str]:
        selects = await item.locator("select").all()
        if selects:
            options = await selects[0].locator("option").all()
            choices = []
            for opt in options:
                t = (await opt.text_content() or "").strip()
                if t and t.lower() not in ("choose", "select", ""):
                    choices.append(t)
            if choices:
                return choices

        # Custom combobox / listbox
        listboxes = await item.locator(selectors.get("dropdown_selector", "[role='listbox']")).all()
        if listboxes:
            listbox = listboxes[0]
            try:
                await listbox.click()
                await page.wait_for_timeout(350)
                opt_sel = selectors.get("dropdown_options_selector", "[role='option']")
                options = await page.locator(opt_sel).all()
                choices = []
                for opt in options:
                    t = (await opt.text_content() or "").strip()
                    if t and t.lower() not in ("choose", "select", ""):
                        choices.append(t)
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(200)
                return choices
            except Exception as exc:
                logger.debug("Could not extract choices from listbox: %s", exc)

        return []

    async def _perform_fill_action(
        self,
        page: Page,
        item: Locator,
        field_type: str,
        value: str,
        target_inputs: list[Locator],
        selectors: dict[str, str],
    ) -> None:
        if field_type in ("short_text", "paragraph"):
            for inp in target_inputs:
                await inp.fill(value)
        elif field_type == "radio":
            target_lower = value.strip().lower()
            radios = target_inputs or await item.locator(selectors["radio_selector"]).all()
            for r in radios:
                val = (
                    await r.get_attribute("data-value")
                    or await r.get_attribute("aria-label")
                    or (await r.text_content())
                    or ""
                ).strip().lower()
                if val == target_lower or target_lower in val:
                    await r.click()
                    return
            if radios:
                await radios[0].click()
        elif field_type == "checkbox":
            targets = [v.strip().lower() for v in value.split("|") if v.strip()]
            checkboxes = target_inputs or await item.locator(selectors["checkbox_selector"]).all()
            for cb in checkboxes:
                val = (
                    await cb.get_attribute("data-value")
                    or await cb.get_attribute("aria-label")
                    or (await cb.text_content())
                    or ""
                ).strip().lower()
                if any(t == val or t in val for t in targets):
                    await cb.click()
        elif field_type == "dropdown":
            selects = await item.locator("select").all()
            if selects:
                try:
                    await selects[0].select_option(label=value)
                    return
                except Exception:
                    pass
            listboxes = await item.locator(selectors.get("dropdown_selector", "[role='listbox']")).all()
            if listboxes:
                listbox = listboxes[0]
                await listbox.click()
                await page.wait_for_timeout(350)
                opt_sel = selectors.get("dropdown_options_selector", "[role='option']")
                options = await page.locator(opt_sel).all()
                target_lower = value.strip().lower()
                for opt in options:
                    txt = (await opt.text_content() or "").strip()
                    data_val = (await opt.get_attribute("data-value") or "").strip()
                    if txt.lower() == "choose":
                        continue
                    if txt.lower() == target_lower or data_val.lower() == target_lower or target_lower in txt.lower():
                        await opt.click()
                        await page.wait_for_timeout(250)
                        return
                await page.keyboard.press("Escape")

    # -----------------------------------------------------------------------
    # submit & cancel
    # -----------------------------------------------------------------------

    async def submit(self, page: Page, platform_id: str, application_id: int) -> str:
        """Click submit, verify confirmation selector, save screenshot receipt."""
        config = load_platform_config(platform_id)
        selectors = config.get("selectors", {})
        submit_sel = selectors.get("submit_selector", "button:has-text('Submit')")

        submit_btn = page.locator(submit_sel).first
        await submit_btn.click()
        logger.info("Submit clicked for application %d on %s", application_id, platform_id)

        conf_sel = selectors.get("confirmation_selector")
        if conf_sel:
            try:
                await page.wait_for_selector(conf_sel, timeout=15_000)
            except Exception as exc:
                await self._screenshot(page, f"submit_error_{application_id}")
                raise RuntimeError(
                    f"Confirmation element not found after submit on {platform_id} — "
                    "submission may not have gone through."
                ) from exc

        receipt_path = await self._screenshot(page, f"receipt_{application_id}")
        logger.info("Receipt saved: %s", receipt_path)
        await page.close()
        return receipt_path

    async def cancel(self, page: Page) -> None:
        await page.close()
        logger.info("Page closed (cancelled by user)")

    async def _screenshot(self, page: Page, label: str) -> str:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = RECEIPTS_DIR / f"{label}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        return str(path)
