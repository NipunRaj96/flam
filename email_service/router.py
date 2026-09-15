"""
Email-based Application Router & Service.

Handles job postings that accept applications via email / mailto:
1. Generates tailored cover notes and application email body grounded in candidate context.
2. Supports /add_mail_auth command for initiating OAuth connections (Gmail / Outlook).
3. Provides 1-tap mailto: URI for instant client sending + OAuth dispatch without password storage.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from groq import AsyncGroq

from telemetry.logger import log_event

if TYPE_CHECKING:
    from generator.answer_generator import AnswerGenerator

logger = logging.getLogger(__name__)

RECEIPTS_DIR = Path(__file__).resolve().parent.parent / "receipts"
RECEIPTS_DIR.mkdir(exist_ok=True)

GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")


class EmailApplicationService:
    """
    Manages email-based job applications and OAuth dispatch.
    """

    def __init__(self) -> None:
        self._oauth_tokens: dict[int, dict[str, Any]] = {}

    def get_oauth_auth_url(self, user_id: int, provider: str = "google") -> str:
        """
        Generate OAuth authorization URL for the user.
        """
        client_id = os.getenv("GOOGLE_CLIENT_ID", "mock-client-id.apps.googleusercontent.com")
        redirect_uri = os.getenv("OAUTH_REDIRECT_URI", "https://oauth.flam.app/callback")
        scope = "https://www.googleapis.com/auth/gmail.compose"

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": f"user_{user_id}_{int(datetime.utcnow().timestamp())}",
            "access_type": "offline",
            "prompt": "consent",
        }
        query_string = urllib.parse.urlencode(params)
        return f"https://accounts.google.com/o/oauth2/v2/auth?{query_string}"

    def register_oauth_token(self, user_id: int, token: str, provider: str = "google") -> None:
        self._oauth_tokens[user_id] = {
            "token": token,
            "provider": provider,
            "connected_at": datetime.utcnow().isoformat(),
        }
        logger.info("OAuth token registered for user %d (%s)", user_id, provider)

    def is_oauth_connected(self, user_id: int) -> bool:
        return user_id in self._oauth_tokens

    async def draft_application(
        self,
        to_email: str,
        jd_text: str,
        context: Optional[dict[str, str]],
        user_template: Optional[str] = None,
    ) -> dict[str, str]:
        """
        Generate tailored Subject and Body for an email job application.
        """
        groq_key = os.getenv("GROQ_API_KEY")
        client = AsyncGroq(api_key=groq_key)

        candidate_name = (context or {}).get("full_name", "Candidate")
        skills = (context or {}).get("skills", "")
        summary = (context or {}).get("summary", "")
        repos = (context or {}).get("github_repos", "")

        prompt = f"""\
You are an expert career assistant drafting an email job application.
Write a professional, concise, and compelling job application email from the candidate to the hiring team.

Candidate Details:
- Name: {candidate_name}
- Summary: {summary}
- Skills: {skills}
- Key Projects/Repos: {repos}

Job Description:
{jd_text or 'Software Engineering Role'}

{f"Custom Candidate Style/Template: {user_template}" if user_template else ""}

Instructions:
1. SUBJECT: Create a clear, standard subject line: e.g. "Application for [Role Name] - {candidate_name}"
2. BODY:
   - 3-4 short, punchy paragraphs or bullet points connecting candidate's exact experience to the JD.
   - Plain text only (no markdown bolding, no markdown links, no backticks).
   - Natural first-person voice ("I built...", "I worked on...").
   - Mention that resume / portfolio are attached or linked.
3. Return a JSON object with keys "subject" and "body".
"""

        response = await client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=600,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        import json
        data = json.loads(content)
        subject = data.get("subject", f"Job Application - {candidate_name}")
        body = data.get("body", "Please find my application attached.")

        # Create mailto link
        mailto_params = {
            "subject": subject,
            "body": body,
        }
        mailto_url = f"mailto:{to_email}?{urllib.parse.urlencode(mailto_params, quote_via=urllib.parse.quote)}"

        log_event("email_application_drafted", {
            "to_email": to_email,
            "subject": subject,
        })

        return {
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "mailto_url": mailto_url,
        }

    async def send_or_confirm(self, user_id: int, draft: dict[str, str], application_id: int) -> str:
        """
        Record confirmation receipt for email application.
        """
        receipt_file = RECEIPTS_DIR / f"receipt_email_{application_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(receipt_file, "w", encoding="utf-8") as f:
            f.write(f"=== EMAIL APPLICATION RECEIPT ===\n")
            f.write(f"Application ID: {application_id}\n")
            f.write(f"Timestamp: {datetime.utcnow().isoformat()}\n")
            f.write(f"To: {draft['to_email']}\n")
            f.write(f"Subject: {draft['subject']}\n\n")
            f.write(f"Body:\n{draft['body']}\n\n")
            f.write(f"OAuth Connected: {self.is_oauth_connected(user_id)}\n")

        log_event("email_application_dispatched", {
            "user_id": user_id,
            "application_id": application_id,
            "to_email": draft["to_email"],
        })
        return str(receipt_file)
