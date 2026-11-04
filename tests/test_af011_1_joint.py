"""双仓库集成测试；使用真实本地 Chroma 和隔离的 SQLite。"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
import io
import json
from unittest.mock import Mock, patch

import AF011_1_JOINT_HOST as joint


class HostDiagnosticHelpersTests(unittest.TestCase):
    def test_summary_failures_map_to_generation_validation_or_persistence(self):
        self.assertEqual("summary_generation", joint._summary_failure_stage(
            {"failure_stage": "provider"}))
        self.assertEqual("summary_validation", joint._summary_failure_stage(
            {"failure_stage": "fact_validation"}))
        self.assertEqual("summary_persistence", joint._summary_failure_stage(
            {"failure_stage": "persistence"}))

    def test_chat_request_totals_aggregate_real_observation_deltas(self):
        totals = joint._chat_request_totals([
            {"attempted": 1, "sent": 1, "succeeded": 1, "failed": 0, "retries": 0},
            {"attempted": 1, "sent": 1, "succeeded": 0, "failed": 1, "retries": 0},
        ])
        self.assertEqual({"attempted": 2, "sent": 2, "succeeded": 1,
                          "failed": 1, "retries": 0, "request_count": 2}, totals)


@unittest.skipUnless(importlib.util.find_spec("chromadb") and importlib.util.find_spec("sqlalchemy"),
                     "isolated Chroma and SQLAlchemy are required")
class JointRepositoryIntegrationTests(unittest.TestCase):
    def test_owned_chroma_client_closes_before_temporary_directory_cleanup(self):
        import chromadb
        from chromadb.config import Settings
        from AF011_1_JOINT_HOST import _close_owned_chroma, _cleanup_owned_temp
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="af0111-close-", dir=root)
        directory = temp.name
        try:
            client = chromadb.PersistentClient(path=str(Path(directory) / "chroma"),
                settings=Settings(anonymized_telemetry=False))
            client.create_collection("owned_cleanup_test", embedding_function=None)
            cleanup = {}
            self.assertTrue(_close_owned_chroma(client, cleanup))
            self.assertEqual("passed", cleanup["chroma_client"])
            self.assertTrue(_cleanup_owned_temp(temp, cleanup), cleanup)
        finally:
            temp.cleanup()
        self.assertFalse(Path(directory).exists())

    def test_real_chroma_repo_and_global_memory_isolation_across_process_restart(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        script = r'''
import os,sys,io,json
os.environ['ANONYMIZED_TELEMETRY']='False'
from pathlib import Path
import chromadb
from chromadb.config import Settings
from sqlalchemy import create_engine
from agentforge.conversation_migrations import upgrade
from agentforge.conversation_store import ConversationStore
from agentforge.memory_store import MemoryStore, GLOBAL_MEMORY_REPOSITORY_ID
from agentforge.memory_retrieval import ChromaMemoryRetriever
from agentforge.embeddings.chroma_store import ChromaCandidateStore, delete_repository_vectors
from agentforge.embeddings.chroma_index import ChromaCodeIndex, repository_identity
from agentforge.indexer import index_repository
from agentforge.store import IndexStore
from contextlib import contextmanager,redirect_stdout,redirect_stderr
from unittest.mock import patch
class Provider:
 model_id='joint-synthetic-embedding'; dimension=2
 def embed_document(self,text): return (1.0, 0.0)
 def embed_query(self,text): return (1.0, 0.0)
 def wait_ready(self): return 0.0
base,mode=Path(sys.argv[1]),sys.argv[2]
client=chromadb.PersistentClient(path=str(base/'chroma'),settings=Settings(anonymized_telemetry=False,allow_reset=False))
collection=client.get_or_create_collection('joint_test',embedding_function=None,configuration={'hnsw':{'space':'cosine'}})
provider=Provider(); a=base/'repo_a'; b=base/'repo_b'; aid=repository_identity(a); bid=repository_identity(b)
engine=create_engine('sqlite+pysqlite:///'+(base/'test.sqlite3').as_posix())
if mode=='write':
 upgrade(engine); conversations=ConversationStore(engine); memories=MemoryStore(engine)
 for repo in (aid,bid):
  c=conversations.create_conversation(repo,repo)
  conversations.append_turn(c['id'],repo,'Please use concise answers.','OK')
  if repo==aid:
   message=conversations.get_messages(c['id'],repo)[0]['id']
   private=memories.propose(repo,c['id'],'Private alpha preference.',[message],memory_type='preference')
   shared=memories.propose(repo,c['id'],'Global concise preference.',[message],memory_type='preference',scope='global')
   memories.approve(shared['id'],GLOBAL_MEMORY_REPOSITORY_ID)
  else:
   message=conversations.get_messages(c['id'],repo)[0]['id']
   other=memories.propose(repo,c['id'],'Private beta preference.',[message],memory_type='preference')
   memories.approve(other['id'],repo)
 for path in (a,b):
  with IndexStore(':memory:') as index:
   index_repository(path,index); ChromaCodeIndex(index,collection,provider).sync()
 from agentforge import cli
 with IndexStore(':memory:') as index:
  index_repository(a,index)
  @contextmanager
  def opened(**kwargs): yield index
  out,err=io.StringIO(),io.StringIO()
  with patch('agentforge.cli._conversation_engine',return_value=engine), patch('agentforge.cli._open_current_index',side_effect=opened), patch('agentforge.af012_runtime.create_persistent_chroma',return_value=client), patch('agentforge.af012_runtime.collection_for_model',return_value=collection), patch('agentforge.af012_runtime.embedding_provider_from_environment',return_value=provider), redirect_stdout(out), redirect_stderr(err):
   code=cli.main(['memory','sync'])
  assert code==0,err.getvalue()
  result=json.loads(out.getvalue())['result']
  assert result['completed']==1,result
  assert memories.outbox_status(bid)=={'pending':1},memories.outbox_status(bid)
 memories.approve(private['id'],aid)
 result=memories.sync_outbox(ChromaCandidateStore(collection,dimension=2),provider,repository_ids=(aid,))
 assert result['completed']==1 and not result['failed'],result
 retriever=ChromaMemoryRetriever(collection,provider,memories)
 assert {m.content for m in retriever.search('preference',repository_id=aid)}=={'Private alpha preference.','Global concise preference.'}
 assert {m.content for m in retriever.search('preference',repository_id=bid)}=={'Global concise preference.'}
 with IndexStore(':memory:') as index:
  index_repository(b,index); found=ChromaCodeIndex(index,collection,provider).search('shared',top_k=10)
  assert len(found)==1 and 'beta' in found[0].text
 (a/'shared.py').write_text("def shared():\n    return 'alpha updated'\n",encoding='utf-8')
 with IndexStore(':memory:') as index:
  index_repository(a,index); svc=ChromaCodeIndex(index,collection,provider); svc.sync()
  assert 'alpha updated' in svc.search('shared',top_k=10)[0].text
 delete_repository_vectors(client,aid)
 assert {m.content for m in retriever.search('preference',repository_id=bid)}=={'Global concise preference.'}
 assert {m.content for m in retriever.search('preference',repository_id=aid)}=={'Global concise preference.'}
 # 删除后的仓库可以重新索引；保留的已批准记忆需要
 # 显式重建向量，且不能影响其他仓库或全局数据。
 with IndexStore(':memory:') as index:
  index_repository(a,index)
  @contextmanager
  def reopened(**kwargs): yield index
  out,err=io.StringIO(),io.StringIO()
  with patch('agentforge.cli._conversation_engine',return_value=engine), patch('agentforge.cli._open_current_index',side_effect=reopened), patch('agentforge.af012_runtime.create_persistent_chroma',return_value=client), patch('agentforge.af012_runtime.collection_for_model',return_value=collection), patch('agentforge.af012_runtime.embedding_provider_from_environment',return_value=provider), redirect_stdout(out), redirect_stderr(err):
   code=cli.main(['memory','sync','--rebuild'])
  assert code==0,err.getvalue()
  assert json.loads(out.getvalue())['result']['completed']==1
 assert {m.content for m in retriever.search('preference',repository_id=aid)}=={'Private alpha preference.','Global concise preference.'}
else:
 assert collection.count()==3,collection.count()
 with IndexStore(':memory:') as index:
  index_repository(b,index); found=ChromaCodeIndex(index,collection,provider).search('shared',top_k=10)
  assert len(found)==1 and 'beta' in found[0].text
 memories=MemoryStore(engine)
 assert {m.content for m in ChromaMemoryRetriever(collection,provider,memories).search('preference',repository_id=bid)}=={'Global concise preference.'}
engine.dispose()
'''
        with tempfile.TemporaryDirectory(prefix="af011-1-joint-", dir=root) as directory:
            base = Path(directory)
            for name, value in (("repo_a", "alpha"), ("repo_b", "beta")):
                repo = base / name
                repo.mkdir()
                (repo / "shared.py").write_text(f"def shared():\n    return '{value}'\n", encoding="utf-8")
            for mode in ("write", "reopen"):
                result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script,
                                         str(base), mode], capture_output=True, text=True, timeout=90,
                                        env={**os.environ, "ANONYMIZED_TELEMETRY": "False"})
                self.assertEqual(0, result.returncode, result.stderr[-3000:])


class HostEnvironmentGateTests(unittest.TestCase):
    def test_semantic_summary_failures_use_safe_specific_codes(self):
        import AF011_1_JOINT_HOST as host
        cases = (
            (ValueError("A complete turn exceeds the bounded semantic summary input; existing summary and messages were retained."),
             "semantic_summary_turn_input_too_large"),
            (ValueError("Semantic summary request exceeds the bounded prompt size; history was retained."),
             "semantic_summary_prompt_too_large"),
            (ValueError("Merged semantic summary exceeds its safe limit; existing summary and messages were retained."),
             "semantic_summary_merged_output_too_large"),
            (ValueError("Summary exceeds the bounded storage budget; existing summary was retained."),
             "semantic_summary_storage_budget_exceeded"),
            (ValueError("private diagnostic detail"), "semantic_summary_validation_error"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                code = host._failure_code(error, "semantic_summary")
                self.assertEqual(expected, code)
                self.assertNotIn(str(error), code)

    def test_fixed_check_failure_code_is_preserved(self):
        import AF011_1_JOINT_HOST as host
        self.assertEqual("known_check_failure", host._failure_code(
            host.CheckFailure("known_check_failure"), "semantic_summary"))

    def test_database_diagnostic_classifies_without_leaking_credentials(self):
        import AF011_1_JOINT_HOST as host
        secret = "synthetic-secret-do-not-record"
        for message, expected in (("connection refused " + secret, "connection_refused"),
                                  ("password authentication failed " + secret, "authentication_failed"),
                                  ("no password supplied " + secret, "password_not_supplied"),
                                  ("unrecognized error " + secret, "unknown")):
            with self.subTest(expected=expected):
                result = host._connection_diagnostic(RuntimeError(message))
                self.assertIn(expected, result["categories"])
                self.assertNotIn(secret, json.dumps(result))

    def test_cleanup_retry_is_bounded_and_does_not_hide_permanent_failure(self):
        import AF011_1_JOINT_HOST as host
        temp = Mock()
        temp.cleanup.side_effect = [PermissionError(), None]
        result = {}
        with patch.object(host.time, "sleep"), patch.object(host.gc, "collect"):
            self.assertTrue(host._cleanup_owned_temp(temp, result))
        self.assertEqual({"temporary_directory": "passed", "temporary_cleanup_attempts": 2}, result)
        temp.cleanup.reset_mock(side_effect=True)
        temp.cleanup.side_effect = PermissionError()
        result = {}
        with patch.object(host.time, "sleep"), patch.object(host.gc, "collect"):
            self.assertFalse(host._cleanup_owned_temp(temp, result))
        self.assertEqual(6, temp.cleanup.call_count)
        self.assertEqual("failed_PermissionError", result["temporary_directory"])

    def test_network_disabled_returns_without_connecting_or_creating_database(self):
        import AF011_1_JOINT_HOST as host
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="af0111-gate-", dir=root) as directory:
            with patch.object(host, "ROOT", Path(directory)), patch.dict(
                    os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}), redirect_stdout(io.StringIO()):
                self.assertEqual(2, host.main())
            results = list(Path(directory).glob("AF011_1_JOINT_HOST_RESULT_*.json"))
            self.assertEqual(1, len(results))
            result = json.loads(results[0].read_text(encoding="utf-8"))
            self.assertEqual("BLOCKED", result["status"])
            self.assertEqual("NETWORK_RESTRICTED", result["failure_code"])
            self.assertEqual([], result["chat_requests"])
            self.assertEqual("not_created", result["cleanup"]["database"])
