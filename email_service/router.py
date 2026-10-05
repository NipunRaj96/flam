"""
Email applications.

What this does: writes the email (subject + body), builds one-tap links that open
it ready to send in Gmail or your mail app, and records a receipt when you confirm.

What it does NOT do: send mail by itself. Sending needs a Gmail OAuth app that is not
configured yet (see get_oauth_auth_url). The user taps the link, attaches the resume
and presses send.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from context.store import get_facts
from generator import llm
from generator.humanize import clean_answer
from generator.prompts import build_email_prompt, email_system
from telemetry.logger import log_event

if TYPE_CHECKING:
    from intake.post import PostInfo

logger = logging.getLogger(__name__)

RECEIPTS_DIR = Path(__file__).resolve().parent.parent / "receipts"
RECEIPTS_DIR.mkdir(exist_ok=True)


def build_signature(facts: dict[str, str]) -> str:
    lines = ["Thanks,", facts.get("name") or ""]
    contact = [facts.get(k) for k in ("phone", "linkedin", "github", "portfolio") if facts.get(k)]
    if contact:
        lines.append(" | ".join(contact))
    return "\n".join(l for l in lines if l)


_MAX_LINK = 1900  # long URLs can be rejected by Telegram; drop the body from the link when needed


def build_links(to_email: str, subject: str, body: str) -> tuple[str, str]:
    q = urllib.parse.quote

    def make(with_body: bool) -> tuple[str, str]:
        tail = f"&body={q(body)}" if with_body else ""
        return (
            f"mailto:{to_email}?subject={q(subject)}{tail}",
            f"https://mail.google.com/mail/?view=cm&fs=1&to={q(to_email)}&su={q(subject)}{tail}",
        )

    mailto, gmail = make(True)
    if max(len(mailto), len(gmail)) > _MAX_LINK:
        mailto, gmail = make(False)   # body is still shown in the message to copy
    return mailto, gmail


class EmailApplicationService:
    def __init__(self) -> None:
        self._oauth_tokens: dict[int, dict[str, Any]] = {}

    # --- OAuth (not wired up yet) -------------------------------------------

    def oauth_configured(self) -> bool:
        return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("OAUTH_REDIRECT_URI"))

    def get_oauth_auth_url(self, user_id: int) -> str:
        params = {
            "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
            "redirect_uri": os.getenv("OAUTH_REDIRECT_URI", ""),
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/gmail.compose",
            "state": f"user_{user_id}_{int(datetime.utcnow().timestamp())}",
            "access_type": "offline",
            "prompt": "consent",
        }
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    def is_oauth_connected(self, user_id: int) -> bool:
        return user_id in self._oauth_tokens

    # --- Drafting ------------------------------------------------------------

    async def draft_application(
        self,
        to_email: str,
        jd_text: str,
        context: Optional[dict[str, str]],
        user_template: Optional[str] = None,
        post: Optional["PostInfo"] = None,
    ) -> dict[str, Any]:
        ctx = context or {}
        facts = get_facts(ctx)
        if not facts.get("name"):
            from context.resume import regex_facts
            for k, v in regex_facts(ctx.get("resume", "")).items():
                facts.setdefault(k, v)

        messages = [
            {"role": "system", "content": email_system(user_template)},
            {"role": "user", "content": build_email_prompt(jd_text, ctx, facts, post)},
        ]
        data = None
        for attempt in range(2):
            try:
                data = await llm.chat_json(messages, temperature=0.4, max_tokens=800)
                if isinstance(data, dict) and data.get("body"):
                    break
            except ValueError:
                logger.warning("Email draft was not valid JSON (attempt %d)", attempt + 1)
        if not isinstance(data, dict) or not data.get("body"):
            raise RuntimeError("Could not generate the email draft. Try again.")

        name = facts.get("name", "")
        role = post.role if post else ""
        subject = clean_answer(str(data.get("subject") or ""), "short_text") or (
            f"Application for {role}, {name}" if role and name else "Job application"
        )
        body = clean_answer(str(data["body"]).replace("\\n", "\n"), "paragraph")
        body = f"{body}\n\n{build_signature(facts)}"

        missing = [str(m).strip() for m in (data.get("missing_info") or []) if str(m).strip()]
        mailto_url, gmail_url = build_links(to_email, subject, body)

        log_event("email_application_drafted", {"to_email": to_email, "subject": subject})
        return {
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "mailto_url": mailto_url,
            "gmail_url": gmail_url,
            "missing_info": missing,
            "name_missing": not name,
        }

    def rebuild_links(self, draft: dict[str, Any]) -> None:
        draft["mailto_url"], draft["gmail_url"] = build_links(draft["to_email"], draft["subject"], draft["body"])

    # --- Receipt -------------------------------------------------------------

    async def record_receipt(self, user_id: int, draft: dict[str, Any], application_id: int) -> str:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = RECEIPTS_DIR / f"receipt_email_{application_id}_{ts}.txt"
        path.write_text(
            "=== EMAIL APPLICATION RECEIPT ===\n"
            f"Application ID: {application_id}\n"
            f"Timestamp: {datetime.utcnow().isoformat()}\n"
            f"To: {draft['to_email']}\n"
            f"Subject: {draft['subject']}\n\n"
            f"Body:\n{draft['body']}\n\n"
            "Sent by: the user, from their own mail app (confirmed with /approve)\n",
            encoding="utf-8",
        )
        log_event("email_application_confirmed", {
            "user_id": user_id, "application_id": application_id, "to_email": draft["to_email"],
        })
        return str(path)
