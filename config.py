import os
from dataclasses import dataclass, field

import yaml


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    password: str = ""
    allowed_origins: list[str] = field(default_factory=list)
    max_connections: int = 20
    max_message_length: int = 10000


@dataclass
class IMessageConfig:
    db_path: str = "~/Library/Messages/chat.db"
    attachments_path: str = "~/Library/Messages/Attachments/"
    poll_interval_seconds: int = 2


@dataclass
class AppConfig:
    state_db: str = "db/bridge.db"
    temp_dir: str = "tmp/"


@dataclass
class Config:
    imessage: IMessageConfig
    app: AppConfig
    web: WebConfig


def load_config(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    im = raw.get("imessage", {})
    imessage_cfg = IMessageConfig(
        db_path=os.path.expanduser(im.get("db_path", "~/Library/Messages/chat.db")),
        attachments_path=os.path.expanduser(im.get("attachments_path", "~/Library/Messages/Attachments/")),
        poll_interval_seconds=im.get("poll_interval_seconds", 2),
    )

    br = raw.get("app", {})
    app_cfg = AppConfig(
        state_db=br.get("state_db", "db/bridge.db"),
        temp_dir=br.get("temp_dir", "tmp/"),
    )

    w = raw.get("web", {})
    web_cfg = WebConfig(
        host=w.get("host", "127.0.0.1"),
        port=w.get("port", 8080),
        password=w.get("password", ""),
        allowed_origins=w.get("allowed_origins", []),
        max_connections=w.get("max_connections", 20),
        max_message_length=w.get("max_message_length", 10000),
    )

    return Config(imessage=imessage_cfg, app=app_cfg, web=web_cfg)
