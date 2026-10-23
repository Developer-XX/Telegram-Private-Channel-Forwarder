"""
Configuration module for the Telegram Channel Copier Bot.
Loads settings from environment variables and provides sensible defaults.
"""

import os
from pathlib import Path
from typing import List
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load .env file
load_dotenv(BASE_DIR / ".env")


def get_admin_ids() -> List[int]:
    """Parse comma-separated admin user IDs from environment."""
    raw = os.getenv("ADMIN_USER_IDS", "")
    ids = []
    for item in raw.split(","):
        item = item.strip()
        if item.isdigit() or (item.startswith("-") and item[1:].isdigit()):
            ids.append(int(item))
    return ids


class Config:
    # Telegram API Credentials (from https://my.telegram.org)
    API_ID: int = int(os.getenv("API_ID", "0"))
    API_HASH: str = os.getenv("API_HASH", "")

    # Bot Token (from https://t.me/BotFather)
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

    # User Account Session String (generated via generate_session.py)
    # If empty, Pyrogram will look for user.session file in data directory
    USER_SESSION_STRING: str = os.getenv("USER_SESSION_STRING", "")

    # Whitelist of Telegram User IDs allowed to control the bot
    ADMIN_USER_IDS: List[int] = get_admin_ids()

    # Default Target Channel ID or Username (optional initial setting)
    DEFAULT_TARGET_CHANNEL: str = os.getenv("DEFAULT_TARGET_CHANNEL", "")

    # Rate limiting: delay in seconds between copying messages (avoids FloodWait)
    DELAY_BETWEEN_MESSAGES: float = float(os.getenv("DELAY_BETWEEN_MESSAGES", "1.5"))

    # Whether the bot account or user account should post to target channel
    # False: User account posts to target (recommended for private channels where bot is not admin)
    # True: Bot account posts to target (bot must be admin in target channel)
    COPY_AS_BOT: bool = os.getenv("COPY_AS_BOT", "false").lower() in ("true", "1", "yes")

    # Storage paths
    DATA_DIR: Path = BASE_DIR / "data"
    TEMP_DIR: Path = BASE_DIR / "data" / "temp"
    DB_PATH: Path = BASE_DIR / "data" / "bot.db"

    @classmethod
    def validate(cls) -> List[str]:
        """Validate required configuration settings."""
        errors = []
        if not cls.API_ID:
            errors.append("API_ID is required (get it from https://my.telegram.org)")
        if not cls.API_HASH:
            errors.append("API_HASH is required (get it from https://my.telegram.org)")
        if not cls.BOT_TOKEN:
            errors.append("BOT_TOKEN is required (get it from https://t.me/BotFather)")
        if not cls.ADMIN_USER_IDS:
            errors.append("ADMIN_USER_IDS is required (comma-separated Telegram IDs of admins)")
        return errors

    @classmethod
    def setup_directories(cls) -> None:
        """Ensure necessary directories exist."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.TEMP_DIR.mkdir(parents=True, exist_ok=True)


# Ensure directories are initialized
Config.setup_directories()
