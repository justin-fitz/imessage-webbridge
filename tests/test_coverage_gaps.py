"""Tests to fill coverage gaps across the codebase."""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app_core import AppCore
from config import AppConfig, Config, IMessageConfig, WebConfig
from imessage_reader import APPLE_EPOCH_OFFSET
from models import ChatAttachment, ChatMessage


def _make_config(tmp_path):
    return Config(
        imessage=IMessageConfig(db_path=str(tmp_path / "chat.db"), poll_interval_seconds=1),
        app=AppConfig(state_db=str(tmp_path / "bridge.db"), temp_dir=str(tmp_path / "tmp")),
        web=WebConfig(password="testpass"),
    )


def _create_test_chatdb(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT, style INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, is_from_me INTEGER DEFAULT 0,
            date INTEGER, handle_id INTEGER, cache_has_attachments INTEGER DEFAULT 0,
            item_type INTEGER DEFAULT 0, associated_message_type INTEGER DEFAULT 0,
            associated_message_guid TEXT,
            attributedBody BLOB, date_delivered INTEGER DEFAULT 0, date_read INTEGER DEFAULT 0,
            thread_originator_guid TEXT, is_read INTEGER DEFAULT 0
        );
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT,
            transfer_name TEXT, total_bytes INTEGER
        );
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);

        INSERT INTO handle (ROWID, id) VALUES (1, '+15551234567');
        INSERT INTO handle (ROWID, id) VALUES (2, '+15559876543');
        INSERT INTO chat (ROWID, chat_identifier, display_name, style) VALUES (1, '+15551234567', 'John', 45);
        INSERT INTO chat (ROWID, chat_identifier, display_name, style) VALUES (2, 'chat123', 'Group', 43);
        INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (2, 1);
        INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (2, 2);
    """)
    apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, associated_message_type) "
            "VALUES (?, ?, ?, ?, ?, 1, 0, 0)",
            (i, f"guid-{i}", f"msg {i}", i % 2, apple_ns + i * 1_000_000_000),
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, ?)", (i,))
    conn.commit()
    conn.close()
    return path


# --- _sanitize tests ---

class TestSanitize:
    def test_sanitize_string(self):
        from web_server import _sanitize
        assert _sanitize("hello") == "hello"

    def test_sanitize_dict(self):
        from web_server import _sanitize
        result = _sanitize({"key": "value", "num": 42})
        assert result == {"key": "value", "num": 42}

    def test_sanitize_list(self):
        from web_server import _sanitize
        result = _sanitize(["a", "b", 3])
        assert result == ["a", "b", 3]

    def test_sanitize_nested(self):
        from web_server import _sanitize
        result = _sanitize({"msgs": [{"text": "hi"}, {"text": "bye"}]})
        assert result == {"msgs": [{"text": "hi"}, {"text": "bye"}]}

    def test_sanitize_passthrough(self):
        from web_server import _sanitize
        assert _sanitize(42) == 42
        assert _sanitize(None) is None
        assert _sanitize(True) is True


# --- Session management tests ---

class TestSessionManagement:
    def test_init_session_db(self, tmp_path):
        from web_server import _init_session_db, _session_db
        db_path = str(tmp_path / "sessions.db")
        _init_session_db(db_path)
        from web_server import _session_db
        assert _session_db is not None
        # Table should exist
        row = _session_db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()
        assert row is not None

    def test_create_and_validate_session(self, tmp_path):
        from web_server import _init_session_db, _create_session, _valid_session
        _init_session_db(str(tmp_path / "sessions.db"))
        token = _create_session()
        assert isinstance(token, str)
        assert len(token) > 20
        assert _valid_session(token) is True

    def test_invalid_session(self, tmp_path):
        from web_server import _init_session_db, _valid_session
        _init_session_db(str(tmp_path / "sessions.db"))
        assert _valid_session("bogus-token") is False
        assert _valid_session(None) is False
        assert _valid_session("") is False

    def test_expired_session(self, tmp_path):
        from web_server import _init_session_db, _valid_session, _session_db
        _init_session_db(str(tmp_path / "sessions.db"))
        # Insert already-expired session
        _session_db.execute("INSERT INTO sessions (token, expiry) VALUES (?, ?)", ("expired-tok", time.time() - 1))
        _session_db.commit()
        assert _valid_session("expired-tok") is False


# --- _extract_custom_emoji tests ---

class TestExtractCustomEmoji:
    def test_extract_emoji_from_text(self):
        from web_server import _extract_custom_emoji
        assert _extract_custom_emoji("Reacted 🔥 to hello", None) == "🔥"

    def test_extract_emoji_no_match(self):
        from web_server import _extract_custom_emoji
        assert _extract_custom_emoji("Just a normal message", None) is None

    def test_extract_emoji_none_inputs(self):
        from web_server import _extract_custom_emoji
        assert _extract_custom_emoji(None, None) is None


# --- _register_attachment / _prune_attachments tests ---

class TestAttachmentRegistry:
    def test_register_attachment(self, tmp_path):
        from web_server import _register_attachment, _attachment_registry
        f = tmp_path / "test.jpg"
        f.write_bytes(b"fake")
        url = _register_attachment(str(f))
        assert url is not None
        assert url.startswith("/api/attachments/")
        assert "test.jpg" in url

    def test_register_nonexistent_attachment(self):
        from web_server import _register_attachment
        assert _register_attachment("/nonexistent/file.jpg") is None
        assert _register_attachment(None) is None
        assert _register_attachment("") is None

    def test_prune_attachments(self, tmp_path):
        from web_server import _attachment_registry, _prune_attachments
        # Add an expired entry
        _attachment_registry["old-token"] = ("/some/file.jpg", time.time() - 7200)
        _prune_attachments()
        assert "old-token" not in _attachment_registry


# --- _get_known_chat_identifiers tests ---

class TestKnownChatIdentifiers:
    def test_get_known_chats(self, tmp_path):
        from web_server import _get_known_chat_identifiers
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        chats = _get_known_chat_identifiers(db_path)
        assert "+15551234567" in chats
        assert "chat123" in chats


# --- Reactions tests ---

class TestReactions:
    def _create_db_with_reactions(self, path):
        db_path = _create_test_chatdb(path)
        conn = sqlite3.connect(db_path)
        # Add a heart reaction to guid-1
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, associated_message_guid) "
            "VALUES (100, 'react-1', '', 0, 0, 1, 0, 2000, 'p:0/guid-1')"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 100)")
        # Add a thumbs up reaction to guid-1
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, associated_message_guid) "
            "VALUES (101, 'react-2', '', 1, 0, 1, 0, 2001, 'p:0/guid-1')"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 101)")
        conn.commit()
        conn.close()
        return db_path

    def test_reactions_loaded(self, tmp_path):
        from web_server import _get_reactions_for_messages
        db_path = self._create_db_with_reactions(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        reactions = _get_reactions_for_messages(conn, "+15551234567")
        conn.close()
        assert "guid-1" in reactions
        assert len(reactions["guid-1"]) == 2

    def test_reaction_removal(self, tmp_path):
        from web_server import _get_reactions_for_messages
        db_path = self._create_db_with_reactions(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        # Remove the heart reaction (3000 removes 2000)
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, associated_message_guid) "
            "VALUES (102, 'react-3', '', 0, 0, 1, 0, 3000, 'p:0/guid-1')"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 102)")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        reactions = _get_reactions_for_messages(conn, "+15551234567")
        conn.close()
        # Heart removed, only thumbs up remains
        assert len(reactions["guid-1"]) == 1
        # Thumbs up is the only one left (heart was removed)
        assert reactions["guid-1"][0]["is_from_me"] is True

    def test_custom_emoji_reaction(self, tmp_path):
        from web_server import _get_reactions_for_messages
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, associated_message_guid) "
            "VALUES (103, 'react-4', 'Reacted 🔥 to hello', 0, 0, 1, 0, 2006, 'p:0/guid-1')"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 103)")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        reactions = _get_reactions_for_messages(conn, "+15551234567")
        conn.close()
        assert "guid-1" in reactions
        assert reactions["guid-1"][0]["emoji"] == "🔥"


# --- Attachment retrieval tests ---

class TestGetAttachmentsForMessage:
    def test_get_attachments(self, tmp_path):
        from web_server import _get_attachments_for_message
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        # Add an attachment
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO attachment (ROWID, filename, mime_type, transfer_name, total_bytes) "
            "VALUES (1, ?, 'image/jpeg', 'photo.jpg', 1024)", (str(photo),)
        )
        conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
        conn.commit()
        conn.row_factory = sqlite3.Row
        atts = _get_attachments_for_message(conn, 1)
        conn.close()
        assert len(atts) == 1
        assert atts[0]["transfer_name"] == "photo.jpg"
        assert atts[0]["url"].startswith("/api/attachments/")

    def test_no_attachments(self, tmp_path):
        from web_server import _get_attachments_for_message
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        atts = _get_attachments_for_message(conn, 999)
        conn.close()
        assert atts == []


# --- Reply/thread tests ---

class TestReplyToMessages:
    def test_reply_included_in_messages(self, tmp_path):
        from web_server import get_chat_messages
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
        # Add a reply to guid-1
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, thread_originator_guid) "
            "VALUES (10, 'reply-1', 'replying here', 0, ?, 1, 0, 0, 'guid-1')",
            (apple_ns + 10 * 1_000_000_000,)
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 10)")
        conn.commit()
        conn.close()
        messages = get_chat_messages(db_path, "+15551234567", {})
        reply_msg = [m for m in messages if m["text"] == "replying here"][0]
        assert reply_msg["reply_to"] is not None
        assert reply_msg["reply_to"]["text"] == "msg 1"


# --- StatusPoller tests ---

class TestStatusPoller:
    @pytest.mark.asyncio
    async def test_status_changes_broadcast(self, tmp_path):
        from web_server import StatusPoller, ConnectionManager
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        # Set date_delivered on a sent message
        conn = sqlite3.connect(db_path)
        apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
        conn.execute("UPDATE message SET date_delivered = ? WHERE ROWID = 1", (apple_ns,))
        conn.commit()
        conn.close()

        mgr = ConnectionManager()
        poller = StatusPoller(db_path, mgr)

        # First poll populates cache
        await poller._check_status_changes()
        # Now update to read
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE message SET date_read = ? WHERE ROWID = 1", (apple_ns + 1000,))
        conn.commit()
        conn.close()

        ws = AsyncMock()
        await mgr.connect(ws)
        await poller._check_status_changes()
        # Should have broadcast a status update
        assert ws.send_text.called
        data = json.loads(ws.send_text.call_args[0][0])
        assert data["type"] == "status_update"
        assert data["status"] == "read"


# --- Connection limit tests ---

class TestConnectionLimits:
    @pytest.mark.asyncio
    async def test_max_connections_rejected(self):
        from web_server import ConnectionManager
        mgr = ConnectionManager(max_connections=2)
        ws1, ws2, ws3 = AsyncMock(), AsyncMock(), AsyncMock()
        assert await mgr.connect(ws1) is True
        assert await mgr.connect(ws2) is True
        assert await mgr.connect(ws3) is False
        ws3.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent(self):
        from web_server import ConnectionManager
        mgr = ConnectionManager()
        ws = AsyncMock()
        # Should not raise
        mgr.disconnect(ws)


# --- contacts.py tests ---

class TestContacts:
    def test_search_contacts(self):
        from contacts import search_contacts
        contacts = {"5551234567": "John Smith", "5559876543": "Jane Doe", "test@email.com": "Bob Test"}
        results = search_contacts("john", contacts)
        assert len(results) == 1
        assert results[0]["name"] == "John Smith"

    def test_search_contacts_by_identifier(self):
        from contacts import search_contacts
        contacts = {"5551234567": "John Smith"}
        results = search_contacts("555123", contacts)
        assert len(results) == 1

    def test_search_contacts_limit(self):
        from contacts import search_contacts
        contacts = {f"555000{i:04d}": f"Person {i}" for i in range(30)}
        results = search_contacts("Person", contacts, limit=5)
        assert len(results) == 5

    def test_search_contacts_dedup_by_name(self):
        from contacts import search_contacts
        contacts = {"5551111111": "John Smith", "john@test.com": "John Smith"}
        results = search_contacts("john", contacts)
        assert len(results) == 1

    def test_search_contacts_phone_formatting(self):
        from contacts import search_contacts
        contacts = {"5551234567": "John Smith"}
        results = search_contacts("john", contacts)
        assert results[0]["identifier"] == "+15551234567"

    def test_search_contacts_email(self):
        from contacts import search_contacts
        contacts = {"john@test.com": "John Smith"}
        results = search_contacts("john", contacts)
        assert results[0]["identifier"] == "john@test.com"

    def test_resolve_identifier_phone(self):
        from contacts import resolve_identifier
        contacts = {"5551234567": "John"}
        assert resolve_identifier("+15551234567", contacts) == "John"
        assert resolve_identifier("5551234567", contacts) == "John"

    def test_resolve_identifier_email(self):
        from contacts import resolve_identifier
        contacts = {"john@test.com": "John"}
        assert resolve_identifier("john@test.com", contacts) == "John"
        assert resolve_identifier("John@Test.com", contacts) == "John"

    def test_resolve_identifier_unknown(self):
        from contacts import resolve_identifier
        assert resolve_identifier("+15559999999", {}) is None
        assert resolve_identifier("", {}) is None

    def test_get_group_members(self, tmp_path):
        from contacts import get_group_members
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        members = get_group_members(db_path, "chat123")
        assert set(members) == {"+15551234567", "+15559876543"}

    def test_get_group_members_nonexistent(self, tmp_path):
        from contacts import get_group_members
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        members = get_group_members(db_path, "nonexistent")
        assert members == []

    def test_format_name(self):
        from contacts import _format_name
        assert _format_name("John", "Smith") == "John Smith"
        assert _format_name("John", None) == "John"
        assert _format_name(None, "Smith") == "Smith"
        assert _format_name(None, None) == ""

    def test_normalize_phone(self):
        from contacts import _normalize_phone
        assert _normalize_phone("+1 (555) 123-4567") == "5551234567"
        assert _normalize_phone("15551234567") == "5551234567"
        assert _normalize_phone("5551234567") == "5551234567"


# --- AppCore tests ---

class TestAppCoreSend:
    def test_send_text(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("app_core.IMessageReader"):
            core = AppCore(config)
        core.sender = MagicMock()
        core.send_to_imessage("+15551234567", 45, text="hello")
        core.sender.send_text.assert_called_once_with("+15551234567", 45, "hello")

    def test_send_file(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("app_core.IMessageReader"):
            core = AppCore(config)
        core.sender = MagicMock()
        core.send_to_imessage("+15551234567", 45, file_path="/tmp/photo.jpg")
        core.sender.send_file.assert_called_once_with("+15551234567", 45, "/tmp/photo.jpg")

    @pytest.mark.asyncio
    async def test_poll_loop_handler_error(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("app_core.IMessageReader") as mock_reader:
            core = AppCore(config)

        msg = ChatMessage(
            rowid=1, text="test", is_from_me=False, sender_id="+1",
            chat_identifier="+1", chat_display_name="", chat_style=45,
            timestamp=datetime.now(timezone.utc),
        )
        core.reader = MagicMock()
        core.reader.poll.return_value = [msg]

        handler = AsyncMock()
        handler.forward_to_output.side_effect = Exception("handler broke")
        core.add_handler(handler)

        # Patch sleep to break after one iteration
        call_count = 0
        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise StopIteration()

        with patch("asyncio.sleep", side_effect=fake_sleep):
            try:
                await core.poll_loop()
            except (StopIteration, StopAsyncIteration, RuntimeError):
                pass
        # Handler was called despite error
        handler.forward_to_output.assert_called_once()


# --- Full app endpoint tests ---

class TestAppEndpoints:
    def _make_client(self, tmp_path):
        from web_server import create_app
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        config = Config(
            imessage=IMessageConfig(db_path=db_path),
            app=AppConfig(state_db=str(tmp_path / "state.db"), temp_dir=str(tmp_path / "tmp")),
            web=WebConfig(password="testpass"),
        )
        with patch("app_core.IMessageReader"):
            core = AppCore(config)
        app = create_app(core)
        from fastapi.testclient import TestClient
        return TestClient(app), core

    def _login(self, client):
        resp = client.post("/login", data={"password": "testpass"})
        assert resp.status_code == 200  # redirected
        return client.cookies.get("session")

    def test_login_page(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "iMessage Web Gateway" in resp.text

    def test_login_success(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        resp = client.post("/login", data={"password": "testpass"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert "session" in resp.cookies

    def test_login_failure(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        resp = client.post("/login", data={"password": "wrong"})
        assert resp.status_code == 401
        assert "Invalid password" in resp.text

    def test_logout(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        self._login(client)
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_unauthenticated_redirect(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303

    def test_api_chats_unauthenticated(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        resp = client.get("/api/chats")
        assert resp.status_code == 401

    def test_api_chats_authenticated(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        self._login(client)
        resp = client.get("/api/chats")
        assert resp.status_code == 200
        chats = resp.json()
        assert len(chats) >= 1

    def test_contacts_search(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        self._login(client)
        resp = client.get("/api/contacts/search?q=test")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_contacts_search_too_short(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        self._login(client)
        resp = client.get("/api/contacts/search?q=a")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_contacts_status(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        self._login(client)
        resp = client.get("/api/contacts/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "count" in data
        assert "last_sync" in data

    def test_contacts_sync(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        self._login(client)
        resp = client.post("/api/contacts/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert "count" in data

    def test_send_new_message(self, tmp_path):
        client, core = self._make_client(tmp_path)
        self._login(client)
        core.sender = MagicMock()
        core.sender.send_text.return_value = True
        resp = client.post("/api/messages/new", json={"recipients": ["+15551234567"], "text": "hello"})
        assert resp.status_code == 200
        core.sender.send_text.assert_called_once()

    def test_send_new_message_missing_fields(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        self._login(client)
        resp = client.post("/api/messages/new", json={"recipients": [], "text": ""})
        assert resp.status_code == 400

    def test_attachment_404(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        self._login(client)
        resp = client.get("/api/attachments/bogus-token/file.jpg")
        assert resp.status_code == 404

    def test_service_worker(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        resp = client.get("/sw.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]

    def test_index_authenticated(self, tmp_path):
        client, _ = self._make_client(tmp_path)
        self._login(client)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "iMessage Web Bridge" in resp.text


# --- No-password mode tests ---

# --- Additional web_server coverage ---

class TestSessionExpiry:
    def test_expired_session_cleanup(self, tmp_path):
        """Line 72-74: expired session deletion in _valid_session."""
        import web_server
        web_server._init_session_db(str(tmp_path / "sessions.db"))
        # Insert a session that expired 10 seconds ago
        web_server._session_db.execute("INSERT INTO sessions (token, expiry) VALUES (?, ?)", ("exp-tok", time.time() - 10))
        web_server._session_db.commit()
        assert web_server._valid_session("exp-tok") is False
        # Should have been deleted
        row = web_server._session_db.execute("SELECT token FROM sessions WHERE token = 'exp-tok'").fetchone()
        assert row is None


class TestStatusPollerEdgeCases:
    @pytest.mark.asyncio
    async def test_cache_eviction(self, tmp_path):
        """Lines 158-160: cache eviction when > 100 entries."""
        from web_server import StatusPoller, ConnectionManager
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        mgr = ConnectionManager()
        poller = StatusPoller(db_path, mgr)
        # Fill cache with >100 fake entries
        for i in range(110):
            poller._status_cache[10000 + i] = "sent"
        await poller._check_status_changes()
        # Cache should have been pruned to only contain actual message ROWIDs
        assert len(poller._status_cache) <= 100

    @pytest.mark.asyncio
    async def test_poll_loop_error_handling(self, tmp_path):
        """Lines 113-118: poll_loop catches exceptions."""
        from web_server import StatusPoller, ConnectionManager
        mgr = ConnectionManager()
        poller = StatusPoller("/nonexistent/path.db", mgr)
        call_count = 0
        async def fake_sleep(n):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise StopAsyncIteration()
        with patch("asyncio.sleep", side_effect=fake_sleep):
            try:
                await poller.poll_loop()
            except (StopAsyncIteration, RuntimeError):
                pass
        # Should have attempted and caught the error without crashing


class TestGetRecentChatsContactResolution:
    def test_group_chat_no_display_name(self, tmp_path):
        """Lines 220-223: group chat with no display_name resolves member names."""
        from web_server import get_recent_chats
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        # Clear group display name
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE chat SET display_name = '' WHERE chat_identifier = 'chat123'")
        # Add messages to the group chat
        apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, associated_message_type) "
            "VALUES (10, 'g-1', 'hi group', 0, ?, 1, 0, 0)", (apple_ns + 100 * 1_000_000_000,)
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (2, 10)")
        conn.commit()
        conn.close()
        contacts = {"5551234567": "John", "5559876543": "Jane"}
        chats = get_recent_chats(db_path, contacts)
        group = [c for c in chats if c["chat_identifier"] == "chat123"][0]
        # Display name should be resolved from members
        assert "John" in group["display_name"]
        assert "Jane" in group["display_name"]

    def test_individual_chat_no_display_name(self, tmp_path):
        """Lines 224-225: individual chat with no display_name resolves from contacts."""
        from web_server import get_recent_chats
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE chat SET display_name = '' WHERE chat_identifier = '+15551234567'")
        conn.commit()
        conn.close()
        contacts = {"5551234567": "John Smith"}
        chats = get_recent_chats(db_path, contacts)
        ind = [c for c in chats if c["chat_identifier"] == "+15551234567"][0]
        assert ind["display_name"] == "John Smith"


class TestGetChatMessagesEdgeCases:
    def test_message_with_attributedBody(self, tmp_path):
        """Line 396: attributedBody fallback when text is None."""
        from web_server import get_chat_messages
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
        # Insert message with no text but with attributedBody — use a dummy that won't parse
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, attributedBody) VALUES (20, 'ab-1', NULL, 0, ?, 1, 0, 0, X'00')",
            (apple_ns + 20 * 1_000_000_000,)
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 20)")
        conn.commit()
        conn.close()
        # Should not crash — message may be skipped if attributedBody can't be parsed
        messages = get_chat_messages(db_path, "+15551234567", {})
        # Original 3 messages should still be there
        assert len(messages) >= 3

    def test_message_with_attachments(self, tmp_path):
        """Line 399-400: message with cache_has_attachments."""
        from web_server import get_chat_messages
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8")
        conn = sqlite3.connect(db_path)
        apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, "
            "cache_has_attachments, item_type, associated_message_type) "
            "VALUES (21, 'att-1', 'see pic', 0, ?, 1, 1, 0, 0)",
            (apple_ns + 21 * 1_000_000_000,)
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 21)")
        conn.execute(
            "INSERT INTO attachment (ROWID, filename, mime_type, transfer_name, total_bytes) "
            "VALUES (1, ?, 'image/jpeg', 'photo.jpg', 1024)", (str(photo),)
        )
        conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (21, 1)")
        conn.commit()
        conn.close()
        messages = get_chat_messages(db_path, "+15551234567", {})
        att_msg = [m for m in messages if m["text"] == "see pic"][0]
        assert len(att_msg["attachments"]) == 1

    def test_message_with_zero_date(self, tmp_path):
        """Line 405-406: message with date = 0."""
        from web_server import get_chat_messages
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, associated_message_type) "
            "VALUES (22, 'zero-date', 'no date', 0, 0, 1, 0, 0)"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 22)")
        conn.commit()
        conn.close()
        messages = get_chat_messages(db_path, "+15551234567", {})
        zero_msg = [m for m in messages if m["text"] == "no date"][0]
        assert zero_msg["timestamp"] == ""

    def test_message_with_delivery_status(self, tmp_path):
        """Lines 416-421: status for sent messages."""
        from web_server import get_chat_messages
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
        # Sent, delivered
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, date_delivered, date_read) "
            "VALUES (23, 'del-1', 'delivered msg', 1, ?, 1, 0, 0, ?, 0)",
            (apple_ns + 23 * 1_000_000_000, apple_ns + 24 * 1_000_000_000)
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 23)")
        # Sent, read
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, date_delivered, date_read) "
            "VALUES (24, 'read-1', 'read msg', 1, ?, 1, 0, 0, ?, ?)",
            (apple_ns + 25 * 1_000_000_000, apple_ns + 26 * 1_000_000_000, apple_ns + 27 * 1_000_000_000)
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 24)")
        conn.commit()
        conn.close()
        messages = get_chat_messages(db_path, "+15551234567", {})
        del_msg = [m for m in messages if m["text"] == "delivered msg"][0]
        assert del_msg["status"] == "delivered"
        read_msg = [m for m in messages if m["text"] == "read msg"][0]
        assert read_msg["status"] == "read"

    def test_null_text_null_body_skipped(self, tmp_path):
        """Line 407-408: messages with no text, no body, no attachments are skipped."""
        from web_server import get_chat_messages
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, attributedBody) "
            "VALUES (25, 'empty-1', NULL, 0, ?, 1, 0, 0, NULL)",
            (apple_ns + 30 * 1_000_000_000,)
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 25)")
        conn.commit()
        conn.close()
        messages = get_chat_messages(db_path, "+15551234567", {})
        # The empty message should be skipped
        guids = [m.get("guid") for m in messages]
        assert "empty-1" not in str(messages)

    def test_reply_to_from_me(self, tmp_path):
        """Line 429,432: reply_to with reply_to_from_me and resolve sender."""
        from web_server import get_chat_messages
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        conn = sqlite3.connect(db_path)
        apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
        # Original message from me (handle_id=0 since it's from me)
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, associated_message_type) "
            "VALUES (30, 'orig-me', 'my original', 1, ?, 0, 0, 0)",
            (apple_ns + 30 * 1_000_000_000,)
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 30)")
        # Reply to it from someone else
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, is_from_me, date, handle_id, item_type, "
            "associated_message_type, thread_originator_guid) "
            "VALUES (31, 'reply-me', 'replying to you', 0, ?, 1, 0, 0, 'orig-me')",
            (apple_ns + 31 * 1_000_000_000,)
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 31)")
        conn.commit()
        conn.close()
        messages = get_chat_messages(db_path, "+15551234567", {})
        reply = [m for m in messages if m["text"] == "replying to you"][0]
        assert reply["reply_to"] is not None
        assert reply["reply_to"]["is_from_me"] is True
        assert reply["reply_to"]["sender"] == "me"


class TestCustomEmojiFromBody:
    def test_extract_from_attributed_body(self):
        """Lines 269-271: fallback to attributedBody for emoji extraction."""
        from web_server import _extract_custom_emoji
        # When text is None but attributed_body has content, it should try to parse
        # We pass a dummy body that won't parse — should return None gracefully
        assert _extract_custom_emoji(None, b"\x00\x01\x02") is None


class TestAttachmentEndpointEdgeCases:
    def _make_client(self, tmp_path):
        from web_server import create_app
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        config = Config(
            imessage=IMessageConfig(db_path=db_path),
            app=AppConfig(state_db=str(tmp_path / "state.db"), temp_dir=str(tmp_path / "tmp")),
            web=WebConfig(password="testpass"),
        )
        with patch("app_core.IMessageReader"):
            core = AppCore(config)
        app = create_app(core)
        from fastapi.testclient import TestClient
        return TestClient(app), core

    def _login(self, client):
        client.post("/login", data={"password": "testpass"})

    def test_serve_valid_attachment(self, tmp_path):
        """Lines 635-655: successful attachment serving."""
        from web_server import _attachment_registry
        client, _ = self._make_client(tmp_path)
        self._login(client)
        f = tmp_path / "test.txt"
        f.write_text("hello")
        token = "test-token-123"
        _attachment_registry[token] = (str(f), time.time())
        resp = client.get(f"/api/attachments/{token}/test.txt")
        assert resp.status_code == 200
        assert resp.text == "hello"
        del _attachment_registry[token]

    def test_serve_expired_attachment(self, tmp_path):
        """Line 636-638: expired attachment returns 404."""
        from web_server import _attachment_registry
        client, _ = self._make_client(tmp_path)
        self._login(client)
        f = tmp_path / "old.txt"
        f.write_text("old data")
        token = "expired-tok"
        _attachment_registry[token] = (str(f), time.time() - 7200)
        resp = client.get(f"/api/attachments/{token}/old.txt")
        assert resp.status_code == 404

    def test_serve_missing_file_attachment(self, tmp_path):
        """Line 639-640: file doesn't exist on disk."""
        from web_server import _attachment_registry
        client, _ = self._make_client(tmp_path)
        self._login(client)
        token = "gone-tok"
        _attachment_registry[token] = ("/nonexistent/file.txt", time.time())
        resp = client.get(f"/api/attachments/{token}/file.txt")
        assert resp.status_code == 404
        del _attachment_registry[token]

    def test_serve_heic_attachment(self, tmp_path):
        """Lines 642-653: HEIC conversion path."""
        from web_server import _attachment_registry
        client, core = self._make_client(tmp_path)
        self._login(client)
        heic = tmp_path / "photo.heic"
        heic.write_bytes(b"\x00\x00\x00\x1cftyp")  # fake HEIC header
        token = "heic-tok"
        _attachment_registry[token] = (str(heic), time.time())
        # sips will fail on fake data, so it should fall back to serving the original
        resp = client.get(f"/api/attachments/{token}/photo.heic")
        assert resp.status_code == 200
        del _attachment_registry[token]


