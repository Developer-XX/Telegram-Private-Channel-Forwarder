"""
Telegram Bot controller and event handler.
Orchestrates Bot client (for UI and commands) and User client (for MTProto access to private channels).
Supports interactive in-bot User Session login (Phone -> OTP -> 2FA), session removal, and status monitoring.
"""

import os
import re
import asyncio

# Python 3.12+ event loop compatibility patch for Pyrogram
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import logging
from typing import Optional, Dict, Any

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import (
    UserAlreadyParticipant,
    InviteHashExpired,
    InviteHashInvalid,
    InviteRequestSent,
    ChannelPrivate,
    ChatAdminRequired,
    PeerIdInvalid,
    RPCError,
)

from config import Config
from database import db
from helpers import (
    is_admin,
    parse_chat_identifier,
    format_progress_bar,
    clean_channel_title,
    get_media_type,
)
from cloner import MessageCloner, BatchCloner
from session_manager import session_mgr, LoginState, mask_phone

logger = logging.getLogger("telegram_copier.bot")

# Global instances initialized at runtime
bot_app: Optional[Client] = None
user_app: Optional[Client] = None
cloner: Optional[MessageCloner] = None
batch_cloner: Optional[BatchCloner] = None
active_batch_task: Optional[asyncio.Task] = None


def admin_filter():
    """Custom filter to allow only authorized admin user IDs."""
    return filters.create(lambda _, __, message: bool(message.from_user and is_admin(message.from_user.id)))


def register_user_channel_listener(user: Client):
    """Attach the real-time incoming channel message listener to the user client."""
    @user.on_message(filters.channel)
    async def on_new_channel_post(client: Client, message: Message):
        try:
            # Check global live toggle
            if not await db.is_global_auto_forward_enabled():
                return

            source_chat_id = message.chat.id
            source_entry = await db.get_source(source_chat_id)
            if not source_entry or not source_entry.get("auto_forward"):
                return

            # Check target channel
            target = await db.get_target_channel()
            if not target:
                return
            target_chat_id = target["chat_id"]

            # Prevent forward loops if source is target
            if source_chat_id == target_chat_id:
                return

            # Prevent duplicates
            if await db.is_message_copied(source_chat_id, message.id, target_chat_id):
                return

            logger.info(f"Live post detected in source {source_chat_id} (msg {message.id}). Cloning...")

            # Handle album or single message
            if message.media_group_id:
                await asyncio.sleep(1.0)
                album_msgs = await user.get_media_group(source_chat_id, message.id)
                copied = await cloner.clone_media_group(source_chat_id, target_chat_id, album_msgs)
                if copied:
                    for m in album_msgs:
                        await db.record_copied_message(
                            source_chat_id=source_chat_id,
                            source_msg_id=m.id,
                            target_chat_id=target_chat_id,
                            media_type="album"
                        )
            else:
                copied = await cloner.clone_single_message(source_chat_id, target_chat_id, message)
                if copied:
                    await db.record_copied_message(
                        source_chat_id=source_chat_id,
                        source_msg_id=message.id,
                        target_chat_id=target_chat_id,
                        target_msg_id=copied.id if hasattr(copied, "id") else None,
                        media_type=get_media_type(message)
                    )

        except Exception as e:
            logger.error(f"Live forward error: {e}", exc_info=True)


async def activate_user_session(session_string: str, user_info: Dict[str, Any]) -> Tuple[bool, str]:
    """Start and bind a new user session client in memory."""
    global user_app, cloner, batch_cloner
    try:
        # Disconnect old client if running
        if user_app and user_app.is_connected:
            try:
                await user_app.stop()
            except Exception:
                pass

        new_user = Client(
            name="user",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=session_string,
            workdir=str(Config.DATA_DIR),
        )
        await new_user.start()

        user_app = new_user
        if cloner:
            cloner.user_client = new_user
        else:
            cloner = MessageCloner(user_client=new_user, bot_client=bot_app)
            batch_cloner = BatchCloner(cloner)

        # Register channel listener
        register_user_channel_listener(new_user)

        user_obj = user_info.get("user")
        user_id = user_obj.id if hasattr(user_obj, "id") else user_info.get("user_id", 0)
        first_name = user_obj.first_name if hasattr(user_obj, "first_name") else user_info.get("first_name", "User")
        username = user_obj.username if hasattr(user_obj, "username") else user_info.get("username", "")
        phone = user_info.get("phone", "")

        # Save to database
        await db.save_user_session(
            session_string=session_string,
            user_id=user_id,
            first_name=first_name,
            username=username,
            phone=phone
        )

        logger.info(f"User session activated for {first_name} (ID: {user_id})")
        return True, "User session activated successfully."
    except Exception as e:
        logger.error(f"Failed to activate user session: {e}", exc_info=True)
        return False, str(e)


