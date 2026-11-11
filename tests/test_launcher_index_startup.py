from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LauncherIndexStartupTests(unittest.TestCase):
    def test_default_start_checks_embedding_and_does_not_start_chat_model_or_chroma_service(self):
        entry = (ROOT / "Start-AgentForge.cmd").read_text(encoding="utf-8-sig")
        launcher = (ROOT / "launchers" / "start-all.ps1").read_text(encoding="utf-8-sig")
        runtime = (ROOT / "agentforge" / "af012_runtime.py").read_text(encoding="utf-8")

        self.assertIn("start-all.ps1\" -EmbeddingOnly", entry)
        self.assertIn("param([switch]$EmbeddingOnly)", launcher)
        self.assertIn("if (-not $EmbeddingOnly)", launcher)
        self.assertIn("Ensure-Model -Label 'Embedding 模型'", launcher)
        self.assertIn("PersistentClient(path=str(target.resolve())", runtime)
        self.assertNotIn("Start-Chroma", launcher)
        self.assertNotIn("chroma.exe", launcher.casefold())


if __name__ == "__main__":
    unittest.main()
