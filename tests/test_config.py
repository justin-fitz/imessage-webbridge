import os
import tempfile

import pytest

from config import load_config


@pytest.fixture
def config_file(tmp_path):
    def _write(content):
        p = tmp_path / "config.yaml"
        p.write_text(content)
        return str(p)
    return _write


def test_load_full_config(config_file):
    path = config_file("""
discord:
  bot_token: "test-token-123"
  guild_id: 999888777
  category_name: "TestCategory"

imessage:
  db_path: "~/Library/Messages/chat.db"
  poll_interval_seconds: 5

bridge:
  allowed_chats:
    - "+15551234567"
  state_db: "db/bridge.db"
  temp_dir: "tmp/"
""")
    cfg = load_config(path)
    assert cfg.discord.bot_token == "test-token-123"
    assert cfg.discord.guild_id == 999888777
    assert cfg.discord.category_name == "TestCategory"
    assert cfg.imessage.poll_interval_seconds == 5
    assert "~" not in cfg.imessage.db_path  # expanded
    assert cfg.bridge.allowed_chats == ["+15551234567"]


def test_load_minimal_config(config_file):
    path = config_file("""
discord:
  bot_token: "tok"
  guild_id: 123
""")
    cfg = load_config(path)
    assert cfg.discord.bot_token == "tok"
    assert cfg.discord.category_name == "iMessage"  # default
    assert cfg.imessage.poll_interval_seconds == 2  # default
    assert cfg.bridge.allowed_chats == []  # default


def test_guild_id_coerced_to_int(config_file):
    path = config_file("""
discord:
  bot_token: "tok"
  guild_id: "456"
""")
    cfg = load_config(path)
    assert cfg.discord.guild_id == 456
    assert isinstance(cfg.discord.guild_id, int)


def test_db_path_expanded(config_file):
    path = config_file("""
discord:
  bot_token: "tok"
  guild_id: 1
imessage:
  db_path: "~/some/path.db"
""")
    cfg = load_config(path)
    assert cfg.imessage.db_path == os.path.expanduser("~/some/path.db")
