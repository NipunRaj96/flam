"""
Resume extraction: PDF -> text, and text -> structured candidate facts.

Facts feed the "boring" form fields (name, phone, college...) so those are
filled from your real data and never guessed by the model.
"""
from __future__ import annotations

import io
import logging
import re

import pdfplumber

from generator import llm

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[-.\s]?)?(?:\(?\d{3,5}\)?[-.\s]?){2,3}\d{2,4}")
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w%-]+/?", re.IGNORECASE)
_GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+/?", re.IGNORECASE)


def extract_text_from_pdf(pdf_bytes_or_path: bytes | str) -> str:
    src = io.BytesIO(pdf_bytes_or_path) if isinstance(pdf_bytes_or_path, bytes) else pdf_bytes_or_path
    parts = []
    with pdfplumber.open(src) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                parts.append(t.strip())
    return "\n\n".join(parts).strip()


def regex_facts(text: str) -> dict[str, str]:
    """Identifiers that regex finds reliably."""
    out: dict[str, str] = {}
    if m := _EMAIL_RE.search(text):
        out["email"] = m.group(0)
    if m := _LINKEDIN_RE.search(text):
        out["linkedin"] = m.group(0)
    if m := _GITHUB_RE.search(text):
        out["github"] = m.group(0)
    head = text[:600]  # phone is in the header; avoids matching years/ids further down
    if m := _PHONE_RE.search(head):
        digits = re.sub(r"\D", "", m.group(0))
        if 9 <= len(digits) <= 14:
            out["phone"] = m.group(0).strip()
    return out


_PROMPT = """\
Extract facts about the candidate from this resume. Return a JSON object with exactly these keys.
Use "" for anything the resume does not state. Never guess or infer.

"name": full name
"email", "phone", "linkedin", "github", "portfolio": as written
"location": current city/country if stated
"college": most recent university or college
"degree": degree and major
"graduation_year": year, digits only
"cgpa": CGPA/GPA/percentage as written
"current_company": current or most recent employer
"current_title": current or most recent job title
"years_experience": only if the resume literally states a number of years of experience (do not work it out from dates), else ""
"skills": the 12 most important skills, comma separated
"about": two plain sentences on who this person is professionally, in first person, no buzzwords

RESUME:
\"\"\"
{text}
\"\"\"
"""


async def extract_structured_profile_from_resume(resume_text: str) -> dict[str, str]:
    facts = regex_facts(resume_text)
    if not llm.has_llm():
        return facts
    try:
        data = await llm.chat_json(
            [{"role": "user", "content": _PROMPT.format(text=resume_text[:6000])}],
            temperature=0.0, max_tokens=700,
        )
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, str) and v.strip():
                    # regex hits for identifiers stay; the model fills the rest
                    facts.setdefault(k, v.strip())
    except Exception as exc:
        logger.warning("Resume fact extraction failed: %s", exc)
    return facts
