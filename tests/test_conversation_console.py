import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agentforge import conversation_console


class ConversationConsoleRepositoryTests(unittest.TestCase):
    def test_repository_switch_changes_following_session_commands(self):
        state = {
            "repository": r"D:\synthetic\first",
            "current": {"id": "conversation-first", "title": "first session"},
        }
        repositories = [
            {"repository_id": "repo-one", "display_name": "First",
             "canonical_path": state["repository"], "active": True},
            {"repository_id": "repo-two", "display_name": "Second",
             "canonical_path": r"D:\synthetic\second", "active": False},
        ]
        calls = []

        def fake_call(*args):
            calls.append(args)
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            if args == ("repo", "list"):
                return {"repositories": repositories}
            if args == ("repo", "switch", "repo-two"):
                state["repository"] = repositories[1]["canonical_path"]
                state["current"] = None
                return {"repository": repositories[1]}
            if args == ("session", "create", "new repository session"):
                state["current"] = {"id": "conversation-second", "title": "new repository session"}
                return {"result": state["current"]}
            if args == ("session", "switch", "conversation-second"):
                return {"result": state["current"]}
            raise AssertionError(f"Unexpected console command: {args!r}")

        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("builtins.input", side_effect=["/repo switch 2", "/new new repository session", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())

        self.assertIn(("repo", "switch", "repo-two"), calls)
        self.assertIn(("session", "create", "new repository session"), calls)
        self.assertIn(("session", "switch", "conversation-second"), calls)
        self.assertIn(r"D:\synthetic\second", output.getvalue())
        self.assertEqual("D:\\synthetic\\second", state["repository"])


if __name__ == "__main__":
    unittest.main()
