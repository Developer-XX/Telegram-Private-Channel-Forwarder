"""
Database module for storing channels, configuration, and copied message history.
Uses aiosqlite for asynchronous, non-blocking SQLite operations.
"""

import aiosqlite
from typing import List, Optional, Dict, Any
from pathlib import Path
from config import Config


class Database:
    def __init__(self, db_path: Path = Config.DB_PATH):
        self.db_path = db_path

    async def init(self) -> None:
        """Create tables and indexes if they don't already exist."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Sources table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sources (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    username TEXT,
                    invite_link TEXT,
                    auto_forward INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Settings key-value table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            # Copied message history table to prevent duplicates
            await db.execute("""
                CREATE TABLE IF NOT EXISTS copied_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id INTEGER NOT NULL,
                    source_msg_id INTEGER NOT NULL,
                    target_chat_id INTEGER NOT NULL,
                    target_msg_id INTEGER,
                    media_type TEXT,
                    copied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_chat_id, source_msg_id, target_chat_id)
                )
            """)

            # Indexes for high performance queries
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_copied_lookup 
                ON copied_history(source_chat_id, source_msg_id)
            """)

            await db.commit()

    # --- Source Channel Management ---

    async def add_source(
        self,
        chat_id: int,
        title: str,
        username: Optional[str] = None,
        invite_link: Optional[str] = None,
        auto_forward: bool = True
    ) -> None:
        """Add or update a source channel."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO sources (chat_id, title, username, invite_link, auto_forward)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    invite_link = COALESCE(excluded.invite_link, sources.invite_link),
                    auto_forward = excluded.auto_forward
            """, (chat_id, title, username, invite_link, 1 if auto_forward else 0))
            await db.commit()

    async def remove_source(self, chat_id: int) -> bool:
        """Remove a source channel. Returns True if removed, False if not found."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM sources WHERE chat_id = ?", (chat_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def get_all_sources(self) -> List[Dict[str, Any]]:
        """Retrieve all registered source channels."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM sources ORDER BY created_at DESC") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_source(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Retrieve a specific source channel by ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM sources WHERE chat_id = ?", (chat_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def set_source_auto_forward(self, chat_id: int, enabled: bool) -> bool:
        """Toggle auto-forward for a specific source channel."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE sources SET auto_forward = ? WHERE chat_id = ?",
                (1 if enabled else 0, chat_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    # --- Target Channel Management & Settings ---

    async def set_setting(self, key: str, value: str) -> None:
        """Set a configuration value."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, str(value)))
            await db.commit()

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a configuration value."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else default

    async def set_target_channel(self, chat_id: int, title: str) -> None:
        """Set the active target channel."""
        await self.set_setting("target_chat_id", str(chat_id))
        await self.set_setting("target_chat_title", title)

    async def get_target_channel(self) -> Optional[Dict[str, Any]]:
        """Get the active target channel information."""
        chat_id_str = await self.get_setting("target_chat_id")
        if not chat_id_str:
            return None
        title = await self.get_setting("target_chat_title", "Unknown Target")
        return {"chat_id": int(chat_id_str), "title": title}

    async def is_global_auto_forward_enabled(self) -> bool:
        """Check if global live auto-forwarding is enabled."""
        val = await self.get_setting("global_auto_forward", "1")
        return val in ("1", "true", "True")

    async def set_global_auto_forward(self, enabled: bool) -> None:
        """Enable or disable global live auto-forwarding."""
        await self.set_setting("global_auto_forward", "1" if enabled else "0")

    # --- User Session Management ---

    async def save_user_session(
        self,
        session_string: str,
        user_id: int,
        first_name: str,
        username: Optional[str] = None,
        phone: Optional[str] = None
    ) -> None:
        """Persist user account session string and profile data."""
        await self.set_setting("user_session_string", session_string)
        await self.set_setting("user_id", str(user_id))
        await self.set_setting("user_first_name", first_name)
        await self.set_setting("user_username", username or "")
        await self.set_setting("user_phone", phone or "")

    async def get_user_session(self) -> Optional[Dict[str, Any]]:
        """Retrieve stored user account session string and profile data."""
        session_str = await self.get_setting("user_session_string")
        if not session_str:
            return None
        return {
            "session_string": session_str,
            "user_id": int(await self.get_setting("user_id", "0")),
            "first_name": await self.get_setting("user_first_name", "Telegram User"),
            "username": await self.get_setting("user_username", ""),
            "phone": await self.get_setting("user_phone", ""),
        }

    async def delete_user_session(self) -> None:
        """Remove stored user account session from database."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                DELETE FROM settings WHERE key IN (
                    'user_session_string', 'user_id', 'user_first_name', 'user_username', 'user_phone'
                )
            """)
            await db.commit()

    # --- Duplicate Prevention & History ---

    async def is_message_copied(self, source_chat_id: int, source_msg_id: int, target_chat_id: int) -> bool:
        """Check if a specific message has already been copied to the target channel."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT 1 FROM copied_history 
                WHERE source_chat_id = ? AND source_msg_id = ? AND target_chat_id = ?
            """, (source_chat_id, source_msg_id, target_chat_id)) as cursor:
                return await cursor.fetchone() is not None

    async def record_copied_message(
        self,
        source_chat_id: int,
        source_msg_id: int,
        target_chat_id: int,
        target_msg_id: Optional[int] = None,
        media_type: Optional[str] = None
    ) -> None:
        """Record a successful copy in history to prevent duplicates."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR IGNORE INTO copied_history 
                (source_chat_id, source_msg_id, target_chat_id, target_msg_id, media_type)
                VALUES (?, ?, ?, ?, ?)
            """, (source_chat_id, source_msg_id, target_chat_id, target_msg_id, media_type))
            await db.commit()

    async def get_stats(self) -> Dict[str, Any]:
        """Return statistics on copied messages and channels."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM sources") as c:
                source_count = (await c.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM copied_history") as c:
                copied_count = (await c.fetchone())[0]
            return {
                "source_channels": source_count,
                "copied_messages": copied_count,
            }


# Singleton instance
db = Database()
