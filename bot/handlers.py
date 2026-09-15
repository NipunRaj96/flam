"""
Telegram message and command handlers for Phase 3 Multi-Platform & Channel Support.

Commands:
  /start          — Welcome message & command overview
  /upload         — Upload/paste resume (PDF file or plain text)
  /update_github  — Pull GitHub projects & summarize via Groq (/update_github <username>)
  /linkedin       — Paste LinkedIn profile content (/linkedin <text>)
  /template       — View or edit tone/style instructions (/template edit <text>)
  /add_mail_auth  — Initiate OAuth authorization for email applications
  /status         — View current candidate context summary
  /logs           — View recent debug logs and telemetry metrics
  /approve        — Submit the pending application form or email draft
  /cancel         — Cancel the pending application form or email draft
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import state
from bot.state import PendingApplication
from classifier.link import find_application_channel, is_supported
from context.github import fetch_github_profile_data
from context.resume import extract_structured_profile_from_resume, extract_text_from_pdf
from context.store import (
    TYPE_GITHUB,
    TYPE_LINKEDIN,
    TYPE_RESUME,
    get_context,
    get_context_for_jd,
    get_template,
    has_context,
    set_template,
    upsert_context,
)
from custom_page.executor import CustomPageExecutor
from email_service.router import EmailApplicationService
from executor.form_executor import FormExecutor
from generator.answer_generator import AnswerGenerator
from idempotency import (
    check_duplicate,
    check_rate_limit,
    compute_jd_hash,
    create_application,
    get_or_create_user,
    mark_cancelled,
    mark_failed,
    mark_pending_approval,
    mark_submitted,
    resolve_canonical_url,
    save_application_answers,
)
from telemetry.logger import get_recent_logs, get_telemetry_summary, log_event

logger = logging.getLogger(__name__)

_form_executor: Optional[FormExecutor] = None
_custom_page_executor: Optional[CustomPageExecutor] = None
_email_service: Optional[EmailApplicationService] = None
_generator: Optional[AnswerGenerator] = None


def set_executor(executor: FormExecutor) -> None:
    global _form_executor
    _form_executor = executor


def set_custom_page_executor(executor: CustomPageExecutor) -> None:
    global _custom_page_executor
    _custom_page_executor = executor


def set_email_service(service: EmailApplicationService) -> None:
    global _email_service
    _email_service = service


def set_generator(generator: AnswerGenerator) -> None:
    global _generator
    _generator = generator


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/start", "chat_id": chat_id})

    text = (
        "👋 *Welcome to flam — your AI Job Application Agent*\n\n"
        "I fill and submit job applications across Google Forms, MS Forms, Typeform, Notion Forms, "
        "custom career pages (Greenhouse/Lever/Workday), and direct email postings\\.\n\n"
        "🛠️ *Setup your context:*\n"
        "• `/upload` — Upload your Resume \\(send PDF document or paste text\\)\n"
        "• `/update_github <username>` — Pull & summarize your GitHub projects\n"
        "• `/linkedin` — Paste your LinkedIn profile summary/experience\n"
        "• `/template` — Customize your answer tone & style\n"
        "• `/add_mail_auth` — Connect Gmail/Outlook OAuth for email applications\n"
        "• `/status` — View your current saved profile & context\n"
        "• `/logs` — View recent system logs & telemetry\n\n"
        "🚀 *To Apply:*\n"
        "Simply paste any job posting URL or recruitment message\\!\n"
        "I'll generate grounded answers, show you a preview, and submit upon `/approve`\\."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /add_mail_auth
# ---------------------------------------------------------------------------

async def handle_add_mail_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/add_mail_auth", "chat_id": chat_id})

    user = await get_or_create_user(chat_id)
    email_service = _email_service or EmailApplicationService()
    auth_url = email_service.get_oauth_auth_url(user.id)

    text = (
        "📧 *Email Application OAuth Setup*\n\n"
        "Flam sends email-based job applications using direct OAuth tokens or 1\\-tap client dispatch\\. "
        "Your email passwords are *never* stored\\.\n\n"
        f"🔗 [Click here to authorize Gmail/Outlook]({auth_url})\n\n"
        "_Once authorized, any email-based job post will automatically generate a tailored application draft "
        "and allow 1-tap dispatch upon `/approve`\\._"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/status", "chat_id": chat_id})

    user = await get_or_create_user(chat_id)
    ctx = await get_context(user.id)
    tmpl = await get_template(user.id)

    lines = ["📊 *Candidate Context Status:*\n"]
    if TYPE_RESUME in ctx:
        lines.append(f"✅ *Resume:* Saved \\({len(ctx[TYPE_RESUME])} characters\\)")
    else:
        lines.append("❌ *Resume:* Not uploaded \\(use `/upload` or send PDF\\)")

    if TYPE_GITHUB in ctx:
        lines.append(f"✅ *GitHub:* Synced projects recorded")
    else:
        lines.append("❌ *GitHub:* Not synced \\(use `/update_github <user>`\\)")

    if TYPE_LINKEDIN in ctx:
        lines.append(f"✅ *LinkedIn:* Saved \\({len(ctx[TYPE_LINKEDIN])} characters\\)")
    else:
        lines.append("⚪ *LinkedIn:* Optional \\(use `/linkedin` to add\\)")

    email_service = _email_service or EmailApplicationService()
    if email_service.is_oauth_connected(user.id):
        lines.append("✅ *Email OAuth:* Connected")
    else:
        lines.append("⚪ *Email OAuth:* Optional \\(use `/add_mail_auth` to connect\\)")

    if tmpl:
        lines.append(f"\n🎨 *Custom Style Template:*\n`{_escape_md(tmpl[:200])}`")
    else:
        lines.append("\n🎨 *Style Template:* Default \\(Direct, concise, impact-oriented\\)")

    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /logs
# ---------------------------------------------------------------------------

async def handle_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/logs", "chat_id": chat_id})

    summary = get_telemetry_summary()
    recent = get_recent_logs(lines=15)
    log_snippet = "\n".join(recent)

    msg = (
        f"📋 *Recent Telemetry & Logs:*\n\n"
        f"• Total Events: `{summary['events_count']}`\n"
        f"• Event Breakdown: `{summary['event_types']}`\n\n"
        f"🔍 *Log Tail:*\n```\n{log_snippet}\n```"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /upload
# ---------------------------------------------------------------------------

async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/upload", "chat_id": chat_id})

    if context.args:
        text = " ".join(context.args)
        await _process_resume_text(update, chat_id, text)
    else:
        state.set_waiting(chat_id, "waiting_resume")
        await update.effective_message.reply_text(
            "📄 *Please upload your Resume:*\n\n"
            "Send your resume as a *PDF document* attachment, or paste your full resume text in your next message.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    chat_id = update.effective_chat.id
    if not doc:
        return

    log_event("document_received", {"chat_id": chat_id, "file_name": doc.file_name, "mime_type": doc.mime_type})

    if doc.file_name.lower().endswith(".pdf") or doc.mime_type == "application/pdf":
        file = await doc.get_file()
        os.makedirs("temp", exist_ok=True)
        pdf_path = f"temp/resume_{chat_id}_{int(time.time())}.pdf"
        await file.download_to_drive(pdf_path)

        text = extract_text_from_pdf(pdf_path)
        try:
            os.remove(pdf_path)
        except OSError:
            pass

        if not text or len(text.strip()) < 50:
            await update.effective_message.reply_text(
                "⚠️ *Could not extract readable text from PDF*\n\nPlease make sure the PDF contains text (not scanned images), or paste plain text.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        await _process_resume_text(update, chat_id, text)
    else:
        await update.effective_message.reply_text(
            "⚠️ *Unsupported format*\nPlease send your resume as a `.pdf` file or paste the plain text.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _process_resume_text(update: Update, chat_id: int, text: str) -> None:
    user = await get_or_create_user(chat_id)
    await upsert_context(user.id, TYPE_RESUME, text, source="resume_upload")
    parsed = extract_structured_profile_from_resume(text)
    state.clear_waiting(chat_id)

    name = _escape_md(parsed.get("full_name", "Candidate"))
    skills = _escape_md(", ".join(parsed.get("skills", [])[:8]))
    preview = (
        f"✅ *Resume Saved Successfully\\!*\n\n"
        f"👤 *Identified Profile:* {name}\n"
        f"🛠️ *Extracted Skills:* {skills or 'General Software Engineering'}\n"
        f"📏 *Content Length:* {len(text)} characters\n\n"
        f"_Your applications will now be grounded in this resume data\\._"
    )
    await update.effective_message.reply_text(preview, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /update_github
# ---------------------------------------------------------------------------

async def handle_update_github(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/update_github", "chat_id": chat_id})

    if not context.args:
        await update.effective_message.reply_text(
            "ℹ️ *Usage:* `/update_github <username>`\nExample: `/update_github octocat`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    username = context.args[0].strip()
    status_msg = await update.effective_message.reply_text(
        f"⏳ *Fetching GitHub repositories for* `{_escape_md(username)}`\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    data = await fetch_github_profile_data(username, groq_api_key=os.getenv("GROQ_API_KEY"))
    if not data or not data.get("repos"):
        await status_msg.edit_text(
            f"❌ *Could not fetch public repositories for* `{_escape_md(username)}`\\.\nCheck the username and try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    import json
    user = await get_or_create_user(chat_id)
    await upsert_context(user.id, TYPE_GITHUB, json.dumps(data), source=f"github:{username}")

    repo_names = [r["name"] for r in data["repos"][:5]]
    names_str = _escape_md(", ".join(repo_names))
    await status_msg.edit_text(
        f"✅ *GitHub Profile Synced\\!*\n\n"
        f"📦 *Projects recorded:* {names_str}\n"
        f"_Summaries generated and stored for JD-aware matching\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ---------------------------------------------------------------------------
# /linkedin
# ---------------------------------------------------------------------------

async def handle_linkedin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/linkedin", "chat_id": chat_id})

    if context.args:
        text = " ".join(context.args)
        await _process_linkedin_text(update, chat_id, text)
    else:
        state.set_waiting(chat_id, "waiting_linkedin")
        await update.effective_message.reply_text(
            "💼 *Paste your LinkedIn profile text:*\n\n"
            "Paste your About section, experience history, or full profile summary in your next message.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _process_linkedin_text(update: Update, chat_id: int, text: str) -> None:
    user = await get_or_create_user(chat_id)
    await upsert_context(user.id, TYPE_LINKEDIN, text, source="linkedin_paste")
    state.clear_waiting(chat_id)
    await update.effective_message.reply_text(
        f"✅ *LinkedIn profile saved\\!* \\({len(text)} characters\\)",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ---------------------------------------------------------------------------
# /template
# ---------------------------------------------------------------------------

async def handle_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/template", "chat_id": chat_id})

    user = await get_or_create_user(chat_id)

    if context.args and context.args[0].lower() == "edit":
        if len(context.args) > 1:
            template_text = " ".join(context.args[1:])
            await set_template(user.id, template_text)
            await update.effective_message.reply_text(
                "✅ *Custom template updated successfully\\!*",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            state.set_waiting(chat_id, "waiting_template")
            await update.effective_message.reply_text(
                "📝 *Enter your custom style / answer guidelines in your next message:*\n\n"
                "Example: _Always mention my experience leading distributed teams and emphasize low-latency Rust systems._",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        return

    current = await get_template(user.id)
    if current:
        msg = (
            f"🎨 *Current Custom Style Template:*\n\n"
            f"```\n{current}\n```\n\n"
            f"To update: `/template edit <new instructions>`"
        )
    else:
        msg = (
            "🎨 *Style Template:* _Default (Direct, concise, impact-oriented)_\n\n"
            "To add custom tone/guidelines: `/template edit <instructions>`"
        )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# Main message dispatcher
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    chat_id = update.effective_chat.id

    if not text:
        return

    # Normalize backslash commands
    if text.startswith("\\"):
        cmd_part = text[1:].split()[0].lower()
        args = text.split()[1:]
        context.args = args

        command_map = {
            "start": handle_start,
            "upload": handle_upload,
            "update_github": handle_update_github,
            "linkedin": handle_linkedin,
            "template": handle_template,
            "add_mail_auth": handle_add_mail_auth,
            "add-mail-auth": handle_add_mail_auth,
            "status": handle_status,
            "logs": handle_logs,
            "approve": handle_approve,
            "cancel": handle_cancel,
        }
        if cmd_part in command_map:
            await command_map[cmd_part](update, context)
            return

    # Check multi-step waiting state
    waiting = state.get_waiting(chat_id)
    if waiting:
        if waiting == "waiting_resume":
            await _process_resume_text(update, chat_id, text)
            return
        elif waiting == "waiting_linkedin":
            await _process_linkedin_text(update, chat_id, text)
            return
        elif waiting == "waiting_template":
            user = await get_or_create_user(chat_id)
            await set_template(user.id, text)
            state.clear_waiting(chat_id)
            await update.effective_message.reply_text(
                "✅ *Custom template updated successfully\\!*",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

    # Classify channel
    channel, target = find_application_channel(text)
    if not channel or not target:
        await update.effective_message.reply_text(
            "ℹ️ No supported application link or recruitment email found in that message\\.\n\n"
            "Paste a job posting with a form link or email address to apply\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    log_event("application_channel_detected", {"channel": channel, "target": target, "chat_id": chat_id})

    # Guard: Active pending application
    if state.has_pending(chat_id):
        await update.effective_message.reply_text(
            "⚠️ You already have an application waiting for approval\\.\n"
            "Please `/approve` or `/cancel` it before submitting another\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    user = await get_or_create_user(chat_id)

    # 1. Canonical URL / ID resolution
    form_id = await resolve_canonical_url(target) if channel != "email_application" else target.lower()
    jd_hash = compute_jd_hash(text)

    # 2. Universal Idempotency check
    is_dup = await check_duplicate(user.id, jd_hash, form_id)
    if is_dup:
        log_event("duplicate_blocked", {"user_id": user.id, "form_id": form_id})
        await update.effective_message.reply_text(
            "⚠️ *Duplicate Application Detected*\n\n"
            "You have already applied to this exact posting with this form\\.\n"
            f"• *Form ID:* `{_escape_md(form_id)}`\n"
            f"• *JD Hash:* `{_escape_md(jd_hash[:12])}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # 3. Universal Rate Limit check
    is_limited, today_count, limit = await check_rate_limit(user.id)
    if is_limited:
        log_event("rate_limit_exceeded", {"user_id": user.id, "count": today_count, "limit": limit})
        await update.effective_message.reply_text(
            f"⛔ *Daily Application Limit Reached*\n\n"
            f"You have submitted *{today_count}/{limit}* applications today\\.\n"
            f"Rate limit resets at midnight UTC\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Fetch candidate context & template
    candidate_ctx = await get_context_for_jd(user.id, jd_text=text)
    user_tmpl = await get_template(user.id)

    # -----------------------------------------------------------------------
    # Route A: Email Application
    # -----------------------------------------------------------------------
    if channel == "email_application":
        status_msg = await update.effective_message.reply_text(
            f"📧 *Email application detected:* `{_escape_md(target)}`\n"
            "Generating tailored cover application...",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        email_service = _email_service or EmailApplicationService()
        draft = await email_service.draft_application(
            to_email=target,
            jd_text=text,
            context=candidate_ctx,
            user_template=user_tmpl,
        )
        app = await create_application(user.id, jd_hash, form_id, channel)
        await mark_pending_approval(app.id)

        state.store(
            chat_id,
            PendingApplication(
                application_id=app.id,
                form_url=target,
                platform="email_application",
                filled_fields=[],
                page=None,
                jd_hash=jd_hash,
                form_id=form_id,
                email_draft=draft,
            ),
        )

        preview_text = (
            f"📋 *Email Application Preview — Approval Required*\n\n"
            f"📬 *To:* `{_escape_md(draft['to_email'])}`\n"
            f"📌 *Subject:* `{_escape_md(draft['subject'])}`\n\n"
            f"📝 *Body:*\n```\n{draft['body']}\n```\n\n"
            f"🔗 [1-Tap Send via Mail Client]({draft['mailto_url']})\n\n"
            "• `/approve` — Confirm & mark as submitted\n"
            "• `/cancel` — Discard this application"
        )
        await status_msg.edit_text(preview_text, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # -----------------------------------------------------------------------
    # Route B: Custom Career Page (browser-use primary / Groq fallback)
    # -----------------------------------------------------------------------
    if channel == "custom_career_page":
        status_msg = await update.effective_message.reply_text(
            "🌐 *Custom Career Page detected*\n"
            "Launching browser inspection & field extraction...",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        app = await create_application(user.id, jd_hash, form_id, channel)
        custom_exec = _custom_page_executor or CustomPageExecutor()
        if not custom_exec._browser:
            await custom_exec.start()

        generator = _generator or AnswerGenerator()
        try:
            page, filled_fields, engine_used = await custom_exec.fill(
                page_url=form_id,
                generator=generator,
                jd_text=text,
                context=candidate_ctx,
                user_template=user_tmpl,
            )
        except Exception as exc:
            logger.error("Custom page fill failed: %s", exc, exc_info=True)
            await mark_failed(app.id, str(exc))
            await status_msg.edit_text(f"❌ *Could not process career page:*\n`{_escape_md(str(exc))}`", parse_mode=ParseMode.MARKDOWN_V2)
            return

        await mark_pending_approval(app.id)
        state.store(
            chat_id,
            PendingApplication(
                application_id=app.id,
                form_url=form_id,
                platform="custom_career_page",
                filled_fields=filled_fields,
                page=page,
                jd_hash=jd_hash,
                form_id=form_id,
            ),
        )
        preview_text = _format_preview(app.id, form_id, filled_fields, platform_name=f"Custom Page ({engine_used})")
        await status_msg.edit_text(preview_text, parse_mode=ParseMode.MARKDOWN_V2)
        return

    # -----------------------------------------------------------------------
    # Route C: Known Platform Form (Google Forms, MS Forms, Typeform, Notion)
    # -----------------------------------------------------------------------
    status_msg = await update.effective_message.reply_text(
        f"🔍 *{_escape_md(channel.replace('_', ' ').title())} detected*\n"
        "Opening form & generating grounded answers...",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    app = await create_application(user.id, jd_hash, form_id, channel)
    form_exec = _form_executor or FormExecutor()
    if not form_exec._browser:
        await form_exec.start()

    generator = _generator or AnswerGenerator()
    try:
        page, filled_fields = await form_exec.fill(
            platform_id=channel,
            form_url=form_id,
            generator=generator,
            jd_text=text,
            context=candidate_ctx,
            user_template=user_tmpl,
        )
    except Exception as exc:
        logger.error("Form fill failed for %s: %s", channel, exc, exc_info=True)
        await mark_failed(app.id, str(exc))
        await status_msg.edit_text(f"❌ *Could not fill form:*\n`{_escape_md(str(exc))}`", parse_mode=ParseMode.MARKDOWN_V2)
        return

    await mark_pending_approval(app.id)
    state.store(
        chat_id,
        PendingApplication(
            application_id=app.id,
            form_url=form_id,
            platform=channel,
            filled_fields=filled_fields,
            page=page,
            jd_hash=jd_hash,
            form_id=form_id,
        ),
    )
    preview_text = _format_preview(app.id, form_id, filled_fields, platform_name=channel.replace('_', ' ').title())
    await status_msg.edit_text(preview_text, parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# /approve & /cancel
# ---------------------------------------------------------------------------

async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/approve", "chat_id": chat_id})

    pending = state.remove(chat_id)
    if not pending:
        await update.effective_message.reply_text("No pending application to approve\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    status_msg = await update.effective_message.reply_text("⏳ *Submitting application\\.\\.\\.*", parse_mode=ParseMode.MARKDOWN_V2)

    try:
        if pending.platform == "email_application":
            email_service = _email_service or EmailApplicationService()
            receipt_path = await email_service.send_or_confirm(chat_id, pending.email_draft, pending.application_id)
            await mark_submitted(pending.application_id, receipt_path)
            await status_msg.edit_text(
                f"🎉 *Email Application Dispatched\\!*\n\n"
                f"• *To:* `{_escape_md(pending.email_draft['to_email'])}`\n"
                f"• *Receipt:* `{_escape_md(receipt_path)}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        if pending.platform == "custom_career_page":
            custom_exec = _custom_page_executor or CustomPageExecutor()
            receipt_path = await custom_exec.submit(pending.page, pending.application_id)
        else:
            form_exec = _form_executor or FormExecutor()
            receipt_path = await form_exec.submit(pending.page, pending.platform, pending.application_id)

        await mark_submitted(pending.application_id, receipt_path)
        if pending.filled_fields:
            await save_application_answers(pending.application_id, pending.filled_fields)

        with open(receipt_path, "rb") as photo:
            await update.effective_chat.send_photo(
                photo=photo,
                caption=f"🎉 *Application Submitted Successfully\\!*\n\n• Form: `{_escape_md(pending.form_url)}`\n• Receipt captured",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        await status_msg.delete()

    except Exception as exc:
        logger.error("Submit failed: %s", exc, exc_info=True)
        await mark_failed(pending.application_id, str(exc))
        await status_msg.edit_text(f"❌ *Submission failed:* `{_escape_md(str(exc))}`", parse_mode=ParseMode.MARKDOWN_V2)


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    log_event("command_received", {"command": "/cancel", "chat_id": chat_id})

    pending = state.remove(chat_id)
    if not pending:
        await update.effective_message.reply_text("No pending application to cancel\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if pending.page:
        await pending.page.close()
    await mark_cancelled(pending.application_id)
    await update.effective_message.reply_text("🚫 *Application cancelled\\.*", parse_mode=ParseMode.MARKDOWN_V2)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_preview(app_id: int, form_url: str, fields: list[dict], platform_name: str = "Form") -> str:
    lines = [
        f"📋 *Application Preview — Approval Required*",
        f"• *Platform:* {_escape_md(platform_name)}",
        f"• *Target:* `{_escape_md(form_url)}`\n",
    ]

    for f in fields:
        q = _escape_md(f["question"])
        v = _escape_md(str(f["value"])) if f["value"] is not None else "_\\(skipped\\)_"
        src = _escape_md(f.get("source", "profile"))
        flag = " ❓ _(low confidence)_" if f.get("flagged") else ""
        lines.append(f"• *{q}:* {v}{flag}\n  _↳ source: {src}_")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("• `/approve` — Submit this application")
    lines.append("• `/cancel` — Discard and close")
    return "\n".join(lines)


def _escape_md(text: str) -> str:
    reserved = r"_*[]()~`>#+-=|{}.!"
    out = []
    for ch in text:
        if ch in reserved:
            out.append(f"\\{ch}")
        else:
            out.append(ch)
    return "".join(out)
