# iMessage Bridge

A local macOS application that bridges iMessage with Discord and/or a web UI. Supports bidirectional messaging with attachments.

## Requirements

- macOS with iMessage signed in and Messages.app running
- Python 3.11+

## Setup

### 1. macOS Permissions

1. Open **System Settings > Privacy & Security > Full Disk Access**
   - Add your terminal app (Terminal, iTerm, etc.)
2. **Automation** permission for Messages.app will be prompted on first run

### 2. Install & Configure

```bash
cd ~/imessage-discord-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

### 3. Run

The bridge supports three modes: `web`, `discord`, or `both`.

#### Web UI (default)

No Discord setup needed — just run:

```bash
python main.py
```

Open http://127.0.0.1:8080 in your browser. You'll see a chat interface with your iMessage conversations in the sidebar. Select a conversation to view history and send replies.

#### Discord

```bash
python main.py --mode discord
```

Requires Discord bot configuration (see below).

#### Both

```bash
python main.py --mode both
```

Runs the web UI and Discord bot simultaneously.

### Discord Bot Setup (optional)

Only needed for `--mode discord` or `--mode both`.

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application**, give it a name
3. Go to **Bot** > **Reset Token** and copy the token
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Go to **OAuth2 > URL Generator**
   - Select the `bot` scope
   - Permissions: `Send Messages`, `Manage Channels`, `Attach Files`, `Read Message History`
6. Invite the bot to your server using the generated URL
7. In Discord, enable **Developer Mode** (Settings > Advanced), then right-click your server > **Copy Server ID**

Add to `config.yaml`:

```yaml
discord:
  bot_token: "your-bot-token-here"
  guild_id: 123456789012345678
  category_name: "iMessage"
```

## Configuration

```yaml
# Discord (optional — only needed for discord/both mode)
# discord:
#   bot_token: "YOUR_BOT_TOKEN"
#   guild_id: 123456789
#   category_name: "iMessage"

imessage:
  db_path: "~/Library/Messages/chat.db"
  poll_interval_seconds: 2

bridge:
  allowed_chats: []            # empty = bridge all chats
  # allowed_chats:
  #   - "+15551234567"         # only bridge specific conversations

web:
  host: "127.0.0.1"           # use "0.0.0.0" for network access
  port: 8080
```

## How It Works

- **Reads messages** by polling `~/Library/Messages/chat.db` every 2 seconds
- **Sends messages** via AppleScript through Messages.app
- **Attachments** are forwarded in both directions
- **Dedup** prevents echo when bridge-sent messages appear in chat.db
- **Web UI** uses WebSocket for real-time message push
- **Discord** auto-creates a channel per iMessage conversation under an "iMessage" category

## Discord Channel Naming

- 1-on-1 chats: `#im-15551234567`
- Named group chats: `#im-pizza-night`
- Unnamed group chats: `#im-group-272511`

## Notes

- Your Mac must stay awake and logged in. Use `caffeinate -d` or disable sleep.
- Messages.app must be running (does not need to be in the foreground).
- The first run starts from the current point in time — no backfill of old messages.
- The web UI loads chat history from chat.db when you select a conversation.

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```
