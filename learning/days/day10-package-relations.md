# Day 10｜模块关系图：导入、重导出、定义和调用

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 四种关系不能混为一谈

阅读关系查询时，先区分四个问题：

- **导入**：某模块从哪里引入名称？
- **重导出**：包是否把内部名称公开给外部使用？
- **定义**：函数、类或常量实际写在哪里？
- **调用点**：哪一行源码出现了调用表达式？

导入行不是调用行；`__init__.py` 的重导出也不是最终实现。一个完整的跨文件调用答案可能要把“调用点”和“最终定义”一起作为证据。若解析器只能证明中间导入关系，就不应把它说成实际运行调用。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 静态图的能力边界

关系图来自源码快照。它能说明“文件中写了什么导入/调用表达式”，但不会执行程序，也不会观察插件、环境变量、动态注册和运行时替换。因此图中的关系是静态证据。回答时要把源代码可直接看到的事实与解析器推出来的目标分开写。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 一条边说明关系类型和来源范围
源码原文：[agentforge/package_relations.py，第 58–77 行](../../agentforge/package_relations.py#L58-L77)
```python
class PackageRelation:
    relation_id: str
    relation_type: str
    source_path: str
    source_module: str
    source_symbol_id: str
    start_line: int
    end_line: int
    target_module: str | None
    target_name: str | None
    local_name: str | None
    target_path: str | None
    target_symbol_id: str | None
    candidate_symbol_ids: tuple[str, ...]
    resolution_status: str
    cycle_detected: bool = False
    note: str = ""
    target_definition_name: str | None = None
    target_definition_start_line: int | None = None
    target_definition_end_line: int | None = None
```

**逐段讲解。** import、re-export、definition、callsite 是不同角色；结构化关系对象保留身份与状态，调用证据不能由导入边替代。

### 2. 关系图以每个 Python 模块的静态快照为基础
源码原文：[agentforge/package_relations.py，第 325–376 行](../../agentforge/package_relations.py#L325-L376)
```python
def _parse_module(path: str, text: str) -> _Module:
    module, package, is_init = _module_for_path(path)
    tree = ast.parse(text.replace("\r\n", "\n").replace("\r", "\n"), filename=path)
    definitions = {}
    imports = []
    for node in tree.body:
        # 【学习区补充】定义边来自声明节点，和导入边、调用点边是不同类型的事实。
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = (
                _id("S", path, type(node).__name__, f"{module}.{node.name}"),
                node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno,
            )
        # 【学习区补充】普通 import 单独记录别名和模块，后续才能解析调用目标。
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "kind": "import", "module": alias.name, "name": None,
                    "local": alias.asname or alias.name.split(".", 1)[0],
                    "alias": alias.asname is not None, "line": node.lineno,
                    "end": getattr(node, "end_lineno", node.lineno) or node.lineno,
                })
        # 【学习区补充】区分普通导入和带来源模块的导入，关系图才能记录真实边类型。
        elif isinstance(node, ast.ImportFrom):
            absolute = _absolute_module(package, node.module or "", node.level)
            for alias in node.names:
                imports.append({
                    "kind": "from", "module": absolute, "name": alias.name,
                    "local": alias.asname or alias.name,
                    "alias": alias.asname is not None, "line": node.lineno,
                    "end": getattr(node, "end_lineno", node.lineno) or node.lineno,
                })
        # 动态导入表示存在不确定性，不能猜测其目标。
        # 【学习区补充】遍历语法树里的调用表达式；这是静态调用线索，不等于运行时执行证明。
        for child in ast.walk(node):
            # 【学习区补充】只接受调用表达式节点；属性访问或变量读取不会被误当成调用。
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            dotted = (
                func.id if isinstance(func, ast.Name) else
                f"{func.value.id}.{func.attr}" if isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name) else None
            )
            # 【学习区补充】识别动态导入后走专门分支，不能假定目标已被静态解析。
            if dotted not in {"__import__", "importlib.import_module"}:
                continue
            literal = child.args[0].value if child.args and isinstance(child.args[0], ast.Constant) \
                and isinstance(child.args[0].value, str) else None
            imports.append({
                "kind": "dynamic", "module": literal, "name": None,
                "local": None, "alias": False, "line": child.lineno,
                "end": getattr(child, "end_lineno", child.lineno) or child.lineno,
            })
    all_names, all_line, all_end_line, all_dynamic = _static_all(tree)
    return _Module(
        path, module, package, is_init, tree, _id("S", path, "module", module),
        definitions, imports, all_names, all_line, all_end_line, all_dynamic,
    )
```

**逐段讲解。** AST 提取定义、导入与调用并带上行范围；动态导入可标记 uncertain。图描述源码可见关系，不是运行时调用轨迹。

### 3. 绑定链不等于最终定义或调用点
源码原文：[agentforge/package_relations.py，第 467–489 行](../../agentforge/package_relations.py#L467-L489)
```python
    def resolve_binding(module_name, local_name, trail=()):
        key = (module_name, local_name)
        # 【学习区补充】追踪重导出链时检测回环，避免递归在循环关系中无限继续。
        if key in trail:
            return [], True
        module = modules.get(module_name)
        if not module:
            return [], False
        # 【学习区补充】遇到本模块定义就停在最终目标，不把中间导出别名当成实现。
        if local_name in module.definitions:
            symbol_id, start, end = module.definitions[local_name]
            return [(module.path, symbol_id, start, end, local_name)], False
        # 【学习区补充】一个导出绑定可能展开成多个最终定义，先收集再统一去重。
        results = []
        # 【学习区补充】单独记录是否遇到循环；循环会降低结论确定性。
        cycle = False
        for target_module, target_name, _line in bindings[module_name].get(local_name, ()):
            if target_name is None:
                target = modules.get(target_module or "")
                if target:
                    results.append((target.path, target.module_symbol_id, 1, 1, None))
            else:
                found, hit_cycle = resolve_binding(target_module, target_name, trail + (key,))
                results.extend(found)
                cycle = cycle or hit_cycle
        # 【学习区补充】按出现顺序去重，避免同一目标经多条 re-export 链重复出现。
        unique = list(dict.fromkeys(results))
        return unique, cycle
```

**逐段讲解。** 沿导入/导出找到绑定后，还需定位最终 definition；回答谁调用了目标还要独立找到 callsite。这样才不会把模块转发误报成调用。

## 关系图中的边不能互相替代

图上的每条边都需要带有关系类型和源代码位置。`import` 表示某模块引入名称，`re-export` 表示包入口再次暴露名称，`definition` 指向最终定义，`callsite` 则是代码实际写下调用表达式的位置。查询“谁调用了这个函数”时，只命中导入边并不能回答调用问题；可靠证据链通常需要调用点和目标定义，必要时再补充中间绑定路径。

包关系图建立在可静态读取的 Python 文件和模块规则上，因此动态导入、运行时 monkey patch、插件注册等行为可能无法完整映射。图边越多不意味着结论越确定。系统需要保留 unresolved 状态和来源行范围，并在证据缺少某个角色时限制回答，而不是把 import 自动提升为调用或运行时行为证明。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `path`、`text` | 被解析模块的仓库内路径和源码文本。 | 路径用于推导模块身份；文本提供实际 import、定义和调用语句。 |
| `module_name` | 当前解析绑定所在模块。 | 同一个本地名称在不同模块可能代表不同目标，不能脱离模块解释。 |
| `local_name` | 当前模块中使用或重新导出的名称。 | 它可能对应直接导入、别名或包重导出，需要沿绑定链解析。 |
| `trail` | 已经访问过的绑定路径。 | 用于检测循环重导出并防止递归无限继续；默认空元组表示从新链路开始。 |
| `PackageRelation` 的关系类型、源码范围和状态 | 说明是导入、调用、定义还是重导出，以及从哪里解析到。 | 关系类型影响证据角色；源码范围支持回查，解析状态表达是否唯一确定。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
