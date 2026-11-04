"""仅测试配置，不连接 PostgreSQL。"""
import importlib.util
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy"), "SQLAlchemy optional dependency unavailable")
class LocalConnectionTests(unittest.TestCase):
    def test_uses_fixed_dedicated_loopback_and_does_not_connect(self):
        from agentforge.af012_connection import create_local_engine
        with patch("agentforge.af012_connection.create_engine") as create:
            create_local_engine("synthetic-secret")
        url = create.call_args.args[0]
        self.assertEqual((url.host, url.port, url.username, url.database),
                         ("127.0.0.1", 5432, "agentforge_app", "agentforge"))
        self.assertNotIn("synthetic-secret", str(url))
        self.assertTrue(create.call_args.kwargs["hide_parameters"])
        self.assertTrue(create.call_args.kwargs["pool_pre_ping"])
        create.assert_called_once()

    def test_empty_credentials_fail_without_echoing_secret(self):
        from agentforge.af012_connection import create_local_engine
        for value in (None, "", "test\x00secret"):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError) as caught:
                create_local_engine(value)
            self.assertNotIn("secret", str(caught.exception))

    def test_health_query_and_failure_are_explicit(self):
        from agentforge.af012_connection import check_database_health
        from sqlalchemy import create_engine
        from sqlalchemy.exc import OperationalError
        from unittest.mock import Mock
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        self.assertTrue(check_database_health(engine))
        broken = Mock()
        broken.connect.side_effect = OperationalError("synthetic", {}, Exception("private"))
        self.assertFalse(check_database_health(broken))

    def test_exhausted_pool_reports_unhealthy_and_recovers_after_release(self):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import QueuePool
        from agentforge.af012_connection import check_database_health
        engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=QueuePool,
                               pool_size=1, max_overflow=0, pool_timeout=0.01)
        self.addCleanup(engine.dispose)
        with engine.connect():
            self.assertFalse(check_database_health(engine))
        self.assertTrue(check_database_health(engine))
