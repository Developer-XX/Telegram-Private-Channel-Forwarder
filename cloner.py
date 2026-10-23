"""
Cloning Engine for Telegram Channel Copier.
Handles copying of messages, media files, and albums from private channels without forward headers.
Provides automatic fallback for restricted channels (downloads and re-uploads) and handles FloodWait.
"""

import os
import asyncio

# Python 3.12+ event loop compatibility patch for Pyrogram
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import logging
from typing import Optional, List, Dict, Set, Callable, Any
from pathlib import Path

from pyrogram import Client
from pyrogram.types import (
    Message,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
)
from pyrogram.errors import (
    FloodWait,
    ChatForwardsRestricted,
    RPCError,
    MessageNotModified,
    MessageIdInvalid,
)

from config import Config
import database
from helpers import get_media_type, format_progress_bar

logger = logging.getLogger("telegram_copier.cloner")


async def with_flood_retry(
    func: Callable,
    *args,
    max_retries: int = 5,
    on_flood_wait: Optional[Callable[[int], Any]] = None,
    **kwargs
) -> Any:
    """
    Execute an async Telegram call, catching and sleeping through FloodWait.
    Provides automatic retry up to max_retries with detailed logging and optional user notification callback.
    """
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except FloodWait as e:
            wait_time = int(e.value) + 1
            logger.warning(f"FloodWait received: Telegram requires waiting {wait_time}s (attempt {attempt + 1}/{max_retries})")
            if on_flood_wait:
                try:
                    res = on_flood_wait(wait_time)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as cb_err:
                    logger.debug(f"on_flood_wait callback error: {cb_err}")
            await asyncio.sleep(wait_time)
        except MessageNotModified:
            # Harmless when updating status message with same content
            return None
    raise RuntimeError(f"Max FloodWait retries ({max_retries}) exceeded.")


