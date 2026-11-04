import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agentforge.embeddings.chroma_store import ChromaCandidateStore, ChromaSource
from agentforge.embeddings.base import EmbeddingError


class ChromaAdapterTests(unittest.TestCase):
    def source(self, **changes):
        values = dict(repository_id="repo1", source_type="code", chunk_id="chunk1",
                      file_path="src/a.py", content_hash="a" * 64, model_id="model1")
        values.update(changes)
        return ChromaSource(**values)

    def test_rejects_out_of_scope_paths_invalid_vectors_and_forgery(self):
        store = ChromaCandidateStore(Mock(), dimension=2)
        with self.assertRaises(ValueError):
            store.upsert(self.source(file_path="../outside.py"), [1, 0])
        with self.assertRaises(EmbeddingError):
            store.upsert(self.source(), [float("nan"), 1])
        with self.assertRaises(ValueError):
            store.search([1, 0], repository_id="repo1", model_id="model1", verify_source=None)
        store.collection.upsert.assert_not_called()
        store.collection.query.return_value = {"ids": [["forged"]],
            "metadatas": [[self.source().__dict__]], "distances": [[0.1]]}
        with self.assertRaisesRegex(EmbeddingError, "identity"):
            store.search([1, 0], repository_id="repo1", model_id="model1", verify_source=lambda _: True)

    def test_verifier_is_mandatory_and_false_rejects_candidate(self):
        collection = Mock()
        store = ChromaCandidateStore(collection, dimension=2)
        source = self.source()
        store.upsert(source, [1, 0])
        self.assertEqual([""], collection.upsert.call_args.kwargs["documents"])
        identity = collection.upsert.call_args.kwargs["ids"][0]
        collection.query.return_value = {"ids": [[identity]],
            "metadatas": [[source.__dict__]], "distances": [[0.1]]}
        self.assertEqual(store.search([1, 0], repository_id="repo1", model_id="model1",
                                      verify_source=lambda _: False), [])
        self.assertEqual(len(store.search([1, 0], repository_id="repo1", model_id="model1",
                                          verify_source=lambda _: True)), 1)

    def test_search_can_omit_document_payload(self):
        collection = Mock()
        store = ChromaCandidateStore(collection, dimension=2)
        source = self.source()
        from agentforge.embeddings.chroma_store import _identity
        collection.query.return_value = {
            "ids": [[_identity(source)]], "metadatas": [[source.__dict__]], "distances": [[0.1]],
        }

        result = store.search_with_documents([1, 0], repository_id="repo1", model_id="model1",
            verify_source=lambda _source: True, include_documents=False)

        self.assertEqual([(source, 0.1, None)], result)
        self.assertEqual(["metadatas", "distances"], collection.query.call_args.kwargs["include"])

    def test_identity_and_metadata_validation(self):
        for dimension in (True, 0, 4097, "2"):
            with self.subTest(dimension=dimension), self.assertRaises(ValueError):
                ChromaCandidateStore(Mock(), dimension=dimension)
        store = ChromaCandidateStore(Mock(), dimension=2)
        invalid = [self.source(repository_id=""), self.source(chunk_id="bad\n"),
                   self.source(source_type="invalid"), self.source(content_hash="UPPER"),
                   self.source(file_path=""), self.source(file_path="c:\\private"),
                   self.source(file_path="/absolute"), self.source(source_type="memory", file_path="file")]
        for source in invalid:
            with self.subTest(source=source), self.assertRaises(ValueError):
                store.upsert(source, [1, 0])
        store.upsert(self.source(source_type="memory", file_path=""), [1, 0])

    def test_query_validation_and_malformed_response_fail_closed(self):
        collection = Mock()
        store = ChromaCandidateStore(collection, dimension=2)
        for repo, model, limit in (("", "m", 1), ("r", "", 1), ("r", "m", True), ("r", "m", 101)):
            with self.subTest(repo=repo, model=model, limit=limit), self.assertRaises(ValueError):
                store.search([1, 0], repository_id=repo, model_id=model, verify_source=lambda _: True, limit=limit)
        collection.query.return_value = {"ids": [["x"]], "metadatas": [[]], "distances": [[]]}
        with self.assertRaisesRegex(EmbeddingError, "Malformed"):
            store.search([1, 0], repository_id="repo1", model_id="model1", verify_source=lambda _: True)
        source = self.source()
        store.upsert(source, [1, 0])
        identity = collection.upsert.call_args.kwargs["ids"][0]
        for meta, distance in (({}, 0.0), (source.__dict__, float("nan")),
                               (self.source(repository_id="other").__dict__, 0.0)):
            collection.query.return_value = {"ids": [[identity]], "metadatas": [[meta]], "distances": [[distance]]}
            with self.subTest(meta=meta, distance=distance), self.assertRaises(EmbeddingError):
                store.search([1, 0], repository_id="repo1", model_id="model1", verify_source=lambda _: True)

    @unittest.skipUnless(importlib.util.find_spec("chromadb"), "isolated chromadb not installed")
    def test_real_persistent_chroma_round_trip_in_separate_processes(self):
        # 进程隔离确保清理前释放 Windows 上的 SQLite/HNSW 句柄。
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        script = r'''
import os,sys
os.environ["ANONYMIZED_TELEMETRY"]="False"
import chromadb
from chromadb.config import Settings
from agentforge.embeddings.chroma_store import ChromaSource,ChromaCandidateStore
p,mode=sys.argv[1:]; c=chromadb.PersistentClient(path=p,settings=Settings(anonymized_telemetry=False))
x=c.get_or_create_collection("af012_test",embedding_function=None,configuration={"hnsw":{"space":"cosine"}})
s=ChromaCandidateStore(x,dimension=2)
a=ChromaSource("repo1","code","a","src/a.py","a"*64,"m1")
b=ChromaSource("repo2","code","b","src/b.py","b"*64,"m1")
if mode=="write":
 s.upsert(a,[1,0]);s.upsert(a,[1,0]);s.upsert(b,[0,1]);assert x.count()==2
 s.upsert(a,[1,0],document="legacy source text");s.upsert(a,[1,0])
 assert x.get(include=["documents"])['documents']==["",""]
else:
 assert x.count()==2
 assert len(s.search([1,0],repository_id="repo1",model_id="m1",verify_source=lambda v:v.content_hash=="a"*64))==1
 assert s.search([1,0],repository_id="repo1",model_id="m1",verify_source=lambda v:False)==[]
 s.delete(a);assert x.count()==1
'''
        with tempfile.TemporaryDirectory(prefix="af012-chroma-test-", dir=root) as directory:
            for mode in ("write", "read"):
                completed = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script,
                                            directory, mode], capture_output=True, text=True,
                                           timeout=45, env={**os.environ, "ANONYMIZED_TELEMETRY": "False"})
                self.assertEqual(completed.returncode, 0, completed.stderr[-1500:])

    @unittest.skipUnless(importlib.util.find_spec("chromadb"), "isolated chromadb not installed")
    def test_real_chroma_repository_index_update_delete_and_restart(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        script = r'''
import os,sys,json
os.environ["ANONYMIZED_TELEMETRY"]="False"
from dataclasses import asdict
from chromadb.config import Settings
import chromadb
from agentforge.af012_runtime import collection_for_model
from agentforge.embeddings.chroma_index import ChromaCodeIndex, repository_identity
from agentforge.indexer import index_repository
from agentforge.store import IndexStore
class P:
 model_id="synthetic-local-embed"
 dimension=2
 def embed(self,text): return (1.0, 1.0)
 def embed_query(self,text): return self.embed(text)
 def embed_document(self,text): return self.embed(text)
repo,chroma_path,mode=sys.argv[1:]
client=chromadb.PersistentClient(path=chroma_path,settings=Settings(anonymized_telemetry=False,allow_reset=False))
collection=collection_for_model(client,"synthetic-local-embed",2)
with IndexStore(":memory:") as store:
 index_repository(repo,store)
 svc=ChromaCodeIndex(store,collection,P())
 if mode in ("write","update"):
  stats=asdict(svc.sync())
  existing=collection.get(where={"repository_id":svc.repository_id},include=["embeddings"])
  ids=existing["ids"]
  collection.update(ids=ids,embeddings=existing["embeddings"],
                    documents=["legacy source text"]*len(ids))
  legacy_migration=asdict(svc.sync())
  documents=collection.get(where={"repository_id":svc.repository_id},include=["documents"])["documents"]
  print(json.dumps({"stats":stats,"legacy_migration":legacy_migration,
                    "count":collection.count(),"documents":documents}))
 else:
  repo_id=repository_identity(repo)
  documents=collection.get(where={"repository_id":repo_id},include=["documents"])["documents"]
  assert documents==["",""]
  results=[asdict(x) for x in svc.search("locate refresh state",top_k=20)]
  print(json.dumps({"results":results,"count":collection.count()}))
'''
        with tempfile.TemporaryDirectory(prefix="af012-chroma-code-", dir=root) as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("def alpha():\n    return 'one'\n", encoding="utf-8")
            (repo / "b.py").write_text("def beta():\n    return 'two'\n", encoding="utf-8")
            chroma_path = base / "chroma"
            def run(mode):
                completed = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script,
                    str(repo), str(chroma_path), mode], capture_output=True, text=True,
                    timeout=45, env={**os.environ, "ANONYMIZED_TELEMETRY": "False"})
                self.assertEqual(0, completed.returncode, completed.stderr[-2000:])
                return json.loads(completed.stdout.strip().splitlines()[-1])
            first = run("write")
            self.assertEqual(2, first["stats"]["sources"])
            self.assertEqual(2, first["stats"]["embedded"])
            self.assertEqual(0, first["legacy_migration"]["embedded"])
            self.assertEqual(2, first["legacy_migration"]["unchanged"])
            self.assertEqual(["", ""], first["documents"])
            reopened = run("search")
            self.assertEqual(2, reopened["count"])
            self.assertEqual({"a.py", "b.py"}, {item["relative_path"] for item in reopened["results"]})
            (repo / "a.py").write_text("def alpha():\n    return 'three changed'\n", encoding="utf-8")
            (repo / "b.py").unlink()
            (repo / "c.py").write_text("def gamma():\n    return 'four'\n", encoding="utf-8")
            changed = run("update")
            self.assertEqual(2, changed["stats"]["embedded"])
            self.assertEqual(2, changed["count"])
            final = run("search")
            self.assertEqual(2, final["count"])
            self.assertEqual({"a.py", "c.py"}, {item["relative_path"] for item in final["results"]})
            self.assertIn("three changed", next(item["text"] for item in final["results"]
                                                  if item["relative_path"] == "a.py"))
