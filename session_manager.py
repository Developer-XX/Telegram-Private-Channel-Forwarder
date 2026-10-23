"""
Interactive Telegram User Session Login and Management.
Handles phone number input, OTP verification, and 2FA password authorization directly via bot chat.
"""

import re
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple
from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    PhoneNumberInvalid,
    PasswordHashInvalid,
    FloodWait,
    RPCError,
)

from config import Config
from database import db

logger = logging.getLogger("telegram_copier.session_manager")


class LoginState:
    WAITING_PHONE = "WAITING_PHONE"
    WAITING_OTP = "WAITING_OTP"
    WAITING_2FA = "WAITING_2FA"


def mask_phone(phone: str) -> str:
    """Mask phone number for safe display (e.g. +1234567890 -> +123***7890)."""
    if len(phone) <= 6:
        return phone
    prefix = phone[:4]
    suffix = phone[-4:]
    return f"{prefix}***{suffix}"


class SessionManager:
    def __init__(self):
        # Maps admin_id -> state dict
        self._active_logins: Dict[int, Dict[str, Any]] = {}

    def is_in_login(self, user_id: int) -> bool:
        """Check if user has an active login session in progress."""
        return user_id in self._active_logins

    def get_state(self, user_id: int) -> Optional[str]:
        """Return current login state for user."""
        entry = self._active_logins.get(user_id)
        return entry.get("state") if entry else None

    async def start_login(self, user_id: int) -> Tuple[bool, str]:
        """Initiate the in-bot login flow."""
        # Cancel any previous stale attempt
        if user_id in self._active_logins:
            await self.cancel_login(user_id)

        self._active_logins[user_id] = {
            "state": LoginState.WAITING_PHONE,
            "client": None,
            "phone": None,
            "phone_code_hash": None,
        }

        text = (
            "📱 <b>Connect Telegram User Account</b>\n\n"
            "Please send your phone number with international country code.\n\n"
            "<i>Example:</i> <code>+1234567890</code> or <code>+919876543210</code>\n\n"
            "<i>Send /cancel_login to cancel at any time.</i>"
        )
        return True, text

    async def handle_phone(self, user_id: int, raw_phone: str) -> Tuple[bool, str]:
        """Process phone number, connect in-memory client, and request OTP."""
        entry = self._active_logins.get(user_id)
        if not entry or entry.get("state") != LoginState.WAITING_PHONE:
            return False, "⚠️ No login flow waiting for phone number."

        # Sanitize phone number: keep leading plus and digits
        cleaned = re.sub(r"[^\d+]", "", raw_phone.strip())
        if not cleaned.startswith("+"):
            cleaned = f"+{cleaned}"

        if not re.match(r"^\+\d{7,15}$", cleaned):
            return False, (
                "❌ <b>Invalid Phone Number Format</b>\n\n"
                "Please enter a valid phone number including country code.\n"
                "<i>Example:</i> <code>+1234567890</code>"
            )

        temp_client = Client(
            name=f"auth_{user_id}",
            in_memory=True,
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
        )

        try:
            await temp_client.connect()
            sent_code = await temp_client.send_code(cleaned)

            entry["state"] = LoginState.WAITING_OTP
            entry["client"] = temp_client
            entry["phone"] = cleaned
            entry["phone_code_hash"] = sent_code.phone_code_hash

            text = (
                "📩 <b>Authentication Code Sent!</b>\n\n"
                f"Telegram has sent an OTP code to your account / SMS for <code>{mask_phone(cleaned)}</code>.\n\n"
                "Please send the OTP code here.\n"
                "<i>Format:</i> <code>1 2 3 4 5</code> or <code>12345</code>\n\n"
                "<i>Send /cancel_login to abort.</i>"
            )
            return True, text

        except PhoneNumberInvalid:
            await self._cleanup_client(temp_client)
            return False, "❌ <b>Phone Number Invalid!</b> Please check and enter your phone number again."
        except FloodWait as e:
            await self._cleanup_client(temp_client)
            await self.cancel_login(user_id)
            return False, f"⏳ <b>FloodWait:</b> Telegram requires you to wait {e.value} seconds before requesting a code."
        except RPCError as e:
            await self._cleanup_client(temp_client)
            return False, f"❌ <b>Telegram Error:</b> {e.MESSAGE or str(e)}"
        except Exception as e:
            await self._cleanup_client(temp_client)
            logger.error(f"Error sending code: {e}", exc_info=True)
            return False, f"❌ Failed to request code: {e}"

    async def handle_otp(self, user_id: int, raw_otp: str) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        """
        Process OTP code.
        Returns: (status, message, result_dict)
        Status values: 'SUCCESS', 'NEED_2FA', 'ERROR'
        """
        entry = self._active_logins.get(user_id)
        if not entry or entry.get("state") != LoginState.WAITING_OTP:
            return "ERROR", "⚠️ No login flow waiting for OTP code.", None

        temp_client: Client = entry["client"]
        phone: str = entry["phone"]
        phone_code_hash: str = entry["phone_code_hash"]

        # Strip spaces or dashes
        code = re.sub(r"[^\d]", "", raw_otp.strip())
        if not code:
            return "ERROR", "❌ Please send a valid numeric OTP code (e.g. <code>12345</code>).", None

        try:
            signed_in_user = await temp_client.sign_in(
                phone_number=phone,
                phone_code_hash=phone_code_hash,
                phone_code=code
            )
            # Sign in succeeded without 2FA
            session_str = await temp_client.export_session_string()
            data = {
                "user": signed_in_user,
                "session_string": session_str,
                "phone": phone
            }
            await self._cleanup_client(temp_client)
            self._active_logins.pop(user_id, None)
            return "SUCCESS", "🎉 Logged in successfully!", data

        except SessionPasswordNeeded:
            entry["state"] = LoginState.WAITING_2FA
            text = (
                "🔐 <b>Two-Step Verification (2FA) Required</b>\n\n"
                "Your Telegram account has a 2FA cloud password enabled.\n"
                "Please enter your 2FA password to complete login.\n\n"
                "<i>Send /cancel_login to abort.</i>"
            )
            return "NEED_2FA", text, None

        except (PhoneCodeInvalid, PhoneCodeExpired):
            return "ERROR", "❌ <b>Incorrect or expired OTP code.</b> Please check and enter the code again.", None
        except FloodWait as e:
            await self.cancel_login(user_id)
            return "ERROR", f"⏳ <b>FloodWait:</b> Please wait {e.value} seconds.", None
        except Exception as e:
            logger.error(f"Error verifying OTP: {e}", exc_info=True)
            return "ERROR", f"❌ Failed to verify OTP: {e}", None

    async def handle_2fa(self, user_id: int, password: str) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        """
        Process 2FA Cloud Password.
        Returns: (status, message, result_dict)
        Status values: 'SUCCESS', 'ERROR'
        """
        entry = self._active_logins.get(user_id)
        if not entry or entry.get("state") != LoginState.WAITING_2FA:
            return "ERROR", "⚠️ No login flow waiting for 2FA password.", None

        temp_client: Client = entry["client"]
        phone: str = entry["phone"]

        try:
            signed_in_user = await temp_client.check_password(password)
            session_str = await temp_client.export_session_string()
            data = {
                "user": signed_in_user,
                "session_string": session_str,
                "phone": phone
            }
            await self._cleanup_client(temp_client)
            self._active_logins.pop(user_id, None)
            return "SUCCESS", "🎉 2FA verified successfully!", data

        except PasswordHashInvalid:
            return "ERROR", "❌ <b>Incorrect 2FA password.</b> Please enter your password again.", None
        except FloodWait as e:
            await self.cancel_login(user_id)
            return "ERROR", f"⏳ <b>FloodWait:</b> Please wait {e.value} seconds.", None
        except Exception as e:
            logger.error(f"Error checking 2FA password: {e}", exc_info=True)
            return "ERROR", f"❌ Failed to verify password: {e}", None

    async def cancel_login(self, user_id: int) -> Tuple[bool, str]:
        """Abort and cleanup any ongoing login attempt."""
        entry = self._active_logins.pop(user_id, None)
        if entry:
            client = entry.get("client")
            if client:
                await self._cleanup_client(client)
            return True, "🛑 <b>Login process cancelled.</b>"
        return False, "ℹ️ No active login process to cancel."

    async def _cleanup_client(self, client: Client):
        """Safely disconnect and stop in-memory client."""
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception:
            pass


# Singleton instance
session_mgr = SessionManager()
