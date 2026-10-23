# 🚀 Telegram Channel Copier & Cloner (Production Ready)

A high-performance, production-ready Telegram bot and userbot system that **copies messages, media files, and albums from private channels into another private or public channel without the "Forwarded from" header** using a Telegram User Session.

---

## 🌟 Key Features

- **No Forward Header (Pure Copy / Clone):**
  - Sends messages and media directly into your destination channel so there is **no "Forwarded from" header** at all.
- **Private Channel Access via User Session:**
  - Standard Telegram bots cannot read from private channels or join via invite links. This bot uses a Telegram User Session (MTProto) for reading, joining, and copying.
- **Automatic Invite Link Joining:**
  - Send any private invite link (`https://t.me/+...`, `https://t.me/joinchat/...`, or `@username`) in chat, and the user session automatically joins the channel and presents quick setup options.
- **Full Media Support & Album / Media Group Preservation:**
  - Photos, Videos, Documents/Files, Audio/Music, Voice notes, Video notes (round messages), Animations (GIFs), Stickers, Polls, and Text messages (with bold, italic, code, spoilers, and hyperlink formatting preserved).
  - Multi-item **Albums / Media Groups** (up to 10 photos/videos) are grouped and posted as a single cohesive album in the destination channel.
- **Protected / Restricted Content Fallback:**
  - If a private channel has **"Restrict saving content" (`noforwards`)** enabled, Telegram blocks native copy. Our engine automatically detects this and falls back to streaming/downloading the media and re-uploading it with original captions, entities, and attributes!
- **Historical Range Copying:**
  - Copy hundreds or thousands of historical posts using `/forward <source_channel> <start_id> <end_id> [target_channel]`.
  - Live real-time progress bar updating percentage, copied count, skipped count, and errors.
  - Abort running jobs at any time with `/cancel`.
- **Live Real-Time Auto-Forwarding (Live Sync):**
  - Toggle live sync (`/live on` or `/live off`). Whenever a new post appears in any registered source channel, it is immediately cloned to the destination channel in real time.
- **Anti-Flood & Rate Limiting Engine:**
  - Built-in `FloodWait` protection: automatically intercepts Telegram rate-limit notices, sleeps the exact duration, and resumes without failing or crashing.
  - Configurable message delay (default 1.5s) to keep your Telegram account safe.
- **Duplicate Prevention:**
  - Embedded async SQLite database tracks message IDs and ensures the same post is never duplicated in your target channel.
- **Security & Multi-Admin Whitelist:**
  - Whitelist admin IDs via `.env`. Unauthorized users are strictly rejected.

---

## 📂 Project Architecture

```
telegram-channel-copier/
├── .env.example             # Configuration template
├── config.py                # Configuration loader and validator
├── database.py              # Async SQLite persistence (sources, target, settings, history)
├── helpers.py               # Link parsers, formatters, media type detectors, progress bar
├── cloner.py                # Core cloning engine (albums, single msgs, restricted fallback, floodwait)
├── bot.py                   # Telegram bot UI, command handlers, live channel listener
├── main.py                  # Dual-client entry point with graceful shutdown
├── generate_session.py      # Interactive Pyrogram session string generator
├── requirements.txt         # Production Python dependencies
├── Dockerfile               # Container build file
├── docker-compose.yml       # Production Docker Compose definition
├── data/                    # Persistent storage (bot.db, bot.log, temp media cache)
└── tests/
    ├── conftest.py          # Pytest setup and loop initialization
    └── test_copier.py       # Full unit and integration test suite
```

---

## 🛠️ Prerequisites & Credentials

To run this bot, you need:

