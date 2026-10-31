"""Explicit migrations; caller must authenticate and supply the engine."""
from pathlib import Path
from alembic import command
from alembic.config import Config


def upgrade(engine):
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
