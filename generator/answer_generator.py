"""
AnswerGenerator — turns one form question into one answer.

Order of attack for every question:
1. Choice fields (radio / dropdown / checkbox): the model picks from the real options,
   or says UNSURE. Sensitive questions are never guessed.
2. Plain facts (name, phone, college, notice period...): copied from the candidate's
   own facts. A missing fact is flagged for the user, never invented.
3. Open-ended questions: written by the model from the resume, GitHub projects and
   the role, then cleaned to read like a person wrote it.

There is no placeholder/static fallback: if the profile cannot answer, the field is
flagged and the user decides.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Optional

from context.resume import regex_facts
from context.store import get_facts
from generator import llm
from generator.humanize import clean_answer, limit_words, parse_limit
from generator.prompts import build_choice_prompt, build_user_prompt, system_with_template

if TYPE_CHECKING:
    from intake.post import PostInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Question -> fact routing
# ---------------------------------------------------------------------------

_FACT_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(p, re.IGNORECASE), key) for p, key in [
        (r"\b(first name|given name)\b", "first_name"),
        (r"\b(last name|surname|family name)\b", "last_name"),
        (r"e-?mail", "email"),
        (r"\b(phone|mobile|contact number|whatsapp|telephone)\b", "phone"),
        (r"linkedin", "linkedin"),
        (r"git ?hub", "github"),
        (r"\b(portfolio|personal (web)?site|your website|website (url|link))\b", "portfolio"),
        (r"\b(college|university|institution|institute)\b", "college"),
        (r"\b(graduation|pass(ing)? ?out|year of passing|batch)\b", "graduation_year"),
        (r"\b(cgpa|gpa|percentage)\b", "cgpa"),
        (r"\b(degree|qualification|field of study)\b", "degree"),
        (r"notice period|how soon can you join|joining (time|date)|earliest (start|joining)", "notice_period"),
        (r"current (ctc|salary|compensation|package)", "current_ctc"),
        (r"expected (ctc|salary|compensation|package)|salary expectation", "expected_ctc"),
        (r"years of (work |professional )?experience|total experience", "years_experience"),
        (r"current (company|employer|organi[sz]ation)", "current_company"),
        (r"current (job )?(title|role|designation)", "current_title"),
        (r"(current|present|preferred) location|where are you (based|located)|\bcity\b|\blocation\b", "location"),
        (r"relocat", "relocate"),
        (r"work authori[sz]ation|authori[sz]ed to work|\bvisa\b|sponsorship", "work_authorization"),
        (r"^\W*(full |your |candidate'?s? |applicant'?s? )?name\W*$|\b(full name|your name|candidate name|applicant name)\b", "name"),
    ]
]

# Open questions that merely mention a fact word ("describe your GitHub projects").
_OPEN_RE = re.compile(
    r"\b(describe|explain|tell|why|how|walk|project|achievement|proud|challeng|strength|weakness|experience with)\b",
    re.IGNORECASE,
)

# Questions the bot must never answer on its own.
_SENSITIVE_RE = re.compile(
    r"\b(gender|sex|ethnic\w*|race|racial|veteran|disabilit\w*|religio\w*|caste|marital|pronoun\w*|"
    r"date of birth|dob|sexual orientation|criminal|convict\w*)\b",
    re.IGNORECASE,
)
_DECLINE_RE = re.compile(
    r"prefer not|decline|do not wish|don'?t wish|rather not|not to (say|disclose)|choose not", re.IGNORECASE
)

# Refusals are only legitimate for questions like these; anything else gets one retry.
_PERSONAL_FACT_RE = re.compile(
    r"salary|ctc|compensation|package|notice|visa|sponsor|authori[sz]|date of birth|dob|reference|"
    r"passport|aadhaar|\bpan\b|\bssn\b|expected|current pay",
    re.IGNORECASE,
)

_FACT_HINT = {
    "notice_period": "/fact notice_period 30 days",
    "current_ctc": "/fact current_ctc 12 LPA",
    "expected_ctc": "/fact expected_ctc 18 LPA",
    "years_experience": "/fact years_experience 2",
    "work_authorization": "/fact work_authorization Authorized to work in India",
    "relocate": "/fact relocate Yes",
    "location": "/fact location Bengaluru",
    "current_company": "/fact current_company <company>",
    "current_title": "/fact current_title <title>",
}


def match_fact_key(question: str, field_type: str) -> Optional[str]:
    """Which candidate fact does this question ask for, if any?"""
    if field_type != "short_text":
        return None
    q = re.sub(r"[*:]+", " ", question).strip()
    if len(q.split()) > 18 or _OPEN_RE.search(q):
        return None
    for pattern, key in _FACT_RULES:
        if pattern.search(q):
            return key
    return None


def join_date(notice: str, today: Optional[date] = None) -> Optional[date]:
    """'30 days', '2 months', 'immediate' -> a calendar date. None if it can't be read."""
    today = today or date.today()
    n = notice.lower()
    if re.search(r"immediate|asap|right away|now", n):
        return today
    m = re.search(r"(\d+)\s*(day|week|month)", n)
    if not m:
        return None
    qty, unit = int(m.group(1)), m.group(2)
    return today + timedelta(days=qty * {"day": 1, "week": 7, "month": 30}[unit])


