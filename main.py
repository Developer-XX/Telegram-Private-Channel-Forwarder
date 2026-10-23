"""
Main Application Entry Point for Telegram Channel Copier.
Starts Bot client and optional User Session client concurrently with graceful shutdown.
Supports connecting, changing, and removing user sessions dynamically via /login and /remove_session.
"""

import sys
import os
import signal
import asyncio
import logging

# Ensure event loop exists on Python 3.12+ / 3.14+
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from pyrogram import Client, idle
from config import Config
from database import db
from bot import setup_handlers, register_user_channel_listener

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Config.DATA_DIR / "bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("telegram_copier.main")


async def main():
    logger.info("Initializing Telegram Channel Copier...")

    # Validate config
    errors = Config.validate()
    if errors:
        logger.error("Configuration Errors found:")
        for err in errors:
            logger.error(f"  • {err}")
        logger.error("Please configure your .env file. See .env.example for guidance.")
        sys.exit(1)

    # Initialize SQLite Database
    await db.init()
    logger.info("Database initialized successfully.")

    # Synchronize admin cache with database
    from helpers import reload_admin_cache
    db_admins = await db.get_all_admins()
    reload_admin_cache(db_admins)
    logger.info(f"Loaded {len(db_admins)} authorized administrator(s) from database.")

    # 1. Instantiate Bot Client
    bot_client = Client(
        name="bot",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        bot_token=Config.BOT_TOKEN,
        workdir=str(Config.DATA_DIR),
    )

    # 2. Check for User Session (from DB or .env)
    user_client = None
    saved_session = await db.get_user_session()
    session_string = None

    if saved_session and saved_session.get("session_string"):
        session_string = saved_session["session_string"]
        logger.info(f"Loaded existing user session from database for {saved_session['first_name']}.")
    elif Config.USER_SESSION_STRING:
        session_string = Config.USER_SESSION_STRING
        logger.info("Loaded user session string from environment configuration (.env).")

    if session_string:
        try:
            user_client = Client(
                name="user",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                session_string=session_string,
                workdir=str(Config.DATA_DIR),
            )
            await user_client.start()
            user_me = await user_client.get_me()
            logger.info(f"👤 User Session Connected: {user_me.first_name} (@{user_me.username or 'No Username'}, ID: {user_me.id})")
        except Exception as e:
            logger.error(f"Failed to start stored user session: {e}. You can connect again using /login.")
            user_client = None
    else:
        logger.info("ℹ️ No User Session configured yet. Bot will start and you can connect using /login in chat.")

    # Attach all bot message, callback, and command handlers
    setup_handlers(bot=bot_client, user=user_client)

    # Start Bot client
    logger.info("Starting Telegram Bot client...")
    await bot_client.start()
    bot_me = await bot_client.get_me()

    logger.info("=" * 60)
    logger.info(f"🤖 Bot Client Connected: @{bot_me.username} (ID: {bot_me.id})")
    if user_client and user_client.is_connected:
        user_me = await user_client.get_me()
        logger.info(f"👤 User Session: Connected as {user_me.first_name} (ID: {user_me.id})")
    else:
        logger.info("👤 User Session: Disconnected (Use /login in Telegram DM to connect)")
    logger.info(f"👑 Authorized Admins: {Config.ADMIN_USER_IDS}")
    logger.info("=" * 60)
    logger.info("Telegram Channel Copier is running and ready.")

    # Graceful shutdown handling
    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received. Stopping services...")
        stop_event.set()

    # Register OS signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    try:
        await idle()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Stopping Pyrogram clients...")
        try:
            await bot_client.stop()
        except Exception:
            pass
        if user_client and user_client.is_connected:
            try:
                await user_client.stop()
            except Exception:
                pass
        logger.info("Telegram Channel Copier shut down gracefully.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process terminated by user.")
