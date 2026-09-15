"""
AnswerGenerator — single abstraction for form-question answering.

Phase 2 Design:
- Decoupled from static profiles: receives live candidate context dict per call
  (resume text, GitHub repos, LinkedIn, portfolio, structured facts).
- Factual/identity fields (name, email, phone, URLs, college) are resolved
  factually from structured candidate data or fast resume extractors.
- Open-ended / paragraph fields leverage Groq to cross-reference the JD text
  against candidate projects/experience, respecting the user's editable style template.
- Human form-filling formatting: short paragraphs, bullet points, line breaks.
- Discrete choice selection: handles radio, dropdown, and checkbox questions by
  matching against the DOM's available choices.
- Traces context sources used for each generated answer (doc 04 traceability).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from generator.prompts import build_choice_prompt, build_user_prompt, system_with_template

logger = logging.getLogger(__name__)

# Default Groq model — fast, high quality, active on Groq
DEFAULT_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")

# Identity and factual field keys that are deterministically resolved
_STATIC_ONLY_KEYS = {
    "name", "email", "phone", "linkedin", "github",
    "portfolio", "college", "degree", "graduation_year", "cgpa",
}

# ---------------------------------------------------------------------------
# Keyword → profile-key mapping
# Checked in order: specific questions before generic catch-alls.
# ---------------------------------------------------------------------------
_FIELD_KEYWORDS: list[tuple[list[str], str]] = [
    (["full name", "your name"],                                              "name"),
    (["email address", "email id", "e-mail", "mail id"],                     "email"),
    (["phone", "mobile", "contact number", "whatsapp"],                      "phone"),
    (["linkedin"],                                                            "linkedin"),
    (["github project", "github", "git hub"],                                "github"),
    (["portfolio", "website", "personal site"],                              "portfolio"),
    (["college", "university", "institution", "school"],                     "college"),
    (["graduation", "year of graduation", "pass out", "batch"],              "graduation_year"),
    (["cgpa", "gpa", "percentage", "grade"],                                 "cgpa"),
    # Open-ended questions — checked BEFORE broad 'degree'/'course' keywords
    (["why are you interested", "why do you want", "interested in this",
      "motivation", "why this role", "why this company"],                    "why_interested"),
    (["why should we hire", "why hire you", "why should we select",
      "what makes you", "stand out"],                                        "why_hire"),
    (["past experience", "relevant experience", "summarize your",
      "experience including", "academic project", "internship", "coursework",
      "course work"],                                                         "experience"),
    (["describe", "tell us about", "introduce yourself",
      "about yourself", "background"],                                        "about"),
    # Education
    (["degree", "major", "programme", "program",
      " course ", "field of study"],                                         "degree"),
    # Broad catch-alls — MUST be last
    (["name"],                                                                "name"),
    (["email"],                                                               "email"),
    (["experience", "why"],                                                   "experience"),
]


def match_question_to_key(question: str) -> Optional[str]:
    """
    Return the profile-key whose keywords appear in the question text.
    Returns None if no match.
    """
    q = question.lower()
    for keywords, key in _FIELD_KEYWORDS:
        if any(kw in q for kw in keywords):
            return key
    return None


@dataclass
class AnswerResult:
    """
    The output of a single generate() call.

    value            : The answer string, or None if the field must be filled manually.
    source           : "context_store" | "groq" | "static_fallback" | "manual_required"
    context_keys_used: Which context keys/slices were used (doc 04 traceability).
    flagged          : True = shown with a ❓ icon in preview.
    confidence       : 0–1.
    """
    value: Optional[str]
    source: str
    context_keys_used: list[str] = field(default_factory=list)
    flagged: bool = False
    confidence: float = 1.0


class AnswerGenerator:
    """
    Core answer generation engine.
    """

    def __init__(
        self,
        groq_api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        fallback_profile: Optional[dict[str, str]] = None,
    ) -> None:
        self._groq_api_key = groq_api_key
        self._model = model
        self._fallback_profile = fallback_profile or {}
        self._groq_client = None

        if groq_api_key:
            logger.info("AnswerGenerator initialized with Groq model: %s", model)
        else:
            logger.info("AnswerGenerator initialized without Groq (static mode).")

    async def generate(
        self,
        question: str,
        field_type: str,
        jd_text: str = "",
        context: Optional[dict[str, str]] = None,
        user_template: Optional[str] = None,
        choices: Optional[list[str]] = None,
    ) -> AnswerResult:
        """
        Generate a candidate answer for a form field.

        Args:
            question: Form question string
            field_type: "short_text" | "paragraph" | "radio" | "checkbox" | "dropdown"
            jd_text: Full job description text
            context: Candidate context dict from context_versions (resume, github, etc.)
            user_template: Candidate's custom tone/style instructions
            choices: List of available option strings for radio/dropdown/checkbox

        Returns:
            AnswerResult with answer text, source, and context traceability tags.
        """
        ctx = context or {}
        profile_key = match_question_to_key(question)

        # -------------------------------------------------------------------
        # 1. Discrete Choice Selection (Radio, Dropdown, Checkbox)
        # -------------------------------------------------------------------
        if choices and (field_type in ("radio", "dropdown", "checkbox") or len(choices) > 0):
            if self._groq_api_key:
                try:
                    return await self._groq_choice(
                        question=question,
                        field_type=field_type,
                        choices=choices,
                        jd_text=jd_text,
                        context=ctx,
                    )
                except Exception as exc:
                    logger.warning("Groq choice selection failed for %r: %s", question, exc)

            # Fallback choice selection
            matched = self._fallback_choice_match(question, choices, ctx)
            if matched:
                return AnswerResult(
                    value=matched,
                    source="static_fallback",
                    context_keys_used=["fallback:choice_heuristic"],
                    flagged=False,
                )
            return AnswerResult(
                value=None,
                source="manual_required",
                context_keys_used=[],
                flagged=True,
            )

        # -------------------------------------------------------------------
        # 2. Deterministic/Factual fields (name, email, phone, urls, education)
        # -------------------------------------------------------------------
        if profile_key in _STATIC_ONLY_KEYS and profile_key != "github":
            direct_val = self._resolve_factual_field(profile_key, ctx)
            if direct_val:
                return AnswerResult(
                    value=direct_val,
                    source="context_store",
                    context_keys_used=[f"context:{profile_key}"],
                    flagged=False,
                )

        # -------------------------------------------------------------------
        # 3. For GitHub project listings / links question
        # -------------------------------------------------------------------
        if profile_key == "github" and "github" in ctx and ctx["github"]:
            if "top" in question.lower() or "list" in question.lower() or "project" in question.lower():
                if self._groq_api_key:
                    return await self._groq_answer(question, field_type, jd_text, ctx, user_template)
                else:
                    return AnswerResult(
                        value=ctx["github"],
                        source="context_store",
                        context_keys_used=["context:github"],
                        flagged=False,
                    )
            else:
                github_url = self._resolve_factual_field("github", ctx)
                if github_url:
                    return AnswerResult(
                        value=github_url,
                        source="context_store",
                        context_keys_used=["context:github_url"],
                        flagged=False,
                    )

        # -------------------------------------------------------------------
        # 4. Open-ended / Paragraph fields with Groq available
        # -------------------------------------------------------------------
        if self._groq_api_key and (field_type == "paragraph" or profile_key not in _STATIC_ONLY_KEYS):
            try:
                return await self._groq_answer(
                    question=question,
                    field_type=field_type,
                    jd_text=jd_text,
                    context=ctx,
                    user_template=user_template,
                )
            except Exception as exc:
                logger.warning("Groq generation failed for %r, falling back to static: %s", question, exc)

        # -------------------------------------------------------------------
        # 5. Fallback matching against context or fallback profile
        # -------------------------------------------------------------------
        fallback_val = self._resolve_factual_field(profile_key, ctx) if profile_key else None
        if fallback_val:
            return AnswerResult(
                value=fallback_val,
                source="context_store" if profile_key in ctx else "static_fallback",
                context_keys_used=[f"fallback:{profile_key}"],
                flagged=False,
            )

        # -------------------------------------------------------------------
        # 6. Flag for manual review if no answer could be grounded
        # -------------------------------------------------------------------
        return AnswerResult(
            value=None,
            source="manual_required",
            context_keys_used=[],
            flagged=True,
        )

    def _resolve_factual_field(self, key: Optional[str], context: dict[str, str]) -> Optional[str]:
        """
        Attempt to resolve a factual property from context dict or fallback profile.
        """
        if not key:
            return None

        # Direct key in context
        if key in context and context[key]:
            return context[key]

        # Check resume text with quick heuristics
        if "resume" in context and context["resume"]:
            resume_text = context["resume"]
            if key == "email":
                m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", resume_text)
                if m:
                    return m.group(0)
            elif key == "phone":
                m = re.search(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", resume_text)
                if m:
                    return m.group(0)
            elif key == "linkedin":
                m = re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+", resume_text, re.IGNORECASE)
                if m:
                    return m.group(0)
            elif key == "github":
                m = re.search(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+", resume_text, re.IGNORECASE)
                if m:
                    return m.group(0)

        # Fallback profile (e.g. STATIC_PROFILE placeholder)
        if key in self._fallback_profile and self._fallback_profile[key]:
            return self._fallback_profile[key]

        return None

    def _fallback_choice_match(
        self, question: str, choices: list[str], context: dict[str, str]
    ) -> Optional[str]:
        """
        Simple heuristic choice matching when Groq is unavailable.
        """
        q_lower = question.lower()
        # For yes/no questions
        if any(c.lower() in ("yes", "no") for c in choices):
            # If candidate has background, default to Yes for positive experience queries
            for c in choices:
                if c.strip().lower() == "yes":
                    return c

        return choices[0] if choices else None

    async def _groq_choice(
        self,
        question: str,
        field_type: str,
        choices: list[str],
        jd_text: str,
        context: dict[str, str],
    ) -> AnswerResult:
        """
        Select the best option from available choices using Groq.
        """
        from groq import AsyncGroq

        if self._groq_client is None:
            self._groq_client = AsyncGroq(api_key=self._groq_api_key)

        prompt = build_choice_prompt(
            question=question,
            field_type=field_type,
            choices=choices,
            jd_text=jd_text,
            context=context,
        )

        response = await self._groq_client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": "You are a precise job application assistant. You strictly pick matching options from provided lists."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=60,
        )

        raw = response.choices[0].message.content.strip().strip('"').strip("'")
        logger.info("Groq choice raw output for %r: %r", question, raw)

        # For checkbox (multi-select)
        if field_type == "checkbox" and "|" in raw:
            selected_items = [p.strip() for p in raw.split("|") if p.strip()]
            matched = []
            for item in selected_items:
                for c in choices:
                    if c.lower() == item.lower() or item.lower() in c.lower():
                        if c not in matched:
                            matched.append(c)
            if matched:
                return AnswerResult(
                    value=" | ".join(matched),
                    source="groq",
                    context_keys_used=["groq:choice_select"],
                    flagged=False,
                )

        # For single-select (radio, dropdown): exact or case-insensitive match
        for c in choices:
            if c.strip().lower() == raw.lower():
                return AnswerResult(
                    value=c,
                    source="groq",
                    context_keys_used=["groq:choice_select"],
                    flagged=False,
                )

        # Partial substring match if exact match missed
        for c in choices:
            if c.lower() in raw.lower() or raw.lower() in c.lower():
                return AnswerResult(
                    value=c,
                    source="groq",
                    context_keys_used=["groq:choice_select"],
                    flagged=False,
                )

        # Fallback to first choice if no match
        logger.warning("Could not exact-match Groq output %r to choices %r", raw, choices)
        return AnswerResult(
            value=choices[0],
            source="groq",
            context_keys_used=["groq:choice_select"],
            flagged=False,
        )

    async def _groq_answer(
        self,
        question: str,
        field_type: str,
        jd_text: str,
        context: dict[str, str],
        user_template: Optional[str],
    ) -> AnswerResult:
        """
        Call Groq API with grounded prompt, human-like structure, and traceability tags.
        """
        from groq import AsyncGroq

        if self._groq_client is None:
            self._groq_client = AsyncGroq(api_key=self._groq_api_key)

        system = system_with_template(user_template)
        user_prompt = build_user_prompt(
            question=question,
            field_type=field_type,
            jd_text=jd_text,
            context=context,
        )

        max_tokens = 500 if field_type == "paragraph" else 120

        response = await self._groq_client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
        )

        raw = response.choices[0].message.content.strip()

        if "NEEDS_MANUAL_REVIEW" in raw:
            logger.info("Groq flagged question for manual review: %r", question)
            return AnswerResult(
                value=None,
                source="manual_required",
                context_keys_used=[],
                flagged=True,
            )

        # Trace context sources present in context dict
        sources_used = [f"context:{k}" for k in ("resume", "github", "linkedin", "portfolio") if k in context and context[k]]
        if not sources_used:
            sources_used = ["groq:jd_crossref"]

        return AnswerResult(
            value=raw,
            source="groq",
            context_keys_used=sources_used,
            flagged=False,
        )
