import asyncio

# Ensure event loop exists on Python 3.12+ / 3.14+ before importing Pyrogram
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
