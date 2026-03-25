import asyncio
import hashlib
import os
import time

import discord

from channel_map import ChannelMap
from config import Config
from imessage_reader import IMessageReader
from imessage_sender import IMessageSender
from models import BridgeMessage


class Bridge:
    def __init__(self, config: Config):
        self.config = config
        self.channel_map = ChannelMap(config.bridge.state_db)
        self.reader = IMessageReader(config.imessage.db_path, self.channel_map)
        self.sender = IMessageSender()
        self.discord_bot = None
        self._recently_sent: dict[str, float] = {}

    async def poll_loop(self):
        while True:
            try:
                messages = self.reader.poll()
                for msg in messages:
                    if self._should_skip(msg):
                        continue
                    await self.discord_bot.forward_to_discord(msg)
            except Exception as e:
                print(f"Poll error: {e}")
            await asyncio.sleep(self.config.imessage.poll_interval_seconds)

    async def handle_discord_message(self, message: discord.Message):
        chat_id = self.channel_map.get_chat_identifier(message.channel.id)
        chat_style = self.channel_map.get_chat_style(message.channel.id)
        if not chat_id:
            return

        # Handle attachments
        for att in message.attachments:
            os.makedirs(self.config.bridge.temp_dir, exist_ok=True)
            file_path = os.path.join(self.config.bridge.temp_dir, att.filename)
            await att.save(file_path)
            self.sender.send_file(chat_id, chat_style or 45, os.path.abspath(file_path))
            self._mark_sent(chat_id, None, att.filename)

        # Handle text
        if message.content:
            self.sender.send_text(chat_id, chat_style or 45, message.content)
            self._mark_sent(chat_id, message.content, None)

    def _should_skip(self, msg: BridgeMessage) -> bool:
        if self.config.bridge.allowed_chats and msg.chat_identifier not in self.config.bridge.allowed_chats:
            return True
        if msg.is_from_me and self._was_recently_sent(msg):
            return True
        return False

    def _mark_sent(self, chat_identifier: str, text: str | None, filename: str | None):
        key = self._dedup_key(chat_identifier, text, filename)
        self._recently_sent[key] = time.time()

    def _was_recently_sent(self, msg: BridgeMessage) -> bool:
        # Check text dedup
        if msg.text:
            key = self._dedup_key(msg.chat_identifier, msg.text, None)
            sent_at = self._recently_sent.get(key)
            if sent_at and (time.time() - sent_at) < 30:
                del self._recently_sent[key]
                return True

        # Check attachment dedup
        for att in msg.attachments:
            key = self._dedup_key(msg.chat_identifier, None, att.transfer_name)
            sent_at = self._recently_sent.get(key)
            if sent_at and (time.time() - sent_at) < 30:
                del self._recently_sent[key]
                return True

        # Clean old entries
        now = time.time()
        self._recently_sent = {k: v for k, v in self._recently_sent.items() if now - v < 60}

        return False

    @staticmethod
    def _dedup_key(chat_identifier: str, text: str | None, filename: str | None) -> str:
        content = text or filename or ""
        return hashlib.md5(f"{chat_identifier}:{content}".encode()).hexdigest()
