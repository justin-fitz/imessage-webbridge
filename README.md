# iMessage-Discord Bridge

A local macOS application that bidirectionally bridges iMessage and Discord. Each iMessage conversation gets its own Discord channel, and you can reply from Discord back to iMessage.

## Requirements

- macOS with iMessage signed in and Messages.app running
- Python 3.11+
- A Discord server you have admin access to

## Setup

### 1. Create a Discord Bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application**, give it a name
3. Go to **Bot** in the sidebar
4. Click **Reset Token** and copy the bot token — you'll need it for config
5. Under **Privileged Gateway Intents**, enable **Message Content Intent**
6. Go to **OAuth2 > URL Generator**
   - Select the `bot` scope
   - Select permissions: `Send Messages`, `Manage Channels`, `Attach Files`, `Read Message History`
7. Copy the generated URL and open it in your browser to invite the bot to your server

### 2. Get Your Discord Server ID

1. In Discord, go to **Settings > Advanced** and enable **Developer Mode**
2. Right-click your server name in the sidebar and click **Copy Server ID**

### 3. macOS Permissions

1. Open **System Settings > Privacy & Security > Full Disk Access**
   - Add your terminal app (Terminal, iTerm, etc.)
2. **Automation** permission for Messages.app will be prompted on first run

### 4. Install & Configure

```bash
cd ~/imessage-discord-bridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your bot token and guild (server) ID:

```yaml
discord:
  bot_token: "your-bot-token-here"
  guild_id: 123456789012345678
  category_name: "iMessage"
```

Optional config:

```yaml
imessage:
  poll_interval_seconds: 2    # how often to check for new messages

bridge:
  allowed_chats: []            # empty = bridge all chats
  # allowed_chats:
  #   - "+15551234567"         # only bridge specific conversations
```

### 5. Run

```bash
source venv/bin/activate
python main.py
```

The bridge will:
- Create an "iMessage" category in your Discord server
- Auto-create a channel for each iMessage conversation as messages arrive
- Forward messages and attachments bidirectionally

## How It Works

- **iMessage → Discord:** Polls `~/Library/Messages/chat.db` every 2 seconds for new messages and forwards them to mapped Discord channels
- **Discord → iMessage:** Listens for messages in bridged channels and sends them via AppleScript through Messages.app
- **Attachments:** Images and files are forwarded in both directions
- **Dedup:** Messages sent from Discord are tracked so they don't echo back when they appear in chat.db

## Channel Naming

- 1-on-1 chats: `#im-15551234567`
- Named group chats: `#im-pizza-night`
- Unnamed group chats: `#im-group-272511`

## Notes

- Your Mac must stay awake and logged in. Use `caffeinate -d` or disable sleep to prevent interruptions.
- Messages.app must be running (does not need to be in the foreground).
- The first run starts from the current point in time — it does not backfill old messages.

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```
