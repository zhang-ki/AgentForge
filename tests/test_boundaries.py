import ast
import unittest

from agentforge.config import DATABASE_PATH, PROJECT_ROOT


class BoundaryTests(unittest.TestCase):
    def test_fixed_path_and_ignore_rule(self):
        self.assertEqual(DATABASE_PATH, PROJECT_ROOT / ".agentforge" / "data" / "index.sqlite3")
        self.assertIn(".agentforge/data/", (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_no_command_execution_or_dynamic_code_tools(self):
        banned_calls = {"eval", "exec", "compile", "system", "popen", "spawnl", "spawnv", "Popen"}
        for path in (PROJECT_ROOT / "agentforge").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertNotIn("subprocess", {alias.name for alias in node.names})
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "subprocess")
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                    if (name == "compile" and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == "re"):
                        # re.compile constructs a regex; builtin compile remains forbidden.
                        continue
                    self.assertNotIn(name, banned_calls, str(path))

