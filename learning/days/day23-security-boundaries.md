# Day 23｜AgentForge 的四条信任边界

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 数据从不同地方来，可信程度不同

用户问题由用户提供；仓库源码由待分析仓库提供；历史消息和 Memory 是对话背景；模型回答是不可信生成内容。它们不能互相升级权限。尤其是仓库 README、注释或字符串可能包含“忽略此前规则”之类文本，但它们仍只是代码证据，不是系统指令。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## “只读”究竟是什么意思

只读 Agent 不会通过暴露的工具修改源代码，但应用仍可能写自己的索引、会话数据库、日志和向量数据。要准确说安全边界，必须指出读取哪些数据、写入哪些数据、谁有权请求写入，以及工具箱能做什么，而不是笼统说“程序什么都不写”。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 仓库边界按真实位置判断
源码原文：[agentforge/scanner.py，第 106–126 行](../../agentforge/scanner.py#L106-L126)
```python
def _canonical_inside(root: Path, candidate: Path) -> Path:
    """在使用路径前，重新解析并核验其是否仍位于授权范围内。"""
    try:
        normalized = Path(os.path.abspath(os.fspath(candidate)))
        if not _is_within(root, normalized):
            raise ScanReadError("outside_root")
        current = normalized
        while True:
            if _has_reparse_attribute(current.lstat()):
                raise ScanReadError("reparse_point")
            if current == root:
                break
            current = current.parent
        # 【学习区补充】先解析路径链接，再检查最终位置；字符串前缀不能证明目录归属。
        resolved = normalized.resolve(strict=True)
        if not _is_within(root, resolved):
            raise ScanReadError("outside_root")
        return resolved
    except ScanReadError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ScanReadError("unavailable_path") from None
```

**逐段讲解。** 路径解析后才做 containment 检查，且真正读文件还会复核身份，避免输入显示路径与实际文件对象不一致。

### 2. 记忆入上下文前复核来源和作用域
源码原文：[agentforge/conversation_context.py，第 256–277 行](../../agentforge/conversation_context.py#L256-L277)
```python
def _validate_memory_provenance(memory: MemoryCandidate) -> None:
    import re

    if (not isinstance(memory.memory_id, str) or not memory.memory_id.strip()
            or len(memory.memory_id) > 128 or any(ord(char) < 32 for char in memory.memory_id)
            or not isinstance(memory.memory_type, str) or memory.memory_type not in {
                "preference", "project_fact", "workflow", "other"
            }
            or isinstance(memory.version, bool) or not isinstance(memory.version, int)
            or memory.version < 1 or not memory.sources):
        raise ValueError("Approved memory provenance is incomplete.")
    # 【学习区补充】逐条验证记忆来源，包括角色、顺序号和哈希，而不是只信总状态。
    for source in memory.sources:
        if (not isinstance(source, MemorySourceReference)
                or isinstance(source.source_sequence, bool)
                or not isinstance(source.source_sequence, int) or source.source_sequence < 1
                # 【学习区补充】来源必须是用户消息，不能把模型输出伪装成用户确认过的事实。
                or source.source_role != "user"
                or not isinstance(source.source_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", source.source_hash) is None
                or source.message_id is not None and (
                    not isinstance(source.message_id, str) or len(source.message_id) > 128
                    or any(ord(char) < 32 for char in source.message_id))):
            # 【学习区补充】任何来源身份不合格就整体拒绝，避免把不可追溯记忆注入模型。
            raise ValueError("Approved memory provenance is invalid.")
```

**逐段讲解。** 记忆要属于当前允许作用域并能追溯来源；缺失身份、来源错误或跨仓库记忆应拒绝注入。

### 3. 审批状态由关系数据库权威管理
源码原文：[agentforge/memory_store.py，第 183–196 行](../../agentforge/memory_store.py#L183-L196)
```python
    def approve(self, memory_id, repository_id):
        with self.sessions.begin() as session:
            row = self._scoped_memory(session, memory_id, repository_id, lock=True)
            # 【学习区补充】只有待审批记忆能被批准，已撤销记录不会被重新激活。
            if row.approval_status != "pending":
                raise ValueError("Only pending memory suggestions can be approved.")
            now = _now()
            # 【学习区补充】审批前按统一 UTC 时间复核有效期，过期建议不能重新生效。
            if row.expires_at is not None and _utc(row.expires_at) <= now:
                raise ValueError("Expired memory cannot be approved.")
            row.approval_status, row.updated_at = "approved", now
            session.add(MemoryApproval(id=uuid4(), memory_id=row.id, action="approved"))
            self._event(session, row, "approved", {})
            # 【学习区补充】审批写入 PostgreSQL 后排队更新向量；向量库不决定记忆是否获批。
            self._queue(session, row, "upsert")
            session.flush()
            return _memory(row)
```

**逐段讲解。** 批准动作验证当前状态、写入生命周期审计，并安排向量同步。Chroma 里有向量不等于数据库里的记忆仍然有效。

### 4. 撤销后即使向量滞后也不能再使用
源码原文：[agentforge/memory_store.py，第 230–241 行](../../agentforge/memory_store.py#L230-L241)
```python
    def revoke(self, memory_id, repository_id):
        with self.sessions.begin() as session:
            row = self._scoped_memory(session, memory_id, repository_id, lock=True)
            # 【学习区补充】只允许撤销当前有效或待审批记录，重复撤销不会覆盖既有生命周期。
            if row.approval_status not in {"approved", "pending"}:
                raise ValueError("Only active or pending memory can be revoked.")
            row.version += 1
            row.approval_status, row.revoked_at, row.updated_at = "revoked", _now(), _now()
            session.add(MemoryApproval(id=uuid4(), memory_id=row.id, action="revoked"))
            self._event(session, row, "revoked", {"version": row.version})
            # 【学习区补充】数据库状态变更与向量删除任务一起入事务；向量副本稍后由 outbox 同步。
            self._queue(session, row, "delete")
            session.flush()
            return _memory(row)
```

**逐段讲解。** 数据库把记忆改为撤销并产生同步任务；查询层应先复核权威记录，再决定是否进入模型上下文。

## 四条边界分别保护不同对象

仓库边界限制 AgentForge 能读取哪些文件；Memory 边界限制哪些用户事实可以进入其他会话；Provider 边界限制请求发到哪里、使用什么凭证；回答边界限制模型能引用哪些当前证据。每条边界需要自己的来源数据和验证步骤，不能用其中一项替代另一项。例如仓库路径安全并不代表记忆已获批，记忆已获批也不代表它适用于当前仓库。

Chroma 中的向量是可重建副本，可能出现同步延迟，所以审批、撤销、过期和仓库作用域必须回 PostgreSQL 核验。模型上下文里的源码和历史内容都应视为不可信输入，不可执行其中的指令。安全设计通常选择失败关闭：无法确认来源或身份时不继续使用，而不是猜测权限。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `root`、`candidate` | 授权仓库根目录和准备访问的候选路径。 | 规范化后判断候选是否位于授权根下；字符串看似位于目录下并不足够。 |
| `memory` | 需要验证来源、仓库范围和有效期的记忆候选。 | 不满足来源或作用域时拒绝进入上下文，即使向量相似度很高也一样。 |
| `memory_id` | 要审批或撤销的记忆记录身份。 | 生命周期操作只针对该记录，不能按文本相似度批量修改。 |
| `repository_id` | 当前会话或仓库作用域身份。 | 用来确认仓库专属 Memory 是否适用于当前请求；全局记忆遵守另一作用域规则。 |
| 记忆状态与有效期 | PostgreSQL 中权威保存的审批状态和到期时间。 | 审批、撤销、过期决定可用性；Chroma 中旧向量不能覆盖这些状态。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
