"""Entry point: ``python -m app.mcp_server`` starts the stdio MCP server.

The backend launches this as a child process. It reads only the configuration the
parent chose to pass in its environment (``CLEARCASE_MONGODB_URI``,
``CLEARCASE_MONGODB_DB``, ``CLEARCASE_DEMO_OWNER_ID``, ...). It deliberately does
not read the repository ``.env`` file, so the LLM API key never enters this
process even when it is present on disk.
"""

import logging
import sys

from app.config import Settings
from app.mcp_server.server import create_server


def main() -> None:
    # stdout carries the protocol. Logging goes to stderr, always.
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings(_env_file=None)
    create_server(settings).run("stdio")


if __name__ == "__main__":
    main()
