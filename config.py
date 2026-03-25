import os
from dataclasses import dataclass, field

import yaml


@dataclass
class DiscordConfig:
    bot_token: str
    guild_id: int
    category_name: str = "iMessage"


@dataclass
class IMessageConfig:
    db_path: str = "~/Library/Messages/chat.db"
    attachments_path: str = "~/Library/Messages/Attachments/"
    poll_interval_seconds: int = 2


@dataclass
class BridgeConfig:
    allowed_chats: list[str] = field(default_factory=list)
    state_db: str = "db/bridge.db"
    temp_dir: str = "tmp/"


@dataclass
class Config:
    discord: DiscordConfig
    imessage: IMessageConfig
    bridge: BridgeConfig


def load_config(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    discord_cfg = DiscordConfig(
        bot_token=raw["discord"]["bot_token"],
        guild_id=int(raw["discord"]["guild_id"]),
        category_name=raw["discord"].get("category_name", "iMessage"),
    )

    im = raw.get("imessage", {})
    imessage_cfg = IMessageConfig(
        db_path=os.path.expanduser(im.get("db_path", "~/Library/Messages/chat.db")),
        attachments_path=os.path.expanduser(im.get("attachments_path", "~/Library/Messages/Attachments/")),
        poll_interval_seconds=im.get("poll_interval_seconds", 2),
    )

    br = raw.get("bridge", {})
    bridge_cfg = BridgeConfig(
        allowed_chats=br.get("allowed_chats", []),
        state_db=br.get("state_db", "db/bridge.db"),
        temp_dir=br.get("temp_dir", "tmp/"),
    )

    return Config(discord=discord_cfg, imessage=imessage_cfg, bridge=bridge_cfg)
