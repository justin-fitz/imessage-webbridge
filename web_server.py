import asyncio
import json
import os
import secrets
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bridge import Bridge
from contacts import get_group_members, load_contacts, resolve_identifier
from imessage_reader import APPLE_EPOCH_OFFSET
from models import BridgeMessage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Session tokens: token -> expiry timestamp
_sessions: dict[str, float] = {}
_SESSION_TTL = 86400 * 7  # 7 days


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + _SESSION_TTL
    return token


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    if not expiry:
        return False
    if time.time() > expiry:
        del _sessions[token]
        return False
    return True


class ConnectionManager:
    def __init__(self, max_connections: int = 20):
        self.active: list[WebSocket] = []
        self.max_connections = max_connections

    async def connect(self, ws: WebSocket):
        if len(self.active) >= self.max_connections:
            await ws.close(code=1008, reason="Too many connections")
            return False
        await ws.accept()
        self.active.append(ws)
        return True

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        message = json.dumps(data)
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:
                if ws in self.active:
                    self.active.remove(ws)


class StatusPoller:
    def __init__(self, db_path: str, manager: ConnectionManager, interval: int = 3):
        self.db_path = db_path
        self.manager = manager
        self.interval = interval
        self._status_cache: dict[int, str] = {}

    async def poll_loop(self):
        while True:
            try:
                await self._check_status_changes()
            except Exception as e:
                print(f"Status poll error: {e}")
            await asyncio.sleep(self.interval)

    async def _check_status_changes(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT m.ROWID, m.date_delivered, m.date_read, c.chat_identifier
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE m.is_from_me = 1
              AND m.item_type = 0
              AND m.associated_message_type = 0
            ORDER BY m.ROWID DESC
            LIMIT 20
        """).fetchall()
        conn.close()

        for row in rows:
            rid = row["ROWID"]
            dr = row["date_read"]
            dd = row["date_delivered"]
            if dr and dr != 0:
                s = "read"
            elif dd and dd != 0:
                s = "delivered"
            else:
                s = "sent"

            old = self._status_cache.get(rid)
            if old != s:
                self._status_cache[rid] = s
                if old is not None:
                    await self.manager.broadcast({
                        "type": "status_update",
                        "chat_identifier": row["chat_identifier"],
                        "status": s,
                    })

        if len(self._status_cache) > 100:
            keep = {row["ROWID"] for row in rows}
            self._status_cache = {k: v for k, v in self._status_cache.items() if k in keep}


class WebBridge:
    def __init__(self, manager: ConnectionManager, contacts: dict[str, str] | None = None):
        self.manager = manager
        self.contacts = contacts or {}

    async def forward_to_output(self, msg: BridgeMessage):
        sender_name = msg.sender_id
        if msg.sender_id and msg.sender_id != "me":
            sender_name = resolve_identifier(msg.sender_id, self.contacts) or msg.sender_id
        data = {
            "type": "message",
            "chat_identifier": msg.chat_identifier,
            "chat_display_name": msg.chat_display_name,
            "chat_style": msg.chat_style,
            "sender_id": sender_name,
            "is_from_me": msg.is_from_me,
            "text": msg.text,
            "timestamp": msg.timestamp.isoformat(),
            "attachments": [
                {
                    "transfer_name": a.transfer_name,
                    "mime_type": a.mime_type,
                    "url": _register_attachment(a.filename),
                }
                for a in msg.attachments
                if a.filename and os.path.exists(a.filename)
            ],
        }
        await self.manager.broadcast(data)


def get_recent_chats(db_path: str, contacts: dict[str, str], limit: int = 50) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT c.chat_identifier, c.display_name, c.style,
               MAX(m.date) as last_date,
               (SELECT m2.text FROM message m2
                JOIN chat_message_join cmj2 ON m2.ROWID = cmj2.message_id
                WHERE cmj2.chat_id = c.ROWID
                  AND m2.item_type = 0 AND m2.associated_message_type = 0
                ORDER BY m2.ROWID DESC LIMIT 1) as last_text
        FROM chat c
        JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
        JOIN message m ON cmj.message_id = m.ROWID
        WHERE m.item_type = 0 AND m.associated_message_type = 0
        GROUP BY c.ROWID
        ORDER BY last_date DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    chats = []
    for row in rows:
        display_name = row["display_name"] or ""
        style = row["style"]
        if not display_name:
            if style == 43:
                members = get_group_members(db_path, row["chat_identifier"])
                member_names = [resolve_identifier(m, contacts) or m for m in members]
                display_name = ", ".join(member_names)
            else:
                display_name = resolve_identifier(row["chat_identifier"], contacts) or row["chat_identifier"]
        chats.append({
            "chat_identifier": row["chat_identifier"],
            "display_name": display_name,
            "style": style,
            "last_text": (row["last_text"] or "")[:80],
        })
    return chats


_attachment_registry: dict[str, tuple[str, float]] = {}
_ATTACHMENT_TTL = 3600


def _register_attachment(filepath: str) -> str | None:
    if not filepath or not os.path.exists(filepath):
        return None
    token = secrets.token_urlsafe(24)
    _attachment_registry[token] = (filepath, time.time())
    name = os.path.basename(filepath)
    return f"/api/attachments/{token}/{urllib.parse.quote(name)}"


def _prune_attachments():
    now = time.time()
    expired = [k for k, (_, ts) in _attachment_registry.items() if now - ts > _ATTACHMENT_TTL]
    for k in expired:
        del _attachment_registry[k]


_TAPBACK_MAP = {
    2000: "\u2764\ufe0f", 2001: "\ud83d\udc4d", 2002: "\ud83d\udc4e",
    2003: "\ud83d\ude02", 2004: "\u203c\ufe0f", 2005: "\u2753",
}


def _get_reactions_for_messages(conn, chat_identifier: str) -> dict[str, list[dict]]:
    """Load all reactions for a chat, keyed by target message guid."""
    rows = conn.execute("""
        SELECT m.associated_message_type, m.associated_message_guid, m.is_from_me
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        WHERE c.chat_identifier = ?
          AND m.associated_message_type BETWEEN 2000 AND 2006
    """, (chat_identifier,)).fetchall()

    reactions: dict[str, list[dict]] = {}
    for row in rows:
        guid = row["associated_message_guid"]
        msg_guid = guid.split("/")[-1] if "/" in guid else guid
        rtype = row["associated_message_type"]
        emoji = _TAPBACK_MAP.get(rtype, "")
        if not emoji:
            continue
        reactions.setdefault(msg_guid, []).append({
            "emoji": emoji,
            "is_from_me": bool(row["is_from_me"]),
        })
    return reactions


def _get_attachments_for_message(conn, message_rowid: int) -> list[dict]:
    rows = conn.execute("""
        SELECT a.filename, a.mime_type, a.transfer_name, a.total_bytes
        FROM attachment a
        JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id
        WHERE maj.message_id = ?
          AND a.mime_type IS NOT NULL
    """, (message_rowid,)).fetchall()
    attachments = []
    for row in rows:
        filename = row["filename"]
        if filename:
            filename = os.path.expanduser(filename)
        url = _register_attachment(filename) if filename else None
        if url:
            attachments.append({
                "transfer_name": row["transfer_name"] or os.path.basename(filename),
                "mime_type": row["mime_type"],
                "url": url,
            })
    return attachments


def _get_known_chat_identifiers(db_path: str) -> set[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT chat_identifier FROM chat").fetchall()
    conn.close()
    return {row["chat_identifier"] for row in rows}


def get_chat_messages(db_path: str, chat_identifier: str, contacts: dict[str, str], limit: int = 100, offset: int = 0) -> list[dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT m.ROWID, m.guid, m.text, m.is_from_me, m.date, m.attributedBody,
               m.cache_has_attachments,
               m.date_delivered, m.date_read, h.id as sender_id
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE c.chat_identifier = ?
          AND m.item_type = 0
          AND m.associated_message_type = 0
        ORDER BY m.ROWID DESC
        LIMIT ? OFFSET ?
    """, (chat_identifier, limit, offset)).fetchall()

    from imessage_reader import IMessageReader

    reactions = _get_reactions_for_messages(conn, chat_identifier)

    messages = []
    for row in reversed(rows):
        text = row["text"]
        if text is None and row["attributedBody"]:
            text = IMessageReader._extract_attributed_text(row["attributedBody"])

        attachments = []
        if row["cache_has_attachments"]:
            attachments = _get_attachments_for_message(conn, row["ROWID"])

        date = row["date"]
        if date and date != 0:
            ts = datetime.fromtimestamp(date / 1_000_000_000 + APPLE_EPOCH_OFFSET, tz=timezone.utc).isoformat()
        else:
            ts = ""
        if text is None and not attachments and row["attributedBody"] is None:
            continue
        sender_id = row["sender_id"] or "me"
        sender_name = resolve_identifier(sender_id, contacts) if sender_id != "me" else "me"

        status = None
        if row["is_from_me"]:
            dr = row["date_read"]
            dd = row["date_delivered"]
            if dr and dr != 0:
                status = "read"
            elif dd and dd != 0:
                status = "delivered"
            else:
                status = "sent"

        msg_reactions = reactions.get(row["guid"], [])

        messages.append({
            "text": text,
            "is_from_me": bool(row["is_from_me"]),
            "sender_id": sender_name or sender_id,
            "timestamp": ts,
            "status": status,
            "attachments": attachments,
            "reactions": msg_reactions,
        })

    conn.close()
    return messages


