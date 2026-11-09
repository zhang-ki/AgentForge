"""Read-only inspection of the one database named by the historical Phase 4.2 failure."""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

import AF042_UNIFIED_AGENT_HOST_ACCEPTANCE as host


RUN_ID = "e52101603777468db6b1ec1739c0d292"
EXPECTED_DATABASE = "af033_agent_e52101603777468db6b1"
HISTORY_FILE = host.ROOT / f"AF042_UNIFIED_AGENT_HOST_RESULT_{RUN_ID}.json"


def _inspect() -> dict:
    result = {
        "schema": "agentforge-phase4-2-pending-database-inspection-v1",
        "inspection_id": uuid4().hex,
        "historical_run_id": RUN_ID,
        "database_name": EXPECTED_DATABASE,
        "status": "BLOCKED",
        "database_state": "NOT_MEASURED",
        "mutation_performed": False,
    }
    try:
        if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
            result.update(status="BLOCKED", failure_code="network_disabled_by_environment")
            return result
        if not re.fullmatch(r"af033_agent_[0-9a-f]{20}", EXPECTED_DATABASE):
            result.update(status="FAIL", failure_code="database_scope_invalid")
            return result
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        resources = history.get("temporary_resources", {})
        cleanup = history.get("cleanup", {})
        if not (
            history.get("run_id") == RUN_ID
            and history.get("failure_stage") == "database_provision"
            and history.get("status") == "FAIL"
            and resources.get("database_name") == EXPECTED_DATABASE
            and cleanup.get("database") == "pending"
            and history.get("formal_database_accessed") is False
        ):
            result.update(status="FAIL", failure_code="historical_result_scope_mismatch")
            return result

        import psycopg

        password = getpass.getpass("PostgreSQL administrator password (hidden): ")
        if not password:
            result.update(status="BLOCKED", failure_code="admin_credential_missing")
            return result
        with psycopg.connect(host="127.0.0.1", port=5432, dbname="postgres", user="postgres",
                             password=password, connect_timeout=3, autocommit=True) as connection:
            row = connection.execute(
                "SELECT pg_get_userbyid(d.datdba) AS owner, d.datallowconn "
                "FROM pg_database AS d WHERE d.datname = %s", (EXPECTED_DATABASE,)
            ).fetchone()
            if row is None:
                result.update(status="PASS", database_state="absent",
                              cleanup_conclusion="no_database_to_remove", active_sessions=0)
                return result
            active = connection.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = %s", (EXPECTED_DATABASE,)
            ).fetchone()[0]
            result.update(database_state="present", owner=row[0], allow_connections=row[1],
                          active_sessions=int(active),
                          cleanup_conclusion="requires_provenance_review")

        # Read metadata only. Do not access user rows and never mutate/drop this database.
        with psycopg.connect(host="127.0.0.1", port=5432, dbname=EXPECTED_DATABASE,
                             user="postgres", password=password, connect_timeout=3,
                             autocommit=True) as connection:
            tables = connection.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                "AND table_type = 'BASE TABLE' ORDER BY table_schema, table_name"
            ).fetchall()
            result["user_tables"] = [f"{schema}.{name}" for schema, name in tables]
            has_alembic = connection.execute(
                "SELECT to_regclass('public.alembic_version') IS NOT NULL"
            ).fetchone()[0]
            result["alembic_version_table_present"] = bool(has_alembic)
            if has_alembic:
                result["migration_revisions"] = [row[0] for row in connection.execute(
                    "SELECT version_num FROM public.alembic_version ORDER BY version_num"
                ).fetchall()]
        result["status"] = "PARTIAL"
        return result
    except Exception as exc:
        result.update(status="BLOCKED", database_state="NOT_MEASURED",
                      failure_diagnostic=host._safe_database_error(
                          exc, secrets=(locals().get("password"),)))
        result["failure_diagnostic"]["operation"] = (
            "target_database_metadata_read" if result.get("database_state") == "present"
            else "admin_connection_or_database_lookup")
        return result
    finally:
        if "password" in locals():
            password = None


def main() -> int:
    result = _inspect()
    path = host.ROOT / f"AF042_PENDING_DATABASE_INSPECTION_RESULT_{result['inspection_id']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "database_state": result["database_state"],
                      "mutation_performed": False, "result_file": str(path),
                      **({"failure_code": result["failure_code"]} if "failure_code" in result else {})},
                     ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2 if result["status"] == "BLOCKED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
