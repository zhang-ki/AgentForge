import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import AF012_MVP_PG_RESTART_CHECK as restart_check


class Af012MvpPgRestartRunnerTests(unittest.TestCase):
    def test_restart_observation_aliases_match_observed_server_start_change(self):
        self.assertEqual(
            {"service_restart_observed": True, "server_restart_observed": True},
            restart_check._restart_observation_fields(True),
        )
        self.assertEqual(
            {"service_restart_observed": False, "server_restart_observed": False},
            restart_check._restart_observation_fields(False),
        )

    def test_checkpoint_must_be_unique_project_data_file(self):
        valid = restart_check.ROOT / ".agentforge" / "data" / ("af012-mvp-restart-" + "a" * 32 + ".json")
        self.assertEqual(valid.resolve(), restart_check._checkpoint_path(str(valid)))
        for invalid in (
            restart_check.ROOT / "AF012_MVP_ACCEPTANCE_RESULTS.json",
            restart_check.ROOT / ".agentforge" / "data" / "other.json",
            restart_check.ROOT / ".agentforge" / "data" / "af012-mvp-restart-test.txt",
        ):
            with self.subTest(path=invalid), self.assertRaisesRegex(ValueError, "Checkpoint path"):
                restart_check._checkpoint_path(str(invalid))

    def test_network_disabled_environment_stops_before_database_access(self):
        output = io.StringIO()
        with (patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}),
              patch.object(sys, "argv", ["restart-check", "prepare", "unused"]),
              contextlib.redirect_stdout(output)):
            self.assertEqual(2, restart_check.main())
        self.assertEqual("blocked_by_environment", json.loads(output.getvalue())["status"])

    def test_admin_script_is_explicitly_scoped_to_postgresql_service_restart(self):
        script = (restart_check.ROOT / "AF012_MVP_PG_RESTART_CHECK.ps1").read_text(encoding="utf-8")
        self.assertIn("WindowsBuiltInRole]::Administrator", script)
        self.assertIn("Restart-Service -Name 'postgresql-x64-18'", script)
        self.assertNotIn("-Force", script)


if __name__ == "__main__":
    unittest.main()
