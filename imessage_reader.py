import os
import sqlite3
from datetime import datetime, timezone

from channel_map import ChannelMap
from models import ChatAttachment, ChatMessage

APPLE_EPOCH_OFFSET = 978307200

MESSAGES_QUERY = """
SELECT m.ROWID, m.text, m.is_from_me, m.date, m.handle_id,
       m.cache_has_attachments, m.attributedBody,
       h.id AS sender_id,
       c.ROWID AS chat_rowid, c.chat_identifier, c.display_name, c.style
FROM message m
JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
JOIN chat c ON cmj.chat_id = c.ROWID
LEFT JOIN handle h ON m.handle_id = h.ROWID
WHERE m.ROWID > ?
  AND m.item_type = 0
  AND m.associated_message_type = 0
ORDER BY m.ROWID ASC
"""

ATTACHMENTS_QUERY = """
SELECT a.filename, a.mime_type, a.transfer_name, a.total_bytes
FROM attachment a
JOIN message_attachment_join maj ON a.ROWID = maj.attachment_id
WHERE maj.message_id = ?
  AND a.mime_type IS NOT NULL
"""


class IMessageReader:
    def __init__(self, db_path: str, channel_map: ChannelMap):
        self.db_path = db_path
        self.channel_map = channel_map
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self._init_last_rowid()

    def _init_last_rowid(self):
        stored = self.channel_map.get_state("last_seen_rowid")
        if stored:
            self.last_seen_rowid = int(stored)
        else:
            row = self.conn.execute("SELECT MAX(ROWID) as max_id FROM message").fetchone()
            self.last_seen_rowid = row["max_id"] or 0
            self.channel_map.set_state("last_seen_rowid", str(self.last_seen_rowid))

    def poll(self) -> list[ChatMessage]:
        rows = self.conn.execute(MESSAGES_QUERY, (self.last_seen_rowid,)).fetchall()
        messages = []
        for row in rows:
            attachments = []
            if row["cache_has_attachments"]:
                attachments = self._get_attachments(row["ROWID"])

            text = row["text"]
            if text is None and row["attributedBody"]:
                text = self._extract_attributed_text(row["attributedBody"])

            msg = ChatMessage(
                rowid=row["ROWID"],
                text=text,
                is_from_me=bool(row["is_from_me"]),
                sender_id=row["sender_id"] or "me",
                chat_identifier=row["chat_identifier"],
                chat_display_name=row["display_name"] or "",
                chat_style=row["style"],
                timestamp=self._convert_date(row["date"]),
                attachments=attachments,
            )
            messages.append(msg)
            self.last_seen_rowid = row["ROWID"]

        if messages:
            self.channel_map.set_state("last_seen_rowid", str(self.last_seen_rowid))

        return messages

    def _get_attachments(self, message_rowid: int) -> list[ChatAttachment]:
        rows = self.conn.execute(ATTACHMENTS_QUERY, (message_rowid,)).fetchall()
        attachments = []
        for row in rows:
            filename = row["filename"]
            if filename:
                filename = os.path.expanduser(filename)
            attachments.append(
                ChatAttachment(
                    filename=filename or "",
                    mime_type=row["mime_type"],
                    transfer_name=row["transfer_name"] or os.path.basename(filename or ""),
                    total_bytes=row["total_bytes"] or 0,
                )
            )
        return attachments

    @staticmethod
    def _extract_attributed_text(ab: bytes) -> str | None:
        """Extract plain text from NSAttributedString typedstream blob.

        The typedstream stores the string after a ``\\x01+`` marker.
        Short strings (< 128 bytes) use a single-byte length prefix.
        Longer strings start with ``0x81`` followed by a 2-byte
        little-endian length.
        """
        parts = ab.split(b"NSString")
        if len(parts) < 2:
            return None
        after = parts[1]
        idx = after.find(b"\x01+")
        if idx == -1:
            return None
        idx += 2
        length_byte = after[idx]
        if length_byte < 0x80:
            length = length_byte
            idx += 1
        elif length_byte == 0x81:
            # 2-byte little-endian length
            length = after[idx + 1] + after[idx + 2] * 256
            idx += 3
        else:
            return None
        try:
            text = after[idx : idx + length].decode("utf-8")
            return text.lstrip("\x00") or None
        except (UnicodeDecodeError, IndexError):
            return None

    @staticmethod
    def _convert_date(apple_ns: int) -> datetime:
        if apple_ns is None or apple_ns == 0:
            return datetime.now(timezone.utc)
        unix_ts = apple_ns / 1_000_000_000 + APPLE_EPOCH_OFFSET
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
