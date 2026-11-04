# Day 5｜SQLite、PostgreSQL 和 Chroma 的分工

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 为什么项目里有三种数据库

数据库不是按“新旧”替换，而是按数据责任分工。AgentForge 的每个仓库 SQLite 保存当前代码快照、片段、符号和静态关系；PostgreSQL 保存会话、消息、摘要、Memory 的正式状态和来源；Chroma 保存根据文本计算出的向量候选。Chroma 可重建，不能作为会话或记忆状态的唯一来源。切换仓库时要打开对应仓库的 SQLite 文件，而不是清空前一个仓库的数据。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 权威记录和搜索副本

SQLite/ PostgreSQL 保存系统要信任的当前记录；Chroma 用于更快找到“可能相关”的向量候选。向量命中后仍要按仓库 ID、路径、内容哈希和记忆状态回查权威数据。这个流程有意多做一步校验：相似度高不代表内容仍有效，也不代表候选属于当前仓库。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 规范化路径生成稳定 repository_id
源码原文：[agentforge/repositories.py，第 32–34 行](../../agentforge/repositories.py#L32-L34)
```python
def repository_identity(path: str | os.PathLike[str]) -> str:
    root = canonical_repository(path)
    # 【学习区补充】哈希基于规范化后的完整仓库路径，作为稳定 repository_id。
    return hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
```

**逐段讲解。** 同一个仓库的身份可跨运行复用，不同仓库不因为相同文件名而合并。该 ID 会成为索引、会话和向量过滤的隔离依据。

### 2. 登记仓库与索引内容是两步
源码原文：[agentforge/repositories.py，第 138–157 行](../../agentforge/repositories.py#L138-L157)
```python
    def register(self, path: str | os.PathLike[str], *, display_name: str | None = None,
                 index_path: str | os.PathLike[str] | None = None) -> dict:
        root = canonical_repository(path)
        repo_id = repository_identity(root)
        # 【学习区补充】显示名去除首尾空格；空白名称会退回目录名。
        name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else root.name
        if len(name) > 200 or any(ord(char) < 32 for char in name):
            raise ValueError("Repository display name is invalid.")
        db_path = Path(index_path) if index_path is not None else self.index_path(repo_id)
        # 【学习区补充】只允许索引存放在 AgentForge 管理的位置，拒绝任意外部数据库路径。
        if db_path not in (DATABASE_PATH, self.index_path(repo_id)):
            raise ValueError("Repository index path is outside AgentForge storage.")
        # 【学习区补充】登记语句通过 SQLite 事务写入，异常时可以整体回滚。
        with self.connection:
            self.connection.execute("""
                INSERT INTO repositories(repository_id, canonical_path, display_name, index_path, indexed_at, registered_at)
                VALUES (?, ?, ?, ?, NULL, ?)
                # 【学习区补充】重复登记使用更新；如果仓库已经索引，原有索引路径不会被覆盖。
                ON CONFLICT(repository_id) DO UPDATE SET
                    canonical_path=excluded.canonical_path,
                    display_name=excluded.display_name,
                    index_path=CASE WHEN repositories.indexed_at IS NULL THEN excluded.index_path ELSE repositories.index_path END
            """, (repo_id, str(root), name, str(db_path), datetime.now(timezone.utc).isoformat()))
        return self.get(repo_id)
```

**逐段讲解。** 注册表保留路径、显示名、索引路径与索引时间；登记不意味着文件已被扫描。

### 3. 切换仓库打开专属 SQLite
源码原文：[agentforge/repositories.py，第 228–242 行](../../agentforge/repositories.py#L228-L242)
```python
def open_index_for_repository(path: str | os.PathLike[str], *, readonly: bool = False):
    """打开仓库专属索引；仅当旧版索引确属该仓库时才复用。"""
    from .store import IndexStore

    root = canonical_repository(path)
    repo_id = repository_identity(root)
    dedicated = RepositoryRegistry.index_path(repo_id)
    if dedicated.exists() or dedicated.is_symlink():
        return IndexStore(dedicated, readonly=readonly)
    if DATABASE_PATH.exists() or DATABASE_PATH.is_symlink():
        with IndexStore(DATABASE_PATH, readonly=True) as legacy:
            owner = legacy.metadata().get("repository")
        # 【学习区补充】旧版索引只有在元数据确认归属当前仓库时才复用，否则打开独立索引。
        if owner and os.path.normcase(owner) == os.path.normcase(str(root)):
            return IndexStore(DATABASE_PATH, readonly=readonly)
    return IndexStore(dedicated, readonly=readonly)
```

**逐段讲解。** 优先打开 repository_id 专属数据库；历史单库仅在元数据证明所有者相同后复用。切换活动仓库不会把另一仓库索引覆写掉。

### 4. 增量同步区分新增、改变和未变化
源码原文：[agentforge/store.py，第 284–353 行](../../agentforge/store.py#L284-L353)
```python
    def sync(self, root: Path, documents: list[dict], signature: str,
             replace_repository: bool = False, *, force_rebuild: bool = False) -> dict:
        if self.schema_version != INDEX_VERSION:
            raise StoreError("Writable index schema migration did not complete.")
        self.check_repository(root, replace_repository)
        old_root = self.metadata().get("repository")
        changing_owner = bool(old_root and os.path.normcase(old_root) != os.path.normcase(str(root)))
        rebuilding = bool(old_root and not changing_owner and force_rebuild)
        # 【学习区补充】切换仓库与同仓库强制重建都会刷新当前库，但删除计数不同。
        switching = changing_owner or rebuilding
        previous = {
            row["relative_path"]: row
            for row in self.connection.execute("SELECT * FROM files")
        }
        accepted = {document["relative_path"] for document in documents}
        added = changed = unchanged = removed = 0
        with self.connection:
            # 【学习区补充】只有确认需要切换所有者或重建时才清空当前索引内容。
            if switching:
                removed = len(previous) if changing_owner else 0
                self.connection.execute("DELETE FROM files")
                previous = {}
            for document in documents:
                relative = document["relative_path"]
                old = previous.get(relative)
                # 【学习区补充】相对路径此前不存在，走新增分支并取得新文件 ID。
                if old is None:
                    cursor = self.connection.execute(
                        "INSERT INTO files(relative_path, content_hash, size_bytes, mtime_ns, chunk_signature) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (relative, document["content_hash"], document["size_bytes"],
                         document["mtime_ns"], signature),
                    )
                    file_id = cursor.lastrowid
                    added += 1
                else:
                    file_id = old["id"]
                    # 【学习区补充】文件内容和切块配置都一致时才复用旧片段；任一改变都要重建。
                    same = old["content_hash"] == document["content_hash"] and old["chunk_signature"] == signature
                    self.connection.execute(
                        "UPDATE files SET content_hash = ?, size_bytes = ?, mtime_ns = ?, chunk_signature = ? WHERE id = ?",
                        (document["content_hash"], document["size_bytes"], document["mtime_ns"], signature, file_id),
                    )
                    # 【学习区补充】内容哈希与切块配置都一致时，保留已有代码块，不重写这些代码块。
                    if same:
                        unchanged += 1
                        continue
                    self.connection.execute(
                        # 【学习区补充】内容更新前删除旧向量，防止过期片段继续成为检索候选。
                        "DELETE FROM vector_embeddings WHERE file_id = ?", (file_id,)
                    )
                    self.connection.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
                    changed += 1
                self.connection.executemany(
                    "INSERT INTO chunks(file_id, start_line, end_line, text, content_hash) VALUES (?, ?, ?, ?, ?)",
                    [(file_id, chunk.start_line, chunk.end_line, chunk.text, chunk.content_hash)
                     for chunk in document["chunks"]],
                )
            for relative, old in previous.items():
                # 【学习区补充】旧索引存在但本次扫描不再出现的文件会被清理。
                if relative not in accepted:
                    self.connection.execute("DELETE FROM files WHERE id = ?", (old["id"],))
                    removed += 1
            # 【学习区补充】符号和关系随文件快照同步，避免结构索引落后于源码块。
            self._sync_structured(documents)
            for key, value in (
                ("repository", str(root)),
                ("repository_id", repository_identity(root)),
                ("indexed_at", datetime.now(timezone.utc).isoformat()),
                ("chunk_signature", signature),
                ("parser_version", "python-ast-v1"),
            ):
                self.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        return {"added": added, "changed": changed, "unchanged": unchanged, "removed": removed}
```

**逐段讲解。** sync 按相对路径和内容/切块哈希比较，事务内维护文件、chunks、结构索引与元数据。未变化文件保留旧代码块；变化文件清理旧块再写新块。注意，当前 `indexer.py` 会在调用 `sync` 前读取、切块并解析本次扫描到的候选文件；`sync` 的 `unchanged` 判断只跳过该文件代码块的重写，不代表扫描、读取或解析被跳过。之后 `_sync_structured(documents)` 仍会按本次结果刷新符号、引用和解析状态。

## 三种存储按数据责任分工

切换仓库时，`repository_id` 决定应打开哪一个代码索引。项目以 SQLite 保存文件快照、切块、符号和静态引用，因此每个仓库有自己的索引文件；重新索引一个仓库时，不应该覆盖另一个仓库的文件表。当前实现仍会扫描、读取、切块并解析本次候选文件；若文件内容和切块配置未变化，哈希会让 `sync` 复用已有代码块，减少重复写入。

PostgreSQL 与 Chroma 的职责不同。PostgreSQL 是会话、消息、记忆审批和来源记录的权威数据源；Chroma 保存可以重建的向量副本，负责相似候选检索。向量命中后仍须回查权威记录和源码身份。仓库登记也不等于已经索引：登记只建立名称、路径和稳定身份的映射，后续扫描与写库才会产生可搜索内容。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `path` | 要登记、识别或打开索引的仓库目录。 | 规范化后的路径参与仓库身份计算；移动仓库可能得到新的身份。 |
| `display_name` | 给用户看的仓库名称。 | 只影响显示，不应成为隔离索引的唯一键。 |
| `index_path` | 仓库索引数据库的位置。 | 只能落在 AgentForge 管理的受控数据目录，防止调用者指定任意数据库文件。 |
| `readonly` | 以只读方式打开已有索引。 | 适用于搜索、检查等不写数据的操作；写入流程需要明确使用可写连接。 |
| `root`、`documents` | 分别是扫描根目录和本次扫描得到的文件集合。 | 增量同步依据它们对比新增、变化、未变和删除的文件。 |
| `signature` | 由 `chunk_signature(window_lines, overlap_lines)` 生成，描述切块配置。 | 切块行数或重叠行数变化时会触发代码块重建；它当前不包含解析器版本。解析状态、符号和引用会在 `sync` 中按本次解析结果刷新。 |
| `replace_repository`、`force_rebuild` | 分别要求替换所有者数据或强制重建当前仓库索引。 | 它们不是普通增量更新；启用前必须确认作用范围，只影响当前仓库数据库。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
