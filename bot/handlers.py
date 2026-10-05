"""
Telegram handlers.

Send flam anything that says how to apply: a link, an email address, a forwarded post,
a screenshot, or a job PDF. It works out the channel, drafts the answers, shows a
preview, and only submits after /approve.

Commands
  /start /upload /update_github /linkedin /template /fact /status /history /logs
  /approve /cancel /edit /add_mail_auth

All replies use Telegram HTML (escape with esc()). MarkdownV2 is not used because one
unescaped '.' or '-' makes Telegram reject the whole message.
"""
from __future__ import annotations

import functools
import logging
import os
import re
from html import escape, unescape
from pathlib import Path
from typing import Optional

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot import state
from bot.state import PendingApplication
from classifier.link import find_application_channel, pick_apply_email
from context.github import fetch_github_profile_data
from context.resume import extract_structured_profile_from_resume, extract_text_from_pdf
from context.store import (
    TYPE_FACTS,
    TYPE_GITHUB,
    TYPE_LINKEDIN,
    TYPE_RESUME,
    get_context,
    get_context_for_jd,
    get_facts,
    get_template,
    set_template,
    set_user_fact,
    upsert_context,
)
from custom_page.executor import CustomPageExecutor
from email_service.router import EmailApplicationService
from executor.form_executor import FormExecutor
from generator import llm
from generator.answer_generator import AnswerGenerator
from idempotency import (
    check_duplicate,
    check_rate_limit,
    create_application,
    get_or_create_user,
    mark_cancelled,
    mark_failed,
    mark_pending_approval,
    mark_submitted,
    recent_applications,
    resolve_canonical_url,
    save_application_answers,
    stable_jd_hash,
)
from intake.post import PostInfo, parse_post
from telemetry.logger import get_recent_logs, get_telemetry_summary, log_event

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = _ROOT / "data"          # saved resume PDFs (gitignored)
TMP_DIR = _ROOT / "temp"

_form_executor: Optional[FormExecutor] = None
_custom_page_executor: Optional[CustomPageExecutor] = None
_email_service: Optional[EmailApplicationService] = None
_generator: Optional[AnswerGenerator] = None

_MAX_MSG = 3800  # Telegram's limit is 4096; keep headroom for tags


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
# Helpers: access control, formatting, sending
# ---------------------------------------------------------------------------

def esc(text: object) -> str:
    return escape(str(text), quote=False)


def _allowed(user_id: int) -> bool:
    raw = os.getenv("ALLOWED_TELEGRAM_IDS", "").strip()
    if not raw:
        return True  # open until configured; main.py warns at startup
    return str(user_id) in {x.strip() for x in raw.split(",") if x.strip()}


def authorized(fn):
    """Ignore everyone who is not on ALLOWED_TELEGRAM_IDS (when it is set)."""
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or not _allowed(user.id):
            log_event("unauthorized_access", {"user_id": getattr(user, "id", None)})
            return
        await fn(update, context)
    return wrapper


def _plain(html_text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html_text))


def _without_mailto(html_text: str) -> str:
    return re.sub(r'<a href="mailto:[^"]*">(.*?)</a>', r"\1", html_text)


async def _reply(update: Update, text: str) -> Message:
    """Send HTML; if Telegram rejects it, retry without mailto links, then as plain text."""
    last: Exception | None = None
    for attempt, mode in ((text, ParseMode.HTML), (_without_mailto(text), ParseMode.HTML), (_plain(text), None)):
        try:
            return await update.effective_message.reply_text(
                attempt, parse_mode=mode, disable_web_page_preview=True)
        except BadRequest as exc:
            last = exc
            logger.warning("Telegram rejected message (%s); retrying simpler", exc)
    assert last is not None
    raise last


async def _edit(msg: Message, text: str) -> None:
    for attempt, mode in ((text, ParseMode.HTML), (_without_mailto(text), ParseMode.HTML), (_plain(text), None)):
        try:
            await msg.edit_text(attempt, parse_mode=mode, disable_web_page_preview=True)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            logger.warning("Telegram rejected edit (%s); retrying simpler", exc)


