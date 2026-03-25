from unittest.mock import patch, MagicMock
import subprocess

from imessage_sender import IMessageSender


@patch("imessage_sender.subprocess.run")
def test_send_text_1on1(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_text("+15551234567", 45, "Hello there")
    assert result is True
    script = mock_run.call_args[0][0][2]  # osascript -e <script>
    assert 'buddy "+15551234567"' in script
    assert '"Hello there"' in script


@patch("imessage_sender.subprocess.run")
def test_send_text_group(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_text("chat123456", 43, "Hi group")
    assert result is True
    script = mock_run.call_args[0][0][2]
    assert 'chat id "iMessage;+;chat123456"' in script
    assert '"Hi group"' in script


@patch("imessage_sender.subprocess.run")
def test_send_text_escapes_quotes(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    sender.send_text("+15551234567", 45, 'He said "hello"')
    script = mock_run.call_args[0][0][2]
    assert r'He said \"hello\"' in script


@patch("imessage_sender.subprocess.run")
def test_send_text_escapes_backslashes(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    sender.send_text("+15551234567", 45, "path\\to\\file")
    script = mock_run.call_args[0][0][2]
    assert "path\\\\to\\\\file" in script


@patch("imessage_sender.subprocess.run")
def test_send_file_1on1(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_file("+15551234567", 45, "/tmp/photo.jpg")
    assert result is True
    script = mock_run.call_args[0][0][2]
    assert 'POSIX file "/tmp/photo.jpg"' in script
    assert 'buddy "+15551234567"' in script


@patch("imessage_sender.subprocess.run")
def test_send_file_group(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    sender = IMessageSender()
    result = sender.send_file("chat999", 43, "/tmp/doc.pdf")
    assert result is True
    script = mock_run.call_args[0][0][2]
    assert 'POSIX file "/tmp/doc.pdf"' in script
    assert 'chat id "iMessage;+;chat999"' in script


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
