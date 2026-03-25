import sqlite3
from datetime import datetime, timezone

import pytest

from channel_map import ChannelMap
from imessage_reader import APPLE_EPOCH_OFFSET, IMessageReader


def _create_mock_chatdb(path):
    """Create a minimal iMessage-like database for testing."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY,
            id TEXT
        );
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY,
            chat_identifier TEXT,
            display_name TEXT,
            style INTEGER
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            is_from_me INTEGER DEFAULT 0,
            date INTEGER,
            handle_id INTEGER,
            cache_has_attachments INTEGER DEFAULT 0,
            item_type INTEGER DEFAULT 0,
            associated_message_type INTEGER DEFAULT 0,
            attributedBody BLOB
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        );
        CREATE TABLE attachment (
            ROWID INTEGER PRIMARY KEY,
            filename TEXT,
            mime_type TEXT,
            transfer_name TEXT,
            total_bytes INTEGER
        );
        CREATE TABLE message_attachment_join (
            message_id INTEGER,
            attachment_id INTEGER
        );

        INSERT INTO handle (ROWID, id) VALUES (1, '+15551234567');
        INSERT INTO chat (ROWID, chat_identifier, display_name, style) VALUES (1, '+15551234567', '', 45);
    """)
    conn.commit()
    return conn


def _insert_message(conn, rowid, text, handle_id=1, chat_id=1, is_from_me=0, has_attachments=0):
    """Insert a test message with a valid Apple epoch timestamp."""
    # Use a fixed timestamp: 2024-01-01 00:00:00 UTC
    apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
    conn.execute(
        "INSERT INTO message (ROWID, text, is_from_me, date, handle_id, cache_has_attachments, item_type, associated_message_type) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
        (rowid, text, is_from_me, apple_ns, handle_id, has_attachments),
    )
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (chat_id, rowid))
    conn.commit()


@pytest.fixture
def mock_db(tmp_path):
    db_path = str(tmp_path / "chat.db")
    state_db_path = str(tmp_path / "bridge.db")
    conn = _create_mock_chatdb(db_path)
    cmap = ChannelMap(state_db_path)
    return conn, db_path, cmap


def test_init_sets_last_rowid_to_zero_on_empty_db(mock_db):
    conn, db_path, cmap = mock_db
    reader = IMessageReader(db_path, cmap)
    assert reader.last_seen_rowid == 0


def test_init_sets_last_rowid_to_max_on_existing_messages(mock_db):
    conn, db_path, cmap = mock_db
    _insert_message(conn, 10, "hello")
    _insert_message(conn, 20, "world")
    reader = IMessageReader(db_path, cmap)
    assert reader.last_seen_rowid == 20


def test_init_restores_last_rowid_from_state(mock_db):
    conn, db_path, cmap = mock_db
    _insert_message(conn, 10, "hello")
    cmap.set_state("last_seen_rowid", "5")
    reader = IMessageReader(db_path, cmap)
    assert reader.last_seen_rowid == 5


def test_poll_returns_new_messages(mock_db):
    conn, db_path, cmap = mock_db
    reader = IMessageReader(db_path, cmap)
    _insert_message(conn, 1, "first message")
    _insert_message(conn, 2, "second message")
    messages = reader.poll()
    assert len(messages) == 2
    assert messages[0].text == "first message"
    assert messages[1].text == "second message"
    assert messages[0].sender_id == "+15551234567"
    assert messages[0].chat_style == 45


def test_poll_only_returns_unseen(mock_db):
    conn, db_path, cmap = mock_db
    _insert_message(conn, 1, "old")
    reader = IMessageReader(db_path, cmap)
    # Now reader.last_seen_rowid == 1
    _insert_message(conn, 2, "new")
    messages = reader.poll()
    assert len(messages) == 1
    assert messages[0].text == "new"


def test_poll_updates_last_seen_rowid(mock_db):
    conn, db_path, cmap = mock_db
    reader = IMessageReader(db_path, cmap)
    _insert_message(conn, 5, "msg")
    reader.poll()
    assert reader.last_seen_rowid == 5
    assert cmap.get_state("last_seen_rowid") == "5"


def test_poll_empty_returns_empty(mock_db):
    conn, db_path, cmap = mock_db
    reader = IMessageReader(db_path, cmap)
    assert reader.poll() == []


def test_convert_date():
    # 2024-01-01 00:00:00 UTC
    apple_ns = (1704067200 - APPLE_EPOCH_OFFSET) * 1_000_000_000
    dt = IMessageReader._convert_date(apple_ns)
    assert dt.year == 2024
    assert dt.month == 1
    assert dt.day == 1
    assert dt.tzinfo == timezone.utc


def test_convert_date_none():
    dt = IMessageReader._convert_date(None)
    assert dt.tzinfo == timezone.utc


def test_convert_date_zero():
    dt = IMessageReader._convert_date(0)
    assert dt.tzinfo == timezone.utc


def test_is_from_me_flag(mock_db):
    conn, db_path, cmap = mock_db
    reader = IMessageReader(db_path, cmap)
    _insert_message(conn, 1, "from them", is_from_me=0)
    _insert_message(conn, 2, "from me", is_from_me=1)
    messages = reader.poll()
    assert messages[0].is_from_me is False
    assert messages[1].is_from_me is True


def test_attachments_loaded(mock_db):
    conn, db_path, cmap = mock_db
    reader = IMessageReader(db_path, cmap)
    _insert_message(conn, 1, "see attached", has_attachments=1)
    conn.execute(
        "INSERT INTO attachment (ROWID, filename, mime_type, transfer_name, total_bytes) "
        "VALUES (1, '/tmp/photo.jpg', 'image/jpeg', 'photo.jpg', 2048)"
    )
    conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
    conn.commit()
    messages = reader.poll()
    assert len(messages) == 1
    assert len(messages[0].attachments) == 1
    assert messages[0].attachments[0].mime_type == "image/jpeg"
    assert messages[0].attachments[0].transfer_name == "photo.jpg"
    assert messages[0].attachments[0].total_bytes == 2048


def test_attachments_without_mime_type_skipped(mock_db):
    conn, db_path, cmap = mock_db
    reader = IMessageReader(db_path, cmap)
    _insert_message(conn, 1, "plugin data", has_attachments=1)
    conn.execute(
        "INSERT INTO attachment (ROWID, filename, mime_type, transfer_name, total_bytes) "
        "VALUES (1, '/tmp/payload', NULL, 'payload', 100)"
    )
    conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
    conn.commit()
    messages = reader.poll()
    assert len(messages[0].attachments) == 0
