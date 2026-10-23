"""
Comprehensive unit and integration test suite for Telegram Channel Copier.
Tests link parsing, database persistence, deduplication, progress calculations, and range logic.
"""

import sys
import os
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add parent directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from helpers import (
    parse_chat_identifier,
    format_progress_bar,
    get_media_type,
    is_admin,
    add_admin_to_cache,
    remove_admin_from_cache,
    reload_admin_cache,
    clean_channel_title,
)
from database import Database
from cloner import MessageCloner, BatchCloner


# ====================================================================
# 1. LINK AND IDENTIFIER PARSING TESTS
# ====================================================================

def test_parse_private_invite_links():
    """Verify private invite links are correctly parsed."""
    kind1, val1 = parse_chat_identifier("https://t.me/+AbCdEfGhIjK_123")
    assert kind1 == "invite_link"
    assert val1 == "https://t.me/+AbCdEfGhIjK_123"

    kind2, val2 = parse_chat_identifier("https://t.me/joinchat/XyZ987-Abc")
    assert kind2 == "invite_link"
    assert val2 == "https://t.me/+XyZ987-Abc"

    kind3, val3 = parse_chat_identifier("+ShortInviteHash")
    assert kind3 == "invite_link"
    assert val3 == "https://t.me/+ShortInviteHash"


def test_parse_chat_ids():
    """Verify chat IDs with or without -100 prefix are handled."""
    kind1, val1 = parse_chat_identifier("-1001234567890")
    assert kind1 == "id"
    assert val1 == -1001234567890

    # Auto-prefix for 10+ digit positive IDs
    kind2, val2 = parse_chat_identifier("1234567890")
    assert kind2 == "id"
    assert val2 == -1001234567890


def test_parse_usernames_and_public_links():
    """Verify usernames and public links are parsed."""
    kind1, val1 = parse_chat_identifier("@my_telegram_channel")
    assert kind1 == "username"
    assert val1 == "my_telegram_channel"

    kind2, val2 = parse_chat_identifier("https://t.me/my_test_channel")
    assert kind2 == "username"
    assert val2 == "my_test_channel"

    kind3, val3 = parse_chat_identifier("simple_channel_name")
    assert kind3 == "username"
    assert val3 == "simple_channel_name"


# ====================================================================
# 2. HELPER FUNCTIONS TESTS
# ====================================================================

def test_format_progress_bar():
    """Verify visual progress bar rendering."""
    assert "0%" in format_progress_bar(0, 100)
    assert "50%" in format_progress_bar(50, 100)
    assert "100%" in format_progress_bar(100, 100)
    assert "█" in format_progress_bar(50, 100)
    assert "░" in format_progress_bar(50, 100)


def test_is_admin():
    """Verify admin check whitelisting and dynamic cache updates."""
    Config.ADMIN_USER_IDS = [111, 222, 333]
    reload_admin_cache([])
    assert is_admin(111) is True
    assert is_admin(222) is True
    assert is_admin(999) is False

    # Add dynamic admin
    add_admin_to_cache(999)
    assert is_admin(999) is True

    # Remove dynamic admin
    remove_admin_from_cache(999)
    assert is_admin(999) is False


def test_get_media_type():
    """Verify media type categorization."""
    msg = MagicMock()
    msg.media_group_id = None
    msg.photo = None
    msg.video = None
    msg.document = None
    msg.audio = None
    msg.voice = None
    msg.video_note = None
    msg.animation = None
    msg.sticker = None
    msg.poll = None
    msg.location = None
    msg.contact = None
    msg.text = "Hello world"

    assert get_media_type(msg) == "text"

    msg.text = None
    msg.photo = MagicMock()
    assert get_media_type(msg) == "photo"

    msg.media_group_id = "album_123"
    assert get_media_type(msg) == "album"


# ====================================================================
# 3. DATABASE ASYNC TESTS
# ====================================================================

