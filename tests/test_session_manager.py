"""
Unit and End-to-End tests for Interactive User Session Login & Management.
Tests phone input, OTP verification, 2FA password flow, cancellation, and session removal.
"""

import sys
import os
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyrogram.errors import (
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PasswordHashInvalid,
    PhoneNumberInvalid,
)

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
from session_manager import SessionManager, LoginState, mask_phone
import bot


@pytest.mark.asyncio
async def test_mask_phone():
    """Verify phone masking."""
    assert mask_phone("+1234567890") == "+123***7890"
    assert mask_phone("+919876543210") == "+919***3210"
    assert mask_phone("123") == "123"


@pytest.mark.asyncio
async def test_session_lifecycle_without_2fa():
    """
    End-to-End test of phone -> OTP -> session saved -> session removed.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Database(Path(tmpdir) / "test_sess.db")
        await test_db.init()

        # Temporarily set bot database
        bot.db = test_db

        mgr = SessionManager()
        admin_id = 99999

        # 1. Start Login
        ok, prompt = await mgr.start_login(admin_id)
        assert ok is True
        assert mgr.get_state(admin_id) == LoginState.WAITING_PHONE

        # Mock Pyrogram in-memory client
        mock_temp_client = MagicMock()
        mock_temp_client.is_connected = True
        mock_temp_client.connect = AsyncMock()
        mock_temp_client.disconnect = AsyncMock()
        mock_temp_client.send_code = AsyncMock(return_value=MagicMock(phone_code_hash="fake_hash_123"))

        mock_user = MagicMock()
        mock_user.id = 88888
        mock_user.first_name = "Alice"
        mock_user.username = "alice_tg"
        mock_temp_client.sign_in = AsyncMock(return_value=mock_user)
        mock_temp_client.export_session_string = AsyncMock(return_value="1BVtsFakeSessionStringWithout2FA...")

        with patch("session_manager.Client", return_value=mock_temp_client):
            # 2. Send Phone
            phone_ok, phone_prompt = await mgr.handle_phone(admin_id, "+1234567890")
            assert phone_ok is True
            assert mgr.get_state(admin_id) == LoginState.WAITING_OTP
            mock_temp_client.send_code.assert_called_once_with("+1234567890")

            # 3. Send OTP
            status, otp_msg, data = await mgr.handle_otp(admin_id, " 1 2 3 4 5 ")
            assert status == "SUCCESS"
            assert data["session_string"] == "1BVtsFakeSessionStringWithout2FA..."
            assert data["user"].first_name == "Alice"
            assert mgr.is_in_login(admin_id) is False

        # 4. Save Session to Database
        await test_db.save_user_session(
            session_string=data["session_string"],
            user_id=data["user"].id,
            first_name=data["user"].first_name,
            username=data["user"].username,
            phone=data["phone"]
        )

        saved = await test_db.get_user_session()
        assert saved is not None
        assert saved["session_string"] == "1BVtsFakeSessionStringWithout2FA..."
        assert saved["first_name"] == "Alice"
        assert saved["user_id"] == 88888
        assert saved["phone"] == "+1234567890"

        # 5. Remove Session
        await test_db.delete_user_session()
        assert await test_db.get_user_session() is None


@pytest.mark.asyncio
async def test_session_lifecycle_with_2fa():
    """
    End-to-End test of phone -> OTP (2FA triggered) -> Password -> session saved.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Database(Path(tmpdir) / "test_sess_2fa.db")
        await test_db.init()

        mgr = SessionManager()
        admin_id = 11111

        await mgr.start_login(admin_id)

        mock_temp_client = MagicMock()
        mock_temp_client.is_connected = True
        mock_temp_client.connect = AsyncMock()
        mock_temp_client.disconnect = AsyncMock()
        mock_temp_client.send_code = AsyncMock(return_value=MagicMock(phone_code_hash="hash_2fa"))
        # sign_in raises SessionPasswordNeeded
        mock_temp_client.sign_in = AsyncMock(side_effect=SessionPasswordNeeded())

        mock_user = MagicMock()
        mock_user.id = 77777
        mock_user.first_name = "Bob"
        mock_user.username = "bob_2fa"
        mock_temp_client.check_password = AsyncMock(return_value=mock_user)
        mock_temp_client.export_session_string = AsyncMock(return_value="1BVtsFake2FASessionString...")

        with patch("session_manager.Client", return_value=mock_temp_client):
            # Send Phone
            await mgr.handle_phone(admin_id, "+919876543210")
            assert mgr.get_state(admin_id) == LoginState.WAITING_OTP

            # Send OTP -> triggers 2FA
            status, otp_msg, data = await mgr.handle_otp(admin_id, "98765")
            assert status == "NEED_2FA"
            assert "Two-Step Verification" in otp_msg
            assert mgr.get_state(admin_id) == LoginState.WAITING_2FA

            # Send 2FA Password
            pwd_status, pwd_msg, pwd_data = await mgr.handle_2fa(admin_id, "my_super_secret_password")
            assert pwd_status == "SUCCESS"
            assert pwd_data["session_string"] == "1BVtsFake2FASessionString..."
            assert pwd_data["user"].first_name == "Bob"
            assert mgr.is_in_login(admin_id) is False

        # Verify DB saving
        await test_db.save_user_session(
            session_string=pwd_data["session_string"],
            user_id=pwd_data["user"].id,
            first_name=pwd_data["user"].first_name,
            username=pwd_data["user"].username,
            phone=pwd_data["phone"]
        )
        saved = await test_db.get_user_session()
        assert saved["session_string"] == "1BVtsFake2FASessionString..."
        assert saved["first_name"] == "Bob"


@pytest.mark.asyncio
async def test_session_errors_and_cancellation():
    """
    Test invalid inputs and cancellation flow.
    """
    mgr = SessionManager()
    admin_id = 55555

    # 1. Invalid phone number format
    await mgr.start_login(admin_id)
    ok, err_msg = await mgr.handle_phone(admin_id, "not-a-number")
    assert ok is False
    assert "Invalid Phone Number" in err_msg
    assert mgr.get_state(admin_id) == LoginState.WAITING_PHONE

    # 2. Cancel login
    can_ok, can_msg = await mgr.cancel_login(admin_id)
    assert can_ok is True
    assert "cancelled" in can_msg
    assert mgr.is_in_login(admin_id) is False

    # 3. Cancel when none in progress
    can_ok2, _ = await mgr.cancel_login(admin_id)
    assert can_ok2 is False