class TestWebSocketEndpoint:
    def _make_client(self, tmp_path):
        from web_server import create_app
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        config = Config(
            imessage=IMessageConfig(db_path=db_path),
            app=AppConfig(state_db=str(tmp_path / "state.db"), temp_dir=str(tmp_path / "tmp")),
            web=WebConfig(password="testpass"),
        )
        with patch("app_core.IMessageReader"):
            core = AppCore(config)
        app = create_app(core)
        from fastapi.testclient import TestClient
        return TestClient(app), core

    def _get_session(self, client):
        from web_server import _create_session
        return _create_session()

    def test_ws_auth_failure(self, tmp_path):
        """Lines 660-666: WebSocket rejected without valid token."""
        client, _ = self._make_client(tmp_path)
        with client.websocket_connect("/ws?token=invalid") as ws:
            data = json.loads(ws.receive_text())
            assert data["type"] == "error"
            assert data["message"] == "Unauthorized"

    def test_ws_auth_success_and_send(self, tmp_path):
        """Lines 678-694: WebSocket connect and send a message."""
        client, core = self._make_client(tmp_path)
        token = self._get_session(client)
        core.sender = MagicMock()
        core.sender.send_text.return_value = True
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_text(json.dumps({
                "type": "send",
                "chat_identifier": "+15551234567",
                "chat_style": 45,
                "text": "hello from ws",
            }))
            # Give it a moment then close
        core.sender.send_text.assert_called_once_with("+15551234567", 45, "hello from ws")

    def test_ws_unknown_chat_ignored(self, tmp_path):
        """Line 689-690: message to unknown chat is ignored."""
        client, core = self._make_client(tmp_path)
        token = self._get_session(client)
        core.sender = MagicMock()
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_text(json.dumps({
                "type": "send",
                "chat_identifier": "unknown-chat-id",
                "chat_style": 45,
                "text": "should be ignored",
            }))
        core.sender.send_text.assert_not_called()

    def test_ws_oversized_data_ignored(self, tmp_path):
        """Lines 681-682: oversized raw data is skipped."""
        client, core = self._make_client(tmp_path)
        token = self._get_session(client)
        core.sender = MagicMock()
        # Send data larger than max_msg_len * 2
        huge = "x" * 25000
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_text(huge)
        core.sender.send_text.assert_not_called()

    def test_ws_empty_text_not_sent(self, tmp_path):
        """Line 693: empty text doesn't trigger send."""
        client, core = self._make_client(tmp_path)
        token = self._get_session(client)
        core.sender = MagicMock()
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_text(json.dumps({
                "type": "send",
                "chat_identifier": "+15551234567",
                "chat_style": 45,
                "text": "",
            }))
        core.sender.send_text.assert_not_called()


