"""Bounded local PostgreSQL engine using runtime-only caller credentials."""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError


def create_local_engine(password: str):
    """Create a bounded engine for the dedicated role/database, without connecting."""
    if not isinstance(password, str) or not password or "\x00" in password:
        raise ValueError("A non-empty database credential is required.")
    url = URL.create(drivername="postgresql+psycopg", username="agentforge_app",
                     password=password, host="127.0.0.1", port=5432, database="agentforge")
    return create_engine(url, connect_args={"connect_timeout": 3}, pool_size=2,
                         max_overflow=0, pool_timeout=5, pool_pre_ping=True,
                         hide_parameters=True)


def check_database_health(engine):
    """Perform a single bounded health query without exposing driver messages."""
    try:
        with engine.connect() as connection:
            return connection.scalar(text("SELECT 1")) == 1
    except SQLAlchemyError:
        return False
