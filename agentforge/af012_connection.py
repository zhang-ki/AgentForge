"""使用调用方运行时凭证的有界本地 PostgreSQL 引擎。"""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError


def create_local_engine(password: str):
    """为专用角色和数据库创建有界引擎，但不立即连接。"""
    if not isinstance(password, str) or not password or "\x00" in password:
        raise ValueError("A non-empty database credential is required.")
    url = URL.create(drivername="postgresql+psycopg", username="agentforge_app",
                     password=password, host="127.0.0.1", port=5432, database="agentforge")
    return create_engine(url, connect_args={"connect_timeout": 3}, pool_size=2,
                         max_overflow=0, pool_timeout=5, pool_pre_ping=True,
                         hide_parameters=True)


def check_database_health(engine):
    """执行一次有界健康查询，不暴露数据库驱动的原始错误信息。"""
    try:
        with engine.connect() as connection:
            return connection.scalar(text("SELECT 1")) == 1
    except SQLAlchemyError:
        return False
