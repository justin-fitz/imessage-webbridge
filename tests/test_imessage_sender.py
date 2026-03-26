from unittest.mock import patch, MagicMock

from imessage_sender import IMessageSender, _validate_identifier


class TestValidateIdentifier:
    def test_valid_phone(self):
        assert _validate_identifier("+15551234567", 45) is True

    def test_valid_email(self):
        assert _validate_identifier("user@example.com", 45) is True

    def test_valid_group_id(self):
        assert _validate_identifier("d720dea5fcf64c33975a748ef6410622", 43) is True

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
    args = mock_run.call_args[0][0]
    assert args[0] == "osascript"
    assert args[1] == "-l"
    assert args[2] == "JavaScript"
    script = args[4]
    assert '"Hello there"' in script
    assert '"+15551234567"' in script


@patch("imessage_sender.subprocess.run")
def test_send_text_group(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_text("abc123def456", 43, "Hi group")
    assert result is True
    script = mock_run.call_args[0][0][4]
    assert '"iMessage;+;abc123def456"' in script
    assert '"Hi group"' in script


@patch("imessage_sender.subprocess.run")
def test_send_text_escapes_via_json(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    sender.send_text("+15551234567", 45, 'He said "hello" and \\backslash')
    script = mock_run.call_args[0][0][4]
    # json.dumps handles escaping safely
    assert '\\"hello\\"' in script
    assert "\\\\backslash" in script


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
    script = mock_run.call_args[0][0][4]
    assert "Path" in script
    assert '"/tmp/photo.jpg"' in script


@patch("imessage_sender.subprocess.run")
def test_send_file_group(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_file("abc999", 43, "/tmp/doc.pdf")
    assert result is True
    script = mock_run.call_args[0][0][4]
    assert "Path" in script
    assert '"iMessage;+;abc999"' in script


@patch("imessage_sender.subprocess.run")
def test_send_returns_false_on_error(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stderr="error msg")
    sender = IMessageSender()
    result = sender.send_text("+15551234567", 45, "test")
    assert result is False


@patch("imessage_sender.subprocess.run")
def test_jxa_called_with_timeout(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    sender.send_text("+15551234567", 45, "test")
    assert mock_run.call_args[1]["timeout"] == 10
