# Day 20｜有界 Agent 循环怎样运行

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 一个循环包含计划、执行和观察

Agent 的最小循环可以描述为：Planner 根据目标和之前结果提出下一次调用；工具执行器验证并运行；系统把结果写入审计；Planner 再根据新状态决定继续还是结束。没有停止条件时，循环可能重复同一动作。即使工具只读，重复查询也会消耗时间和资源。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 超时和重试的边界

总超时通常是协作式检查：系统在步骤之间查看时间预算。若某个工具内部卡在不可中断的阻塞调用，外层不一定能立刻杀掉它。自动重试也不是总是安全，尤其是操作可能产生写入时。明确步数、总预算、工具白名单和逐步审计，使 Agent 的行为可以解释并有界停止。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 循环是有界状态推进而非无限代理
源码原文：[agentforge/agent_loop.py，第 285–352 行](../../agentforge/agent_loop.py#L285-L352)
```python
    def run(self, objective: str) -> AgentRunReport:
        objective = _nonempty_text(objective, "objective")
        started = self.monotonic()
        audit: list[ToolAuditRecord] = []
        status = "completed"
        # 【学习区补充】循环有硬步数上限，即使 Planner 持续生成调用也不会无限执行。
        for step in range(1, self.max_steps + 1):
            # 【学习区补充】单调时钟不受系统时间回拨影响，适合测量运行时限。
            if self.monotonic() - started >= self.run_timeout_seconds:
                status = "run_timeout"
                break
            call = self.planner.next_call(objective, tuple(audit))
            # 【学习区补充】None 是 Planner 的停止信号，循环在这里正常结束而不是空转。
            if call is None:
                break
            if not isinstance(call, ToolCall):
                audit.append(ToolAuditRecord(
                    step, "invalid", "unknown", {}, "rejected", 0.0,
                    error="Planner output must be a ToolCall.",
                ))
                status = "failed"
                break
            call_started = self.monotonic()
            try:
                result = self.toolbox.execute(call)
                elapsed = self.monotonic() - call_started
                # 【学习区补充】工具返回后比较耗时；这是超时判定，不会强行中断已运行的工具。
                if elapsed > self.tool_timeout_seconds:
                    audit.append(ToolAuditRecord(
                        step, call.name, call.origin, dict(call.arguments), "timeout",
                        round(elapsed, 6), error="Tool exceeded its wall-clock budget; no retry was attempted.",
                    ))
                    status = "tool_timeout"
                    break
                audit.append(ToolAuditRecord(
                    step, call.name, call.origin, dict(call.arguments), "ok",
                    round(elapsed, 6), result=result,
                ))
            # 【学习区补充】受控错误被写进审计并停止循环，不自动重试或隐藏失败。
            except (AgentForgeError, ValueError, OSError, sqlite3.Error) as exc:
                elapsed = self.monotonic() - call_started
                audit.append(ToolAuditRecord(
                    step, call.name if isinstance(call, ToolCall) else "invalid",
                    call.origin if isinstance(call, ToolCall) else "unknown",
                    dict(call.arguments) if isinstance(call, ToolCall) and isinstance(call.arguments, dict) else {},
                    "rejected", round(elapsed, 6), error=str(exc),
                ))
                status = "failed"
                break
        else:
            # 达到循环上限本身不代表失败：计划可能刚好
            # 用完允许的工具调用次数。此时只查看一次计划器
            # （不执行其他工具），以判断计划是否已经完成
            # 还是仍有工作待处理。
            status = (
                "completed"
                # 【学习区补充】步数用完后只询问 Planner 是否还有任务，不再执行额外工具。
                if self.planner.next_call(objective, tuple(audit)) is None
                else "step_budget_exhausted"
            )

        elapsed = self.monotonic() - started
        if elapsed > self.run_timeout_seconds and status == "completed":
            status = "run_timeout"
        answer, citations, contract, retrieval = self._offline_answer(objective)
        return AgentRunReport(
            objective=objective, status=status, max_steps=self.max_steps,
            completed_steps=len(audit), elapsed_seconds=round(elapsed, 6),
            tool_timeout_seconds=self.tool_timeout_seconds,
            run_timeout_seconds=self.run_timeout_seconds,
            allowed_tools=tuple(sorted(ALLOWED_AGENT_TOOLS)), audit=tuple(audit),
            answer=answer, citations=citations,
            retrieval=retrieval, answer_contract=contract,
        )
```

**逐段讲解。** 每一步要经过计划、步数/总时限检查、执行和审计；失败、超时、步数耗尽或正常完成都会形成不同结束状态。

### 2. 离线汇总与真实聊天有不同身份
源码原文：[agentforge/agent_loop.py，第 354–387 行](../../agentforge/agent_loop.py#L354-L387)
```python
    def _offline_answer(
        self, objective: str,
    ) -> tuple[str, tuple[str, ...], VerifiedAnswerContract, dict[str, Any]]:
        results = getattr(self.toolbox, "last_search_results", [])
        context = build_context(objective, results, max_candidates=5, budget=6000)
        # 【学习区补充】这段 Agent 汇总路径明确使用离线 Provider，不是 Qwen 聊天调用。
        answer = OfflineProvider().answer(context)
        outcome = getattr(self.toolbox, "last_retrieval_outcome", None)
        # 【学习区补充】缺少检索结果时使用不确定路由信息，不能虚构候选或来源。
        if outcome is None:
            route = "uncertain"
            confidence = "low"
            uncertainty = "No product retrieval outcome was produced."
            candidates = {}
            retrieval = {
                "route": route, "confidence": confidence,
                "uncertainty": uncertainty, "unavailable_channels": [],
            }
        else:
            route = outcome.decision.route
            confidence = outcome.decision.confidence
            uncertainty = outcome.uncertainty
            candidates = {item.citation: item for item in outcome.candidates}
            retrieval = {
                "route": route,
                "rule": outcome.decision.rule,
                "reason": outcome.decision.reason,
                "confidence": confidence,
                "uncertainty": uncertainty,
                "unavailable_channels": list(outcome.unavailable_channels),
            }
        contract = build_offline_contract(
            answer, context, route=route, route_confidence=confidence,
            candidate_by_citation=candidates, uncertainty=uncertainty,
        )
        return answer.text, answer.citations, contract, retrieval
```

**逐段讲解。** 它从工具审计和已读证据生成确定性结果，并标为 offline；即使内容有用，也不能写成 Qwen 生成。

## Agent 循环是有上限的状态机

循环通常依次产生计划、验证动作、调用允许的工具、记录结果，再判断是否继续或结束。最大步数、超时和循环检测限制它在错误计划下持续运行；工具调用失败需要进入清晰的失败状态，不能伪装成已经完成。逐步审计记录输入动作和结果，让人能看出 Agent 是在哪一步找到证据或停下来的。

模型生成计划文本不代表工具已经执行，工具成功也不等于最终回答合同通过。每个动作都必须经过执行器验证，最终回答仍需引用校验。离线摘要或确定性 planner 可以用于测试状态推进，但只有 Provider 实际发送并返回模型响应时，才能称为真实模型推理。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `objective` | 用户希望 Agent 完成的目标描述。 | 它会被规划器与工具读取；本身不是授权，也不能覆盖工具白名单。 |
| `audit` | 之前每一步调用和结果的审计记录。 | Planner 用它决定是否继续、是否已完成或是否陷入重复。 |
| 最大步数、超时和工具名单 | 通常由循环对象初始化参数配置。 | 最大步数限制动作数量，超时限制总运行时间，工具名单限制能力范围。 |
| `_offline_answer` 的 `objective` | 无法进入真实聊天时，用于确定性受限回答的目标文本。 | 离线实现不得伪装成模型生成；其结果来源必须保留为离线。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
