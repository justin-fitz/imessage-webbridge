from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class BridgeAttachment:
    filename: str
    mime_type: str | None
    transfer_name: str
    total_bytes: int


@dataclass
class BridgeMessage:
    rowid: int
    text: str | None
    is_from_me: bool
    sender_id: str
    chat_identifier: str
    chat_display_name: str
    chat_style: int  # 43 = group, 45 = 1-on-1
    timestamp: datetime
    attachments: list[BridgeAttachment] = field(default_factory=list)
