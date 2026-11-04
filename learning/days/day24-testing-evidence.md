# Day 24｜不同测试分别能证明什么

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 测试层次不是简单的好坏排序

单元测试用小输入检查一个函数的边界；集成测试把多个真实模块接起来，但可能仍用临时数据库或 Mock Provider；真实服务测试使用实际 PostgreSQL、Chroma、Embedding 或聊天模型。它们回答不同问题。Mock 能稳定模拟 HTTP 400、超时或奇怪 JSON；它不能证明本机服务当前能连接。真实模型的一次成功也不代表所有查询都正确。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 覆盖率和跳过项

行覆盖率表示某行是否执行过，分支覆盖率检查条件的不同结果是否执行过。覆盖率高不能证明测试断言抓住了业务错误；覆盖率低则提示可能有未检查路径。权限受限导致跳过的测试不是通过，Mock 行为也不能冒充 Windows 文件系统真实验证。应根据每项证据的来源和范围作结论。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 测试要求调用点与定义同时出现
源码原文：[tests/test_af011_product.py，第 229–250 行](../../tests/test_af011_product.py#L229-L250)
```python
    def test_cross_file_call_includes_exact_callsite_and_final_definition(self):
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            outcome = retriever.search(
                "Which caller calls launch_job and where is its implementation?", top_k=1,
            )
            context = build_context(outcome.query,
                                    [item.as_search_result() for item in outcome.candidates],
                                    max_candidates=5, budget=6000)
        # 【学习区补充】断言实际调用位置和最终定义都存在，防止只命中 import 就算跨文件查询成功。
        self.assertTrue(any(item.relative_path == "app.py" and item.start_line <= 3 <= item.end_line
                            and "launch_job(item)" in item.text for item in outcome.candidates))
        self.assertTrue(any(item.relative_path == "pkg/impl.py" and item.start_line <= 1 <= item.end_line
                            and "def start_job" in item.text for item in outcome.candidates))
        self.assertTrue(any(item.result.relative_path == "app.py" and
                            item.result.start_line <= 3 <= item.result.end_line
                            for item in context.evidence))
        self.assertTrue(any(item.result.relative_path == "pkg/impl.py" and
                            item.result.start_line <= 1 <= item.result.end_line
                            for item in context.evidence))
        # 【学习区补充】不只检查路径，还检查结果确实带有调用点和目标定义这两种证据角色。
        required_roles = {role for item in outcome.candidates for role in item.evidence_roles}
        self.assertTrue({"callsite", "target_definition"} <= required_roles)
        self.assertEqual("caller_definition", outcome.candidates[0].evidence_roles[0])
```

**逐段讲解。** 独立 fixture 的断言检查两个角色而不是单纯结果数量，能发现用 import 代替实际调用的错误。

### 2. 测试定义了引用证明的边界
源码原文：[tests/test_af011_product.py，第 668–686 行](../../tests/test_af011_product.py#L668-L686)
```python
    def test_cited_generated_answer_is_provenance_checked_but_not_semantically_verified(self):
        context = build_context("start_job", [self._result()])
        answer = Answer("It returns a started marker [E1]", (), False)
        contract = verify_generated_answer(
            answer, context, route="exact_symbol", route_confidence="high",
        )
        self.assertEqual(contract.status, "limited")
        self.assertEqual(contract.claims[0].kind, "inference")
        tampered = replace(
            contract,
            evidence=(replace(contract.evidence[0], relative_path="invented.py"),),
        )
        with self.assertRaises(ProviderError):
            validate_answer_contract(tampered, context)
        with self.assertRaises(ProviderError):
            verify_generated_answer(
                # 【学习区补充】E99 不属于本轮证据，断言它会被拒绝，检查的是来源身份门禁。
                Answer("Invented [E99]", (), False), context,
                route="exact_symbol", route_confidence="high",
            )
```

**逐段讲解。** 用例验证引用映射有效，同时保留语义尚未自动证明的状态。它证明合同行为，不证明真实模型回答质量。

### 3. 预算测试验证优先级而非模型理解
源码原文：[tests/test_conversation_context.py，第 23–33 行](../../tests/test_conversation_context.py#L23-L33)
```python
    def test_approved_memory_has_priority_over_optional_history(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "q" * 150),
                 ConversationTurn("c1", "r1", 2, "assistant", "a" * 150))
        memory = self.memory("approved project preference")
        base = self.base().bytes_used
        memory_cost = self.base(memories=(memory,)).bytes_used - base
        history_cost = self.base(history=turns).bytes_used - base
        budget = base + max(memory_cost, history_cost) + 1
        plan = self.base(history=turns, memories=(memory,), budget_bytes=budget)
        # 【学习区补充】预算紧张时仍保留相关获批 Memory；这里验证的是优先级规则。
        self.assertEqual(("m1",), plan.memory_ids)
        self.assertEqual(2, plan.dropped_turns)
```

**逐段讲解。** 测试确认可选历史不会把获批相关 Memory 挤出预算；这属于确定性代码行为，不能冒充真实 Qwen 测试。

## 测试通过证明的是一个明确范围

定向单元测试可以证明某个函数在给定输入下符合断言；临时目录和 SQLite 集成测试能证明多个模块在隔离环境里协作；Mock 可以控制 HTTP 超时或数据库错误，但不能证明真实 Windows 权限或真实模型行为。宿主验收则能证明指定服务、模型、数据和请求在那次运行中完成了什么，仍不能推导任意仓库或所有问题都可靠。

测试报告应同时列运行数、通过数、失败数、错误数和跳过数，跳过不算通过。真实链接权限测试被跳过时，相关边界只能说由模拟或静态检查覆盖，不能声称物理行为已验证。代码行覆盖率也只说明哪些行执行过，不说明断言是否充分或业务结论正确。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `self` | unittest 当前测试用例对象。 | 提供断言方法和测试生命周期；它不是产品函数需要的业务参数。 |
| `test_cross_file_call...` 中的 fixture | 测试创建的合成仓库、查询和索引。 | 让测试能明确断言调用位置与最终定义同时出现。 |
| `context`、`answer` | 被测试的上下文对象和模型回答对象。 | 断言可分别检查当前证据身份与生成引用，不需要真实模型服务。 |
| Memory 与 history 样本 | 测试构造的获批记忆和可选历史消息。 | 用来观察预算选择优先级；Mock 通过只证明确定性规则，不证明 Qwen 语义。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
