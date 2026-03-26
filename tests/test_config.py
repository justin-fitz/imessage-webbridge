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
imessage:
  db_path: "~/Library/Messages/chat.db"
  poll_interval_seconds: 5

web:
  host: "0.0.0.0"
  port: 9090
""")
    cfg = load_config(path)
    assert cfg.imessage.poll_interval_seconds == 5
    assert "~" not in cfg.imessage.db_path  # expanded
    assert cfg.web.host == "0.0.0.0"
    assert cfg.web.port == 9090


def test_load_minimal_config(config_file):
    path = config_file("""
imessage:
  db_path: "~/Library/Messages/chat.db"
""")
    cfg = load_config(path)
    assert cfg.imessage.poll_interval_seconds == 2  # default
    assert cfg.web.host == "127.0.0.1"  # default
    assert cfg.web.port == 8080  # default


def test_db_path_expanded(config_file):
    path = config_file("""
imessage:
  db_path: "~/some/path.db"
""")
    cfg = load_config(path)
    assert cfg.imessage.db_path == os.path.expanduser("~/some/path.db")


def test_defaults_applied(config_file):
    path = config_file("""
web:
  port: 9999
""")
    cfg = load_config(path)
    assert cfg.web.port == 9999
    assert cfg.web.host == "127.0.0.1"
    assert cfg.imessage.poll_interval_seconds == 2