class MessageCloner:
    def __init__(self, user_client: Client, bot_client: Optional[Client] = None, db_instance=None):
        self.user_client = user_client
        self.bot_client = bot_client
        self._db = db_instance

    @property
    def db(self):
        return self._db if self._db is not None else database.db

    def get_sender_client(self) -> Client:
        """Determine which client should send messages to the target channel."""
        if Config.COPY_AS_BOT and self.bot_client:
            return self.bot_client
        return self.user_client

    async def clone_single_message(
        self,
        source_chat_id: int,
        target_chat_id: int,
        msg: Message
    ) -> Optional[Message]:
        """
        Copy a single message without forward header.
        Tries fast copy first; falls back to download & re-upload for restricted channels.
        """
        sender = self.get_sender_client()
        media_type = get_media_type(msg)

        # 1. Skip service messages (e.g. pinned message alerts, user joined)
        if media_type == "service":
            logger.debug(f"Skipping service message {msg.id}")
            return None

        # 2. Text message
        if msg.text:
            return await with_flood_retry(
                sender.send_message,
                chat_id=target_chat_id,
                text=msg.text,
                entities=msg.entities,
                disable_web_page_preview=not bool(msg.web_page),
            )

        # 3. Poll message
        if msg.poll:
            try:
                options = [opt.text for opt in msg.poll.options]
                return await with_flood_retry(
                    sender.send_poll,
                    chat_id=target_chat_id,
                    question=msg.poll.question,
                    options=options,
                    is_anonymous=msg.poll.is_anonymous,
                    type=msg.poll.type,
                    allows_multiple_answers=msg.poll.allows_multiple_answers,
                    correct_option_id=msg.poll.correct_option_id,
                    explanation=msg.poll.explanation,
                    explanation_entities=msg.poll.explanation_entities,
                )
            except Exception as e:
                logger.warning(f"Failed to clone poll: {e}")
                return None

        # 4. Media messages: Attempt native fast copy first (no forward header)
        try:
            copied = await with_flood_retry(
                sender.copy_message,
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=msg.id,
                caption=msg.caption,
                caption_entities=msg.caption_entities,
            )
            return copied
        except ChatForwardsRestricted:
            logger.info(f"Message {msg.id} has restricted content. Downloading and re-uploading...")
            return await self._download_and_reupload(source_chat_id, target_chat_id, msg)
        except RPCError as e:
            logger.warning(f"Native copy failed for message {msg.id} ({e}). Attempting re-upload fallback...")
            return await self._download_and_reupload(source_chat_id, target_chat_id, msg)

    async def _download_and_reupload(
        self,
        source_chat_id: int,
        target_chat_id: int,
        msg: Message
    ) -> Optional[Message]:
        """Download media from restricted channel and re-upload to destination."""
        sender = self.get_sender_client()
        temp_path = None
        thumb_path = None

        try:
            # Download media using user client
            temp_path = await with_flood_retry(
                self.user_client.download_media,
                message=msg,
                file_name=str(Config.TEMP_DIR) + os.sep
            )

            if not temp_path or not os.path.exists(temp_path):
                logger.error(f"Failed to download media for message {msg.id}")
                return None

            # Photo
            if msg.photo:
                return await with_flood_retry(
                    sender.send_photo,
                    chat_id=target_chat_id,
                    photo=temp_path,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                    has_spoiler=msg.has_media_spoiler,
                )

            # Video
            elif msg.video:
                return await with_flood_retry(
                    sender.send_video,
                    chat_id=target_chat_id,
                    video=temp_path,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                    duration=msg.video.duration,
                    width=msg.video.width,
                    height=msg.video.height,
                    supports_streaming=msg.video.supports_streaming,
                    has_spoiler=msg.has_media_spoiler,
                )

            # Document / Generic File
            elif msg.document:
                return await with_flood_retry(
                    sender.send_document,
                    chat_id=target_chat_id,
                    document=temp_path,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                    file_name=msg.document.file_name,
                )

            # Audio / Music
            elif msg.audio:
                return await with_flood_retry(
                    sender.send_audio,
                    chat_id=target_chat_id,
                    audio=temp_path,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                    duration=msg.audio.duration,
                    performer=msg.audio.performer,
                    title=msg.audio.title,
                )

            # Voice Note
            elif msg.voice:
                return await with_flood_retry(
                    sender.send_voice,
                    chat_id=target_chat_id,
                    voice=temp_path,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                    duration=msg.voice.duration,
                )

            # Video Note (Round video)
            elif msg.video_note:
                return await with_flood_retry(
                    sender.send_video_note,
                    chat_id=target_chat_id,
                    video_note=temp_path,
                    duration=msg.video_note.duration,
                )

            # Animation / GIF
            elif msg.animation:
                return await with_flood_retry(
                    sender.send_animation,
                    chat_id=target_chat_id,
                    animation=temp_path,
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                    has_spoiler=msg.has_media_spoiler,
                )

            # Sticker
            elif msg.sticker:
                return await with_flood_retry(
                    sender.send_sticker,
                    chat_id=target_chat_id,
                    sticker=temp_path,
                )

            else:
                logger.warning(f"Unsupported media type for message {msg.id}")
                return None

        except Exception as e:
            logger.error(f"Error re-uploading media {msg.id}: {e}", exc_info=True)
            return None
        finally:
            # Clean up temp files
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            if thumb_path and os.path.exists(thumb_path):
                try:
                    os.remove(thumb_path)
                except Exception:
                    pass

    async def clone_media_group(
        self,
        source_chat_id: int,
        target_chat_id: int,
        messages: List[Message]
    ) -> List[Message]:
        """
        Clone an album (media group) without forward headers.
        Tries fast copy_media_group; falls back to downloading all items and send_media_group.
        """
        sender = self.get_sender_client()
        first_msg = messages[0]

        # 1. Attempt native fast copy_media_group
        try:
            copied_list = await with_flood_retry(
                sender.copy_media_group,
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=first_msg.id,
            )
            return copied_list
        except ChatForwardsRestricted:
            logger.info(f"Album group {first_msg.media_group_id} has restricted content. Downloading items...")
        except Exception as e:
            logger.warning(f"copy_media_group failed ({e}). Falling back to re-upload...")

        # 2. Fallback: Download each item and construct InputMedia list
        downloaded_paths = []
        input_media_list = []

        try:
            for item in messages:
                temp_file = await with_flood_retry(
                    self.user_client.download_media,
                    message=item,
                    file_name=str(Config.TEMP_DIR) + os.sep
                )
                if not temp_file or not os.path.exists(temp_file):
                    continue

                downloaded_paths.append(temp_file)

                if item.photo:
                    input_media_list.append(
                        InputMediaPhoto(
                            media=temp_file,
                            caption=item.caption or "",
                            caption_entities=item.caption_entities,
                            has_spoiler=item.has_media_spoiler,
                        )
                    )
                elif item.video:
                    input_media_list.append(
                        InputMediaVideo(
                            media=temp_file,
                            caption=item.caption or "",
                            caption_entities=item.caption_entities,
                            duration=item.video.duration,
                            width=item.video.width,
                            height=item.video.height,
                            supports_streaming=item.video.supports_streaming,
                            has_spoiler=item.has_media_spoiler,
                        )
                    )
                elif item.document:
                    input_media_list.append(
                        InputMediaDocument(
                            media=temp_file,
                            caption=item.caption or "",
                            caption_entities=item.caption_entities,
                        )
                    )
                elif item.audio:
                    input_media_list.append(
                        InputMediaAudio(
                            media=temp_file,
                            caption=item.caption or "",
                            caption_entities=item.caption_entities,
                            duration=item.audio.duration,
                            performer=item.audio.performer,
                            title=item.audio.title,
                        )
                    )

            if not input_media_list:
                logger.error(f"No media items could be prepared for album {first_msg.media_group_id}")
                return []

            # Send album to target
            sent = await with_flood_retry(
                sender.send_media_group,
                chat_id=target_chat_id,
                media=input_media_list,
            )
            return sent

        except Exception as e:
            logger.error(f"Failed to re-upload album {first_msg.media_group_id}: {e}", exc_info=True)
            return []
        finally:
            # Clean up all downloaded album files
            for p in downloaded_paths:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass


