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


_STRONG_LAN_PW = "a-very-strong-password-123"
_STRONG_PW = "strongpw1"


def test_load_full_config(config_file):
    path = config_file(f"""
imessage:
  db_path: "~/Library/Messages/chat.db"
  poll_interval_seconds: 5

web:
  host: "0.0.0.0"
  port: 9090
  password: "{_STRONG_LAN_PW}"
""")
    cfg = load_config(path)
    assert cfg.imessage.poll_interval_seconds == 5
    assert "~" not in cfg.imessage.db_path  # expanded
    assert cfg.web.host == "0.0.0.0"
    assert cfg.web.port == 9090


def test_load_minimal_config(config_file):
    path = config_file(f"""
imessage:
  db_path: "~/Library/Messages/chat.db"
web:
  password: "{_STRONG_PW}"
""")
    cfg = load_config(path)
    assert cfg.imessage.poll_interval_seconds == 2  # default
    assert cfg.web.host == "127.0.0.1"  # default
    assert cfg.web.port == 8080  # default


def test_db_path_expanded(config_file):
    path = config_file(f"""
imessage:
  db_path: "~/some/path.db"
web:
  password: "{_STRONG_PW}"
""")
    cfg = load_config(path)
    assert cfg.imessage.db_path == os.path.expanduser("~/some/path.db")


def test_defaults_applied(config_file):
    path = config_file(f"""
web:
  port: 9999
  password: "{_STRONG_PW}"
""")
    cfg = load_config(path)
    assert cfg.web.port == 9999
    assert cfg.web.host == "127.0.0.1"
    assert cfg.imessage.poll_interval_seconds == 2


def test_rejects_default_password(config_file):
    path = config_file("""
web:
  password: "CHANGE_ME"
""")
    with pytest.raises(ValueError, match="password"):
        load_config(path)


def test_rejects_empty_password(config_file):
    path = config_file("""
web:
  port: 8080
""")
    with pytest.raises(ValueError, match="password"):
        load_config(path)


def test_rejects_short_password(config_file):
    path = config_file("""
web:
  password: "short12"
""")
    with pytest.raises(ValueError, match="8 characters"):
        load_config(path)


def test_rejects_letters_only_password(config_file):
    path = config_file("""
web:
  password: "alllettersok"
""")
    with pytest.raises(ValueError, match="letters and numbers"):
        load_config(path)


def test_rejects_digits_only_password(config_file):
    path = config_file("""
web:
  password: "12345678"
""")
    with pytest.raises(ValueError, match="letters and numbers"):
        load_config(path)