async def deactivate_user_session() -> bool:
    """Disconnect and delete the active user session."""
    global user_app, cloner
    try:
        if user_app and user_app.is_connected:
            try:
                await user_app.stop()
            except Exception:
                pass
        user_app = None
        if cloner:
            cloner.user_client = None

        await db.delete_user_session()
        logger.info("User session deactivated and removed from database.")
        return True
    except Exception as e:
        logger.error(f"Error deactivating user session: {e}", exc_info=True)
        return False


def setup_handlers(bot: Client, user: Optional[Client] = None):
    """Register all message, command, and event handlers on bot client."""
    global bot_app, user_app, cloner, batch_cloner
    bot_app = bot
    user_app = user
    cloner = MessageCloner(user_client=user, bot_client=bot)
    batch_cloner = BatchCloner(cloner)

    if user and user.is_connected:
        register_user_channel_listener(user)

    # -------------------------------------------------------------
    # 1. BOT COMMAND HANDLERS
    # -------------------------------------------------------------

    @bot.on_message(filters.command("start") & admin_filter())
    async def cmd_start(client: Client, message: Message):
        """Welcome message and dashboard."""
        target = await db.get_target_channel()
        target_info = f"<code>{target['title']}</code> (ID: <code>{target['chat_id']}</code>)" if target else "<i>Not Set</i>"
        auto_fwd = await db.is_global_auto_forward_enabled()
        status_text = "🟢 Enabled" if auto_fwd else "🔴 Disabled"

        session_data = await db.get_user_session()
        is_user_connected = bool(user_app and user_app.is_connected)
        if is_user_connected and session_data:
            session_status = f"🟢 Connected (<b>{session_data['first_name']}</b>)"
        elif is_user_connected:
            session_status = "🟢 Connected"
        else:
            session_status = "🔴 <b>Not Connected</b> (/login to add)"

        text = (
            "🚀 <b>Telegram Channel Copier & Cloner</b>\n\n"
            "This bot copies posts & media from private channels to another channel "
            "<b>without forward tags</b> using your user session.\n\n"
            f"👤 <b>User Session:</b> {session_status}\n"
            f"🎯 <b>Target Channel:</b> {target_info}\n"
            f"⚡ <b>Live Auto-Forward:</b> {status_text}\n\n"
            "<b>Quick Commands:</b>\n"
            "• <code>/login</code> - Connect user session (Phone -> OTP -> 2FA)\n"
            "• <code>/session</code> - View or remove user session\n"
            "• Send any channel link (e.g. <code>https://t.me/+xxxx</code>) to join it\n"
            "• <code>/forward &lt;source_id&gt; &lt;start_id&gt; &lt;end_id&gt;</code> - Range copy\n"
            "• <code>/set_target &lt;channel_id_or_link&gt;</code> - Set target channel\n"
            "• <code>/sources</code> - Manage source channels\n"
            "• <code>/help</code> - Full command manual"
        )

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📱 User Session", callback_data="btn_session"),
                InlineKeyboardButton("🎯 Target", callback_data="btn_target"),
            ],
            [
                InlineKeyboardButton("📋 Sources", callback_data="btn_sources"),
                InlineKeyboardButton("⚡ Toggle Live Sync", callback_data="btn_toggle_live"),
            ],
            [
                InlineKeyboardButton("📊 Status", callback_data="btn_status"),
                InlineKeyboardButton("📖 Help & Manual", callback_data="btn_help"),
            ]
        ])

        await message.reply_text(text, reply_markup=buttons, disable_web_page_preview=True)

    @bot.on_message(filters.command("help") & admin_filter())
    async def cmd_help(client: Client, message: Message):
        """Full command guide."""
        help_text = (
            "📖 <b>Telegram Channel Copier Manual</b>\n\n"
            "<b>1. User Session Management:</b>\n"
            "• <code>/login</code> (or <code>/add_session</code>) - Connect your Telegram account by entering Phone -> OTP -> 2FA\n"
            "• <code>/remove_session</code> (or <code>/logout</code>) - Disconnect and remove the user session\n"
            "• <code>/session</code> - Check user session connection status & account info\n"
            "• <code>/cancel_login</code> - Abort an ongoing login process\n\n"
            "<b>2. Joining Private Channels:</b>\n"
            "• <code>/join &lt;link&gt;</code> - Join channel via invite link (<code>https://t.me/+...</code>) or username\n"
            "• Or simply paste the invite link in this chat!\n\n"
            "<b>3. Setting Target Channel:</b>\n"
            "• <code>/set_target &lt;chat_id_or_link&gt;</code> - Set destination channel\n"
            "• <code>/target</code> - View current target\n\n"
            "<b>4. Historical Range Copying (No Forward Tag):</b>\n"
            "• <code>/forward &lt;source_id&gt; &lt;start_id&gt; &lt;end_id&gt; [target_id]</code>\n"
            "  <i>Example:</i> <code>/forward -1001928374 1 50</code>\n"
            "  (Copies messages 1 to 50 from source to target without forward header)\n"
            "• <code>/cancel</code> - Abort an active running range copy job\n\n"
            "<b>5. Source Channel Management:</b>\n"
            "• <code>/add_source &lt;chat_id_or_link&gt;</code> - Add channel to monitored sources\n"
            "• <code>/del_source &lt;chat_id&gt;</code> - Remove a source channel\n"
            "• <code>/sources</code> - View all registered source channels\n\n"
            "<b>6. Live Auto-Forwarding (Real-Time Cloning):</b>\n"
            "• <code>/live on</code> or <code>/live off</code> - Toggle real-time forward\n\n"
            "<b>7. Status & Utilities:</b>\n"
            "• <code>/status</code> - Show active tasks, channels, and stats\n"
            "• <code>/dialogs</code> - List channels joined by your user session\n"
            "• <code>/clean</code> - Clean temporary cache directory"
        )
        await message.reply_text(help_text, disable_web_page_preview=True)

    # -------------------------------------------------------------
    # 2. USER SESSION COMMANDS (/login, /remove_session, /session)
    # -------------------------------------------------------------

    @bot.on_message(filters.command(["login", "add_session"]) & admin_filter())
    async def cmd_login(client: Client, message: Message):
        """Initiate in-bot user session login flow."""
        if user_app and user_app.is_connected:
            me = await user_app.get_me()
            return await message.reply_text(
                f"ℹ️ <b>User session is already connected!</b>\n\n"
                f"👤 <b>Name:</b> {me.first_name}\n"
                f"🆔 <b>ID:</b> <code>{me.id}</code>\n"
                f"🔗 <b>Username:</b> @{me.username or 'None'}\n\n"
                "If you want to switch or connect a different account, first run <code>/remove_session</code>."
            )

        success, text = await session_mgr.start_login(message.from_user.id)
        await message.reply_text(text)

    @bot.on_message(filters.command("cancel_login") & admin_filter())
    async def cmd_cancel_login(client: Client, message: Message):
        """Abort ongoing login process."""
        success, text = await session_mgr.cancel_login(message.from_user.id)
        await message.reply_text(text)

    @bot.on_message(filters.command(["session", "user"]) & admin_filter())
    async def cmd_session(client: Client, message: Message):
        """Show current user session status and account details."""
        session_data = await db.get_user_session()
        is_connected = bool(user_app and user_app.is_connected)

        if is_connected:
            me = await user_app.get_me()
            phone_str = mask_phone(session_data.get("phone", "")) if session_data else "N/A"
            text = (
                "📱 <b>User Session Status: 🟢 Connected</b>\n\n"
                f"👤 <b>Name:</b> {me.first_name}\n"
                f"🆔 <b>User ID:</b> <code>{me.id}</code>\n"
                f"🔗 <b>Username:</b> @{me.username or 'None'}\n"
                f"📞 <b>Phone:</b> <code>{phone_str}</code>\n\n"
                "Your user session is active and can access private channels."
            )
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Remove / Disconnect Session", callback_data="btn_logout")]
            ])
            await message.reply_text(text, reply_markup=buttons)
        else:
            text = (
                "📱 <b>User Session Status: 🔴 Disconnected</b>\n\n"
                "No user account is currently connected.\n"
                "Connect an account to enable joining private channels and copying posts without forward tags."
            )
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Connect Session (/login)", callback_data="btn_login")]
            ])
            await message.reply_text(text, reply_markup=buttons)

    @bot.on_message(filters.command(["remove_session", "logout", "del_session"]) & admin_filter())
    async def cmd_remove_session(client: Client, message: Message):
        """Disconnect and delete the user session."""
        if not user_app or not user_app.is_connected:
            session_data = await db.get_user_session()
            if not session_data:
                return await message.reply_text("ℹ️ No user session is currently connected.")

        await deactivate_user_session()
        await message.reply_text(
            "🗑️ <b>User session has been removed!</b>\n\n"
            "The account was disconnected and cleared from the database.\n"
            "You can connect a new account anytime with <code>/login</code>."
        )

    # -------------------------------------------------------------
    # 3. CHANNEL JOIN & FORWARD COMMANDS
    # -------------------------------------------------------------

    @bot.on_message(filters.command("join") & admin_filter())
    async def cmd_join(client: Client, message: Message):
        """Join a private or public channel via user session."""
        if not user_app or not user_app.is_connected:
            return await message.reply_text(
                "⚠️ <b>User Session not connected!</b>\n\n"
                "Please connect your user account with <code>/login</code> first."
            )

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            return await message.reply_text("⚠️ <b>Usage:</b> <code>/join &lt;invite_link_or_username&gt;</code>")

        raw_link = parts[1].strip()
        await handle_join_channel(client, message, raw_link)

    @bot.on_message(filters.command(["forward", "copy"]) & admin_filter())
    async def cmd_forward_range(client: Client, message: Message):
        """
        Copy a range of messages from a source channel to target channel.
        Usage: /forward <channel_id> <start_id> <end_id> [target_id]
        """
        global active_batch_task, batch_cloner

        if not user_app or not user_app.is_connected:
            return await message.reply_text(
                "⚠️ <b>User Session not connected!</b>\n\n"
                "Please connect your user account with <code>/login</code> first."
            )

        parts = message.text.split()
        if len(parts) < 4:
            return await message.reply_text(
                "⚠️ <b>Usage:</b>\n"
                "<code>/forward &lt;source_chat_id&gt; &lt;start_msg_id&gt; &lt;end_msg_id&gt; [target_chat_id]</code>\n\n"
                "<i>Example:</i>\n"
                "<code>/forward -1001234567890 1 100</code>"
            )

        if batch_cloner and batch_cloner.is_running:
            return await message.reply_text(
                "⚠️ A batch copying task is already in progress!\n"
                "Use <code>/cancel</code> to stop it before starting another."
            )

        # Parse source chat
        _, src_val = parse_chat_identifier(parts[1])
        source_chat_id = None
        try:
            source_chat = await user_app.get_chat(src_val)
            source_chat_id = source_chat.id
        except Exception as e:
            return await message.reply_text(f"❌ Failed to find source channel <code>{parts[1]}</code>: {e}")

        # Parse start and end message IDs
        try:
            start_id = int(parts[2])
            end_id = int(parts[3])
        except ValueError:
            return await message.reply_text("❌ <code>start_msg_id</code> and <code>end_msg_id</code> must be numbers.")

        # Parse target chat or use default
        target_chat_id = None
        if len(parts) >= 5:
            _, tgt_val = parse_chat_identifier(parts[4])
            try:
                target_chat = await user_app.get_chat(tgt_val)
                target_chat_id = target_chat.id
            except Exception as e:
                return await message.reply_text(f"❌ Failed to resolve target channel: {e}")
        else:
            saved_target = await db.get_target_channel()
            if not saved_target:
                return await message.reply_text(
                    "❌ No target channel configured!\n"
                    "Please set one with <code>/set_target &lt;channel_id&gt;</code> or specify it in the command."
                )
            target_chat_id = saved_target["chat_id"]

        # Initial Status Message
        status_msg = await message.reply_text(
            f"⏳ <b>Starting Batch Copy...</b>\n\n"
            f"<b>Source:</b> <code>{source_chat.title}</code> (<code>{source_chat_id}</code>)\n"
            f"<b>Target:</b> <code>{target_chat_id}</code>\n"
            f"<b>Range:</b> {start_id} to {end_id}\n\n"
            f"Preparing to clone messages without forward header..."
        )

        async def update_progress(stats: Dict[str, Any]):
            total = stats["total"]
            processed = stats["processed"]
            copied = stats["copied"]
            skipped = stats["skipped"]
            failed = stats["failed"]
            p_bar = format_progress_bar(processed, total)

            text = (
                f"🔄 <b>Cloning Messages In Progress...</b>\n\n"
                f"<b>Progress:</b> {p_bar} ({processed}/{total})\n"
                f"✅ <b>Copied:</b> {copied}\n"
                f"⏩ <b>Skipped (empty/duplicate):</b> {skipped}\n"
                f"❌ <b>Errors:</b> {failed}\n\n"
                f"<i>Use /cancel to stop at any time.</i>"
            )
            try:
                await status_msg.edit_text(text)
            except Exception:
                pass

        # Reset cancellation state for the new batch task
        batch_cloner.cancel_requested = False

        # Execute range copy in background task so bot stays responsive
        async def run_task():
            final_stats = await batch_cloner.run_range_copy(
                source_chat_id=source_chat_id,
                target_chat_id=target_chat_id,
                start_msg_id=start_id,
                end_msg_id=end_id,
                progress_callback=update_progress
            )
            status_desc = "🛑 <b>Cancelled by user!</b>" if batch_cloner.cancel_requested else "🎉 <b>Batch Copy Complete!</b>"
            final_text = (
                f"{status_desc}\n\n"
                f"<b>Source:</b> <code>{source_chat.title}</code>\n"
                f"<b>Processed:</b> {final_stats['processed']}/{final_stats['total']}\n"
                f"✅ <b>Successfully Copied:</b> {final_stats['copied']}\n"
                f"⏩ <b>Skipped:</b> {final_stats['skipped']}\n"
                f"❌ <b>Failed:</b> {final_stats['failed']}"
            )
            try:
                await status_msg.edit_text(final_text)
            except Exception:
                await message.reply_text(final_text)

        active_batch_task = asyncio.create_task(run_task())

    @bot.on_message(filters.command("cancel") & admin_filter())
    async def cmd_cancel(client: Client, message: Message):
        """Cancel active batch copying task."""
        global batch_cloner
        if batch_cloner and batch_cloner.is_running:
            batch_cloner.cancel()
            await message.reply_text("🛑 <b>Cancellation requested.</b> Stopping after current message...")
        else:
            await message.reply_text("ℹ️ No batch copy task is currently running.")

    @bot.on_message(filters.command("set_target") & admin_filter())
    async def cmd_set_target(client: Client, message: Message):
        """Set active destination channel."""
        if not user_app or not user_app.is_connected:
            return await message.reply_text(
                "⚠️ <b>User Session not connected!</b>\n\n"
                "Please connect your user account with <code>/login</code> first."
            )

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            return await message.reply_text("⚠️ <b>Usage:</b> <code>/set_target &lt;chat_id_or_link&gt;</code>")

        raw = parts[1].strip()
        kind, val = parse_chat_identifier(raw)
        try:
            chat = await user_app.get_chat(val)
            await db.set_target_channel(chat.id, chat.title or "Target Channel")
            await message.reply_text(
                f"🎯 <b>Target Channel Set Successfully!</b>\n\n"
                f"<b>Title:</b> <code>{chat.title}</code>\n"
                f"<b>ID:</b> <code>{chat.id}</code>\n"
                f"<b>Type:</b> {chat.type.name}\n\n"
                f"All copied posts will now be sent here."
            )
        except Exception as e:
            await message.reply_text(f"❌ Failed to find or access channel <code>{raw}</code>: {e}")

    @bot.on_message(filters.command("target") & admin_filter())
    async def cmd_target(client: Client, message: Message):
        """Show current target channel info."""
        target = await db.get_target_channel()
        if not target:
            return await message.reply_text("ℹ️ No target channel is currently set. Use <code>/set_target</code>.")

        if user_app and user_app.is_connected:
            try:
                chat = await user_app.get_chat(target["chat_id"])
                return await message.reply_text(
                    f"🎯 <b>Current Target Channel:</b>\n\n"
                    f"<b>Title:</b> <code>{chat.title}</code>\n"
                    f"<b>ID:</b> <code>{chat.id}</code>\n"
                    f"<b>Type:</b> {chat.type.name}\n"
                    f"<b>Members:</b> {chat.members_count or 'N/A'}"
                )
            except Exception:
                pass

        await message.reply_text(f"🎯 <b>Target Channel:</b> <code>{target['title']}</code> (ID: <code>{target['chat_id']}</code>)")

    @bot.on_message(filters.command("add_source") & admin_filter())
    async def cmd_add_source(client: Client, message: Message):
        """Add channel to monitored sources."""
        if not user_app or not user_app.is_connected:
            return await message.reply_text(
                "⚠️ <b>User Session not connected!</b>\n\n"
                "Please connect your user account with <code>/login</code> first."
            )

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            return await message.reply_text("⚠️ <b>Usage:</b> <code>/add_source &lt;chat_id_or_link&gt;</code>")

        raw = parts[1].strip()
        kind, val = parse_chat_identifier(raw)
        try:
            chat = await user_app.get_chat(val)
            await db.add_source(
                chat_id=chat.id,
                title=chat.title or "Source Channel",
                username=chat.username,
                auto_forward=True
            )
            await message.reply_text(
                f"✅ <b>Source Channel Added!</b>\n\n"
                f"<b>Title:</b> <code>{chat.title}</code>\n"
                f"<b>ID:</b> <code>{chat.id}</code>\n"
                f"<b>Live Sync:</b> Enabled"
            )
        except Exception as e:
            await message.reply_text(f"❌ Failed to add source: {e}")

    @bot.on_message(filters.command("del_source") & admin_filter())
    async def cmd_del_source(client: Client, message: Message):
        """Remove a source channel."""
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            return await message.reply_text("⚠️ <b>Usage:</b> <code>/del_source &lt;chat_id&gt;</code>")

        try:
            chat_id = int(parts[1].strip())
            removed = await db.remove_source(chat_id)
            if removed:
                await message.reply_text(f"🗑️ Removed source channel <code>{chat_id}</code>.")
            else:
                await message.reply_text(f"ℹ️ Channel <code>{chat_id}</code> was not found in sources.")
        except ValueError:
            await message.reply_text("❌ Invalid Chat ID. Must be a number (e.g. <code>-100...</code>).")

    @bot.on_message(filters.command("sources") & admin_filter())
    async def cmd_sources(client: Client, message: Message):
        """List all registered source channels."""
        sources = await db.get_all_sources()
        if not sources:
            return await message.reply_text("ℹ️ No source channels registered yet. Add one with <code>/add_source</code>.")

        lines = ["📋 <b>Registered Source Channels:</b>\n"]
        for s in sources:
            sync = "🟢" if s["auto_forward"] else "🔴"
            u = f" (@{s['username']})" if s.get("username") else ""
            lines.append(f"{sync} <b>{s['title']}</b>{u}\n   ID: <code>{s['chat_id']}</code>")

        lines.append("\n<i>To remove: /del_source &lt;id&gt;</i>")
        await message.reply_text("\n".join(lines))

    @bot.on_message(filters.command(["live", "autoforward"]) & admin_filter())
    async def cmd_toggle_live(client: Client, message: Message):
        """Toggle live auto-forwarding."""
        parts = message.text.split()
        if len(parts) >= 2:
            arg = parts[1].lower()
            enabled = arg in ("on", "1", "true", "enable")
            await db.set_global_auto_forward(enabled)
        else:
            curr = await db.is_global_auto_forward_enabled()
            await db.set_global_auto_forward(not curr)

        new_status = await db.is_global_auto_forward_enabled()
        status_str = "🟢 Enabled" if new_status else "🔴 Disabled"
        await message.reply_text(f"⚡ <b>Live Auto-Forward is now:</b> {status_str}")

    @bot.on_message(filters.command("status") & admin_filter())
    async def cmd_status(client: Client, message: Message):
        """Show full status summary."""
        target = await db.get_target_channel()
        target_str = f"<code>{target['title']}</code> (<code>{target['chat_id']}</code>)" if target else "Not Set"
        stats = await db.get_stats()
        is_live = await db.is_global_auto_forward_enabled()
        running_batch = "Active" if batch_cloner and batch_cloner.is_running else "Idle"
        is_user_connected = bool(user_app and user_app.is_connected)

        text = (
            "📊 <b>Bot Status Overview</b>\n\n"
            f"👤 <b>User Session:</b> {'🟢 Connected' if is_user_connected else '🔴 Disconnected'}\n"
            f"🎯 <b>Target:</b> {target_str}\n"
            f"⚡ <b>Live Auto-Forward:</b> {'🟢 ON' if is_live else '🔴 OFF'}\n"
            f"🔄 <b>Batch Copy Task:</b> {running_batch}\n"
            f"📁 <b>Monitored Sources:</b> {stats['source_channels']}\n"
            f"📨 <b>Total Copied Messages:</b> {stats['copied_messages']}\n"
            f"🤖 <b>Posting Mode:</b> {'Bot Account' if Config.COPY_AS_BOT else 'User Session'}"
        )
        await message.reply_text(text)

    @bot.on_message(filters.command(["chats", "dialogs"]) & admin_filter())
    async def cmd_dialogs(client: Client, message: Message):
        """List recent channels joined by the user session."""
        if not user_app or not user_app.is_connected:
            return await message.reply_text(
                "⚠️ <b>User Session not connected!</b>\n\n"
                "Please connect your user account with <code>/login</code> first."
            )

        msg = await message.reply_text("🔍 Fetching joined channels...")
        lines = ["📚 <b>Recent Joined Channels:</b>\n"]
        count = 0

        try:
            async for dialog in user_app.get_dialogs(limit=30):
                chat = dialog.chat
                if chat.type.name in ("CHANNEL", "SUPERGROUP"):
                    u = f" (@{chat.username})" if chat.username else ""
                    lines.append(f"• <b>{chat.title}</b>{u}\n  ID: <code>{chat.id}</code>")
                    count += 1
                    if count >= 15:
                        break

            if count == 0:
                await msg.edit_text("ℹ️ No channels found in user session dialogs.")
            else:
                await msg.edit_text("\n".join(lines))
        except Exception as e:
            await msg.edit_text(f"❌ Error fetching dialogs: {e}")

    @bot.on_message(filters.command("clean") & admin_filter())
    async def cmd_clean(client: Client, message: Message):
        """Clean temporary download folder."""
        count = 0
        try:
            for f in Config.TEMP_DIR.glob("*"):
                if f.is_file():
                    f.unlink()
                    count += 1
            await message.reply_text(f"🧹 Cleaned {count} temporary files.")
        except Exception as e:
            await message.reply_text(f"❌ Error cleaning cache: {e}")

    # -------------------------------------------------------------
    # 4. INTERACTIVE MESSAGE HANDLER (LOGIN STEPS & LINK JOINING)
    # -------------------------------------------------------------

    @bot.on_message(filters.private & filters.text & admin_filter() & ~filters.command([
        "start", "help", "join", "forward", "copy", "cancel", "set_target",
        "target", "add_source", "del_source", "sources", "live", "autoforward",
        "status", "chats", "dialogs", "clean", "login", "add_session",
        "remove_session", "logout", "del_session", "session", "user", "cancel_login"
    ]))
    async def on_private_text(client: Client, message: Message):
        user_id = message.from_user.id
        raw_text = message.text.strip()

        # A) Check if admin is currently in a login flow
        if session_mgr.is_in_login(user_id):
            curr_state = session_mgr.get_state(user_id)

            # Step 1: Handling Phone Number
            if curr_state == LoginState.WAITING_PHONE:
                status_msg = await message.reply_text("🔄 Sending authentication code...")
                success, reply_text = await session_mgr.handle_phone(user_id, raw_text)
                await status_msg.edit_text(reply_text)
                return

            # Step 2: Handling OTP Code
            elif curr_state == LoginState.WAITING_OTP:
                status_msg = await message.reply_text("🔄 Verifying OTP code...")
                status, reply_text, data = await session_mgr.handle_otp(user_id, raw_text)

                if status == "SUCCESS":
                    act_ok, act_msg = await activate_user_session(data["session_string"], data)
                    if act_ok:
                        user_obj = data["user"]
                        success_msg = (
                            "🎉 <b>User Session Connected Successfully!</b>\n\n"
                            f"👤 <b>Name:</b> {user_obj.first_name}\n"
                            f"🆔 <b>User ID:</b> <code>{user_obj.id}</code>\n"
                            f"🔗 <b>Username:</b> @{user_obj.username or 'None'}\n"
                            f"📞 <b>Phone:</b> <code>{mask_phone(data.get('phone', ''))}</code>\n\n"
                            "You can now join private channels with <code>/join &lt;link&gt;</code> and copy messages with <code>/forward</code>!"
                        )
                        await status_msg.edit_text(success_msg)
                    else:
                        await status_msg.edit_text(f"❌ Failed to activate session: {act_msg}")
                else:
                    await status_msg.edit_text(reply_text)
                return

            # Step 3: Handling 2FA Cloud Password
            elif curr_state == LoginState.WAITING_2FA:
                # Delete password message from chat for privacy
                try:
                    await message.delete()
                except Exception:
                    pass

                status_msg = await message.reply_text("🔄 Verifying 2FA password...")
                status, reply_text, data = await session_mgr.handle_2fa(user_id, raw_text)

                if status == "SUCCESS":
                    act_ok, act_msg = await activate_user_session(data["session_string"], data)
                    if act_ok:
                        user_obj = data["user"]
                        success_msg = (
                            "🎉 <b>2FA Verified & User Session Connected!</b>\n\n"
                            f"👤 <b>Name:</b> {user_obj.first_name}\n"
                            f"🆔 <b>User ID:</b> <code>{user_obj.id}</code>\n"
                            f"🔗 <b>Username:</b> @{user_obj.username or 'None'}\n"
                            f"📞 <b>Phone:</b> <code>{mask_phone(data.get('phone', ''))}</code>\n\n"
                            "You can now join private channels with <code>/join &lt;link&gt;</code> and copy messages with <code>/forward</code>!"
                        )
                        await status_msg.edit_text(success_msg)
                    else:
                        await status_msg.edit_text(f"❌ Failed to activate session: {act_msg}")
                else:
                    await status_msg.edit_text(reply_text)
                return

        # B) Channel links sent directly in chat
        kind, val = parse_chat_identifier(raw_text)
        if kind in ("invite_link", "username"):
            if not user_app or not user_app.is_connected:
                return await message.reply_text(
                    "⚠️ <b>User Session not connected!</b>\n\n"
                    "Please connect your user account with <code>/login</code> before joining channels."
                )
            await handle_join_channel(client, message, raw_text)

    # -------------------------------------------------------------
    # 5. INLINE BUTTON CALLBACK HANDLERS
    # -------------------------------------------------------------

    @bot.on_callback_query()
    async def on_callback(client: Client, callback: CallbackQuery):
        """Handle inline button actions."""
        if not is_admin(callback.from_user.id):
            return await callback.answer("Unauthorized.", show_alert=True)

        data = callback.data

        if data == "btn_session":
            await cmd_session(client, callback.message)
            await callback.answer()

        elif data == "btn_login":
            await cmd_login(client, callback.message)
            await callback.answer()

        elif data == "btn_logout":
            await cmd_remove_session(client, callback.message)
            await callback.answer("User session removed!")

        elif data == "btn_sources":
            sources = await db.get_all_sources()
            if not sources:
                await callback.message.reply_text("ℹ️ No sources registered yet.")
            else:
                lines = ["📋 <b>Source Channels:</b>\n"]
                for s in sources:
                    sync = "🟢" if s["auto_forward"] else "🔴"
                    lines.append(f"{sync} <b>{s['title']}</b> (<code>{s['chat_id']}</code>)")
                await callback.message.reply_text("\n".join(lines))
            await callback.answer()

        elif data == "btn_target":
            target = await db.get_target_channel()
            if target:
                await callback.message.reply_text(
                    f"🎯 <b>Target:</b> <code>{target['title']}</code> (ID: <code>{target['chat_id']}</code>)"
                )
            else:
                await callback.message.reply_text("ℹ️ No target channel set. Use <code>/set_target &lt;id&gt;</code>.")
            await callback.answer()

        elif data == "btn_toggle_live":
            curr = await db.is_global_auto_forward_enabled()
            await db.set_global_auto_forward(not curr)
            new_status = not curr
            await callback.answer(f"Live sync {'enabled' if new_status else 'disabled'}!")
            await callback.message.reply_text(f"⚡ Live Auto-Forward is now: {'🟢 Enabled' if new_status else '🔴 Disabled'}")

        elif data == "btn_status":
            stats = await db.get_stats()
            await callback.message.reply_text(
                f"📊 <b>Stats:</b>\n• Sources: {stats['source_channels']}\n• Copied Messages: {stats['copied_messages']}"
            )
            await callback.answer()

        elif data == "btn_help":
            await cmd_help(client, callback.message)
            await callback.answer()

        elif data.startswith("set_src_"):
            if not user_app or not user_app.is_connected:
                return await callback.answer("User session not connected!", show_alert=True)
            chat_id = int(data.split("_")[2])
            chat = await user_app.get_chat(chat_id)
            await db.add_source(chat.id, chat.title or "Source", chat.username, auto_forward=True)
            await callback.answer("Added to sources!")
            await callback.message.reply_text(f"✅ Added <b>{chat.title}</b> to source channels.")

        elif data.startswith("set_tgt_"):
            if not user_app or not user_app.is_connected:
                return await callback.answer("User session not connected!", show_alert=True)
            chat_id = int(data.split("_")[2])
            chat = await user_app.get_chat(chat_id)
            await db.set_target_channel(chat.id, chat.title or "Target")
            await callback.answer("Set as target!")
            await callback.message.reply_text(f"🎯 Set <b>{chat.title}</b> as active target channel.")