class BatchCloner:
    """Manages batch historical range copying with live progress tracking and cancellation."""

    def __init__(self, cloner: MessageCloner):
        self.cloner = cloner
        self.is_running = False
        self.cancel_requested = False
        self.task_id: Optional[str] = None
        self.stats = {
            "total": 0,
            "processed": 0,
            "copied": 0,
            "skipped": 0,
            "failed": 0,
        }

    def cancel(self) -> None:
        """Signal the running batch job to cancel."""
        self.cancel_requested = True

    async def run_range_copy(
        self,
        source_chat_id: int,
        target_chat_id: int,
        start_msg_id: int,
        end_msg_id: int,
        progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None
    ) -> Dict[str, Any]:
        """
        Copy a range of message IDs from source_chat_id to target_chat_id.
        Handles albums, empty messages, FloodWait, and rate limiting.
        """
        self.is_running = True
        # Only reset if not already flagged for cancellation
        if not hasattr(self, "cancel_requested"):
            self.cancel_requested = False

        if start_msg_id > end_msg_id:
            start_msg_id, end_msg_id = end_msg_id, start_msg_id

        total_msgs = (end_msg_id - start_msg_id) + 1
        self.stats = {
            "total": total_msgs,
            "processed": 0,
            "copied": 0,
            "skipped": 0,
            "failed": 0,
        }

        # Cache of processed media_group_ids so albums aren't duplicated
        processed_albums: Set[str] = set()

        curr_id = start_msg_id
        last_progress_update = 0

        logger.info(f"Starting batch copy: {source_chat_id} -> {target_chat_id} [{start_msg_id}..{end_msg_id}]")

        while curr_id <= end_msg_id:
            if self.cancel_requested:
                logger.info("Batch copy cancelled by user request.")
                break

            try:
                # Fetch message using user client
                msg: Optional[Message] = None
                try:
                    msg = await with_flood_retry(
                        self.cloner.user_client.get_messages,
                        chat_id=source_chat_id,
                        message_ids=curr_id
                    )
                except Exception as e:
                    logger.debug(f"Could not fetch message {curr_id}: {e}")

                # If message is deleted or empty
                if not msg or msg.empty:
                    self.stats["skipped"] += 1
                    self.stats["processed"] += 1
                    curr_id += 1
                    continue

                # Check if this message was already copied previously to this target
                already_copied = await self.cloner.db.is_message_copied(source_chat_id, msg.id, target_chat_id)
                if already_copied:
                    self.stats["skipped"] += 1
                    self.stats["processed"] += 1
                    curr_id += 1
                    continue

                # Handle Media Group / Album
                if msg.media_group_id:
                    if msg.media_group_id in processed_albums:
                        # Already copied as part of the group earlier
                        self.stats["processed"] += 1
                        curr_id += 1
                        continue

                    # Fetch the full album group
                    album_msgs = await with_flood_retry(
                        self.cloner.user_client.get_media_group,
                        chat_id=source_chat_id,
                        message_id=msg.id
                    )

                    copied_album = await self.cloner.clone_media_group(
                        source_chat_id=source_chat_id,
                        target_chat_id=target_chat_id,
                        messages=album_msgs
                    )

                    if copied_album:
                        self.stats["copied"] += len(album_msgs)
                        processed_albums.add(msg.media_group_id)
                        for item in album_msgs:
                            await self.cloner.db.record_copied_message(
                                source_chat_id=source_chat_id,
                                source_msg_id=item.id,
                                target_chat_id=target_chat_id,
                                media_type="album"
                            )
                    else:
                        self.stats["failed"] += len(album_msgs)

                    self.stats["processed"] += 1
                    curr_id += 1

                else:
                    # Single message copy
                    copied_msg = await self.cloner.clone_single_message(
                        source_chat_id=source_chat_id,
                        target_chat_id=target_chat_id,
                        msg=msg
                    )

                    if copied_msg:
                        self.stats["copied"] += 1
                        await self.cloner.db.record_copied_message(
                            source_chat_id=source_chat_id,
                            source_msg_id=msg.id,
                            target_chat_id=target_chat_id,
                            target_msg_id=copied_msg.id if hasattr(copied_msg, "id") else None,
                            media_type=get_media_type(msg)
                        )
                    else:
                        self.stats["skipped"] += 1

                    self.stats["processed"] += 1
                    curr_id += 1

            except Exception as e:
                logger.error(f"Error copying message {curr_id}: {e}", exc_info=True)
                self.stats["failed"] += 1
                self.stats["processed"] += 1
                curr_id += 1

            # Notify progress callback every 5 messages or when done
            if progress_callback and (self.stats["processed"] - last_progress_update >= 5 or curr_id > end_msg_id):
                last_progress_update = self.stats["processed"]
                try:
                    await progress_callback(self.stats)
                except Exception as e:
                    logger.debug(f"Progress callback error: {e}")

            # Safe delay between messages to prevent FloodWait
            if Config.DELAY_BETWEEN_MESSAGES > 0:
                await asyncio.sleep(Config.DELAY_BETWEEN_MESSAGES)

        self.is_running = False
        return self.stats
