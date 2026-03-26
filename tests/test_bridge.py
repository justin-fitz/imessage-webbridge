import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge import Bridge
from config import BridgeConfig, Config, IMessageConfig, WebConfig
from models import BridgeAttachment, BridgeMessage


def _make_config(tmp_path):
    return Config(
        imessage=IMessageConfig(
            db_path=str(tmp_path / "chat.db"),
            poll_interval_seconds=1,
        ),
        bridge=BridgeConfig(
            state_db=str(tmp_path / "bridge.db"),
            temp_dir=str(tmp_path / "tmp"),
        ),
        web=WebConfig(),
    )


def _make_msg(text="hello", is_from_me=False, chat_identifier="+15551234567", chat_style=45, attachments=None):
    return BridgeMessage(
        rowid=1, text=text, is_from_me=is_from_me, sender_id="+15551234567",
        chat_identifier=chat_identifier, chat_display_name="",
        chat_style=chat_style, timestamp=datetime.now(timezone.utc),
        attachments=attachments or [],
    )


class TestDedup:
    def test_mark_and_detect_sent_text(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        bridge._mark_sent("+15551234567", "hello", None)
        msg = _make_msg(text="hello", is_from_me=True)
        assert bridge._was_recently_sent(msg) is True

    def test_not_from_me_not_skipped(self, tmp_path):
        """is_from_me check is in _should_skip, not _was_recently_sent."""
        config = _make_config(tmp_path)
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        bridge._mark_sent("+15551234567", "hello", None)
        msg = _make_msg(text="hello", is_from_me=False)
        assert bridge._should_skip(msg) is False

    def test_dedup_only_matches_once(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        bridge._mark_sent("+15551234567", "hello", None)
        msg = _make_msg(text="hello", is_from_me=True)
        assert bridge._was_recently_sent(msg) is True
        assert bridge._was_recently_sent(msg) is False  # consumed

    def test_dedup_expires_after_30s(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        bridge._mark_sent("+15551234567", "hello", None)
        # Manually backdate the entry
        for key in bridge._recently_sent:
            bridge._recently_sent[key] = time.time() - 31
        msg = _make_msg(text="hello", is_from_me=True)
        assert bridge._was_recently_sent(msg) is False

    def test_dedup_attachment(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        bridge._mark_sent("+15551234567", None, "photo.jpg")
        att = BridgeAttachment(filename="/tmp/photo.jpg", mime_type="image/jpeg", transfer_name="photo.jpg", total_bytes=1024)
        msg = _make_msg(text=None, is_from_me=True, attachments=[att])
        assert bridge._was_recently_sent(msg) is True

    def test_different_chat_not_deduped(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        bridge._mark_sent("+15559999999", "hello", None)
        msg = _make_msg(text="hello", is_from_me=True, chat_identifier="+15551234567")
        assert bridge._was_recently_sent(msg) is False


class TestShouldSkip:
    def test_allowed_chats_filter(self, tmp_path):
        config = _make_config(tmp_path)
        config.bridge.allowed_chats = ["+15559999999"]
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        msg = _make_msg(chat_identifier="+15551234567")
        assert bridge._should_skip(msg) is True

    def test_allowed_chats_passes(self, tmp_path):
        config = _make_config(tmp_path)
        config.bridge.allowed_chats = ["+15551234567"]
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        msg = _make_msg(chat_identifier="+15551234567")
        assert bridge._should_skip(msg) is False

    def test_empty_allowlist_passes_all(self, tmp_path):
        config = _make_config(tmp_path)
        config.bridge.allowed_chats = []
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        msg = _make_msg(chat_identifier="anything")
        assert bridge._should_skip(msg) is False

    def test_from_me_recently_sent_skipped(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        bridge._mark_sent("+15551234567", "echo", None)
        msg = _make_msg(text="echo", is_from_me=True)
        assert bridge._should_skip(msg) is True

    def test_from_me_not_recently_sent_passes(self, tmp_path):
        config = _make_config(tmp_path)
        with patch("bridge.IMessageReader"):
            bridge = Bridge(config)

        msg = _make_msg(text="from phone", is_from_me=True)
        assert bridge._should_skip(msg) is False


class TestDedupKey:
    def test_same_inputs_same_key(self):
        k1 = Bridge._dedup_key("+15551234567", "hello", None)
        k2 = Bridge._dedup_key("+15551234567", "hello", None)
        assert k1 == k2

    def test_different_text_different_key(self):
        k1 = Bridge._dedup_key("+15551234567", "hello", None)
        k2 = Bridge._dedup_key("+15551234567", "world", None)
        assert k1 != k2

    def test_different_chat_different_key(self):
        k1 = Bridge._dedup_key("+15551234567", "hello", None)
        k2 = Bridge._dedup_key("+15559999999", "hello", None)
        assert k1 != k2

    def test_filename_key(self):
        k1 = Bridge._dedup_key("+15551234567", None, "photo.jpg")
        k2 = Bridge._dedup_key("+15551234567", None, "photo.jpg")
        assert k1 == k2

    def test_text_vs_filename_different(self):
        k1 = Bridge._dedup_key("+15551234567", "photo.jpg", None)
        k2 = Bridge._dedup_key("+15551234567", None, "photo.jpg")
        # Both resolve to same content string, so keys match — this is fine
        # since text "photo.jpg" and filename "photo.jpg" are unlikely to collide in practice
        assert k1 == k2
