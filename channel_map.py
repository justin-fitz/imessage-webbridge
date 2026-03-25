import sqlite3
import os


class ChannelMap:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS channel_map (
                chat_identifier TEXT PRIMARY KEY,
                discord_channel_id INTEGER NOT NULL,
                display_name TEXT,
                chat_style INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bridge_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        self.conn.commit()

    def get_channel_id(self, chat_identifier: str) -> int | None:
        row = self.conn.execute(
            "SELECT discord_channel_id FROM channel_map WHERE chat_identifier = ?",
            (chat_identifier,),
        ).fetchone()
        return row["discord_channel_id"] if row else None

    def set_mapping(self, chat_identifier: str, channel_id: int, display_name: str, chat_style: int):
        self.conn.execute(
            """INSERT INTO channel_map (chat_identifier, discord_channel_id, display_name, chat_style)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_identifier) DO UPDATE SET
                 discord_channel_id = excluded.discord_channel_id,
                 display_name = excluded.display_name,
                 chat_style = excluded.chat_style""",
            (chat_identifier, channel_id, display_name, chat_style),
        )
        self.conn.commit()

    def get_chat_identifier(self, channel_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT chat_identifier FROM channel_map WHERE discord_channel_id = ?",
            (channel_id,),
        ).fetchone()
        return row["chat_identifier"] if row else None

    def get_chat_style(self, channel_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT chat_style FROM channel_map WHERE discord_channel_id = ?",
            (channel_id,),
        ).fetchone()
        return row["chat_style"] if row else None

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM bridge_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO bridge_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()
