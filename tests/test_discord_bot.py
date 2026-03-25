from datetime import datetime, timezone

from discord_bot import make_channel_name, sanitize_channel_name
from models import BridgeMessage


def test_sanitize_basic():
    assert sanitize_channel_name("John Smith") == "john-smith"


def test_sanitize_special_chars():
    assert sanitize_channel_name("Mom 🍕❤️") == "mom"


def test_sanitize_multiple_spaces():
    assert sanitize_channel_name("  too   many   spaces  ") == "too-many-spaces"


def test_sanitize_empty():
    assert sanitize_channel_name("") == "unknown"


def test_sanitize_emoji_only():
    assert sanitize_channel_name("🎉🎊") == "unknown"


def test_sanitize_long_name():
    name = "a" * 200
    assert len(sanitize_channel_name(name)) <= 90


def _make_msg(chat_style=45, chat_identifier="+15551234567", chat_display_name=""):
    return BridgeMessage(
        rowid=1, text="hi", is_from_me=False, sender_id="+15551234567",
        chat_identifier=chat_identifier, chat_display_name=chat_display_name,
        chat_style=chat_style, timestamp=datetime.now(timezone.utc),
    )


def test_channel_name_1on1():
    msg = _make_msg(chat_style=45, chat_identifier="+15551234567")
    assert make_channel_name(msg) == "im-15551234567"


def test_channel_name_1on1_no_plus():
    msg = _make_msg(chat_style=45, chat_identifier="15551234567")
    assert make_channel_name(msg) == "im-15551234567"


def test_channel_name_group_with_name():
    msg = _make_msg(chat_style=43, chat_display_name="Pizza Night")
    assert make_channel_name(msg) == "im-pizza-night"


def test_channel_name_group_no_name():
    msg = _make_msg(chat_style=43, chat_identifier="chat483140163795272511")
    assert make_channel_name(msg) == "im-group-272511"


def test_channel_name_group_short_identifier():
    msg = _make_msg(chat_style=43, chat_identifier="abc")
    assert make_channel_name(msg) == "im-group-abc"
