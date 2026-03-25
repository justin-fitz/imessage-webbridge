import os
import re

import discord

from models import BridgeMessage


def sanitize_channel_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s_]+", "-", name)
    name = name.strip("-")
    return name[:90] if name else "unknown"


def make_channel_name(msg: BridgeMessage) -> str:
    if msg.chat_style == 43:  # group
        if msg.chat_display_name:
            return f"im-{sanitize_channel_name(msg.chat_display_name)}"
        return f"im-group-{msg.chat_identifier[-6:]}"
    else:  # 1-on-1
        clean = msg.chat_identifier.replace("+", "").replace(" ", "")
        return f"im-{sanitize_channel_name(clean)}"


class DiscordBridge(discord.Client):
    def __init__(self, bridge):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.bridge = bridge
        self.category: discord.CategoryChannel | None = None

    async def on_ready(self):
        print(f"Discord bot logged in as {self.user}")
        guild = self.get_guild(self.bridge.config.discord.guild_id)
        if not guild:
            print(f"ERROR: Guild {self.bridge.config.discord.guild_id} not found")
            return

        cat_name = self.bridge.config.discord.category_name
        self.category = discord.utils.get(guild.categories, name=cat_name)
        if not self.category:
            self.category = await guild.create_category(cat_name)
            print(f"Created category: {cat_name}")

        self.loop.create_task(self.bridge.poll_loop())
        print("Bridge started — polling for iMessages")

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return
        if not self.category or message.channel.category_id != self.category.id:
            return

        chat_id = self.bridge.channel_map.get_chat_identifier(message.channel.id)
        chat_style = self.bridge.channel_map.get_chat_style(message.channel.id)
        if not chat_id:
            return

        for att in message.attachments:
            os.makedirs(self.bridge.config.bridge.temp_dir, exist_ok=True)
            file_path = os.path.join(self.bridge.config.bridge.temp_dir, att.filename)
            await att.save(file_path)
            self.bridge.send_to_imessage(chat_id, chat_style or 45, file_path=os.path.abspath(file_path))

        if message.content:
            self.bridge.send_to_imessage(chat_id, chat_style or 45, text=message.content)

    async def forward_to_output(self, msg: BridgeMessage):
        channel = await self._get_or_create_channel(msg)
        if not channel:
            return

        # Format message
        if msg.chat_style == 43:  # group
            prefix = "**You**: " if msg.is_from_me else f"**{msg.sender_id}**: "
        else:
            prefix = "**You**: " if msg.is_from_me else ""

        files = []
        for att in msg.attachments:
            if att.filename and os.path.exists(att.filename):
                try:
                    files.append(discord.File(att.filename, filename=att.transfer_name))
                except Exception as e:
                    print(f"Failed to attach {att.filename}: {e}")

        content = f"{prefix}{msg.text}" if msg.text else (f"{prefix}(attachment)" if files else None)
        if content or files:
            await channel.send(content=content, files=files if files else None)

    async def _get_or_create_channel(self, msg: BridgeMessage) -> discord.TextChannel | None:
        if not self.category:
            return None

        channel_id = self.bridge.channel_map.get_channel_id(msg.chat_identifier)
        if channel_id:
            channel = self.get_channel(channel_id)
            if channel:
                return channel
            # Channel was deleted — recreate

        name = make_channel_name(msg)
        topic = f"iMessage: {msg.chat_identifier}"
        if msg.chat_display_name:
            topic += f" ({msg.chat_display_name})"

        channel = await self.category.guild.create_text_channel(
            name=name, category=self.category, topic=topic
        )
        self.bridge.channel_map.set_mapping(
            msg.chat_identifier, channel.id, msg.chat_display_name, msg.chat_style
        )
        print(f"Created channel #{name} for {msg.chat_identifier}")
        return channel