async def handle_join_channel(client: Client, message: Message, raw_link: str):
    """Helper to join a channel using user session and offer quick actions."""
    if not user_app or not user_app.is_connected:
        return await message.reply_text(
            "⚠️ <b>User Session not connected!</b>\n\n"
            "Please connect your user account with <code>/login</code> first."
        )

    status_msg = await message.reply_text(f"🔄 <b>Processing link:</b> <code>{raw_link}</code>...")
    kind, val = parse_chat_identifier(raw_link)

    try:
        chat = None
        try:
            chat = await user_app.join_chat(val)
        except UserAlreadyParticipant:
            chat = await user_app.get_chat(val)
        except InviteRequestSent:
            return await status_msg.edit_text(
                "📩 <b>Join request submitted!</b>\n"
                "This channel requires admin approval to join. Once approved, you can use it."
            )
        except (InviteHashExpired, InviteHashInvalid) as e:
            return await status_msg.edit_text(f"❌ <b>Invalid or expired invite link:</b> {e}")

        if not chat:
            chat = await user_app.get_chat(val)

        # Build inline options
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Add as Source", callback_data=f"set_src_{chat.id}"),
                InlineKeyboardButton("🎯 Set as Target", callback_data=f"set_tgt_{chat.id}"),
            ]
        ])

        await status_msg.edit_text(
            f"✅ <b>Successfully Joined / Found Channel!</b>\n\n"
            f"<b>Title:</b> <code>{chat.title}</code>\n"
            f"<b>ID:</b> <code>{chat.id}</code>\n"
            f"<b>Username:</b> @{chat.username or 'Private'}\n"
            f"<b>Members:</b> {chat.members_count or 'N/A'}\n\n"
            f"Choose an action below or use <code>/forward {chat.id} 1 50</code> to copy posts.",
            reply_markup=buttons
        )

    except Exception as e:
        logger.error(f"Failed to join channel: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ <b>Error joining channel:</b> {e}")