async def _send_blocks(update: Update, first: Optional[Message], blocks: list[str]) -> None:
    """Send blocks as one or more messages, never splitting inside a block."""
    chunks: list[str] = []
    cur = ""
    for b in blocks:
        b = b if len(b) <= _MAX_MSG else b[:_MAX_MSG] + "…"
        if cur and len(cur) + len(b) + 2 > _MAX_MSG:
            chunks.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        chunks.append(cur)
    for i, chunk in enumerate(chunks):
        if i == 0 and first is not None:
            await _edit(first, chunk)
        else:
            await _reply(update, chunk)


def _resume_path(user_id: int) -> Path:
    return DATA_DIR / f"resume_{user_id}.pdf"


def _chat_id(update: Update) -> int:
    return update.effective_chat.id


# ---------------------------------------------------------------------------
# /start /status /history /logs
# ---------------------------------------------------------------------------

@authorized
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_event("command_received", {"command": "/start", "chat_id": _chat_id(update)})
    await _reply(update, (
        "👋 <b>flam applies to jobs for you.</b>\n\n"
        "<b>To apply</b>, send me any of these:\n"
        "• a form or career-page link\n"
        "• a post that says <i>mail me at …</i> (paste it, forward it, or send a screenshot)\n"
        "• a job PDF\n"
        "I write the answers in your voice, show you a preview, and submit only after you /approve.\n\n"
        "<b>Set up once</b>\n"
        "/upload - your resume (PDF or text)\n"
        "/update_github &lt;username&gt; - your projects\n"
        "/linkedin - paste your profile text\n"
        "/fact &lt;key&gt; &lt;value&gt; - things a resume doesn't say (notice period, expected CTC…)\n"
        "/template - how your answers should sound\n\n"
        "<b>While applying</b>\n"
        "/edit &lt;n&gt; &lt;answer&gt; - change an answer in the preview\n"
        "/approve - submit · /cancel - discard\n\n"
        "/status - what I know about you · /history - past applications"
    ))


@authorized
async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    log_event("command_received", {"command": "/status", "chat_id": chat_id})
    user = await get_or_create_user(chat_id)
    ctx = await get_context(user.id)
    tmpl = await get_template(user.id)
    facts = get_facts(ctx)

    lines = ["📊 <b>What I know about you</b>\n"]
    lines.append(f"{'✅' if TYPE_RESUME in ctx else '❌'} Resume" + (f" ({len(ctx[TYPE_RESUME])} characters)" if TYPE_RESUME in ctx else " - use /upload"))
    lines.append(f"{'✅' if TYPE_GITHUB in ctx else '❌'} GitHub projects" + ("" if TYPE_GITHUB in ctx else " - use /update_github"))
    lines.append(f"{'✅' if TYPE_LINKEDIN in ctx else '⚪'} LinkedIn" + ("" if TYPE_LINKEDIN in ctx else " (optional) - use /linkedin"))
    lines.append(f"{'✅' if _resume_path(user.id).exists() else '⚪'} Resume PDF for file uploads")
    if facts:
        shown = ", ".join(f"{k.replace('_', ' ')}: {v[:40]}" for k, v in facts.items() if k not in ("about", "skills"))
        lines.append(f"\n🧾 <b>Facts</b>\n{esc(shown)}")
    lines.append("\n🎨 <b>Style:</b> " + (esc(tmpl[:200]) if tmpl else "default (plain and direct)"))
    if not os.getenv("ALLOWED_TELEGRAM_IDS", "").strip():
        lines.append("\n⚠️ Anyone can use this bot. Set ALLOWED_TELEGRAM_IDS in secrets/.env.")
    await _reply(update, "\n".join(lines))


