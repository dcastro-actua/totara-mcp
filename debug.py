#!/usr/bin/env python
"""One-off production query runner so we can debug without reloading the MCP."""

import asyncio
import traceback
from importlib.machinery import SourceFileLoader

mcp = SourceFileLoader("totara_mcp", "totara-mcp.py").load_module()

DATABASE = "_pre_totara20"
QUERIES = [
    "SELECT COUNT(*) AS user_count FROM ttr_user",
    "SELECT id, username, firstname, lastname, email, auth, deleted, suspended, confirmed "
    "FROM ttr_user ORDER BY id",
]


def main() -> None:
    print(f"SSH {mcp.PROD_SSH_USER}@{mcp.PROD_SSH_HOST}:{mcp.PROD_SSH_PORT}")
    print(f"Key {mcp.PROD_SSH_KEY_PATH}")
    print(f"Database {DATABASE}")
    for sql in QUERIES:
        print(f"\n--- {sql} ---")
        try:
            print(asyncio.run(mcp.make_production_mysql_query(sql, DATABASE)))
        except Exception:
            traceback.print_exc()
            raise


if __name__ == "__main__":
    main()