@dataclass
class AnswerResult:
    """
    value             : answer text, or None when the user must fill it in.
    source            : "profile" | "llm" | "manual_required"
    context_keys_used : what the answer drew from (shown in the preview).
    flagged           : True = shown with a warning in the preview.
    note              : short hint for the user (e.g. how to add a missing fact).
    """
    value: Optional[str]
    source: str
    context_keys_used: list[str] = field(default_factory=list)
    flagged: bool = False
    confidence: float = 1.0
    note: str = ""


def _manual(note: str = "") -> AnswerResult:
    return AnswerResult(value=None, source="manual_required", flagged=True, confidence=0.0, note=note)


class AnswerGenerator:
    def __init__(self, groq_api_key: Optional[str] = None, model: Optional[str] = None,
                 fallback_profile: Optional[dict[str, str]] = None) -> None:
        # Arguments kept for backward compatibility. The key and model are read from the
        # environment at call time (generator/llm.py); fallback_profile is no longer used.
        self._sem = asyncio.Semaphore(4)  # parallel questions, gentle on Groq rate limits

    # -----------------------------------------------------------------------

    async def generate(
        self,
        question: str,
        field_type: str,
        jd_text: str = "",
        context: Optional[dict[str, str]] = None,
        user_template: Optional[str] = None,
        choices: Optional[list[str]] = None,
        post: Optional["PostInfo"] = None,
    ) -> AnswerResult:
        ctx = context or {}
        facts = self._facts(ctx)

        if choices:
            return await self._choose(question, field_type, choices, jd_text, ctx, facts, post)

        if field_type == "date":
            return self._date_answer(question, facts)

        key = match_fact_key(question, field_type)
        if key:
            value = self._fact_value(key, facts)
            if value:
                return AnswerResult(value=value, source="profile", context_keys_used=[f"fact:{key}"])
            return _manual(f"Not in your profile. Add it with {_FACT_HINT.get(key, f'/fact {key} <value>')}")

        return await self._open_answer(question, field_type, jd_text, ctx, facts, user_template, post)

    # -----------------------------------------------------------------------
    # Dates
    # -----------------------------------------------------------------------

    @staticmethod
    def _date_answer(question: str, facts: dict[str, str]) -> AnswerResult:
        """Join-date questions are answered from the notice-period fact; everything else is the user's call."""
        if re.search(r"join|joining|start|available|availability|notice", question, re.IGNORECASE):
            when = join_date(facts.get("notice_period", ""))
            if when:
                return AnswerResult(value=when.isoformat(), source="profile", context_keys_used=["fact:notice_period"])
            return _manual("Add your notice period with /fact notice_period 30 days, or set the date with /edit N YYYY-MM-DD")
        return _manual("Date field. Set it with /edit N YYYY-MM-DD")

    # -----------------------------------------------------------------------
    # Facts
    # -----------------------------------------------------------------------

    @staticmethod
    def _facts(ctx: dict[str, str]) -> dict[str, str]:
        facts = get_facts(ctx)
        # Identifiers found in the resume text fill any gap (never the pasted LinkedIn blob).
        for k, v in regex_facts(ctx.get("resume", "")).items():
            facts.setdefault(k, v)
        return facts

    @staticmethod
    def _fact_value(key: str, facts: dict[str, str]) -> Optional[str]:
        if key in ("first_name", "last_name"):
            parts = facts.get("name", "").split()
            if not parts:
                return None
            return parts[0] if key == "first_name" else (" ".join(parts[1:]) or None)
        return facts.get(key) or None

    # -----------------------------------------------------------------------
    # Choice fields
    # -----------------------------------------------------------------------

    async def _choose(self, question, field_type, choices, jd_text, ctx, facts, post) -> AnswerResult:
        if _SENSITIVE_RE.search(question):
            decline = next((c for c in choices if _DECLINE_RE.search(c)), None)
            if decline:
                return AnswerResult(
                    value=decline, source="profile", flagged=True,
                    context_keys_used=["rule:sensitive_question"],
                    note="Personal question, so I picked the opt-out answer. Change it with /edit if you prefer.",
                )
            return _manual("Personal question. I don't answer these for you.")

        if not llm.has_llm():
            return _manual("No AI key configured, so I can't pick an option.")

        prompt = build_choice_prompt(question, field_type, choices, jd_text, ctx, facts, post)
        try:
            async with self._sem:
                raw = await llm.chat(
                    [{"role": "system", "content": "You answer application-form questions about a candidate strictly from their profile."},
                     {"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=120,
                )
        except Exception as exc:
            logger.warning("Choice selection failed for %r: %s", question, exc)
            return _manual("The AI call failed. Pick this one yourself.")

        raw = raw.strip().strip("\"'").strip()
        logger.info("Choice for %r -> %r", question, raw)
        if not raw or raw.upper().startswith("UNSURE"):
            return _manual("Your profile doesn't say. Pick this one yourself.")

        if field_type == "checkbox":
            wanted = [p.strip().lower() for p in raw.split("|") if p.strip()]
            matched = [c for c in choices if any(w == c.lower() or w in c.lower() for w in wanted)]
            if matched:
                return AnswerResult(" | ".join(matched), "llm", ["llm:choice"])
        else:
            low = raw.lower()
            for c in choices:
                if c.strip().lower() == low:
                    return AnswerResult(c, "llm", ["llm:choice"])
            for c in choices:
                if c.strip().lower() in low or low in c.strip().lower():
                    return AnswerResult(c, "llm", ["llm:choice"])

        logger.warning("Could not match %r to options %r", raw, choices)
        return _manual("I couldn't match an option confidently. Pick this one yourself.")

    # -----------------------------------------------------------------------
    # Open-ended
    # -----------------------------------------------------------------------

    async def _open_answer(self, question, field_type, jd_text, ctx, facts, user_template, post) -> AnswerResult:
        if not llm.has_llm():
            return _manual("No AI key configured.")
        if not (ctx.get("resume") or ctx.get("linkedin") or ctx.get("github") or facts):
            return _manual("I have no profile yet. Send /upload first.")

        max_chars, max_words = parse_limit(question)
        prompt = build_user_prompt(question, field_type, jd_text, ctx, facts, post, max_chars, max_words)
        tokens = 700 if field_type == "paragraph" else 200
        if max_words:
            tokens = min(tokens, max_words * 3 + 40)

        messages = [{"role": "system", "content": system_with_template(user_template)},
                    {"role": "user", "content": prompt}]
        try:
            async with self._sem:
                raw = await llm.chat(messages, temperature=0.4, max_tokens=tokens)
                if "NEEDS_MANUAL_REVIEW" in raw and not _PERSONAL_FACT_RE.search(question):
                    logger.info("Model refused %r; retrying once", question)
                    messages += [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": (
                            "This question is about the candidate's work and background, so it can be answered "
                            "from the profile. Write the answer now. Use only facts in the profile and leave out "
                            "anything it does not cover. Do not reply NEEDS_MANUAL_REVIEW.")},
                    ]
                    raw = await llm.chat(messages, temperature=0.4, max_tokens=tokens)
        except Exception as exc:
            logger.warning("Answer generation failed for %r: %s", question, exc)
            return _manual("The AI call failed. Answer this one yourself.")

        if "NEEDS_MANUAL_REVIEW" in raw:
            return _manual("Your profile has nothing that honestly answers this. Write it yourself.")

        text = clean_answer(raw, field_type, max_chars)
        if max_words:
            text = limit_words(text, max_words)
        if not text:
            return _manual("The AI returned an empty answer.")

        used = [f"context:{k}" for k in ("resume", "github", "linkedin", "portfolio") if ctx.get(k)]
        if post and (post.role or post.requirements):
            used.append("post:role")
        return AnswerResult(value=text, source="llm", context_keys_used=used or ["llm:general"])
