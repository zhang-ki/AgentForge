# Day 9｜导入关系为什么会“不确定”

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 名字不是对象身份

Python 中一个名称可以通过直接导入、别名、模块属性、局部变量或动态导入指向目标。比如 `from package import run as execute` 里，代码中使用 `execute()`，定义却叫 `run`。静态解析器要结合导入表和作用域才能尝试还原关系。若同名符号有多个候选、局部变量遮蔽了导入，或导入由运行时字符串决定，就不能仅凭名字猜答案。

静态解析保留 `resolved`、`ambiguous` 或 `unresolved` 一类状态，是为了把已确认的源码关系和无法确定的情况分开。`resolved` 也只代表源代码静态分析找到唯一目标，不代表这个分支在实际运行中一定执行。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 这对回答意味着什么

当用户问“谁调用了这个函数”，系统如果只找到一个可能的引用，就应该说明它是静态推断，不能说成运行时调用轨迹。安全做法不是“总挑一个最像的”，而是让证据携带状态，必要时缩小结论或拒答。这样用户能知道系统看到的是明确源码，还是一个待确认的关系。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 静态解析用状态表达唯一性
源码原文：[agentforge/parsers/resolver.py，第 39–56 行](../../agentforge/parsers/resolver.py#L39-L56)
```python
def _resolve_one(candidates_by_qname, qname: str | None, reference: ParsedReference) -> ParsedReference:
    candidates = tuple(sorted({
        item.symbol_id for item in candidates_by_qname.get(qname or "", ())
    }))
    # 【学习区补充】只有唯一候选才确认为 resolved，不通过排序任意挑选同名符号。
    if len(candidates) == 1:
        return replace(
            reference, target_qualified_name=qname, resolution_status="resolved",
            target_symbol_id=candidates[0], candidate_symbol_ids=candidates,
        )
    # 【学习区补充】多个候选保留 ambiguous，提示静态证据尚不能确定目标。
    if len(candidates) > 1:
        return replace(
            reference, target_qualified_name=qname, resolution_status="ambiguous",
            target_symbol_id=None, candidate_symbol_ids=candidates,
        )
    return replace(
        reference, target_qualified_name=qname, resolution_status="unresolved",
        target_symbol_id=None, candidate_symbol_ids=(),
    )
```

**逐段讲解。** 唯一目标为 resolved，多候选为 ambiguous，无目标为 unresolved。状态名称描述静态知识，不等同于实际执行事实。

### 2. 解析调用时加入模块和作用域线索
源码原文：[agentforge/parsers/resolver.py，第 59–81 行](../../agentforge/parsers/resolver.py#L59-L81)
```python
def _resolve_call_candidates(candidates_by_qname, qnames, reference: ParsedReference) -> ParsedReference:
    # 【学习区补充】删除重复限定名同时保留先后顺序，避免改变解析优先级。
    ordered = tuple(dict.fromkeys(qname for qname in qnames if qname))
    candidates = tuple(sorted({
        symbol.symbol_id
        for qname in ordered
        for symbol in candidates_by_qname.get(qname, ())
        # 【学习区补充】调用解析只考虑可调用符号，变量同名不会被当作函数目标。
        if symbol.kind in ("class", "function", "method")
    }))
    # 【学习区补充】只有唯一限定名才写入目标字段，多条线索仍保持不确定。
    target_name = ordered[0] if len(ordered) == 1 else None
    if len(candidates) == 1:
        return replace(
            reference, target_qualified_name=target_name, resolution_status="resolved",
            target_symbol_id=candidates[0], candidate_symbol_ids=candidates,
        )
    if len(candidates) > 1:
        return replace(
            reference, target_qualified_name=target_name, resolution_status="ambiguous",
            target_symbol_id=None, candidate_symbol_ids=candidates,
        )
    return replace(
        reference, target_qualified_name=target_name, resolution_status="unresolved",
        target_symbol_id=None, candidate_symbol_ids=(),
    )
```

**逐段讲解。** 目标候选不只按函数名查找；当前模块、导入别名、类作用域和局部绑定都会改变名字指向。

### 3. 不同调用写法必须分别保守解析
源码原文：[agentforge/parsers/resolver.py，第 105–156 行](../../agentforge/parsers/resolver.py#L105-L156)
```python
def _resolve_call(
    reference, parsed, imports_by_scope, invalid_imports, shadowed,
    symbols_by_id, symbols_by_qname,
):
    raw = reference.target_name
    # 【学习区补充】带点号的名称先按属性/模块链处理，不能套用裸函数名规则。
    if "." in raw:
        first, rest = raw.split(".", 1)
        for scope in _scope_chain(symbols_by_id, reference.source_symbol_id):
            # 【学习区补充】局部名称遮蔽导入名称时停止解析，避免把同名局部变量错连到模块。
            if first in shadowed.get(scope.symbol_id, ()):
                return replace(reference, resolution_status="unresolved")
            if first in invalid_imports.get(scope.symbol_id, ()):
                return replace(reference, resolution_status="unresolved")
            local_prefix = _resolve_call_candidates(
                symbols_by_qname, (scope.qualified_name + "." + first,), reference
            )
            if local_prefix.resolution_status != "unresolved":
                # 本地函数或类的绑定不是导入的模块别名；
                # 属性解析不在这个保守解析器的处理范围内。
                return replace(reference, resolution_status="unresolved")
            targets = imports_by_scope.get(scope.symbol_id, {}).get(first)
            if targets:
                return _resolve_call_candidates(
                    symbols_by_qname,
                    (target + "." + rest for target in sorted(targets)),
                    reference,
                )
        # 【学习区补充】self/cls 只能在未被遮蔽且能找到所属类时解析为类方法。
        if first in ("self", "cls"):
            if any(first in shadowed.get(scope.symbol_id, ()) for scope in
                   _scope_chain(symbols_by_id, reference.source_symbol_id)):
                return replace(reference, resolution_status="unresolved")
            owner = _nearest_class(symbols_by_id, reference.source_symbol_id)
            if owner:
                return _resolve_call_candidates(
                    symbols_by_qname, (owner.qualified_name + "." + rest,), reference
                )
        return replace(reference, resolution_status="unresolved")

    for scope in _scope_chain(symbols_by_id, reference.source_symbol_id):
        if raw in shadowed.get(scope.symbol_id, ()):
            return replace(reference, resolution_status="unresolved")
        if raw in invalid_imports.get(scope.symbol_id, ()):
            return replace(reference, resolution_status="unresolved")
        local = _resolve_call_candidates(
            symbols_by_qname, (scope.qualified_name + "." + raw,), reference
        )
        if local.resolution_status != "unresolved":
            return local
        # 【学习区补充】只有作用域确实记录了该导入，才沿导入关系继续找调用目标。
        qnames = set(imports_by_scope.get(scope.symbol_id, {}).get(raw, ()))
        resolved = _resolve_call_candidates(symbols_by_qname, qnames, reference)
        if resolved.resolution_status != "unresolved":
            return resolved
    return replace(reference, resolution_status="unresolved")
```

**逐段讲解。** self/cls、点号访问和别名调用需要不同处理；动态属性、名称遮蔽等情况可能无法解析，正确结果是承认不确定而非猜目标。

## 名称解析为什么需要保留不确定状态

看到 `run()` 并不足以知道它最终调用哪个函数：名称可能来自导入、局部变量、参数、类属性，也可能在运行时被替换。解析器会把当前模块、作用域、导入别名和名称遮蔽情况一起考虑；只有候选目标足够唯一时，才建立已解析关系。如果出现多个可能目标，就保留歧义或未解析状态，而不是挑一个“看起来最像”的目标。

不同调用写法表达的信息也不同。直接调用 `module.run()`、从包导入后调用 `run()`、通过对象属性调用，静态分析可见的线索不一样。静态关系可以支持“源码呈现了这样的调用路径”，却不能证明运行时一定绑定到同一个对象。回答中应将源码事实与基于导入规则的推断分开。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `candidates_by_qname` | 从限定名到可能符号的候选映射。 | 候选唯一时才能形成确定绑定；多个候选应保留不确定状态。 |
| `qname` | 要解析的限定名称；允许为 `None` 表示没有可用名字。 | 没有名称时不能靠猜测建立目标关系。 |
| `qnames` | 一组可能的调用目标限定名。 | 解析器逐个检查候选；多个匹配不等于唯一目标。 |
| `reference` | 当前导入或调用引用记录，含位置和解析状态。 | 解析操作会基于它补充目标信息；保留其来源位置才能解释关系来自哪一行。 |
| `imports_by_scope`、`invalid_imports`、`shadowed` | 当前作用域的导入映射、无效导入和被局部名称遮蔽的名字。 | 三者共同避免把局部变量错误解析成模块导入。 |
| `symbols_by_id`、`symbols_by_qname` | 符号身份表和限定名索引。 | 前者用于按稳定 ID 查找，后者用于名称候选检索。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
