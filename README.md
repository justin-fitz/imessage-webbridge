# iMessage WebBridge

A local macOS application that provides a web UI for iMessage. Supports bidirectional messaging with attachments.

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

```bash
python main.py
```

Open http://127.0.0.1:8080 in your browser. You'll see a chat interface with your iMessage conversations in the sidebar. Select a conversation to view history and send replies.

## Configuration

```yaml
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
  password: "CHANGE_ME"       # required for internet exposure
```

## How It Works

- **Reads messages** by polling `~/Library/Messages/chat.db` every 2 seconds
- **Sends messages** via AppleScript through Messages.app
- **Attachments** are forwarded in both directions
- **Dedup** prevents echo when bridge-sent messages appear in chat.db
- **Web UI** uses WebSocket for real-time message push

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
