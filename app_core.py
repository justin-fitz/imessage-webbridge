import asyncio
import hashlib
import os
import time
from typing import Protocol

from channel_map import ChannelMap
from config import Config
from imessage_reader import IMessageReader
from imessage_sender import IMessageSender
from models import ChatMessage


class MessageHandler(Protocol):
    async def forward_to_output(self, msg: ChatMessage) -> None: ...


class AppCore:
    def __init__(self, config: Config):
        self.config = config
        self.channel_map = ChannelMap(config.app.state_db)
        self.reader = IMessageReader(config.imessage.db_path, self.channel_map)
        self.sender = IMessageSender()
        self._handlers: list[MessageHandler] = []
        self._recently_sent: dict[str, float] = {}

    def add_handler(self, handler: MessageHandler):
        self._handlers.append(handler)

    async def poll_loop(self):
        while True:
            try:
                messages = self.reader.poll()
                for msg in messages:
                    if self._should_skip(msg):
                        continue
                    for handler in self._handlers:
                        try:
                            await handler.forward_to_output(msg)
                        except Exception as e:
                            print(f"Handler error ({handler.__class__.__name__}): {e}")
            except Exception as e:
                print(f"Poll error: {e}")
            await asyncio.sleep(self.config.imessage.poll_interval_seconds)

    def send_to_imessage(self, chat_identifier: str, chat_style: int, text: str | None = None,
                         file_path: str | None = None) -> tuple[bool, str]:
        """Hand a message to Messages.app. Returns (ok, error); `error` is the
        reason a send was rejected, so the UI can say so instead of showing a
        message as sent when it never left the Mac."""
        service, chat_guid = self._chat_route(chat_identifier, chat_style)
        ok, errors = True, []
        if file_path:
            if not self.sender.send_file(chat_identifier, chat_style, file_path, service=service):
                ok = False
                errors.append(f"attachment: {self.sender.last_error or 'send failed'}")
            self._mark_sent(chat_identifier, None, os.path.basename(file_path))
        if text:
            if not self.sender.send_text(chat_identifier, chat_style, text, service=service, chat_guid=chat_guid):
                ok = False
                errors.append(self.sender.last_error or "send failed")
            self._mark_sent(chat_identifier, text, None)
        return ok, "; ".join(str(e) for e in errors)

    def _chat_route(self, chat_identifier: str, chat_style: int) -> tuple[str | None, str | None]:
        """Resolve (service, chat guid) for a 1:1 chat.

        The service (iMessage/SMS/RCS) matters because an SMS-only recipient
        MUST be addressed on the SMS service or the send silently fails (see
        IMessageSender._send_to_buddy).  The guid is the existing thread's id,
        used as a fallback route.  Group chats (style 43) are addressed by chat
        id and need neither.  Prefer the chat row; fall back to the handle's
        service.
        """
        if chat_style == 43:
            return None, None
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{self.config.imessage.db_path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT service_name, guid FROM chat WHERE chat_identifier=? "
                    "ORDER BY ROWID DESC LIMIT 1",
                    (chat_identifier,),
                ).fetchone()
                service, guid = (row[0] or None, row[1] or None) if row else (None, None)
                if not service:
                    hrow = conn.execute(
                        "SELECT service FROM handle WHERE id=? ORDER BY ROWID DESC LIMIT 1",
                        (chat_identifier,),
                    ).fetchone()
                    if hrow and hrow[0]:
                        service = hrow[0]
                return service, guid
            finally:
                conn.close()
        except Exception as e:
            print(f"Service lookup failed for {chat_identifier}: {e}", flush=True)
        return None, None

    def latest_message_rowid(self) -> int:
        """Highest message ROWID in chat.db, taken just before a send so the
        send's own row can be found afterwards."""
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{self.config.imessage.db_path}?mode=ro", uri=True)
            try:
                return conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0]
            finally:
                conn.close()
        except Exception as e:
            print(f"latest_message_rowid failed: {e}", flush=True)
            return 0

    def find_sent_row(self, chat_identifier: str, after_rowid: int) -> tuple[int, int] | None:
        """Our outgoing message in this chat newer than `after_rowid`, as
        (rowid, error). None until Messages has written it to chat.db. A
        non-zero `error` is Messages' own "Not Delivered"."""
        import sqlite3
        conn = sqlite3.connect(f"file:{self.config.imessage.db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                """SELECT m.ROWID, COALESCE(m.error, 0) FROM message m
                   JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                   JOIN chat c ON cmj.chat_id = c.ROWID
                   WHERE c.chat_identifier = ? AND m.is_from_me = 1 AND m.ROWID > ?
                     AND m.item_type = 0 AND m.associated_message_type = 0
                   ORDER BY m.ROWID DESC LIMIT 1""",
                (chat_identifier, after_rowid),
            ).fetchone()
            return (row[0], row[1]) if row else None
        finally:
            conn.close()

    def _should_skip(self, msg: ChatMessage) -> bool:
        if msg.is_from_me and self._was_recently_sent(msg):
            return True
        return False

    def _mark_sent(self, chat_identifier: str, text: str | None, filename: str | None):
        key = self._dedup_key(chat_identifier, text, filename)
        self._recently_sent[key] = time.time()

    def _was_recently_sent(self, msg: ChatMessage) -> bool:
        if msg.text:
            key = self._dedup_key(msg.chat_identifier, msg.text, None)
            sent_at = self._recently_sent.get(key)
            if sent_at and (time.time() - sent_at) < 30:
                del self._recently_sent[key]
                return True

        for att in msg.attachments:
            key = self._dedup_key(msg.chat_identifier, None, att.transfer_name)
            sent_at = self._recently_sent.get(key)
            if sent_at and (time.time() - sent_at) < 30:
                del self._recently_sent[key]
                return True

        now = time.time()
        self._recently_sent = {k: v for k, v in self._recently_sent.items() if now - v < 60}

        return False

    @staticmethod
    def _dedup_key(chat_identifier: str, text: str | None, filename: str | None) -> str:
        content = text or filename or ""
        return hashlib.md5(f"{chat_identifier}:{content}".encode()).hexdigest()
