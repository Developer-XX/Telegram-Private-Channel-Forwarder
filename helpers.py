"""
Helper utilities for parsing Telegram links, identifiers, formatting, and permissions.
"""

import asyncio
# Python 3.12+ event loop compatibility patch for Pyrogram
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import re
from typing import Tuple, Optional, Any
from pyrogram.types import Message
from config import Config


def is_admin(user_id: int) -> bool:
    """Check if the given Telegram user ID is an authorized admin."""
    return user_id in Config.ADMIN_USER_IDS


def parse_chat_identifier(raw: str) -> Tuple[str, Any]:
    """
    Parse an input string (channel link, invite hash, username, or ID).
    Returns (kind, value):
      - ('invite_link', 'https://t.me/+...')
      - ('id', -1001234567890)
      - ('username', 'my_channel')
    """
    cleaned = raw.strip()

    # Integer Chat ID (e.g. -1001234567890 or 1234567890)
    if cleaned.lstrip("-").isdigit():
        val = int(cleaned)
        # If user entered 1234567890 without -100 prefix for supergroup/channel, handle it
        if val > 0 and len(str(val)) >= 9:
            val = int(f"-100{val}")
        return "id", val

    # Telegram Private Invite Links: https://t.me/+hash or https://t.me/joinchat/hash
    invite_match = re.match(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w\-]+)$", cleaned)
    if invite_match:
        hash_code = invite_match.group(1)
        return "invite_link", f"https://t.me/+{hash_code}"

    # Standalone invite hash e.g. "+AbCdEf..."
    if cleaned.startswith("+"):
        return "invite_link", f"https://t.me/{cleaned}"

    # Public Link: https://t.me/username
    pub_match = re.match(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([a-zA-Z0-9_]{4,})$", cleaned)
    if pub_match:
        return "username", pub_match.group(1)

    # Username with @: @username
    if cleaned.startswith("@"):
        return "username", cleaned.lstrip("@")

    # Plain username string: channelname
    if re.match(r"^[a-zA-Z0-9_]{4,}$", cleaned):
        return "username", cleaned

    return "unknown", cleaned


def get_media_type(message: Message) -> str:
    """Identify the primary media type of a Pyrogram Message."""
    if message.media_group_id:
        return "album"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.document:
        return "document"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.animation:
        return "animation"
    if message.sticker:
        return "sticker"
    if message.poll:
        return "poll"
    if message.location:
        return "location"
    if message.contact:
        return "contact"
    if message.text:
        return "text"
    return "service"


def format_progress_bar(current: int, total: int, width: int = 12) -> str:
    """Generate a clean visual progress bar."""
    if total <= 0:
        return f"[{'░' * width}] 0%"
    progress = min(max(current / total, 0.0), 1.0)
    filled = int(progress * width)
    empty = width - filled
    percent = int(progress * 100)
    bar = "█" * filled + "░" * empty
    return f"[{bar}] {percent}%"


def clean_channel_title(title: str, max_len: int = 30) -> str:
    """Truncate channel title safely if too long."""
    if not title:
        return "Untitled Channel"
    if len(title) > max_len:
        return title[: max_len - 3] + "..."
    return title
