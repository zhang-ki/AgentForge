# SQLite 与 Chroma 架构专项审查（2026-10-31）

## 建议：A，保留 SQLite 并先按需求优化

现阶段不迁移。优先解决多仓库索引隔离及干净测试与用户数据库分离；仅当离线基准证明规模/延迟瓶颈存在，再评估 B：SQLite 保留可信元数据、Chroma 作为可选向量候选后端。C（全部迁移 Chroma）不推荐：它不能替代现有事务、文件/行证据及结构化表契约。历史 Vector 排名并非稳定优于词法；改变存储本身不能修复模型语义、排序或拒答。

## 实际代码盘点

- `agentforge/store.py`：SQLite 存放 metadata、files、chunks，以及 symbols、symbol_references、parse_status；v1/v2 可读，显式写入迁移到 v3。固定单仓库数据库，事务内更新、失败回滚。
- `agentforge/indexer.py`、`retriever.py`：索引和词法查询依赖上述行；`package_relations.py` 的独立关系图原型不是 SQLite 中的完整 PackageRelation 表。
- `agentforge/embeddings/vector_store.py`：向量是与来源、内容版本、model ID 关联的 float32 blob；SQL 过滤有效来源后在 Python 中逐条计算余弦并排序，属于精确线性扫描，没有 ANN。文件或符号变化时旧向量失效。上限 10,000 条、4,096 维、128 MiB。
- `agentforge/embeddings/retriever.py` 和 `base.py`：Provider 可替换；index 命令不会自动调用 Embedding。当前用户索引此前报告 vectors=0。测试通常使用 SQLite 内存库或隔离临时库；遗留测试固定校验正式数据库旧哈希，现已不适合用户切换仓库后的状态。

## 对照

| 维度 | 当前 SQLite 实现 | Chroma 单机方案 |
|---|---|---|
| 向量和 ANN | 精确余弦线性遍历；无 ANN | 单机 HNSW 近似检索，可配置余弦；近似结果需验证召回 |
| 精确检索、过滤 | 词法/符号单独实现；向量 SQL 按模型和有效来源过滤 | Collection、metadata filter、向量查询；不替代现有符号与引用校验 |
| 持久化、更新 | 现有 SQLite 同库事务，文件变化清理旧向量 | PersistentClient 本地目录、collection upsert/delete；与现有 SQLite 跨库原子性需另设计 |
| 内存/延迟 | 本轮未测 | 本机未装未测；HNSW 内存与规模和参数有关，官方建议评估足够 RAM |
| Windows/离线 | 本机 Python 标准库已用；不需服务器 | PyPI 有 Windows x86-64 wheel；此机安装、离线依赖闭包和兼容性未实测 |
| 部署和隔离 | 无第三方运行依赖，SQLite 内存测试 | 增加依赖和版本管理；EphemeralClient 可供临时测试，PersistentClient 本地持久化；官方建议生产使用服务模式 |

Chroma 不是“完全不使用 SQLite”：官方架构明确使用 SQLite 等底层组件；其单机存储文档描述 SQLite 元数据与独立 HNSW 索引文件，内部布局随版本变化。HNSW 的距离与当前余弦相似度方向必须转换核验；不能把一个系统的阈值直接套到另一个系统。

## 若将来批准 B，建议的设计与回滚

1. 先冻结版本、许可、离线依赖和 Windows 兼容；保留 SQLite 作为文件身份、源码版本、符号与证据的权威来源。
2. 给可选向量后端定义相同接口，记录仓库 ID、来源 ID、模型/维度/预处理、内容哈希与行范围；避免默认 Embedding 函数自动下载。
3. 由于两套存储没有共享事务，用版本化索引与恢复/失效机制，读取前再次以 SQLite 校验来源，测试中模拟中断、删除、重命名、重复写入及恢复。
4. 在独立临时目录评估相同冻结数据：召回、延迟、内存、磁盘与无证据行为；比较小规模现有精确结果，避免使用 Validation 调参。无性能实测不宣称 Chroma 更快。
5. 默认关闭新后端；出错切回原 SQLite 检索，保留旧数据并通过明确的离线重建恢复，不修改正式索引。任何依赖安装、Schema 或数据迁移需用户另行批准。

## 官方资料

- [Chroma 架构](https://docs.trychroma.com/reference/architecture/overview)
- [单机持久化结构与 SQLite](https://cookbook.chromadb.dev/core/storage-layout/)（文内对内部表的说明有版本限制）
- [HNSW 配置](https://cookbook.chromadb.dev/core/configuration/)
- [Python 本地客户端](https://docs.trychroma.com/reference/python/client)
- [单机性能与内存](https://docs.trychroma.com/guides/deploy/performance)
- [官方 PyPI 包与 Windows wheel](https://pypi.org/project/chromadb/)

本轮未安装 Chroma、没有跑两种后端的性能对照，也没有改产品 Schema 或正式数据。