@pytest.mark.asyncio
async def test_database_crud():
    """Verify SQLite database CRUD operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db_path = Path(tmpdir) / "test_bot.db"
        test_db = Database(test_db_path)
        await test_db.init()

        # Add Source
        await test_db.add_source(
            chat_id=-1001111111111,
            title="Source Channel 1",
            username="src1",
            invite_link="https://t.me/+test1",
            auto_forward=True
        )

        sources = await test_db.get_all_sources()
        assert len(sources) == 1
        assert sources[0]["chat_id"] == -1001111111111
        assert sources[0]["title"] == "Source Channel 1"
        assert sources[0]["auto_forward"] == 1

        # Set & Get Target
        await test_db.set_target_channel(-1002222222222, "Destination Channel")
        target = await test_db.get_target_channel()
        assert target is not None
        assert target["chat_id"] == -1002222222222
        assert target["title"] == "Destination Channel"

        # Global Auto-forward toggle
        assert await test_db.is_global_auto_forward_enabled() is True
        await test_db.set_global_auto_forward(False)
        assert await test_db.is_global_auto_forward_enabled() is False

        # Message Deduplication History
        is_copied = await test_db.is_message_copied(-1001111111111, 42, -1002222222222)
        assert is_copied is False

        await test_db.record_copied_message(
            source_chat_id=-1001111111111,
            source_msg_id=42,
            target_chat_id=-1002222222222,
            target_msg_id=999,
            media_type="photo"
        )

        is_copied_after = await test_db.is_message_copied(-1001111111111, 42, -1002222222222)
        assert is_copied_after is True

        # Stats
        stats = await test_db.get_stats()
        assert stats["source_channels"] == 1
        assert stats["copied_messages"] == 1

        # Remove Source
        removed = await test_db.remove_source(-1001111111111)
        assert removed is True
        sources_after = await test_db.get_all_sources()
        assert len(sources_after) == 0

        # Admin CRUD
        await test_db.add_admin(user_id=777888, username="testadmin", first_name="Tester", added_by=111)
        assert await test_db.is_admin_in_db(777888) is True

        admins = await test_db.get_all_admins()
        admin_ids = [a["user_id"] for a in admins]
        assert 777888 in admin_ids

        admin_removed = await test_db.remove_admin(777888)
        assert admin_removed is True
        assert await test_db.is_admin_in_db(777888) is False


# ====================================================================
# 4. BATCH CLONER RANGE LOGIC TESTS
# ====================================================================

@pytest.mark.asyncio
async def test_batch_cloner_range():
    """Verify range copying logic with mock clients."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db_path = Path(tmpdir) / "test_batch.db"
        test_db = Database(test_db_path)
        await test_db.init()

        # Mock clients
        mock_user = MagicMock()
        mock_bot = MagicMock()

        # Mock messages: msg 1 is text, msg 2 is empty/deleted, msg 3 is photo
        msg1 = MagicMock()
        msg1.id = 1
        msg1.empty = False
        msg1.text = "Message 1"
        msg1.entities = None
        msg1.web_page = None
        msg1.media_group_id = None
        msg1.photo = None
        msg1.video = None
        msg1.document = None
        msg1.audio = None
        msg1.voice = None
        msg1.video_note = None
        msg1.animation = None
        msg1.sticker = None
        msg1.poll = None
        msg1.location = None
        msg1.contact = None

        msg2 = MagicMock()
        msg2.id = 2
        msg2.empty = True

        msg3 = MagicMock()
        msg3.id = 3
        msg3.empty = False
        msg3.text = None
        msg3.photo = MagicMock()
        msg3.caption = "Photo caption"
        msg3.caption_entities = None
        msg3.media_group_id = None
        msg3.video = None
        msg3.document = None
        msg3.audio = None
        msg3.voice = None
        msg3.video_note = None
        msg3.animation = None
        msg3.sticker = None
        msg3.poll = None
        msg3.location = None
        msg3.contact = None

        msg_dict = {1: msg1, 2: msg2, 3: msg3}

        async def fake_get_messages(chat_id, message_ids):
            return msg_dict.get(message_ids)

        mock_user.get_messages = AsyncMock(side_effect=fake_get_messages)
        mock_user.send_message = AsyncMock(return_value=MagicMock(id=101))
        mock_user.copy_message = AsyncMock(return_value=MagicMock(id=103))

        cloner = MessageCloner(user_client=mock_user, bot_client=mock_bot, db_instance=test_db)
        batch = BatchCloner(cloner)

        try:
            # Run range copy 1 to 3
            Config.DELAY_BETWEEN_MESSAGES = 0.0  # Speed up tests
            stats = await batch.run_range_copy(
                source_chat_id=-1001,
                target_chat_id=-1002,
                start_msg_id=1,
                end_msg_id=3
            )

            assert stats["total"] == 3
            assert stats["processed"] == 3
            assert stats["copied"] == 2  # msg 1 and msg 3
            assert stats["skipped"] == 1  # msg 2 was empty
            assert stats["failed"] == 0

            # Verify deduplication prevents re-copying
            stats_second_run = await batch.run_range_copy(
                source_chat_id=-1001,
                target_chat_id=-1002,
                start_msg_id=1,
                end_msg_id=3
            )
            # All 3 should be skipped now
            assert stats_second_run["copied"] == 0
            assert stats_second_run["skipped"] == 3
        finally:
            pass


