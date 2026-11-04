"""迁移使用已通过认证的数据库连接，不接收 URL 或密钥。"""
from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("A caller-supplied local database connection is required.")
context.configure(connection=connection, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
