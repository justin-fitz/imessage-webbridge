import os

import pytest

from channel_map import ChannelMap


@pytest.fixture
def cmap(tmp_path):
    return ChannelMap(str(tmp_path / "test_bridge.db"))


def test_set_and_get_mapping(cmap):
    cmap.set_mapping("+15551234567", 100, "John", 45)
    assert cmap.get_channel_id("+15551234567") == 100


def test_get_nonexistent_returns_none(cmap):
    assert cmap.get_channel_id("nonexistent") is None


def test_reverse_lookup(cmap):
    cmap.set_mapping("+15551234567", 200, "Jane", 45)
    assert cmap.get_chat_identifier(200) == "+15551234567"
    assert cmap.get_chat_style(200) == 45


def test_reverse_lookup_nonexistent(cmap):
    assert cmap.get_chat_identifier(999) is None
    assert cmap.get_chat_style(999) is None


def test_upsert_overwrites(cmap):
    cmap.set_mapping("chat1", 100, "Old Name", 43)
    cmap.set_mapping("chat1", 200, "New Name", 43)
    assert cmap.get_channel_id("chat1") == 200


def test_state_get_set(cmap):
    assert cmap.get_state("last_seen_rowid") is None
    cmap.set_state("last_seen_rowid", "42")
    assert cmap.get_state("last_seen_rowid") == "42"


def test_state_upsert(cmap):
    cmap.set_state("key", "val1")
    cmap.set_state("key", "val2")
    assert cmap.get_state("key") == "val2"


def test_multiple_mappings(cmap):
    cmap.set_mapping("chat_a", 100, "Alice", 45)
    cmap.set_mapping("chat_b", 200, "Bob", 45)
    cmap.set_mapping("chat_c", 300, "Group", 43)
    assert cmap.get_channel_id("chat_a") == 100
    assert cmap.get_channel_id("chat_b") == 200
    assert cmap.get_chat_identifier(300) == "chat_c"
    assert cmap.get_chat_style(300) == 43
