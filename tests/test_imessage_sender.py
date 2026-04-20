from unittest.mock import patch, MagicMock

from imessage_sender import IMessageSender, _validate_identifier


class TestValidateIdentifier:
    def test_valid_phone(self):
        assert _validate_identifier("+15551234567", 45) is True

    def test_valid_email(self):
        assert _validate_identifier("user@example.com", 45) is True

    def test_valid_group_id_hex(self):
        assert _validate_identifier("d720dea5fcf64c33975a748ef6410622", 43) is True

    def test_valid_group_id_chat_prefix(self):
        assert _validate_identifier("chat242846992434562406", 43) is True

    def test_invalid_injection_buddy(self):
        assert _validate_identifier('" of targetService\ndo shell script "evil', 45) is False

    def test_invalid_injection_group(self):
        assert _validate_identifier("abc;do shell script", 43) is False

    def test_empty(self):
        assert _validate_identifier("", 45) is False

    def test_spaces_rejected(self):
        assert _validate_identifier("hello world", 45) is False


@patch("imessage_sender.subprocess.run")
def test_send_text_1on1(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_text("+15551234567", 45, "Hello there")
    assert result is True
    script = mock_run.call_args[0][0][2]
    assert 'buddy "+15551234567"' in script
    # Text passed via env var, not in script
    assert "IMSG_TEXT" in script
    env = mock_run.call_args[1]["env"]
    assert env["IMSG_TEXT"] == "Hello there"


@patch("imessage_sender.subprocess.run")
def test_send_text_group(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_text("abc123def456", 43, "Hi group")
    assert result is True
    script = mock_run.call_args[0][0][2]
    assert 'chat id "any;+;abc123def456"' in script
    env = mock_run.call_args[1]["env"]
    assert env["IMSG_TEXT"] == "Hi group"


@patch("imessage_sender.subprocess.run")
def test_send_text_injection_safe(mock_run):
    """Text with quotes/special chars is safe because it's passed via env var."""
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    sender.send_text("+15551234567", 45, 'He said "hello" and \\backslash')
    env = mock_run.call_args[1]["env"]
    assert env["IMSG_TEXT"] == 'He said "hello" and \\backslash'
    # The actual script should NOT contain the message text
    script = mock_run.call_args[0][0][2]
    assert "hello" not in script


@patch("imessage_sender.subprocess.run")
def test_send_text_injection_blocked(mock_run):
    """Malicious chat_identifier should be rejected before reaching osascript."""
    sender = IMessageSender()
    result = sender.send_text('" ; do shell script "evil"', 45, "test")
    assert result is False
    mock_run.assert_not_called()


@patch("imessage_sender.subprocess.run")
def test_send_file_1on1(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_file("+15551234567", 45, "/tmp/photo.jpg")
    assert result is True
    script = mock_run.call_args[0][0][2]
    # File path is passed via env var, not interpolated into the script.
    assert "/tmp/photo.jpg" not in script
    assert 'system attribute "IMSG_FILE"' in script
    assert 'buddy "+15551234567"' in script
    assert mock_run.call_args[1]["env"]["IMSG_FILE"] == "/tmp/photo.jpg"


@patch("imessage_sender.subprocess.run")
def test_send_file_group(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_file("abc999", 43, "/tmp/doc.pdf")
    assert result is True
    script = mock_run.call_args[0][0][2]
    assert "/tmp/doc.pdf" not in script
    assert 'system attribute "IMSG_FILE"' in script
    assert 'chat id "any;+;abc999"' in script
    assert mock_run.call_args[1]["env"]["IMSG_FILE"] == "/tmp/doc.pdf"


@patch("imessage_sender.subprocess.run")
def test_send_file_path_injection_blocked(mock_run):
    """Malicious file paths must not escape the AppleScript string."""
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    evil = '/tmp/x.jpg" & do shell script "touch /tmp/pwn" & "'
    result = sender.send_file("+15551234567", 45, evil)
    assert result is True
    script = mock_run.call_args[0][0][2]
    assert evil not in script
    assert "do shell script" not in script


@patch("imessage_sender.subprocess.run")
def test_send_returns_false_on_error(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stderr="error msg")
    sender = IMessageSender()
    result = sender.send_text("+15551234567", 45, "test")
    assert result is False


@patch("imessage_sender.subprocess.run")
def test_applescript_called_with_timeout(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    sender.send_text("+15551234567", 45, "test")
    assert mock_run.call_args[1]["timeout"] == 10
