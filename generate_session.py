"""
Session String Generator for Pyrogram.
Run this script once locally or on your server to generate the USER_SESSION_STRING for your .env file.
"""

import sys
import asyncio

# Ensure event loop exists on Python 3.12+ / 3.14+
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from pyrogram import Client


async def generate_string():
    print("=" * 60)
    print("      Pyrogram User Session String Generator")
    print("=" * 60)
    print("Obtain API_ID and API_HASH from https://my.telegram.org (under API development tools)\n")

    api_id_input = input("Enter your API_ID: ").strip()
    api_hash_input = input("Enter your API_HASH: ").strip()

    if not api_id_input.isdigit() or not api_hash_input:
        print("❌ Invalid API_ID or API_HASH. API_ID must be numeric.")
        sys.exit(1)

    api_id = int(api_id_input)
    api_hash = api_hash_input

    print("\nConnecting to Telegram... You will be prompted for your phone number, OTP, and 2FA password (if set).\n")

    async with Client(name="session_gen", in_memory=True, api_id=api_id, api_hash=api_hash) as app:
        session_str = await app.export_session_string()
        user = await app.get_me()

        print("\n" + "=" * 60)
        print("🎉 SUCCESS! Session String Generated for:", user.first_name, f"(@{user.username or 'No Username'})")
        print("=" * 60)
        print("\nCopy the string below and paste it into your .env file as USER_SESSION_STRING:\n")
        print(session_str)
        print("\n" + "=" * 60)
        print("⚠️ KEEP THIS STRING SECRET! Anyone with this string can access your Telegram account.")
        print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(generate_string())
    except KeyboardInterrupt:
        print("\nCancelled by user.")
    except Exception as e:
        print(f"\n❌ Error generating session string: {e}")
