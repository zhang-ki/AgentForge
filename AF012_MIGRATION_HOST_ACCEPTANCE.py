"""Read-only production revision check plus disposable PostgreSQL migration test.

This runner never writes to the configured AgentForge database. All Alembic
upgrade and session lifecycle checks run in a uniquely named disposable DB.
Passwords are read from the process environment and are never included in output.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL

from alembic.config import Config
from alembic.script import ScriptDirectory

from agentforge.conversation_migrations import upgrade
from agentforge.conversation_store import ConversationStore
from agentforge.models import Answer
from agentforge.session_service import SessionService


HOST = "127.0.0.1"
PORT = 5432
APP_ROLE = "agentforge_app"
APP_DATABASE = "agentforge"


def _migration_graph() -> tuple[list[str], set[str]]:
    config = Config(str(Path(__file__).resolve().parent / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    revisions = {revision.revision for revision in script.walk_revisions()}
    return script.get_heads(), revisions


def _formal_database_read_only(app_password: str, heads: list[str], revisions: set[str]) -> dict:
    stage = "formal_read_only_preflight"
    try:
        with psycopg.connect(
            host=HOST,
            port=PORT,
            dbname=APP_DATABASE,
            user=APP_ROLE,
            password=app_password,
            connect_timeout=3,
        ) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                database, role = connection.execute(
                    "SELECT current_database(), current_user"
                ).fetchone()
                exists = connection.execute(
                    "SELECT to_regclass('public.alembic_version') IS NOT NULL"
                ).fetchone()[0]
                if not exists:
                    return {
                        "status": "failed",
                        "stage": stage,
                        "failure_code": "alembic_version_table_missing",
                    }
                current = [row[0] for row in connection.execute(
                    "SELECT version_num FROM public.alembic_version ORDER BY version_num"
                ).fetchall()]
                columns = [row[0] for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'conversations' "
                    "ORDER BY ordinal_position"
                ).fetchall()]
                required = {"capability_mode", "presentation_preferences", "provider_id"}
                unknown_revisions = sorted(set(current) - revisions)
                schema_ready = required.issubset(columns)
                return {
                    "status": "passed" if not unknown_revisions and schema_ready else "partial",
                    "stage": stage,
                    "database": database,
                    "role": role,
                    "current_revisions": current,
                    "local_heads": heads,
                    "unknown_revisions": unknown_revisions,
                    "conversation_columns": columns,
                    "known_current_revision": not unknown_revisions,
                    "schema_has_restored_preference_fields": schema_ready,
                    "would_require_forward_upgrade": current != heads,
                    "formal_database_mutated": False,
                }
    except Exception as exc:  # Do not serialize driver text or connection details.
        return {
            "status": "failed",
            "stage": stage,
            "failure_code": type(exc).__name__,
            "sqlstate": getattr(exc, "sqlstate", None),
            "formal_database_mutated": False,
        }


def _create_disposable_database(admin_password: str, app_password: str, heads: list[str]) -> dict:
    stage = "isolated_postgresql_migration"
    database_name = f"af_migration_check_{uuid.uuid4().hex[:20]}"
    admin = None
    engine = None
    created = False
    result: dict = {"status": "failed", "stage": stage, "database_created": False}
    try:
        admin = psycopg.connect(
            host=HOST,
            port=PORT,
            dbname="postgres",
            user="postgres",
            password=admin_password,
            connect_timeout=3,
            autocommit=True,
        )
        row = admin.execute(
            "SELECT rolcreatedb FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        role_exists = admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)
        ).fetchone() is not None
        if row is None or not row[0] or not role_exists:
            return {
                "status": "blocked",
                "stage": stage,
                "failure_code": "admin_createdb_or_application_role_unavailable",
                "database_created": False,
            }
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database_name), sql.Identifier(APP_ROLE)
            )
        )
        created = True
        result["database_created"] = True

        url = URL.create(
            "postgresql+psycopg",
            username=APP_ROLE,
            password=app_password,
            host=HOST,
            port=PORT,
            database=database_name,
        )
        engine = create_engine(
            url,
            connect_args={"connect_timeout": 5},
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        upgrade(engine)
        upgrade(engine)
        with engine.connect() as connection:
            current = connection.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            ).scalars().all()
            tables = set(inspect(connection).get_table_names())
            required_tables = {"conversations", "messages", "agent_executions", "agent_execution_results"}
            conversation_columns = {
                column["name"] for column in inspect(connection).get_columns("conversations")
            }
        if current != heads or not required_tables.issubset(tables):
            raise RuntimeError("migration_result_mismatch")
        if not {"capability_mode", "presentation_preferences", "provider_id"}.issubset(conversation_columns):
            raise RuntimeError("conversation_columns_missing")

        store = ConversationStore(engine)
        service = SessionService()
        repository_id = "migration-host-" + uuid.uuid4().hex
        conversation = service.create(store, repository_id, "migration host verification")
        service.commit_completed_turn(
            store,
            conversation["id"],
            repository_id,
            "Check restored session",
            Answer("Disposable PostgreSQL session restored.", (), True),
        )
        engine.dispose()
        engine = None

        # A new Engine verifies durable persistence independent of the migration pool.
        engine = create_engine(
            url,
            connect_args={"connect_timeout": 5},
            pool_size=1,
            max_overflow=0,
            pool_pre_ping=True,
            hide_parameters=True,
        )
        restored = SessionService().load_context(
            ConversationStore(engine), conversation["id"], repository_id
        )
        if len(restored.history) != 2 or restored.history[0].content != "Check restored session":
            raise RuntimeError("session_restore_mismatch")
        result.update({
            "status": "passed",
            "stage": stage,
            "head_revisions": current,
            "repeat_upgrade": "passed",
            "session_create_read_reconnect_restore": "passed",
            "formal_database_mutated": False,
        })
    except Exception as exc:
        result.update({
            "status": "failed",
            "stage": stage,
            "failure_code": type(exc).__name__,
            "sqlstate": getattr(exc, "sqlstate", None),
            "formal_database_mutated": False,
        })
    finally:
        if engine is not None:
            engine.dispose()
        if admin is not None:
            if created:
                try:
                    admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (database_name,),
                    )
                    admin.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name))
                    )
                    result["cleanup"] = "passed"
                except Exception as exc:
                    result["cleanup"] = "failed"
                    result["cleanup_failure_code"] = type(exc).__name__
                    result["temporary_database_name"] = database_name
            else:
                result["cleanup"] = "not_needed"
            admin.close()
    return result


def main() -> int:
    app_password = os.environ.get("AGENTFORGE_DB_PASSWORD", "")
    admin_password = os.environ.get("AGENTFORGE_PG_ADMIN_PASSWORD", "")
    if not app_password or not admin_password:
        print(json.dumps({"status": "blocked", "failure_code": "required_credential_unavailable"}))
        return 2

    try:
        heads, revisions = _migration_graph()
        if len(heads) != 1:
            print(json.dumps({"status": "failed", "failure_code": "migration_graph_not_single_head"}))
            return 1
        formal = _formal_database_read_only(app_password, heads, revisions)
        isolated = _create_disposable_database(admin_password, app_password, heads)
        overall = "passed" if formal["status"] == "passed" and isolated["status"] == "passed" else "partial"
        print(json.dumps({
            "status": overall,
            "migration_heads": heads,
            "formal_database_read_only": formal,
            "isolated_postgresql": isolated,
            "credentials_logged": False,
        }, ensure_ascii=False))
        return 0 if overall == "passed" else 1
    finally:
        # Best effort; the PowerShell wrapper removes these variables as well.
        os.environ.pop("AGENTFORGE_DB_PASSWORD", None)
        os.environ.pop("AGENTFORGE_PG_ADMIN_PASSWORD", None)


if __name__ == "__main__":
    raise SystemExit(main())
