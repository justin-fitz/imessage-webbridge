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

    def send_to_imessage(self, chat_identifier: str, chat_style: int, text: str | None = None, file_path: str | None = None):
        if file_path:
            self.sender.send_file(chat_identifier, chat_style, file_path)
            self._mark_sent(chat_identifier, None, os.path.basename(file_path))
        if text:
            self.sender.send_text(chat_identifier, chat_style, text)
            self._mark_sent(chat_identifier, text, None)

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