1. **API ID & API Hash:**
   - Log in at [https://my.telegram.org](https://my.telegram.org).
   - Go to **API development tools** and create an application (App title and short name can be anything).
   - Copy your `api_id` (numeric) and `api_hash` (alphanumeric).

2. **Bot Token:**
   - Open Telegram and message [@BotFather](https://t.me/BotFather).
   - Send `/newbot`, choose a name and username.
   - Copy the HTTP API token provided.

3. **Your Telegram User ID (for Admin Access):**
   - Message [@userinfobot](https://t.me/userinfobot) to get your numeric ID (e.g. `123456789`).

---

## 🚀 Quick Setup Guide

### Step 1: Clone or Navigate to the Project

```bash
cd C:\Users\SUJAL\.gemini\antigravity\scratch\telegram-channel-copier
```

### Step 2: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 3: Configure Environment Variables

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials (`API_ID`, `API_HASH`, `BOT_TOKEN`, and `ADMIN_USER_IDS`):

```ini
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
ADMIN_USER_IDS=123456789
DEFAULT_TARGET_CHANNEL=-1001987654321
DELAY_BETWEEN_MESSAGES=1.5
COPY_AS_BOT=false
```

> **Note:** `USER_SESSION_STRING` is now optional in `.env`! You can connect your user account directly inside Telegram chat using `/login`.

### Step 4: Start the Bot

```bash
python main.py
```

### Step 5: Connect User Session (In-Bot or CLI)

You have two easy ways to connect:

**Method A: Directly inside Telegram Chat (Recommended):**
1. Open your bot in Telegram and send `/login`.
2. Send your phone number (e.g. `+1234567890`).
3. Send the OTP code Telegram sends to your app or SMS.
4. If you have 2-Step Verification (2FA) enabled, send your cloud password (the bot automatically deletes your password message for security).
5. Done! Your session is connected and securely persisted in the SQLite database.

**Method B: Via CLI Session Generator:**
```bash
python generate_session.py
```
Paste the generated string into your `.env` as `USER_SESSION_STRING`.

You will see:
```text
🤖 Bot Client Connected: @YourBotUsername (ID: 1234567890)
👤 User Session Connected: YourName (@YourUsername, ID: 987654321)
👑 Authorized Admins: [123456789]
Bot is running and ready to accept commands.
```

---

## 📖 Telegram Bot Command Reference

Send these commands directly to your **Bot in private DM**:

| Command | Arguments | Description |
|---|---|---|
| `/start` | None | Open the interactive dashboard with inline buttons. |
| `/help` | None | Display complete command reference manual. |
| `/login` (or `/add_session`) | None | Connect your user account directly via Telegram chat (Phone -> OTP -> 2FA). |
| `/remove_session` (or `/logout`) | None | Disconnect and clear the current user account session from the bot. |
| `/session` | None | View current user session connection status and account information. |
| `/cancel_login` | None | Abort an ongoing user session login process. |
| `/join` | `<link_or_username>` | Join a private/public channel using user session. |
| `/set_target` | `<chat_id_or_link>` | Set the active target/destination channel. |
| `/target` | None | View current target channel details and status. |
| `/forward` (or `/copy`) | `<source_id> <start> <end> [target]` | Copy historical range of messages without forward header. |
| `/cancel` | None | Stop/abort an active running batch copy task. |
| `/add_source` | `<chat_id_or_link>` | Add a channel to monitored sources list. |
| `/del_source` | `<chat_id>` | Remove a channel from monitored sources list. |
| `/sources` | None | List all registered source channels. |
| `/live` (or `/autoforward`) | `on` or `off` | Toggle real-time automatic copying of new incoming posts. |
| `/status` | None | Overview of active jobs, source counts, target channel, and stats. |
| `/chats` (or `/dialogs`) | None | List the latest channels/groups your user session has joined. |
| `/clean` | None | Clean temporary downloaded files from cache directory. |

---

## 💡 Practical Examples & Workflows

### 1. Joining a Private Channel via Invite Link
Simply send the invite link into the bot chat, or type:
```text
/join https://t.me/+AbCdEfGhIjK123
```
The bot will join the channel via your user session and reply with:
```text
✅ Successfully Joined / Found Channel!
Title: Secret VIP Channel
ID: -1001928374650
Members: 4,520

[➕ Add as Source]  [🎯 Set as Target]
```

### 2. Setting Your Destination Channel
Make sure your user account (or bot account) is a member/admin in your destination channel, then run:
```text
/set_target -1002233445566
```

### 3. Copying Historical Messages (Batch Range)
To copy messages `1` to `200` from source `-1001928374650` to your active target channel:
```text
/forward -1001928374650 1 200
```
The bot will provide a real-time updating status message:
```text
🔄 Cloning Messages In Progress...

Progress: [██████░░░░░░] 50% (100/200)
✅ Copied: 94
⏩ Skipped (empty/duplicate): 6
❌ Errors: 0

Use /cancel to stop at any time.
```

### 4. Enabling Live Auto-Forwarding (Real-Time Sync)
Add the channel to your sources list:
```text
/add_source -1001928374650
```
Turn on live sync:
```text
/live on
```
From now on, whenever an admin in `Secret VIP Channel` publishes any post (text, photo, video, document, or album), it is cloned to your destination channel within seconds **with zero forward tags**!

---

## 🐳 Docker Production Deployment

Run in the background 24/7 on any Linux VPS or server using Docker:

```bash
# Build and run containers
docker-compose up -d --build

# View real-time logs
docker-compose logs -f

# Stop container
docker-compose down
```

---

## ⚙️ VPS Production Deployment with Supervisor

Supervisor manages your bot as a background service on Ubuntu/Debian/CentOS, restarting it automatically if it crashes or the server reboots.

### 1. Install Dependencies on VPS
```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git supervisor
```

### 2. Clone Repository & Setup Virtual Environment
```bash
sudo git clone <YOUR_GITHUB_REPO_URL> /opt/telegram-channel-copier
cd /opt/telegram-channel-copier
sudo python3 -m venv venv
sudo ./venv/bin/pip install --upgrade pip
sudo ./venv/bin/pip install -r requirements.txt
```

### 3. Setup Environment File
```bash
sudo cp .env.example .env
sudo nano .env
```
*(Enter your API_ID, API_HASH, BOT_TOKEN, ADMIN_USER_IDS)*

### 4. Configure Supervisor
```bash
sudo cp telegram-copier.conf.example /etc/supervisor/conf.d/telegram-copier.conf
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status telegram-copier
```

### 5. Common Supervisor Commands
- View live logs: `sudo supervisorctl tail -f telegram-copier`
- Restart bot: `sudo supervisorctl restart telegram-copier`
- Stop bot: `sudo supervisorctl stop telegram-copier`

---

## 🧪 Automated Testing

Run the test suite to verify all components:

```bash
python -m pytest tests/test_copier.py -v
```

All 11 tests cover:
- Private invite link and username parsing (`https://t.me/+...`, `https://t.me/joinchat/...`, `@...`)
- Chat ID normalization (`-100...`)
- Visual progress bar generation
- SQLite database migrations, source/target CRUD, and deduplication
- Range copying logic, empty message handling, and deduplication
- Batch cancellation (`/cancel`)
- Media Group / Album grouping and batch copying
