# Day 21｜确定性 Planner 和模型 Agent 的区别

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 看起来会“多步操作”，不代表模型自主规划

AgentForge 的循环框架允许 Planner 提出下一步，但当前 `OfflineAgentPlanner` 根据目标和已观察结果用确定性规则选择工具。也就是说，循环和工具是真实代码，决策策略是确定性的；它并不等于 Qwen 在每一步自由决定调用何种工具。真实聊天问答成功，也不证明模型工具选择已经接入或通过验收。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 设计层次

可以把系统分成三种责任：Planner 决定“下一步建议什么”；执行器决定“这件事是否获准、如何执行”；循环管理器决定“还允许运行多久、何时停止”。把责任分层后，未来替换决策器不必同时放松安全检查。当前产品只承诺已实现的确定性只读路径；模型自主规划是另一个需要独立设计与真实验收的能力。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 规划接口本身不授予工具能力
源码原文：[agentforge/agent_loop.py，第 71–75 行](../../agentforge/agent_loop.py#L71-L75)
```python
# 【学习区补充】Planner 协议只描述返回 ToolCall 的接口；是否能执行工具由 Toolbox 控制。
class AgentPlanner(Protocol):
    def next_call(
        self, objective: str, audit: tuple[ToolAuditRecord, ...]
    ) -> ToolCall | None:
        """返回下一条结构化调用；规划完成时返回 None。"""
```

**逐段讲解。** Planner 只提供下一步调用建议；读文件和搜索必须由分离的 Toolbox 进行校验。

### 2. 当前 Agent 规划是确定性规则
源码原文：[agentforge/agent_loop.py，第 233–255 行](../../agentforge/agent_loop.py#L233-L255)
```python
    def next_call(self, objective: str, audit: tuple[ToolAuditRecord, ...]) -> ToolCall | None:
        # 【学习区补充】只把执行成功的审计记录用于下一步决策，失败调用不会被当成证据。
        successful = [item for item in audit if item.status == "ok"]
        if not successful:
            return ToolCall(
                "index",
                {"repository": str(self.repository), "replace_repository": self.replace_repository},
                "offline_planner",
            )
        # 【学习区补充】只有一个成功步骤时按预设顺序继续搜索，不假设其他工具已运行。
        if len(successful) == 1:
            return ToolCall("search", {"query": objective, "top_k": self.top_k}, "offline_planner")
        search_record = next((item for item in successful if item.name == "search"), None)
        if len(successful) == 2 and _IDENTIFIER.fullmatch(objective.strip()):
            return ToolCall("list-symbols", {"name": objective.strip(), "limit": self.top_k}, "offline_planner")
        if not any(item.name == "read" for item in successful) and search_record:
            results = search_record.result.get("results", []) if isinstance(search_record.result, dict) else []
            if results:
                first = results[0]
                return ToolCall(
                    "read",
                    {"path": first["relative_path"], "start_line": first["start_line"], "end_line": first["end_line"]},
                    "offline_planner",
                )
        return None
```

**逐段讲解。** 它根据目标及审计历史选择工具或停止；这可复现且易解释，不表示模型已自主规划或能突破只读白名单。

## 规则路由与模型 Agent 不能混为一谈

确定性 Planner 根据明确规则选择下一步，例如精确符号问题走符号索引、调用关系问题查静态关系。相同输入在相同代码和索引下通常会得到相同路由，便于测试和定位。模型 Agent 则可能根据上下文生成新的动作提议，灵活度更高，也带来不可预测输入和权限控制问题。

不论计划来自规则还是模型，计划层都不应该直接执行任意内容。实际执行必须由有白名单的工具层接手，并重新核验参数和路径。AgentForge 当前的只读边界来自工具执行器和仓库约束，不能因为模型说“我已经查过”就把它当成工具执行证据。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `objective` | 当前要完成的任务目标。 | Planner 根据它选择下一项白名单动作；任务文字不能自动赋予额外权限。 |
| `audit` | 先前执行步骤及工具输出的不可变记录序列。 | 它提供当前状态和重复检测依据；缺失历史会影响 Planner 判断是否继续。 |
| `ToolCall | None` 返回值 | 返回下一步调用，或 `None` 表示停止。 | `None` 是有意义的结束状态，不代表异常；调用对象还要交给执行器验证。 |
| `Protocol` 上的 `objective`、`audit` | 接口约定的输入名称和类型。 | 它只保证调用形状，不保证 Planner 是模型驱动或具备执行权限。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