class TestLoginRateLimit:
    def _make_client(self, tmp_path):
        from web_server import create_app, _login_attempts
        _login_attempts.clear()
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        config = Config(
            imessage=IMessageConfig(db_path=db_path),
            app=AppConfig(state_db=str(tmp_path / "state.db"), temp_dir=str(tmp_path / "tmp")),
            web=WebConfig(password="testpass"),
        )
        with patch("app_core.IMessageReader"):
            core = AppCore(config)
        app = create_app(core)
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_rate_limit_after_5_failures(self, tmp_path):
        client = self._make_client(tmp_path)
        for _ in range(5):
            resp = client.post("/login", data={"password": "wrong"})
            assert resp.status_code == 401
        # 6th attempt should be rate limited
        resp = client.post("/login", data={"password": "wrong"})
        assert resp.status_code == 429
        assert "Too many attempts" in resp.text

    def test_successful_login_resets_attempts(self, tmp_path):
        client = self._make_client(tmp_path)
        for _ in range(3):
            client.post("/login", data={"password": "wrong"})
        # Successful login should clear attempts
        resp = client.post("/login", data={"password": "testpass"}, follow_redirects=False)
        assert resp.status_code == 303


class TestNoPasswordMode:
    def test_login_redirects_when_no_password(self, tmp_path):
        from web_server import create_app
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        config = Config(
            imessage=IMessageConfig(db_path=db_path),
            app=AppConfig(state_db=str(tmp_path / "state.db"), temp_dir=str(tmp_path / "tmp")),
            web=WebConfig(password=""),
        )
        with patch("app_core.IMessageReader"):
            core = AppCore(config)
        app = create_app(core)
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/login", follow_redirects=False)
        assert resp.status_code == 303

    def test_api_accessible_without_password(self, tmp_path):
        from web_server import create_app
        db_path = _create_test_chatdb(str(tmp_path / "chat.db"))
        config = Config(
            imessage=IMessageConfig(db_path=db_path),
            app=AppConfig(state_db=str(tmp_path / "state.db"), temp_dir=str(tmp_path / "tmp")),
            web=WebConfig(password=""),
        )
        with patch("app_core.IMessageReader"):
            core = AppCore(config)
        app = create_app(core)
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/chats")
        assert resp.status_code == 200
