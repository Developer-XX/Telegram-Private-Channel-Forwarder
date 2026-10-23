"""
Tests for dynamic Administrator Management and FloodWait resilience.
"""

import sys
import os
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyrogram.errors import FloodWait

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Python 3.12+ loop compatibility
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from config import Config
from database import Database
from helpers import is_admin, add_admin_to_cache, remove_admin_from_cache, reload_admin_cache
from cloner import with_flood_retry


@pytest.mark.asyncio
async def test_dynamic_admin_database_lifecycle():
    """Verify admins table operations and in-memory cache sync."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Database(Path(tmpdir) / "test_admin.db")
        Config.ADMIN_USER_IDS = [12345]
        await test_db.init()

        # Initial admin should be seeded
        admins = await test_db.get_all_admins()
        assert len(admins) == 1
        assert admins[0]["user_id"] == 12345

        # Sync cache
        reload_admin_cache(admins)
        assert is_admin(12345) is True
        assert is_admin(99999) is False

        # Add new admin
        await test_db.add_admin(
            user_id=99999,
            username="new_admin",
            first_name="NewAdmin",
            added_by=12345
        )
        add_admin_to_cache(99999)

        assert is_admin(99999) is True
        assert await test_db.is_admin_in_db(99999) is True

        # Remove admin
        removed = await test_db.remove_admin(99999)
        remove_admin_from_cache(99999)
        assert removed is True
        assert is_admin(99999) is False
        assert await test_db.is_admin_in_db(99999) is False


@pytest.mark.asyncio
async def test_floodwait_retry_with_callback():
    """Verify with_flood_retry catches FloodWait and invokes on_flood_wait callback."""
    call_count = 0
    callback_waits = []

    async def fake_api_call():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise FloodWait(5)
        return "finished"

    async def on_flood_cb(wait_time):
        callback_waits.append(wait_time)

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await with_flood_retry(
            fake_api_call,
            max_retries=3,
            on_flood_wait=on_flood_cb
        )
        assert res == "finished"
        assert call_count == 2
        assert callback_waits == [6]  # 5 + 1 second buffer
        mock_sleep.assert_called_once_with(6)
