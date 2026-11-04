"""显式迁移；调用方必须先完成身份验证并提供数据库引擎。"""
from pathlib import Path
from alembic import command
from alembic.config import Config


def upgrade(engine):
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
