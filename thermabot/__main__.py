"""`python -m thermabot create-user NAME --role owner` (D§10). Password via prompt only."""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from thermabot.adapters import db
from thermabot.app.auth import cli_create_user


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "create-user":
        print("usage: python -m thermabot create-user NAME --role owner|admin|member")
        return 2
    conn = db.open_db(Path(os.environ.get("DATA_DIR", "/data")) / "thermabot.db")
    cli_create_user(argv[1:], conn, getpass.getpass)
    conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
