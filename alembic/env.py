"""Migrations receive an already authenticated connection, never a URL or key."""
from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("A caller-supplied local database connection is required.")
context.configure(connection=connection, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