LOGIN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>iMessage Bridge - Login</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #1a1a1a; color: #e0e0e0;
         display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
  form { background: #222; padding: 32px; border-radius: 12px; width: 300px; }
  h2 { margin: 0 0 20px; font-size: 18px; }
  input { width: 100%; padding: 10px 14px; border: 1px solid #444; border-radius: 8px;
          background: #2a2a2a; color: #e0e0e0; font-size: 14px; box-sizing: border-box; }
  input:focus { border-color: #0b84fe; outline: none; }
  button { width: 100%; padding: 10px; margin-top: 12px; background: #0b84fe; color: #fff;
           border: none; border-radius: 8px; font-size: 14px; cursor: pointer; }
  button:hover { background: #0a75e0; }
  .error { color: #ff3b30; font-size: 12px; margin-top: 8px; }
</style>
</head><body>
<form method="POST" action="/login">
  <h2>iMessage Bridge</h2>
  <input type="password" name="password" placeholder="Password" autofocus>
  <button type="submit">Login</button>
  {error}
</form>
</body></html>"""


def create_app(bridge: Bridge) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
    manager = ConnectionManager(max_connections=bridge.config.web.max_connections)
    contacts = load_contacts()
    web_handler = WebBridge(manager, contacts)
    bridge.add_handler(web_handler)
    print(f"Loaded {len(contacts)} contacts from AddressBook")

    status_poller = StatusPoller(bridge.config.imessage.db_path, manager)
    known_chats = _get_known_chat_identifiers(bridge.config.imessage.db_path)
    password = bridge.config.web.password
    max_msg_len = bridge.config.web.max_message_length
    allowed_origins = set(bridge.config.web.allowed_origins)

    def require_auth(session: str | None = Cookie(default=None, alias="session")):
        if not password:
            return
        if not _valid_session(session):
            raise HTTPException(status_code=303, headers={"Location": "/login"})

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(bridge.poll_loop())
        asyncio.create_task(status_poller.poll_loop())
        asyncio.create_task(_attachment_prune_loop())
        if not password:
            print("WARNING: No password set — web UI is unauthenticated!")
        print(f"Web UI started — http://{bridge.config.web.host}:{bridge.config.web.port}")

    async def _attachment_prune_loop():
        while True:
            _prune_attachments()
            await asyncio.sleep(300)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        if not password:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(LOGIN_HTML.replace("{error}", ""))

    @app.post("/login")
    async def login_submit(response: Response, password_input: str = Form(alias="password")):
        if not password or secrets.compare_digest(password_input, password):
            token = _create_session()
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie("session", token, httponly=True, samesite="strict", max_age=_SESSION_TTL)
            return resp
        return HTMLResponse(LOGIN_HTML.replace("{error}", '<div class="error">Invalid password</div>'), status_code=401)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            return RedirectResponse("/login", status_code=303)
        chats = get_recent_chats(bridge.config.imessage.db_path, contacts)
        return templates.TemplateResponse(request, "chat.html", {"chats": chats, "ws_token": session or ""})

    @app.get("/api/chats")
    async def api_chats(session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        return get_recent_chats(bridge.config.imessage.db_path, contacts)

    @app.get("/api/chats/{chat_identifier:path}/messages")
    async def api_messages(chat_identifier: str, offset: int = 0, limit: int = 100, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        limit = min(limit, 200)
        return get_chat_messages(bridge.config.imessage.db_path, chat_identifier, contacts, limit=limit, offset=offset)

    @app.get("/api/attachments/{token}/{filename}")
    async def serve_attachment(token: str, filename: str, session: str | None = Cookie(default=None, alias="session")):
        if password and not _valid_session(session):
            raise HTTPException(status_code=401)
        entry = _attachment_registry.get(token)
        if not entry:
            raise HTTPException(status_code=404)
        filepath, created_at = entry
        if time.time() - created_at > _ATTACHMENT_TTL:
            del _attachment_registry[token]
            raise HTTPException(status_code=404)
        if not os.path.exists(filepath):
            raise HTTPException(status_code=404)

        ext = os.path.splitext(filepath)[1].lower()
        if ext in (".heic", ".heif"):
            jpeg_path = os.path.join(bridge.config.bridge.temp_dir, f"{token}.jpg")
            if not os.path.exists(jpeg_path):
                os.makedirs(bridge.config.bridge.temp_dir, exist_ok=True)
                import subprocess
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80", filepath, "--out", jpeg_path],
                    capture_output=True, timeout=15,
                )
            if os.path.exists(jpeg_path):
                return FileResponse(jpeg_path, media_type="image/jpeg")

        return FileResponse(filepath)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        # Authenticate via session token in query param
        if password:
            token = ws.query_params.get("token", "")
            if not _valid_session(token):
                await ws.accept()
                await ws.send_text(json.dumps({"type": "error", "message": "Unauthorized"}))
                await ws.close(code=1008)
                return

        # Check Origin header
        origin = ws.headers.get("origin", "")
        if allowed_origins and origin and origin not in allowed_origins:
            await ws.accept()
            await ws.close(code=1008)
            return

        connected = await manager.connect(ws)
        if not connected:
            return
        try:
            while True:
                data = await ws.receive_text()
                if len(data) > max_msg_len * 2:
                    continue
                msg = json.loads(data)
                if msg.get("type") == "send":
                    chat_id = msg.get("chat_identifier", "")
                    chat_style = msg.get("chat_style", 45)
                    text = msg.get("text", "")

                    if chat_id not in known_chats:
                        continue
                    if len(text) > max_msg_len:
                        text = text[:max_msg_len]
                    if text:
                        bridge.send_to_imessage(chat_id, chat_style, text=text)
        except WebSocketDisconnect:
            manager.disconnect(ws)
        except Exception:
            manager.disconnect(ws)

    return app
