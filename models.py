from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ChatAttachment:
    filename: str
    mime_type: str | None
    transfer_name: str
    total_bytes: int


@dataclass
class ChatMessage:
    rowid: int
    text: str | None
    is_from_me: bool
    sender_id: str
    chat_identifier: str
    chat_display_name: str
    chat_style: int  # 43 = group, 45 = 1-on-1
    timestamp: datetime
    attachments: list[ChatAttachment] = field(default_factory=list)
