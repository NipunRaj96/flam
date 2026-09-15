"""
Resume extraction and parsing utilities.

Supports:
- Extracting raw text from PDF files using pdfplumber.
- Extracting key structured fields (name, email, phone, links, education)
  using regex and Groq structured extraction, so that both static identity
  fields and LLM open-ended answers are grounded in the user's real resume.
"""
from __future__ import annotations

import io
import json
import logging
import re
from typing import Optional

import pdfplumber
from groq import AsyncGroq

logger = logging.getLogger(__name__)


def extract_text_from_pdf(pdf_bytes_or_path: bytes | str) -> str:
    """
    Extract text content from a PDF file path or byte stream using pdfplumber.
    """
    text_parts = []
    if isinstance(pdf_bytes_or_path, bytes):
        stream = io.BytesIO(pdf_bytes_or_path)
        with pdfplumber.open(stream) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text_parts.append(t.strip())
    else:
        with pdfplumber.open(pdf_bytes_or_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text_parts.append(t.strip())

    full_text = "\n\n".join(text_parts).strip()
    return full_text


async def extract_structured_profile_from_resume(
    resume_text: str,
    groq_api_key: Optional[str] = None,
    model: str = "qwen/qwen3.8-27b",
) -> dict[str, str]:
    """
    Extract structured candidate fields from resume text.
    Returns a dict with standard profile keys:
      name, email, phone, linkedin, github, portfolio, college, degree, graduation_year, cgpa, experience, about
    """
    # Quick regex extractors for standard identifiers
    extracted: dict[str, str] = {}

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", resume_text)
    if email_match:
        extracted["email"] = email_match.group(0)

    phone_match = re.search(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", resume_text)
    if phone_match:
        extracted["phone"] = phone_match.group(0)

    linkedin_match = re.search(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+", resume_text, re.IGNORECASE)
    if linkedin_match:
        extracted["linkedin"] = linkedin_match.group(0)

    github_match = re.search(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+", resume_text, re.IGNORECASE)
    if github_match:
        extracted["github"] = github_match.group(0)

    if not groq_api_key:
        return extracted

    # Use Groq to accurately extract candidate identity and education facts from the resume
    try:
        client = AsyncGroq(api_key=groq_api_key)
        prompt = f"""\
Extract factual candidate profile details from the resume below.
Return a valid JSON object ONLY, with these exact keys (leave value as empty string if not found):
- "name": candidate full name
- "email": candidate email
- "phone": candidate phone number
- "linkedin": linkedin profile url
- "github": github profile url
- "portfolio": portfolio/website url
- "college": university/college name
- "degree": degree name / major (e.g. B.Tech in Computer Science)
- "graduation_year": year of graduation (e.g. 2025)
- "cgpa": CGPA or GPA or percentage (e.g. 8.5/10)
- "about": 2-sentence summary of candidate background and strengths

Resume:
\"\"\"
{resume_text[:4000]}
\"\"\"
"""
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        data = json.loads(content)
        # Merge with non-empty keys
        for k, v in data.items():
            if isinstance(v, str) and v.strip():
                extracted[k] = v.strip()
    except Exception as exc:
        logger.warning("Groq structured extraction on resume failed: %s", exc)

    return extracted
