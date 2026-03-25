from datetime import datetime, timezone

from models import BridgeAttachment, BridgeMessage


def test_bridge_message_defaults():
    msg = BridgeMessage(
        rowid=1,
        text="hello",
        is_from_me=False,
        sender_id="+15551234567",
        chat_identifier="+15551234567",
        chat_display_name="",
        chat_style=45,
        timestamp=datetime.now(timezone.utc),
    )
    assert msg.attachments == []
    assert msg.text == "hello"
    assert msg.is_from_me is False


def test_bridge_message_with_attachments():
    att = BridgeAttachment(
        filename="/path/to/photo.jpg",
        mime_type="image/jpeg",
        transfer_name="photo.jpg",
        total_bytes=1024,
    )
    msg = BridgeMessage(
        rowid=2,
        text=None,
        is_from_me=True,
        sender_id="me",
        chat_identifier="chat123",
        chat_display_name="Group",
        chat_style=43,
        timestamp=datetime.now(timezone.utc),
        attachments=[att],
    )
    assert len(msg.attachments) == 1
    assert msg.attachments[0].mime_type == "image/jpeg"
    assert msg.text is None
