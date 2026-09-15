"""
Google Forms executor adapter for backward compatibility with Phase 1/2 callers.
Delegates to the unified FormExecutor using 'google_forms' platform config.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from playwright.async_api import Page

from executor.form_executor import FormExecutor

if TYPE_CHECKING:
    from generator.answer_generator import AnswerGenerator


class GoogleFormsExecutor(FormExecutor):
    """
    Adapter that preserves the GoogleFormsExecutor interface while delegating
    to the unified FormExecutor engine with platform_id="google_forms".
    """

    async def fill(
        self,
        form_url: str,
        generator: AnswerGenerator,
        jd_text: str = "",
        context: Optional[dict[str, str]] = None,
        user_template: Optional[str] = None,
    ) -> tuple[Page, list[dict]]:
        return await super().fill(
            platform_id="google_forms",
            form_url=form_url,
            generator=generator,
            jd_text=jd_text,
            context=context,
            user_template=user_template,
        )

    async def submit(self, page: Page, application_id: int) -> str:
        return await super().submit(
            page=page,
            platform_id="google_forms",
            application_id=application_id,
        )
