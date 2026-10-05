"""
Bot entry point.

Startup order:
  1. Load secrets/.env
  2. Telemetry + file logs
  3. DB tables, then cancel drafts left over from a previous run
  4. Start browsers, email service, answer generator
  5. Register handlers (commands, text, photos, documents) and an error handler
  6. Poll Telegram
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from bot import handlers, state
from custom_page.executor import CustomPageExecutor
from db.session import create_tables, init_db
from email_service.router import EmailApplicationService
from executor.form_executor import FormExecutor
from generator.answer_generator import AnswerGenerator
from idempotency import cancel_stale_applications
from telemetry.logger import setup_telemetry

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_ENV_PATH = _PROJECT_ROOT / "secrets" / ".env"

_MENU = [
    ("start", "How to use flam"),
    ("upload", "Upload your resume (PDF or text)"),
    ("update_github", "Sync your GitHub projects"),
    ("linkedin", "Add LinkedIn text"),
    ("fact", "Set facts like notice period or CTC"),
    ("template", "Set how your answers sound"),
    ("edit", "Change an answer in the preview"),
    ("approve", "Submit the pending application"),
    ("cancel", "Discard the pending application"),
    ("status", "What I know about you"),
    ("history", "Recent applications"),
]


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Something went wrong on my side. Please try again. If it keeps happening, check /logs."
            )
        except Exception:
            pass


async def main() -> None:
    load_dotenv(dotenv_path=_ENV_PATH)
    setup_telemetry()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Check secrets/.env.")
        sys.exit(1)
    if not os.getenv("GROQ_API_KEY"):
        logger.warning("GROQ_API_KEY is not set: answers, email drafts and screenshot reading will not work.")
    if not os.getenv("ALLOWED_TELEGRAM_IDS", "").strip():
        logger.warning("ALLOWED_TELEGRAM_IDS is not set: ANYONE who finds this bot can use it. "
                       "Add your Telegram user id to secrets/.env.")

    init_db(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./flam.db"))
    await create_tables()
    stale = await cancel_stale_applications()
    if stale:
        logger.info("Cancelled %d draft(s) left over from the last run", stale)

    form_executor = FormExecutor()
    await form_executor.start()
    handlers.set_executor(form_executor)

    custom_page_executor = CustomPageExecutor()
    await custom_page_executor.start()
    handlers.set_custom_page_executor(custom_page_executor)

    handlers.set_email_service(EmailApplicationService())
    handlers.set_generator(AnswerGenerator())

    app = ApplicationBuilder().token(token).build()
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in _MENU])

    for name, fn in handlers._COMMANDS.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CommandHandler("add-mail-auth", handlers.handle_add_mail_auth))
    app.add_handler(MessageHandler(filters.PHOTO, handlers.handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handlers.handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))
    app.add_error_handler(_on_error)

    logger.info("Bot starting - polling for updates")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await stop_event.wait()
    finally:
        logger.info("Shutting down")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        for pending in state.all_pending():  # close live pages left open
            if pending.page:
                try:
                    await pending.page.close()
                except Exception:
                    pass
        await form_executor.stop()
        await custom_page_executor.stop()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
