import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from bridge import Bridge
from contacts import get_group_members, load_contacts, resolve_identifier
from imessage_reader import APPLE_EPOCH_OFFSET
from models import BridgeMessage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        message = json.dumps(data)
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:
                self.active.remove(ws)


class StatusPoller:
    """Polls chat.db for delivery/read status changes on recent sent messages."""

    def __init__(self, db_path: str, manager: ConnectionManager, interval: int = 3):
        self.db_path = db_path
        self.manager = manager
        self.interval = interval
        # Track known status: rowid -> "sent" | "delivered" | "read"
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
                status = "read"
            elif dd and dd != 0:
                status = "delivered"
            else:
                status = "sent"

            old = self._status_cache.get(rid)
            if old != status:
                self._status_cache[rid] = status
                if old is not None:
                    await self.manager.broadcast({
                        "type": "status_update",
                        "chat_identifier": row["chat_identifier"],
                        "status": status,
                    })

        # Prune old entries
        if len(self._status_cache) > 100:
            keep = {row["ROWID"] for row in rows}
            self._status_cache = {k: v for k, v in self._status_cache.items() if k in keep}


class WebBridge:
    """Web UI message handler — registered with Bridge.add_handler()."""

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
                {"transfer_name": a.transfer_name, "mime_type": a.mime_type}
                for a in msg.attachments
            ],
        }
        await self.manager.broadcast(data)


def get_recent_chats(db_path: str, contacts: dict[str, str], limit: int = 50) -> list[dict]:
    """Load recent conversations from chat.db for the sidebar."""
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
            if style == 43:  # group chat
                members = get_group_members(db_path, row["chat_identifier"])
                member_names = [resolve_identifier(m, contacts) or m for m in members]
                display_name = ", ".join(member_names)
            else:  # 1-on-1
                display_name = resolve_identifier(row["chat_identifier"], contacts) or row["chat_identifier"]

        chats.append({
            "chat_identifier": row["chat_identifier"],
            "display_name": display_name,
            "style": style,
            "last_text": (row["last_text"] or "")[:80],
        })
    return chats


def get_chat_messages(db_path: str, chat_identifier: str, contacts: dict[str, str], limit: int = 100) -> list[dict]:
    """Load recent messages for a specific chat."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT m.text, m.is_from_me, m.date, m.attributedBody,
               m.date_delivered, m.date_read, h.id as sender_id
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE c.chat_identifier = ?
          AND m.item_type = 0
          AND m.associated_message_type = 0
        ORDER BY m.ROWID DESC
        LIMIT ?
    """, (chat_identifier, limit)).fetchall()
    conn.close()

    from imessage_reader import IMessageReader

    messages = []
    for row in reversed(rows):
        text = row["text"]
        if text is None and row["attributedBody"]:
            text = IMessageReader._extract_attributed_text(row["attributedBody"])
        date = row["date"]
        if date and date != 0:
            ts = datetime.fromtimestamp(date / 1_000_000_000 + APPLE_EPOCH_OFFSET, tz=timezone.utc).isoformat()
        else:
            ts = ""
        if text is None and row["attributedBody"] is None:
            continue  # skip delivery receipts
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

        messages.append({
            "text": text,
            "is_from_me": bool(row["is_from_me"]),
            "sender_id": sender_name or sender_id,
            "timestamp": ts,
            "status": status,
        })
    return messages


def create_app(bridge: Bridge) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
    manager = ConnectionManager()
    contacts = load_contacts()
    web_handler = WebBridge(manager, contacts)
    bridge.add_handler(web_handler)
    print(f"Loaded {len(contacts)} contacts from AddressBook")

    status_poller = StatusPoller(bridge.config.imessage.db_path, manager)

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(bridge.poll_loop())
        asyncio.create_task(status_poller.poll_loop())
        print(f"Web UI started — http://{bridge.config.web.host}:{bridge.config.web.port}")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        chats = get_recent_chats(bridge.config.imessage.db_path, contacts)
        return templates.TemplateResponse(request, "chat.html", {"chats": chats})

    @app.get("/api/chats")
    async def api_chats():
        return get_recent_chats(bridge.config.imessage.db_path, contacts)

    @app.get("/api/chats/{chat_identifier:path}/messages")
    async def api_messages(chat_identifier: str):
        return get_chat_messages(bridge.config.imessage.db_path, chat_identifier, contacts)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await manager.connect(ws)
        try:
            while True:
                data = await ws.receive_text()
                msg = json.loads(data)
                if msg.get("type") == "send":
                    chat_id = msg["chat_identifier"]
                    chat_style = msg.get("chat_style", 45)
                    text = msg.get("text")
                    if text:
                        bridge.send_to_imessage(chat_id, chat_style, text=text)
        except WebSocketDisconnect:
            manager.disconnect(ws)

    return app