@authorized
async def handle_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_or_create_user(_chat_id(update))
    apps = await recent_applications(user.id, 10)
    if not apps:
        await _reply(update, "No applications yet.")
        return
    icon = {"submitted": "✅", "unconfirmed": "❓", "failed": "❌", "cancelled": "🚫"}
    lines = ["🗂 <b>Recent applications</b>\n"]
    for a in apps:
        what = " at ".join(x for x in (a.role, a.company) if x) or a.form_id[:50]
        when = a.created_at.strftime("%d %b")
        lines.append(f"{icon.get(a.status, '⏳')} {esc(what)} · {esc(a.platform.replace('_', ' '))} · {when}")
    await _reply(update, "\n".join(lines))


@authorized
async def handle_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    summary = get_telemetry_summary()
    tail = "\n".join(get_recent_logs(lines=12))[-2500:]
    await _reply(update, (
        f"📋 <b>Telemetry</b>\nEvents: {summary['events_count']}\n"
        f"<code>{esc(summary['breakdown'])}</code>\n\n<pre>{esc(tail)}</pre>"
    ))


# ---------------------------------------------------------------------------
# Profile: /upload /update_github /linkedin /template /fact
# ---------------------------------------------------------------------------

@authorized
async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    if context.args:
        await _process_resume_text(update, chat_id, " ".join(context.args))
        return
    state.set_waiting(chat_id, "waiting_resume")
    await _reply(update, "📄 Send your resume as a <b>PDF</b>, or paste the text in your next message.")


async def _process_resume_text(update: Update, chat_id: int, text: str) -> None:
    user = await get_or_create_user(chat_id)
    status = await _reply(update, "⏳ Reading your resume…")
    await upsert_context(user.id, TYPE_RESUME, text)
    facts = await extract_structured_profile_from_resume(text)
    if facts:
        import json
        await upsert_context(user.id, TYPE_FACTS, json.dumps(facts))
    state.clear_waiting(chat_id)

    skills = ", ".join(s.strip() for s in facts.get("skills", "").split(",")[:8] if s.strip())
    missing = [k for k in ("name", "email", "phone", "college") if not facts.get(k)]
    msg = [
        "✅ <b>Resume saved</b>",
        f"👤 {esc(facts.get('name') or 'Name not found')}",
        f"🛠 {esc(skills or 'Skills not found')}",
    ]
    if missing:
        msg.append(f"\nI couldn't find: {esc(', '.join(missing))}. Add with /fact if a form will ask.")
    msg.append("\nTip: add things a resume doesn't say, e.g. <code>/fact notice_period 30 days</code>")
    await _edit(status, "\n".join(msg))


