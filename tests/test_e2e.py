"""
Comprehensive End-to-End (E2E) Test Suite for Telegram Channel Copier.
Simulates complete real-world workflows:
1. Joining private channels from invite links.
2. Managing target & source channels via DB and commands.
3. Historical batch range cloning (/forward) with text, photos, albums, and restricted content fallback.
4. Real-time live auto-forwarding event handling.
5. Deduplication, anti-flood simulation, and authorization controls.
"""

import sys
import os
import asyncio
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyrogram.errors import ChatForwardsRestricted, FloodWait

# Add project root to sys.path
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
from helpers import parse_chat_identifier, is_admin, get_media_type
from cloner import MessageCloner, BatchCloner


@asynccontextmanager
async def create_e2e_env():
    """Setup an isolated temporary database, temp directories, and mock clients."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "e2e_test.db"
        test_db = Database(db_path)
        await test_db.init()

        temp_media_dir = tmp_path / "temp_media"
        temp_media_dir.mkdir(parents=True, exist_ok=True)
        Config.TEMP_DIR = temp_media_dir
        Config.DELAY_BETWEEN_MESSAGES = 0.0  # Fast execution in tests
        Config.ADMIN_USER_IDS = [123456]

        # Mock Pyrogram User and Bot Clients
        mock_user = MagicMock()
        mock_bot = MagicMock()

        cloner = MessageCloner(user_client=mock_user, bot_client=mock_bot, db_instance=test_db)
        batch = BatchCloner(cloner)

        yield {
            "db": test_db,
            "tmpdir": tmp_path,
            "user": mock_user,
            "bot": mock_bot,
            "cloner": cloner,
            "batch": batch,
        }


# ====================================================================
# WORKFLOW 1: Channel Joining & Management End-to-End
# ====================================================================

@pytest.mark.asyncio
async def test_e2e_channel_join_and_management():
    """
    Simulate user sending private channel invite links, joining them,
    and configuring them as source and target channels.
    """
    async with create_e2e_env() as e2e_env:
        db = e2e_env["db"]
        mock_user = e2e_env["user"]

        # 1. Parse Private Invite Link
        raw_invite = "https://t.me/+SecretInviteHash123"
        kind, invite_url = parse_chat_identifier(raw_invite)
        assert kind == "invite_link"
        assert invite_url == "https://t.me/+SecretInviteHash123"

        # 2. Simulate User Session Joining Channel
        joined_chat_target = MagicMock()
        joined_chat_target.id = -10099998888
        joined_chat_target.title = "Target VIP Channel"
        joined_chat_target.username = None
        joined_chat_target.members_count = 1500
        mock_user.join_chat = AsyncMock(return_value=joined_chat_target)

        chat = await mock_user.join_chat(invite_url)
        assert chat.id == -10099998888

        # 3. Save as Target Channel in Database
        await db.set_target_channel(chat.id, chat.title)
        active_target = await db.get_target_channel()
        assert active_target is not None
        assert active_target["chat_id"] == -10099998888
        assert active_target["title"] == "Target VIP Channel"

        # 4. Join and Add a Source Channel
        source_chat = MagicMock()
        source_chat.id = -10011112222
        source_chat.title = "Source Private Channel"
        source_chat.username = "private_source"
        mock_user.join_chat = AsyncMock(return_value=source_chat)

        s_chat = await mock_user.join_chat("https://t.me/+SourceHash456")
        await db.add_source(
            chat_id=s_chat.id,
            title=s_chat.title,
            username=s_chat.username,
            invite_link="https://t.me/+SourceHash456",
            auto_forward=True
        )

        sources = await db.get_all_sources()
        assert len(sources) == 1
        assert sources[0]["chat_id"] == -10011112222
        assert sources[0]["title"] == "Source Private Channel"
        assert sources[0]["auto_forward"] == 1


# ====================================================================
# WORKFLOW 2: End-to-End Batch Cloning (/forward)
# ====================================================================

@pytest.mark.asyncio
async def test_e2e_batch_cloning_pipeline():
    """
    Simulate running /forward 1 5:
    - Msg 1: Formatted Text (bold, links) -> copied without forward tag
    - Msg 2: Photo -> copied without forward tag
    - Msg 3: Multi-item Album (part 1 of media group) -> copies group as album
    - Msg 4: Empty / deleted message -> gracefully skipped
    - Msg 5: Restricted Photo (ChatForwardsRestricted) -> triggers download and re-upload fallback
    """
    async with create_e2e_env() as e2e_env:
        db = e2e_env["db"]
        mock_user = e2e_env["user"]
        batch = e2e_env["batch"]

        source_id = -10011112222
        target_id = -10099998888

        # Msg 1: Text message
        msg1 = MagicMock(
            id=1, empty=False, media_group_id=None, text="<b>Important Announcement</b>",
            entities=MagicMock(), web_page=None, photo=None, video=None, document=None,
            audio=None, voice=None, video_note=None, animation=None, sticker=None,
            poll=None, location=None, contact=None
        )

        # Msg 2: Single Photo
        msg2 = MagicMock(
            id=2, empty=False, media_group_id=None, text=None, photo=MagicMock(),
            caption="A nice photo", caption_entities=None, has_media_spoiler=False,
            video=None, document=None, audio=None, voice=None, video_note=None,
            animation=None, sticker=None, poll=None, location=None, contact=None
        )

        # Msg 3: Album (part of 2-photo media group)
        album_part1 = MagicMock(
            id=3, empty=False, media_group_id="group_xyz", text=None, photo=MagicMock(),
            caption="Album cover", caption_entities=None, has_media_spoiler=False,
            video=None, document=None, audio=None, voice=None, video_note=None,
            animation=None, sticker=None, poll=None, location=None, contact=None
        )
        album_part2 = MagicMock(
            id=4, empty=False, media_group_id="group_xyz", text=None, photo=MagicMock(),
            caption="", caption_entities=None, has_media_spoiler=False,
            video=None, document=None, audio=None, voice=None, video_note=None,
            animation=None, sticker=None, poll=None, location=None, contact=None
        )

        # Msg 5: Restricted Photo
        msg5 = MagicMock(
            id=5, empty=False, media_group_id=None, text=None, photo=MagicMock(),
            caption="Secret protected photo", caption_entities=None, has_media_spoiler=False,
            video=None, document=None, audio=None, voice=None, video_note=None,
            animation=None, sticker=None, poll=None, location=None, contact=None
        )

        messages_store = {
            1: msg1,
            2: msg2,
            3: album_part1,
            4: album_part2,
            5: msg5,
        }

        mock_user.get_messages = AsyncMock(side_effect=lambda chat_id, message_ids: messages_store.get(message_ids))
        mock_user.send_message = AsyncMock(return_value=MagicMock(id=101))

        # copy_message behavior: succeeds for msg 2, raises ChatForwardsRestricted for msg 5
        def fake_copy_message(chat_id, from_chat_id, message_id, **kwargs):
            if message_id == 5:
                raise ChatForwardsRestricted()
            return MagicMock(id=100 + message_id)

        mock_user.copy_message = AsyncMock(side_effect=fake_copy_message)

        # Album handling
        mock_user.get_media_group = AsyncMock(return_value=[album_part1, album_part2])
        mock_user.copy_media_group = AsyncMock(return_value=[MagicMock(id=201), MagicMock(id=202)])

        # Download & re-upload fallback for msg 5
        temp_file = Config.TEMP_DIR / "temp_photo_5.jpg"
        temp_file.write_bytes(b"dummy image data")

        mock_user.download_media = AsyncMock(return_value=str(temp_file))
        mock_user.send_photo = AsyncMock(return_value=MagicMock(id=105))

        # Run the batch range copy
        progress_records = []
        async def on_progress(stats):
            progress_records.append(dict(stats))

        final_stats = await batch.run_range_copy(
            source_chat_id=source_id,
            target_chat_id=target_id,
            start_msg_id=1,
            end_msg_id=5,
            progress_callback=on_progress
        )

        # Verifications
        assert final_stats["total"] == 5
        # Total copied: msg 1 (text), msg 2 (photo), msg 3+4 (album 2 items), msg 5 (restricted fallback)
        assert final_stats["copied"] == 5
        assert final_stats["failed"] == 0

        # Verify native text message sent
        mock_user.send_message.assert_called_once()

        # Verify native copy_message was called for msg 2 and msg 5
        assert mock_user.copy_message.call_count >= 2

        # Verify copy_media_group was called once for the album
        mock_user.copy_media_group.assert_called_once_with(
            chat_id=target_id,
            from_chat_id=source_id,
            message_id=3
        )

        # Verify fallback download and send_photo was called for restricted msg 5
        mock_user.download_media.assert_called_once()
        mock_user.send_photo.assert_called_once()

        # Verify temp file was cleaned up by cloner
        assert not temp_file.exists()

        # Verify all messages were recorded in the database
        for m_id in [1, 2, 3, 4, 5]:
            assert await db.is_message_copied(source_id, m_id, target_id) is True

        # Run again to verify duplicate prevention
        second_run_stats = await batch.run_range_copy(
            source_chat_id=source_id,
            target_chat_id=target_id,
            start_msg_id=1,
            end_msg_id=5
        )
        # All messages already in DB -> all skipped!
        assert second_run_stats["copied"] == 0
        assert second_run_stats["skipped"] == 5


# ====================================================================
# WORKFLOW 3: End-to-End Real-Time Live Sync Simulation
# ====================================================================

@pytest.mark.asyncio
async def test_e2e_live_sync_simulation():
    """
    Simulate real-time post arrival in monitored vs unmonitored channels.
    """
    async with create_e2e_env() as e2e_env:
        db = e2e_env["db"]
        cloner = e2e_env["cloner"]
        mock_user = e2e_env["user"]

        source_id = -10011112222
        target_id = -10099998888
        other_channel_id = -10055556666

        await db.set_target_channel(target_id, "Target Channel")
        await db.add_source(source_id, "Monitored Channel", auto_forward=True)

        # Incoming live message in monitored channel
        live_msg = MagicMock(
            id=77, empty=False, media_group_id=None, text="Live breaking update!",
            entities=None, web_page=None, photo=None, video=None, document=None,
            audio=None, voice=None, video_note=None, animation=None, sticker=None,
            poll=None, location=None, contact=None
        )
        live_msg.chat = MagicMock(id=source_id)

        mock_user.send_message = AsyncMock(return_value=MagicMock(id=301))

        # Helper simulating on_new_channel_post logic
        async def simulate_incoming_channel_post(msg):
            if not await db.is_global_auto_forward_enabled():
                return False
            src_entry = await db.get_source(msg.chat.id)
            if not src_entry or not src_entry.get("auto_forward"):
                return False
            target = await db.get_target_channel()
            if not target or msg.chat.id == target["chat_id"]:
                return False
            if await db.is_message_copied(msg.chat.id, msg.id, target["chat_id"]):
                return False

            copied = await cloner.clone_single_message(msg.chat.id, target["chat_id"], msg)
            if copied:
                await db.record_copied_message(
                    source_chat_id=msg.chat.id,
                    source_msg_id=msg.id,
                    target_chat_id=target["chat_id"],
                    target_msg_id=copied.id,
                    media_type="text"
                )
                return True
            return False

        # 1. Monitored post should be copied
        result1 = await simulate_incoming_channel_post(live_msg)
        assert result1 is True
        assert await db.is_message_copied(source_id, 77, target_id) is True

        # 2. Duplicate post should NOT be copied
        result2 = await simulate_incoming_channel_post(live_msg)
        assert result2 is False

        # 3. Post from unmonitored channel should NOT be copied
        unmonitored_msg = MagicMock(id=88, empty=False, media_group_id=None, text="Random channel post")
        unmonitored_msg.chat = MagicMock(id=other_channel_id)
        result3 = await simulate_incoming_channel_post(unmonitored_msg)
        assert result3 is False

        # 4. When live auto-forward is globally disabled, posts should NOT be copied
        await db.set_global_auto_forward(False)
        live_msg_2 = MagicMock(
            id=78, empty=False, media_group_id=None, text="Post while live sync OFF",
            entities=None, web_page=None, photo=None, video=None, document=None,
            audio=None, voice=None, video_note=None, animation=None, sticker=None,
            poll=None, location=None, contact=None
        )
        live_msg_2.chat = MagicMock(id=source_id)
        result4 = await simulate_incoming_channel_post(live_msg_2)
        assert result4 is False


# ====================================================================
# WORKFLOW 4: End-to-End FloodWait Resilience Simulation
# ====================================================================

@pytest.mark.asyncio
async def test_e2e_floodwait_resilience():
    """
    Verify cloner catches FloodWait, sleeps the requested duration, and automatically retries.
    """
    from cloner import with_flood_retry

    call_count = 0

    async def api_call_with_flood():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate 1-second FloodWait on first call
            raise FloodWait(1)
        return "success_after_flood"

    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await with_flood_retry(api_call_with_flood)
        assert res == "success_after_flood"
        assert call_count == 2
        mock_sleep.assert_called_once_with(2)  # value (1) + 1 second buffer
