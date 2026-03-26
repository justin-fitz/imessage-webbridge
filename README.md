# iMessage Web Gateway

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

### Messaging
- **Real-time messages** via WebSocket — new messages appear instantly
- **Send and receive** iMessages from the web UI
- **Infinite scroll** to load older messages (100 at a time)
- **Attachments** — images, videos, and files forwarded in both directions
- **Inline images/videos** displayed in chat with automatic HEIC-to-JPEG conversion for iPhone photos
- **Tapback reactions** (❤️ 👍 👎 😂 ‼️ ❓) displayed on messages

### Contacts & Conversations
- **Contact names** resolved from macOS AddressBook — shows names instead of phone numbers
- **Group chats** show member names when no group name is set
- **Contact sync** — resync contacts from the Preferences panel without restarting

### Status & Notifications
- **Delivery/Read status** on sent messages (Sent → Delivered → Read), updated in real time
- **Unread badges** — blue dot with count on sidebar chats, clears when you open the chat
- **Tab title notifications** — unread count in the tab title (e.g. `(3) iMessage Web Gateway`) and flashing tab title with sender name when new messages arrive on an inactive tab
- **Desktop notifications** — native browser notifications with sender name and message preview (opt-in via Preferences)
- **Notification sound** — toggleable sound with desktop notifications

### Preferences
Access via the **Preferences** link at the bottom of the sidebar:

| Setting | Default | Description |
|---------|---------|-------------|
| Flash tab title | On | Flash the browser tab with sender name when new messages arrive while tab is inactive |
| Desktop notifications | Off | Show native browser notifications for new messages (prompts for permission when enabled) |
| Notification sound | On | Play a sound with desktop notifications |
| Contacts | — | Shows contact count and last sync time, with a **Sync** button to reload contacts from AddressBook |

Preferences are saved in your browser's localStorage and persist across sessions.

### Security
- **Cookie-based authentication** with configurable password and 7-day sessions
- **WebSocket authentication** via session token
- **JXA message sending** — uses JavaScript for Automation with `json.dumps()` parameterization to prevent command injection
- **Input validation** — messages only sent to known conversations in chat.db
- **Chat identifier validation** — regex allowlist blocks malformed identifiers
- **Attachment tokens** — cryptographic random tokens with 1-hour TTL, auto-pruned
- **Connection limits** — configurable max WebSocket connections (default 20)
- **Message length cap** — configurable maximum (default 10,000 characters)
- **Origin checking** — optional WebSocket origin allowlist

## Configuration

```yaml
imessage:
  db_path: "~/Library/Messages/chat.db"
  attachments_path: "~/Library/Messages/Attachments/"
  poll_interval_seconds: 2          # how often to check for new messages

app:
  allowed_chats: []                  # empty = all chats
  # allowed_chats:                   # restrict to specific conversations
  #   - "+15551234567"
  state_db: "db/bridge.db"          # internal state database
  temp_dir: "tmp/"                   # temp files for HEIC conversion, etc.

web:
  host: "127.0.0.1"                 # use "0.0.0.0" for network access
  port: 8080
  password: "CHANGE_ME"             # required for internet exposure
  max_connections: 20                # max concurrent WebSocket connections
  max_message_length: 10000         # max characters per message
  # allowed_origins:                # restrict WebSocket origins
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

For TLS, use Caddy as a reverse proxy with a self-signed certificate:

```
:8081 {
    tls /path/to/cert.pem /path/to/key.pem
    reverse_proxy localhost:8080
}
```

Generate a self-signed cert:
```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 3650 -nodes \
  -subj "/CN=yourdomain.com" -addext "subjectAltName=DNS:yourdomain.com"
```

Install and start Caddy:
```bash
brew install caddy
brew services start caddy
```

Forward the HTTPS port (e.g. 8081) on your router. The browser will show a certificate warning on first visit for self-signed certs.

## Notes

- Your Mac must stay awake and logged in. Use `caffeinate -d` or disable sleep.
- Messages.app must be running (does not need to be in the foreground).
- The first run starts from the current point in time — no backfill of old messages.
- The web UI loads chat history when you select a conversation, with infinite scroll for older messages.
- Sending messages uses JXA (JavaScript for Automation) with parameterized inputs to prevent injection.
- Sending tapback reactions is not supported — Apple does not expose this via any public scripting API.

## Tests

```bash
source venv/bin/activate
python -m pytest tests/ -v
```

71 tests covering config loading, message reading, message sending, contact resolution, channel mapping, WebSocket management, delivery status, pagination, and full app integration.