@authorized
async def handle_update_github(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    if not context.args:
        await _reply(update, "Usage: <code>/update_github username</code>")
        return
    username = context.args[0].strip().lstrip("@")
    status = await _reply(update, f"⏳ Reading <code>{esc(username)}</code>'s public repositories…")
    try:
        repos = await fetch_github_profile_data(username)
    except ValueError:
        await _edit(status, f"❌ No GitHub user called <code>{esc(username)}</code>.")
        return
    except Exception as exc:
        logger.warning("GitHub fetch failed: %s", exc)
        await _edit(status, "❌ GitHub didn't answer (rate limit or network). Try again in a minute.")
        return
    if not repos:
        await _edit(status, "❌ That account has no public original repositories.")
        return

    import json
    user = await get_or_create_user(chat_id)
    await upsert_context(user.id, TYPE_GITHUB, json.dumps(repos))
    names = ", ".join(r["name"] for r in repos[:6])
    await _edit(status, f"✅ <b>GitHub synced</b>\n📦 {esc(names)}\nI'll pick the most relevant ones for each job.")


@authorized
async def handle_linkedin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    if context.args:
        await _process_linkedin_text(update, chat_id, " ".join(context.args))
        return
    state.set_waiting(chat_id, "waiting_linkedin")
    await _reply(update, "💼 Paste your LinkedIn About and experience text in your next message.")


async def _process_linkedin_text(update: Update, chat_id: int, text: str) -> None:
    user = await get_or_create_user(chat_id)
    await upsert_context(user.id, TYPE_LINKEDIN, text)
    state.clear_waiting(chat_id)
    await _reply(update, f"✅ LinkedIn saved ({len(text)} characters).")


@authorized
async def handle_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    user = await get_or_create_user(chat_id)

    if context.args and context.args[0].lower() == "edit":
        if len(context.args) > 1:
            await set_template(user.id, " ".join(context.args[1:]))
            await _reply(update, "✅ Style updated.")
        else:
            state.set_waiting(chat_id, "waiting_template")
            await _reply(update, (
                "📝 Tell me how your answers should sound, in your next message.\n"
                "Example: <i>Short sentences. Mention that I like small teams. Never use the word 'passionate'.</i>"
            ))
        return

    current = await get_template(user.id)
    if current:
        await _reply(update, f"🎨 <b>Your style notes</b>\n<pre>{esc(current)}</pre>\nChange: <code>/template edit …</code>")
    else:
        await _reply(update, "🎨 Using the default voice: plain, direct, specific.\nTo add your own notes: <code>/template edit …</code>")


@authorized
async def handle_fact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fact -> list.  /fact key value -> set.  /fact key - -> remove."""
    chat_id = _chat_id(update)
    user = await get_or_create_user(chat_id)

    if not context.args:
        facts = get_facts(await get_context(user.id))
        shown = "\n".join(f"<code>{esc(k)}</code>: {esc(v[:80])}" for k, v in facts.items() if k not in ("about", "skills"))
        await _reply(update, (
            "🧾 <b>Your facts</b> (used for plain questions)\n" + (shown or "none yet") +
            "\n\nSet: <code>/fact notice_period 30 days</code>\nRemove: <code>/fact notice_period -</code>\n"
            "Useful keys: notice_period, current_ctc, expected_ctc, years_experience, location, relocate, work_authorization"
        ))
        return

    key = re.sub(r"[\s-]+", "_", context.args[0].strip().lower())
    value = " ".join(context.args[1:]).strip()
    if not value:
        await _reply(update, "Usage: <code>/fact key value</code>")
        return
    if value == "-":
        await set_user_fact(user.id, key, "")
        await _reply(update, f"🗑 Removed <code>{esc(key)}</code>.")
    else:
        await set_user_fact(user.id, key, value)
        await _reply(update, f"✅ <code>{esc(key)}</code> = {esc(value)}")


@authorized
async def handle_add_mail_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = _email_service or EmailApplicationService()
    if not svc.oauth_configured():
        await _reply(update, (
            "📧 Automatic Gmail sending isn't set up yet.\n"
            "Email applications still work: I write the email, you tap <b>Open in Gmail</b>, "
            "attach your resume and send."
        ))
        return
    user = await get_or_create_user(_chat_id(update))
    url = escape(svc.get_oauth_auth_url(user.id), quote=True)
    await _reply(update, f'📧 <a href="{url}">Authorize Gmail</a>')


# ---------------------------------------------------------------------------
# Intake: text, screenshots, documents
# ---------------------------------------------------------------------------

_COMMANDS: dict = {}  # filled at the bottom; used for backslash-style commands


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    chat_id = _chat_id(update)
    if not text:
        return

    if text.startswith("\\") and len(text) > 1:
        parts = text[1:].split()
        if parts and parts[0].lower() in _COMMANDS:
            context.args = parts[1:]
            await _COMMANDS[parts[0].lower()](update, context)
            return

    waiting = state.get_waiting(chat_id)
    if waiting == "waiting_resume":
        await _process_resume_text(update, chat_id, text)
        return
    if waiting == "waiting_linkedin":
        await _process_linkedin_text(update, chat_id, text)
        return
    if waiting == "waiting_template":
        user = await get_or_create_user(chat_id)
        await set_template(user.id, text)
        state.clear_waiting(chat_id)
        await _reply(update, "✅ Style updated.")
        return

    await _handle_post(update, text)


@authorized
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    await _handle_image(update, await msg.photo[-1].get_file(), "image/jpeg", msg.caption or "")


async def _handle_image(update: Update, tg_file, mime: str, caption: str) -> None:
    if state.has_pending(_chat_id(update)):
        await _reply(update, "⚠️ You have an application waiting. /approve or /cancel it first.")
        return
    if not llm.has_llm():
        await _reply(update, "❌ I need GROQ_API_KEY to read screenshots.")
        return
    status = await _reply(update, "👀 Reading the screenshot…")
    try:
        data = bytes(await tg_file.download_as_bytearray())
        text = await llm.read_image_text(data, mime)
    except Exception as exc:
        logger.warning("Screenshot read failed: %s", exc)
        await _edit(status, "❌ I couldn't read that image. Try a sharper screenshot, or paste the text.")
        return
    if len(text.strip()) < 15:
        await _edit(status, "❌ I couldn't find readable text in that image.")
        return
    log_event("screenshot_read", {"chars": len(text)})
    await _handle_post(update, f"{caption}\n\n{text}".strip(), status)


@authorized
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    doc = msg.document
    chat_id = _chat_id(update)
    if not doc:
        return
    name = (doc.file_name or "").lower()
    mime = doc.mime_type or ""
    log_event("document_received", {"chat_id": chat_id, "mime_type": mime})

    if mime.startswith("image/"):
        await _handle_image(update, await doc.get_file(), mime, msg.caption or "")
        return
    if not (name.endswith(".pdf") or mime == "application/pdf"):
        await _reply(update, "⚠️ I can read PDFs and images. Send a PDF resume, a job PDF, or a screenshot.")
        return

    user = await get_or_create_user(chat_id)
    DATA_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    tmp = TMP_DIR / f"incoming_{chat_id}.pdf"
    await (await doc.get_file()).download_to_drive(str(tmp))
    try:
        text = extract_text_from_pdf(str(tmp))
    except Exception as exc:
        logger.warning("PDF read failed: %s", exc)
        text = ""

    caption = (msg.caption or "").lower()
    has_resume = TYPE_RESUME in await get_context(user.id)
    is_resume = state.get_waiting(chat_id) == "waiting_resume" or "resume" in caption or re.search(r"\bcv\b", caption) or not has_resume

    if len(text.strip()) < 50:
        tmp.unlink(missing_ok=True)
        await _reply(update, "⚠️ I couldn't read text from that PDF (scanned image?). Paste the text instead.")
        return

    if is_resume:
        tmp.replace(_resume_path(user.id))
        await _process_resume_text(update, chat_id, text)
    else:
        tmp.unlink(missing_ok=True)
        await _handle_post(update, f"{msg.caption or ''}\n\n{text}".strip())


# ---------------------------------------------------------------------------
# The main flow: post -> channel -> draft -> preview
# ---------------------------------------------------------------------------

async def _handle_post(update: Update, text: str, status: Optional[Message] = None) -> None:
    chat_id = _chat_id(update)
    if state.has_pending(chat_id):
        await _reply(update, "⚠️ You already have an application waiting.\n/approve or /cancel it before sending another.")
        return

    async def say(t: str) -> None:
        nonlocal status
        if status is None:
            status = await _reply(update, t)
        else:
            await _edit(status, t)

    await say("🔍 Reading the post…")
    user = await get_or_create_user(chat_id)
    post = await parse_post(text)

    channel, target = find_application_channel(text)
    if not channel and post.emails:
        channel, target = "email_application", pick_apply_email(text, post.emails)
    if not channel or not target:
        seen = f" (<b>{esc(post.headline())}</b>)" if post.role or post.company else ""
        await say(f"ℹ️ I read the post{seen} but found no email address or apply link in it.\n"
                  "Send the link or email too and I'll take it from there.")
        return
    log_event("application_channel_detected", {"channel": channel, "chat_id": chat_id})

    ctx = await get_context_for_jd(user.id, jd_text=text)
    if not (ctx.get(TYPE_RESUME) or ctx.get(TYPE_LINKEDIN) or ctx.get(TYPE_GITHUB)):
        await say("📄 I need your resume first. Send it with /upload, then send this post again.")
        return

    is_email = channel == "email_application"
    form_id = target.lower() if is_email else await resolve_canonical_url(target)
    jd_hash = stable_jd_hash(post.company, post.role, text)

    dup = await check_duplicate(user.id, jd_hash, form_id, by_form_only=not is_email)
    if dup:
        log_event("duplicate_blocked", {"user_id": user.id})
        when = (dup.submitted_at or dup.created_at).strftime("%d %b")
        await say(f"⚠️ <b>Already applied</b> on {when} ({esc(dup.status)}).\n<code>{esc(form_id)}</code>")
        return

    limited, count, limit = await check_rate_limit(user.id)
    if limited:
        await say(f"⛔ Daily limit reached ({count}/{limit}). It resets at midnight UTC.")
        return

    app = await create_application(user.id, jd_hash, form_id, channel, post.role, post.company)
    if app is None:
        await say("⚠️ That application was already submitted.")
        return

    tmpl = await get_template(user.id)
    if is_email:
        await _run_email(update, say, status, user.id, app.id, target, text, ctx, tmpl, post, jd_hash, form_id)
    else:
        await _run_form(update, say, status, user.id, app.id, channel, form_id, text, ctx, tmpl, post, jd_hash)


async def _run_email(update, say, status, user_id, app_id, to_email, text, ctx, tmpl, post: PostInfo, jd_hash, form_id):
    await say(f"✍️ Writing your email to <code>{esc(to_email)}</code>…")
    svc = _email_service or EmailApplicationService()
    try:
        draft = await svc.draft_application(to_email, text, ctx, tmpl, post)
    except Exception as exc:
        logger.error("Email draft failed: %s", exc, exc_info=True)
        await mark_failed(app_id, str(exc))
        await say(f"❌ Couldn't write the email: {esc(exc)}\nSend the post again to retry.")
        return

    await mark_pending_approval(app_id)
    state.store(_chat_id(update), PendingApplication(
        application_id=app_id, form_url=to_email, platform="email_application", filled_fields=[],
        jd_hash=jd_hash, form_id=form_id, email_draft=draft, post=post,
    ))
    await _send_blocks(update, status, _email_blocks(draft, post))


def _email_blocks(draft: dict, post: PostInfo) -> list[str]:
    head = (
        f"📧 <b>Email draft</b> - {esc(post.headline())}\n"
        f"<b>To:</b> <code>{esc(draft['to_email'])}</code>\n"
        f"<b>Subject:</b> {esc(draft['subject'])}"
    )
    blocks = [head, f"<pre>{esc(draft['body'])}</pre>"]
    warn = []
    if draft.get("name_missing"):
        warn.append("I don't know your name. Send your resume with /upload.")
    for m in draft.get("missing_info", []):
        warn.append(f"The post asks for <b>{esc(m)}</b> and I don't have it. Add it with /fact or /edit body.")
    warn.append("Attach your resume before sending.")
    blocks.append("⚠️ " + "\n⚠️ ".join(warn))
    gmail = escape(draft["gmail_url"], quote=True)
    mailto = escape(draft["mailto_url"], quote=True)
    no_body = "&body=" not in draft["gmail_url"]
    blocks.append(
        f'<a href="{gmail}">📨 Open in Gmail</a>  ·  <a href="{mailto}">Open in mail app</a>\n'
        + ("(The email is long, so the link fills in the address and subject only. Tap the text above to copy the body.)\n" if no_body else "")
        + "\n"
        "Send it, then /approve to log it as applied.\n"
        "/edit subject … · /edit body … · /cancel"
    )
    return blocks


async def _run_form(update, say, status, user_id, app_id, channel, form_id, text, ctx, tmpl, post: PostInfo, jd_hash):
    is_custom = channel == "custom_career_page"
    await say(("🌐 Opening the career page" if is_custom else f"🔍 Opening the {esc(channel.replace('_', ' ').title())}")
              + " and drafting your answers… (this can take up to a minute)")
    generator = _generator or AnswerGenerator()
    try:
        if is_custom:
            ex = _custom_page_executor or CustomPageExecutor()
            if not ex._browser:
                await ex.start()
            resume = _resume_path(user_id)
            page, fields, notes = await ex.fill(form_id, generator, text, ctx, tmpl, post,
                                                str(resume) if resume.exists() else None)
        else:
            ex = _form_executor or FormExecutor()
            if not ex._browser:
                await ex.start()
            page, fields, notes = await ex.fill(channel, form_id, generator, text, ctx, tmpl, post)
    except Exception as exc:
        logger.error("Form fill failed (%s): %s", channel, exc, exc_info=True)
        await mark_failed(app_id, str(exc))
        await say(f"❌ <b>Couldn't prepare that application</b>\n{esc(exc)}\n\nSend it again to retry.")
        return

    await mark_pending_approval(app_id)
    state.store(_chat_id(update), PendingApplication(
        application_id=app_id, form_url=form_id, platform=channel, filled_fields=fields,
        page=page, jd_hash=jd_hash, form_id=form_id, post=post, notes=notes,
    ))
    name = "Career page" if is_custom else channel.replace("_", " ").title()
    await _send_blocks(update, status, _preview_blocks(post, name, form_id, fields, notes))


def _preview_blocks(post: PostInfo, platform: str, target: str, fields: list[dict], notes: list[str]) -> list[str]:
    answered = sum(1 for f in fields if f["value"] is not None)
    needs = len(fields) - answered
    blocks = [
        f"📋 <b>Preview</b> - {esc(post.headline())}\n"
        f"<b>Where:</b> {esc(platform)}\n<code>{esc(target)}</code>\n"
        f"✅ {answered} answered" + (f"  ·  ⚠️ {needs} need you" if needs else "")
    ]
    for n, f in enumerate(fields, 1):
        value = "<i>(empty)</i>" if f["value"] is None else esc(f["value"])
        lines = [f"<b>{n}. {esc(f['question'])}</b>", value]
        if f.get("note"):
            lines.append(f"⚠️ <i>{esc(f['note'])}</i>")
        elif f.get("flagged"):
            lines.append("⚠️ <i>Please check this one.</i>")
        elif f.get("source") == "llm" and f.get("context_keys_used"):
            used = ", ".join(k.split(":", 1)[-1] for k in f["context_keys_used"])
            lines.append(f"<i>↳ from {esc(used)}</i>")
        blocks.append("\n".join(lines))
    if notes:
        blocks.append("\n".join(f"ℹ️ {esc(n)}" for n in notes))
    blocks.append("/approve to submit  ·  /edit &lt;n&gt; &lt;new answer&gt; to change one  ·  /cancel to discard")
    return blocks


# ---------------------------------------------------------------------------
# /edit /approve /cancel
# ---------------------------------------------------------------------------

_EDIT_RE = re.compile(r"^[/\\]edit(?:@\w+)?\s+(\S+)\s+(.+)$", re.IGNORECASE | re.DOTALL)


@authorized
async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    pending = state.get(chat_id)
    if not pending:
        await _reply(update, "Nothing to edit. Send me a post first.")
        return

    m = _EDIT_RE.match((update.effective_message.text or "").strip())
    if not m:
        usage = ("<code>/edit subject …</code> or <code>/edit body …</code>" if pending.email_draft
                 else "<code>/edit 3 your new answer</code> (the number is from the preview)")
        await _reply(update, f"Usage: {usage}")
        return
    target, value = m.group(1).lower(), m.group(2).strip()

    if pending.email_draft:
        if target not in ("subject", "body"):
            await _reply(update, "Use <code>/edit subject …</code> or <code>/edit body …</code>.")
            return
        draft = pending.email_draft
        draft[target] = value
        (_email_service or EmailApplicationService()).rebuild_links(draft)
        await _send_blocks(update, None, _email_blocks(draft, pending.post or PostInfo()))
        return

    if not target.isdigit() or not 1 <= int(target) <= len(pending.filled_fields):
        await _reply(update, f"Pick a number from 1 to {len(pending.filled_fields)}.")
        return
    f = pending.filled_fields[int(target) - 1]
    try:
        ex = (_custom_page_executor or CustomPageExecutor()) if pending.platform == "custom_career_page" \
            else (_form_executor or FormExecutor())
        await ex.refill(pending.page, f, value)
    except Exception as exc:
        await _reply(update, f"⚠️ I couldn't change that one in the live form: {esc(exc)}")
        return
    f.update(value=value, skipped=False, flagged=False, note="", source="user", edited=True)
    pending.warned = False
    await _reply(update, f"✏️ <b>{esc(target)}.</b> updated to:\n{esc(value)}")


@authorized
async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    pending = state.get(chat_id)
    if not pending:
        await _reply(update, "No pending application to approve.")
        return

    empty = [i for i, f in enumerate(pending.filled_fields, 1)
             if f["value"] is None and f["field_type"] != "unsupported"]
    if empty and not pending.warned:
        pending.warned = True
        nums = ", ".join(map(str, empty[:15]))
        await _reply(update, (
            f"⚠️ {len(empty)} answer(s) are still empty (#{nums}).\n"
            "Fill them with <code>/edit &lt;n&gt; &lt;answer&gt;</code>, or send /approve again to submit anyway."
        ))
        return

    state.remove(chat_id)
    log_event("command_received", {"command": "/approve", "chat_id": chat_id})
    status = await _reply(update, "⏳ Submitting…")
    app_id = pending.application_id

    try:
        if pending.platform == "email_application":
            svc = _email_service or EmailApplicationService()
            receipt = await svc.record_receipt(chat_id, pending.email_draft, app_id)
            await mark_submitted(app_id, receipt, confirmed=True)
            await _edit(status, f"✅ Logged as applied: <code>{esc(pending.email_draft['to_email'])}</code>")
            return

        if pending.platform == "custom_career_page":
            receipt, confirmed = await (_custom_page_executor or CustomPageExecutor()).submit(pending.page, app_id)
        else:
            receipt, confirmed = await (_form_executor or FormExecutor()).submit(pending.page, pending.platform, app_id)

        await mark_submitted(app_id, receipt, confirmed)
        if pending.filled_fields:
            await save_application_answers(app_id, pending.filled_fields)

        caption = (
            f"✅ <b>Submitted.</b>\n<code>{esc(pending.form_url)}</code>" if confirmed else
            "⚠️ <b>I clicked submit but couldn't confirm it went through.</b>\n"
            "Check this screenshot. I've kept it as applied so you don't apply twice."
        )
        with open(receipt, "rb") as photo:
            await update.effective_chat.send_photo(photo=photo, caption=caption, parse_mode=ParseMode.HTML)
        await status.delete()

    except Exception as exc:
        logger.error("Submit failed: %s", exc, exc_info=True)
        if pending.page:
            try:
                await pending.page.close()
            except Exception:
                pass
        await mark_failed(app_id, str(exc))
        await _edit(status, f"❌ <b>Submission failed</b>\n{esc(exc)}\n\nSend the post again to retry.")


@authorized
async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _chat_id(update)
    waiting = state.clear_waiting(chat_id)
    pending = state.remove(chat_id)
    if not pending:
        await _reply(update, "Okay, cancelled." if waiting else "Nothing to cancel.")
        return
    if pending.page:
        try:
            await pending.page.close()
        except Exception:
            pass
    await mark_cancelled(pending.application_id)
    await _reply(update, "🚫 Discarded. Send the post again any time to retry.")


_COMMANDS.update({
    "start": handle_start, "upload": handle_upload, "update_github": handle_update_github,
    "github": handle_update_github, "linkedin": handle_linkedin, "template": handle_template,
    "fact": handle_fact, "add_mail_auth": handle_add_mail_auth, "status": handle_status,
    "history": handle_history, "logs": handle_logs, "approve": handle_approve,
    "cancel": handle_cancel, "edit": handle_edit,
})
