"""Programmatic Alembic runner: ``python -m socialcontrol.database.migrate [up|down|current]``."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url:
        # '%' must be doubled for ConfigParser interpolation
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(make_config(url), revision)


def downgrade(url: str | None = None, revision: str = "base") -> None:
    command.downgrade(make_config(url), revision)


def main(argv: list[str]) -> int:
    action = argv[0] if argv else "up"
    if action == "up":
        upgrade()
    elif action == "down":
        downgrade(revision=argv[1] if len(argv) > 1 else "-1")
    elif action == "current":
        command.current(make_config(), verbose=True)
    else:
        print("usage: migrate [up|down [rev]|current]")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
