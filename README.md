# iMessage WebBridge

A local macOS application that provides a web UI for iMessage. Supports bidirectional messaging with attachments, contact name resolution, tapback reactions, delivery/read status, and inline image display.

## Requirements

- macOS with iMessage signed in and Messages.app running
- Python 3.11+

## Setup

### 1. macOS Permissions

1. Open **System Settings > Privacy & Security > Full Disk Access**
   - Add your terminal app (Terminal, iTerm, etc.)
   - If running as a service, also add the Python binary
2. **Automation** permission for Messages.app will be prompted on first run

### 2. Install & Configure

```bash
cd ~/imessage-webbridge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` and set a password:

```yaml
web:
  password: "your-secure-password"
```

### 3. Run

```bash
python main.py
```

Open http://127.0.0.1:8080 in your browser. Log in with your password, and you'll see a chat interface with your iMessage conversations in the sidebar.

## Features

- **Real-time messages** via WebSocket — new messages appear instantly
- **Contact names** resolved from macOS AddressBook
- **Group chats** show member names
- **Inline images/videos** with automatic HEIC-to-JPEG conversion
- **Tapback reactions** (❤️ 👍 👎 😂 ‼️ ❓) displayed on messages
- **Delivery/Read status** on sent messages, updated in real time
- **Unread badges** on sidebar chats with count
- **Infinite scroll** to load older messages
- **Attachments** forwarded in both directions
- **Authentication** with cookie-based sessions
- **Input validation** — messages only sent to known conversations

## Configuration

```yaml
imessage:
  db_path: "~/Library/Messages/chat.db"
  poll_interval_seconds: 2

bridge:
  allowed_chats: []              # empty = bridge all chats
  # allowed_chats:
  #   - "+15551234567"           # only bridge specific conversations

web:
  host: "127.0.0.1"             # use "0.0.0.0" for network access
  port: 8080
  password: "CHANGE_ME"         # required for internet exposure
  max_connections: 20            # max concurrent WebSocket connections
  max_message_length: 10000     # max characters per message
  # allowed_origins:            # restrict WebSocket origins
  #   - "https://yourdomain.com"
```

## Running as a Service

To start automatically on login, create a launchd plist at `~/Library/LaunchAgents/com.imessage-webbridge.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.imessage-webbridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/imessage-webbridge/venv/bin/python</string>
        <string>/path/to/imessage-webbridge/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/imessage-webbridge</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

Load with: `launchctl load ~/Library/LaunchAgents/com.imessage-webbridge.plist`

## HTTPS with Caddy

For TLS, use Caddy as a reverse proxy:

```
:8081 {
    tls /path/to/cert.pem /path/to/key.pem
    reverse_proxy localhost:8080
}
```

## Notes

- Your Mac must stay awake and logged in. Use `caffeinate -d` or disable sleep.
- Messages.app must be running (does not need to be in the foreground).
- The first run starts from the current point in time — no backfill of old messages.
- The web UI loads chat history when you select a conversation, with infinite scroll for older messages.
- Sending messages uses JXA (JavaScript for Automation) with parameterized inputs to prevent injection.

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```
