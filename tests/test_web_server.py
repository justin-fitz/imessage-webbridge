import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web_server import ConnectionManager, ContactStore, WebHandler, get_chat_messages, get_recent_chats
from imessage_reader import APPLE_EPOCH_OFFSET
from models import ChatMessage


# --- ConnectionManager tests ---

@pytest.mark.asyncio
async def test_connection_manager_connect():
    mgr = ConnectionManager()
    ws = AsyncMock()
    await mgr.connect(ws)
    assert ws in mgr.active
    ws.accept.assert_called_once()


@pytest.mark.asyncio
async def test_connection_manager_disconnect():
    mgr = ConnectionManager()
    ws = AsyncMock()
    await mgr.connect(ws)
    mgr.disconnect(ws)
    assert ws not in mgr.active


@pytest.mark.asyncio
async def test_connection_manager_broadcast():
    mgr = ConnectionManager()
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    await mgr.connect(ws1)
    await mgr.connect(ws2)
    await mgr.broadcast({"type": "test"})
    ws1.send_text.assert_called_once_with('{"type": "test"}')
    ws2.send_text.assert_called_once_with('{"type": "test"}')


@pytest.mark.asyncio
async def test_connection_manager_removes_dead_connections():
    mgr = ConnectionManager()
    ws_good = AsyncMock()
    ws_bad = AsyncMock()
    ws_bad.send_text.side_effect = Exception("closed")
    await mgr.connect(ws_good)
    await mgr.connect(ws_bad)
    await mgr.broadcast({"type": "test"})
    assert ws_bad not in mgr.active
    assert ws_good in mgr.active


# --- WebHandler tests ---

@pytest.mark.asyncio
async def test_web_handler_forward():
    mgr = ConnectionManager()
    ws = AsyncMock()
    await mgr.connect(ws)
    handler = WebHandler(mgr)

    msg = ChatMessage(
        rowid=1, text="hello", is_from_me=False, sender_id="+15551234567",
        chat_identifier="+15551234567", chat_display_name="",
        chat_style=45, timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    await handler.forward_to_output(msg)

    sent = json.loads(ws.send_text.call_args[0][0])
    assert sent["type"] == "message"
    assert sent["text"] == "hello"
    assert sent["chat_identifier"] == "+15551234567"
    assert sent["is_from_me"] is False


@pytest.mark.asyncio
async def test_web_handler_forward_with_attachments(tmp_path):
    mgr = ConnectionManager()
    ws = AsyncMock()
    await mgr.connect(ws)
    handler = WebHandler(mgr)

    # Create a real temp file so the attachment passes the exists() check
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0")

    from models import ChatAttachment
    msg = ChatMessage(
        rowid=2, text=None, is_from_me=True, sender_id="me",
        chat_identifier="chat123", chat_display_name="Group",
        chat_style=43, timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        attachments=[ChatAttachment(str(photo), "image/jpeg", "photo.jpg", 1024)],
    )
    await handler.forward_to_output(msg)

    sent = json.loads(ws.send_text.call_args[0][0])
    assert len(sent["attachments"]) == 1
    assert sent["attachments"][0]["transfer_name"] == "photo.jpg"
    assert sent["attachments"][0]["url"].startswith("/api/attachments/")


# --- Database query tests ---

def _create_test_chatdb(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT, style INTEGER);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, is_from_me INTEGER DEFAULT 0,
            date INTEGER, handle_id INTEGER, cache_has_attachments INTEGER DEFAULT 0,
            item_type INTEGER DEFAULT 0, associated_message_type INTEGER DEFAULT 0,
            associated_message_guid TEXT,
            attributedBody BLOB, date_delivered INTEGER DEFAULT 0, date_read INTEGER DEFAULT 0,
            thread_originator_guid TEXT
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);

        INSERT INTO handle (ROWID, id) VALUES (1, '+15551234567');
        INSERT INTO chat (ROWID, chat_identifier, display_name, style) VALUES (1, '+15551234567', 'John', 45);
        INSERT INTO chat (ROWID, chat_identifier, display_name, style) VALUES (2, 'group123', 'Pizza Night', 43);
    """)
    # Insert messages for chat 1
    apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, associated_message_type) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, 0)",
            (i, f"guid-{i}", f"msg {i}", i % 2, apple_ns + i * 1_000_000_000),
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, ?)", (i,))

    # Insert messages for chat 2
    for i in range(4, 6):
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, associated_message_type) "
            "VALUES (?, ?, ?, 0, ?, 1, 0, 0)",
            (i, f"guid-{i}", f"group msg {i}", apple_ns + i * 1_000_000_000),
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (2, ?)", (i,))

    conn.commit()
    conn.close()
    return path


def test_get_recent_chats(tmp_path):
    db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
    chats = get_recent_chats(db_path, {})
    assert len(chats) == 2
    # Most recent chat first (group has higher ROWIDs)
    assert chats[0]["chat_identifier"] == "group123"
    assert chats[0]["display_name"] == "Pizza Night"
    assert chats[1]["chat_identifier"] == "+15551234567"
    assert chats[1]["display_name"] == "John"


def test_get_chat_messages(tmp_path):
    db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
    messages = get_chat_messages(db_path, "+15551234567", {})
    assert len(messages) == 3
    assert messages[0]["text"] == "msg 1"
    assert messages[2]["text"] == "msg 3"
    # ROWID 1: is_from_me = 1%2 = 1 (True), ROWID 2: 0, ROWID 3: 1
    assert messages[0]["is_from_me"] is True
    assert messages[1]["is_from_me"] is False
    assert messages[2]["is_from_me"] is True


def test_get_chat_messages_empty(tmp_path):
    db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
    messages = get_chat_messages(db_path, "nonexistent", {})
    assert messages == []


def test_get_recent_chats_limit(tmp_path):
    db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
    chats = get_recent_chats(db_path, {}, limit=1)
    assert len(chats) == 1


def test_get_chat_messages_with_offset(tmp_path):
    db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
    first = get_chat_messages(db_path, "+15551234567", {}, limit=2, offset=0)
    assert len(first) == 2
    second = get_chat_messages(db_path, "+15551234567", {}, limit=2, offset=2)
    assert len(second) == 1
    # No overlap
    assert first[0]["text"] != second[0]["text"]


def test_contact_store_sync():
    """ContactStore loads contacts and tracks sync metadata."""
    store = ContactStore()
    assert store.count >= 0
    assert store.last_sync != ""
    old_sync = store.last_sync
    import time
    time.sleep(0.01)
    store.sync()
    # Sync time should update
    assert store.last_sync >= old_sync
    assert store.count == len(store.contacts)


def test_create_app_endpoints_use_contact_store(tmp_path):
    """Verify the app wires contact_store correctly by calling the messages API."""
    from unittest.mock import patch
    from app_core import AppCore
    from config import Config, IMessageConfig, AppConfig, WebConfig

    # Create a test chat.db
    db_path = _create_test_chatdb(str(tmp_path / "chat.db"))

    config = Config(
        imessage=IMessageConfig(db_path=db_path),
        app=AppConfig(state_db=str(tmp_path / "state.db"), temp_dir=str(tmp_path / "tmp")),
        web=WebConfig(),
    )
    with patch("app_core.IMessageReader"):
        core = AppCore(config)

    from web_server import create_app
    app = create_app(core)

    from fastapi.testclient import TestClient
    client = TestClient(app)

    # Messages endpoint should work (no NameError on contacts)
    resp = client.get("/api/chats/%2B15551234567/messages")
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) == 3
    assert messages[0]["text"] == "msg 1"
