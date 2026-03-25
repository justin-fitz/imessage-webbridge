import argparse
import asyncio
import sys

from bridge import Bridge
from config import load_config


def main():
    parser = argparse.ArgumentParser(description="iMessage-Discord Bridge")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--mode", choices=["discord", "web", "both"], default="web",
                        help="Run mode: discord, web, or both (default: web)")
    args = parser.parse_args()

    config = load_config(args.config)
    bridge = Bridge(config)

    if args.mode in ("discord", "both"):
        if not config.discord:
            print("ERROR: Discord config required for discord/both mode. Set bot_token and guild_id in config.yaml")
            sys.exit(1)

        from discord_bot import DiscordBridge
        bot = DiscordBridge(bridge)
        bridge.add_handler(bot)

        if args.mode == "both":
            import uvicorn
            from web_server import create_app
            app = create_app(bridge)

            async def run_both():
                uvi_config = uvicorn.Config(app, host=config.web.host, port=config.web.port, log_level="info")
                server = uvicorn.Server(uvi_config)
                await asyncio.gather(
                    bot.start(config.discord.bot_token),
                    server.serve(),
                )

            asyncio.run(run_both())
        else:
            bot.run(config.discord.bot_token)

    elif args.mode == "web":
        import uvicorn
        from web_server import create_app
        app = create_app(bridge)
        uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="info")


if __name__ == "__main__":
    main()
