import argparse
import os

import uvicorn

from app_core import AppCore
from config import load_config
from web_server import create_app


def main():
    parser = argparse.ArgumentParser(description="iMessage Web Gateway")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    args = parser.parse_args()

    # Everything the bridge writes (logins, logs, uploads, converted photos) is
    # private: create it readable by this account only, and close up what an
    # earlier version left world-readable.
    os.umask(0o077)
    config = load_config(args.config)
    here = os.path.dirname(os.path.abspath(__file__))
    for d in ("db", "logs"):
        d = os.path.join(here, d)
        try:
            os.chmod(d, 0o700)
            for name in os.listdir(d):
                if os.path.isfile(os.path.join(d, name)):
                    os.chmod(os.path.join(d, name), 0o600)
        except OSError:
            pass
    core = AppCore(config)
    app = create_app(core)
    uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="info")


if __name__ == "__main__":
    main()