@pytest.mark.asyncio
async def test_batch_cloner_cancellation():
    """Verify batch copy respects user cancellation request."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Database(Path(tmpdir) / "test_cancel.db")
        await test_db.init()

        mock_user = MagicMock()
        mock_msg = MagicMock(id=1, empty=False, text="hello", entities=None, web_page=None, media_group_id=None, photo=None, video=None, document=None, audio=None, voice=None, video_note=None, animation=None, sticker=None, poll=None, location=None, contact=None)
        mock_user.get_messages = AsyncMock(return_value=mock_msg)
        mock_user.send_message = AsyncMock(return_value=MagicMock(id=101))

        cloner = MessageCloner(user_client=mock_user, db_instance=test_db)
        batch = BatchCloner(cloner)

        # Signal cancellation
        batch.cancel()
        Config.DELAY_BETWEEN_MESSAGES = 0.0

        stats = await batch.run_range_copy(-1001, -1002, 1, 10)
        assert batch.cancel_requested is True
        assert stats["copied"] == 0


@pytest.mark.asyncio
async def test_batch_cloner_albums():
    """Verify messages in the same media_group_id are cloned as a single album unit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Database(Path(tmpdir) / "test_album.db")
        await test_db.init()

        mock_user = MagicMock()

        # Messages 10 and 11 belong to the same album
        album_msg1 = MagicMock(id=10, empty=False, media_group_id="group_abc", photo=MagicMock(), caption="Album Part 1", caption_entities=None, video=None, document=None, audio=None, voice=None, video_note=None, animation=None, sticker=None, poll=None, location=None, contact=None, text=None)
        album_msg2 = MagicMock(id=11, empty=False, media_group_id="group_abc", photo=MagicMock(), caption="", caption_entities=None, video=None, document=None, audio=None, voice=None, video_note=None, animation=None, sticker=None, poll=None, location=None, contact=None, text=None)

        msg_map = {10: album_msg1, 11: album_msg2}
        mock_user.get_messages = AsyncMock(side_effect=lambda chat_id, message_ids: msg_map.get(message_ids))
        mock_user.get_media_group = AsyncMock(return_value=[album_msg1, album_msg2])
        mock_user.copy_media_group = AsyncMock(return_value=[MagicMock(id=201), MagicMock(id=202)])

        cloner = MessageCloner(user_client=mock_user, db_instance=test_db)
        batch = BatchCloner(cloner)

        Config.DELAY_BETWEEN_MESSAGES = 0.0
        stats = await batch.run_range_copy(-1001, -1002, 10, 11)

        # copy_media_group should be called exactly once
        mock_user.copy_media_group.assert_called_once_with(
            chat_id=-1002,
            from_chat_id=-1001,
            message_id=10
        )
        assert stats["copied"] == 2
        # Both messages 10 and 11 should be recorded in DB
        assert await test_db.is_message_copied(-1001, 10, -1002) is True
        assert await test_db.is_message_copied(-1001, 11, -1002) is True


def test_clean_channel_title():
    """Verify title truncation helper."""
    assert clean_channel_title("Short Title") == "Short Title"
    long_title = "A" * 50
    truncated = clean_channel_title(long_title, max_len=20)
    assert len(truncated) == 20
    assert truncated.endswith("...")

