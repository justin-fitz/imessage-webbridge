import argparse

import uvicorn

from app_core import AppCore
from config import load_config
from web_server import create_app


def main():
    parser = argparse.ArgumentParser(description="iMessage Web Gateway")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    args = parser.parse_args()

    config = load_config(args.config)
    core = AppCore(config)
    app = create_app(core)
    uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="info")


if __name__ == "__main__":
    main()
