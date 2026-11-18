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
    # Addressed via `participant of service`, not the legacy `buddy` form
    # (which fails for SMS-only numbers on macOS 26).
    assert 'participant "+15551234567" of targetService' in script
    assert "service type = iMessage" in script
    # Text passed via env var, not in script
    assert "IMSG_TEXT" in script
    env = mock_run.call_args[1]["env"]
    assert env["IMSG_TEXT"] == "Hello there"


@patch("imessage_sender.subprocess.run")
def test_send_text_1on1_sms(mock_run):
    """SMS-only recipients must be addressed on the SMS service."""
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_text("+13135450117", 45, "hi", service="SMS")
    assert result is True
    script = mock_run.call_args[0][0][2]
    assert "service type = SMS" in script
    assert 'participant "+13135450117" of targetService' in script
    assert "buddy" not in script


@patch("imessage_sender.subprocess.run")
def test_send_text_unknown_service_defaults_imessage(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    sender.send_text("+15551234567", 45, "hi", service="bogus")
    script = mock_run.call_args[0][0][2]
    assert "service type = iMessage" in script


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
    # File path MUST be a compile-time literal (macOS 26 drops the attachment
    # otherwise — see _as_str), and the recipient is addressed via participant.
    assert 'POSIX file "/tmp/photo.jpg"' in script
    assert 'participant "+15551234567" of targetService' in script
    assert "service type = iMessage" in script


@patch("imessage_sender.subprocess.run")
def test_send_file_group(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_file("abc999", 43, "/tmp/doc.pdf")
    assert result is True
    script = mock_run.call_args[0][0][2]
    # File path is a compile-time literal (see test_send_file_1on1).
    assert 'POSIX file "/tmp/doc.pdf"' in script
    assert 'chat id "any;+;abc999"' in script


@patch("imessage_sender.subprocess.run")
def test_send_file_path_injection_blocked(mock_run):
    """Malicious file paths must not escape the AppleScript string."""
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    evil = '/tmp/x.jpg" & do shell script "touch /tmp/pwn" & "'
    result = sender.send_file("+15551234567", 45, evil)
    assert result is True
    script = mock_run.call_args[0][0][2]
    # The raw (unescaped) payload must not appear — quotes are backslash-escaped
    # so the injection stays inside the AppleScript string literal.
    assert evil not in script
    assert '\\"' in script


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


@patch("imessage_sender.subprocess.run")
def test_send_text_failure_records_error(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stderr="execution error: Messages got an error (-1700)", stdout="")
    sender = IMessageSender()
    assert sender.send_text("+15551234567", 45, "hi", service="SMS") is False
    assert "-1700" in sender.last_error


@patch("imessage_sender.subprocess.run")
def test_send_text_falls_back_to_chat_guid(mock_run):
    """If addressing the participant fails, retry via the existing thread's chat id."""
    mock_run.side_effect = [MagicMock(returncode=1, stderr="bad service", stdout=""),
                            MagicMock(returncode=0, stderr="", stdout="")]
    sender = IMessageSender()
    assert sender.send_text("+15551234567", 45, "hi", service="RCS", chat_guid="RCS;-;+15551234567") is True
    script = mock_run.call_args_list[1][0][0][2]
    assert 'chat id "RCS;-;+15551234567"' in script
    assert mock_run.call_args_list[1][1]["env"]["IMSG_TEXT"] == "hi"


@patch("imessage_sender.subprocess.run")
def test_send_text_fallback_failure_reports_both(mock_run):
    mock_run.side_effect = [MagicMock(returncode=1, stderr="first", stdout=""),
                            MagicMock(returncode=1, stderr="second", stdout="")]
    sender = IMessageSender()
    assert sender.send_text("+15551234567", 45, "hi", chat_guid="SMS;-;+15551234567") is False
    assert "first" in sender.last_error and "second" in sender.last_error


@patch("imessage_sender.subprocess.run")
def test_send_text_rejects_malformed_chat_guid(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stderr="nope", stdout="")
    sender = IMessageSender()
    assert sender.send_text("+15551234567", 45, "hi", chat_guid='x" & do shell script "evil') is False
    assert mock_run.call_count == 1  # no fallback attempted


@patch("imessage_sender.subprocess.run")
def test_send_text_timeout_is_a_failure(mock_run):
    import subprocess
    mock_run.side_effect = subprocess.TimeoutExpired("osascript", 10)
    sender = IMessageSender()
    assert sender.send_text("+15551234567", 45, "hi") is False
    assert "did not respond" in sender.last_error
