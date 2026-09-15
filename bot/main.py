"""
Bot entry point — Phase 3 Multi-Platform & Channel Architecture.

Startup order:
  1. Load .env from secrets/.env
  2. Setup rotating file logs & telemetry
  3. init_db() + create_tables() — schema & auto-migrations verified
  4. Start FormExecutor and CustomPageExecutor
  5. Init AnswerGenerator with Groq key and fallback profile
  6. Init EmailApplicationService
  7. Register command, document, and text handlers
  8. Start polling Telegram
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Ensure project root is in sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from bot import handlers
from custom_page.executor import CustomPageExecutor
from db.session import create_tables, init_db
from email_service.router import EmailApplicationService
from executor.form_executor import FormExecutor
from generator.answer_generator import AnswerGenerator
from profiles.static_test import STATIC_PROFILE
from telemetry.logger import setup_telemetry

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_ENV_PATH = _PROJECT_ROOT / "secrets" / ".env"


async def main() -> None:
    load_dotenv(dotenv_path=_ENV_PATH)

    # --- 1. Setup Telemetry & File Logs ---
    setup_telemetry()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Check secrets/.env.")
        sys.exit(1)

    database_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./flam.db")

    # --- 2. DB setup & auto-migration ---
    logger.info("Initialising database at %s", database_url)
    init_db(database_url)
    await create_tables()
    logger.info("Database ready (all tables + idempotency constraint + schema synced)")

    # --- 3. Playwright Executors ---
    form_executor = FormExecutor()
    await form_executor.start()
    handlers.set_executor(form_executor)

    custom_page_executor = CustomPageExecutor()
    await custom_page_executor.start()
    handlers.set_custom_page_executor(custom_page_executor)

    # --- 4. Email Service ---
    email_service = EmailApplicationService()
    handlers.set_email_service(email_service)

    # --- 5. Answer Generator ---
    groq_api_key = os.getenv("GROQ_API_KEY")
    generator = AnswerGenerator(
        groq_api_key=groq_api_key,
        fallback_profile=STATIC_PROFILE,
    )
    handlers.set_generator(generator)

    # --- 6. Build Telegram application ---
    app = ApplicationBuilder().token(token).build()

    # Set bot commands in Telegram menu
    await app.bot.set_my_commands([
        BotCommand("start", "Show welcome & instructions"),
        BotCommand("upload", "Upload resume (PDF or text)"),
        BotCommand("update_github", "Sync GitHub repos & summaries"),
        BotCommand("linkedin", "Add LinkedIn profile details"),
        BotCommand("template", "Customize tone & style instructions"),
        BotCommand("add_mail_auth", "Connect Gmail/Outlook OAuth for email applications"),
        BotCommand("status", "Check candidate context status"),
        BotCommand("logs", "View recent debug logs and metrics"),
        BotCommand("approve", "Submit the pending application"),
        BotCommand("cancel", "Abort the pending application"),
    ])

    # Register handlers
    app.add_handler(CommandHandler("start", handlers.handle_start))
    app.add_handler(CommandHandler("upload", handlers.handle_upload))
    app.add_handler(CommandHandler("update_github", handlers.handle_update_github))
    app.add_handler(CommandHandler("github", handlers.handle_update_github))
    app.add_handler(CommandHandler("linkedin", handlers.handle_linkedin))
    app.add_handler(CommandHandler("template", handlers.handle_template))
    app.add_handler(CommandHandler("add_mail_auth", handlers.handle_add_mail_auth))
    app.add_handler(CommandHandler("add-mail-auth", handlers.handle_add_mail_auth))
    app.add_handler(CommandHandler("status", handlers.handle_status))
    app.add_handler(CommandHandler("logs", handlers.handle_logs))
    app.add_handler(CommandHandler("approve", handlers.handle_approve))
    app.add_handler(CommandHandler("cancel", handlers.handle_cancel))

    # Document (PDF) handler
    app.add_handler(MessageHandler(filters.Document.ALL, handlers.handle_document))

    # Text message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))

    logger.info("Bot starting — polling for updates…")

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _request_shutdown() -> None:
        logger.info("Shutdown signal received.")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            pass

    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await stop_event.wait()
    finally:
        logger.info("Shutting down…")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await form_executor.stop()
        await custom_page_executor.stop()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
