import asyncio
import sys

from bridge import Bridge
from config import load_config
from discord_bot import DiscordBridge


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    config = load_config(config_path)
    bridge = Bridge(config)
    bot = DiscordBridge(bridge)
    bridge.discord_bot = bot
    bot.run(config.discord.bot_token)


if __name__ == "__main__":
    main()
