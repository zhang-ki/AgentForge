# Day 8｜AST 怎样把源代码变成符号

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 从文本搜索到语法结构

如果在文件中搜索 `Conv2d`，可能会找到类定义、注释、文档、字符串和调用。文本搜索只知道字符出现在哪里，不知道这些字符在 Python 语法中代表什么。AST（抽象语法树）把源码解析成结构节点：函数定义是函数节点，类是类节点，调用是调用节点，导入是导入节点。节点带着名称和行号，因此索引可以区分“定义在哪里”和“文字在哪里”。

AST 解析不是执行程序。它能读到 `def f(x)` 的签名，不会运行函数，也不能自动知道参数在运行时实际传入什么；它能看到 `module.f()` 这段调用表达式，但某些动态绑定可能仍然无法解析。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 为什么符号索引与词法索引并存

问“`Conv2d` 类在哪里”时，符号表能直接找定义；问“负责把通道数乘起来的逻辑在哪里”时，词法和语义线索可能更重要。符号索引知道程序结构，但不理解每个业务概念。解析器遇到语法错误时，文件仍可能作为普通文本被切块和词法检索。因此这些索引互补，而不是 AST 取代所有搜索。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 签名从 AST 参数节点生成
源码原文：[agentforge/parsers/python_ast.py，第 50–65 行](../../agentforge/parsers/python_ast.py#L50-L65)
```python
def _signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # 【学习区补充】异步函数与普通函数使用不同 AST 节点；签名输出要保留 async 差异。
        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        args = ast.unparse(node.args)
        result = f"{prefix}def {node.name}({args})"
        if node.returns is not None:
            result += f" -> {ast.unparse(node.returns)}"
        return result
    if isinstance(node, ast.ClassDef):
        arguments = [ast.unparse(item) for item in node.bases]
        arguments.extend(
            f"{item.arg}={ast.unparse(item.value)}" if item.arg else f"**{ast.unparse(item.value)}"
            for item in node.keywords
        )
        return f"class {node.name}({', '.join(arguments)})"
    raise TypeError("Unsupported symbol node.")
```

**逐段讲解。** 代码使用语法树表示参数、默认值及可变参数，而不是用正则猜函数头。签名说明声明接口，不说明运行时传入的值。

### 2. 符号同时包含限定名和源码范围
源码原文：[agentforge/parsers/python_ast.py，第 106–140 行](../../agentforge/parsers/python_ast.py#L106-L140)
```python
    def _add_definition(self, node: ast.AST, name: str, kind: str) -> None:
        # 【学习区补充】对单文件符号数量设上限，防止极端输入耗尽资源。
        if len(self.symbols) >= MAX_SYMBOLS_PER_FILE:
            raise _ParseLimitExceeded("symbol limit exceeded for this file")
        parent = self.stack[-1]
        qualified_name = f"{parent.qualified_name}.{name}"
        signature = _signature(node)
        key = (kind, qualified_name, signature)
        ordinal = self.occurrences[key]
        self.occurrences[key] += 1
        # 【学习区补充】符号身份组合路径、种类、限定名、签名与同名序号，减少跨文件冲突。
        identity = "\0".join((self.relative_path, kind, qualified_name, signature, str(ordinal)))
        symbol_id = _digest("S", identity)
        decorators = getattr(node, "decorator_list", ())
        # 【学习区补充】装饰器行可能早于 def/class；取最小值才能让引用范围包含装饰器。
        start_line = min([node.lineno, *(item.lineno for item in decorators)])
        end_line = max(start_line, getattr(node, "end_lineno", node.lineno) or node.lineno)
        structure = _definition_structure(node, signature)
        symbol = ParsedSymbol(
            symbol_id=symbol_id,
            source_id=self.symbols[0].source_id,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            start_line=start_line,
            end_line=end_line,
            parent_symbol_id=parent.symbol_id,
            signature=signature,
            docstring=ast.get_docstring(node, clean=False),
            structure_hash=hashlib.sha256(structure.encode("utf-8")).hexdigest(),
        )
        self.symbols.append(symbol)
        self.node_symbols[id(node)] = symbol
        self.symbol_nodes[id(node)] = node
        self.stack.append(symbol)
        # 【学习区补充】继续解析函数或类内部定义，限定名因此带有作用域层级。
        for statement in getattr(node, "body", ()):
            self.visit(statement)
        self.stack.pop()
```

**逐段讲解。** 类中方法的限定名带类作用域，source_id 将身份与路径、位置关联；这能区分不同文件里的同名符号。

### 3. 解析器明确区分成功、语法错误和资源失败
源码原文：[agentforge/parsers/python_ast.py，第 354–421 行](../../agentforge/parsers/python_ast.py#L354-L421)
```python
    def parse(self, relative_path: str, source_text: str) -> ParsedFile:
        relative = _normalise_relative_path(relative_path)
        source_id = _source_id(relative)
        module_name, package_name, is_init = _module_parts(relative)
        fallback_name = PurePosixPath(relative).with_suffix("").as_posix().replace("/", ".")
        module_qualified_name = module_name or fallback_name
        module_name_for_symbol = module_qualified_name.rsplit(".", 1)[-1]
        try:
            # Python AST 与索引器都只把 CR/LF 视为源码行边界。
            normalised_source = source_text.replace("\r\n", "\n").replace("\r", "\n")
            tree = ast.parse(normalised_source, filename=relative, type_comments=True)
            # 【学习区补充】AST 节点数超过上限时作为资源失败处理，避免解析成功后无限遍历。
            if _ast_node_limit_exceeded(tree):
                raise _ParseLimitExceeded("AST node limit exceeded for this file")
            line_count = max(1, len(split_source_lines(source_text)))
            module_dump = ast.dump(tree, annotate_fields=True, include_attributes=False)
            module_symbol = ParsedSymbol(
                symbol_id=_digest("S", "\0".join((relative, "module", module_qualified_name))),
                source_id=source_id,
                kind="module",
                name=module_name_for_symbol,
                qualified_name=module_qualified_name,
                start_line=1,
                end_line=line_count,
                parent_symbol_id=None,
                signature=f"module {module_qualified_name}",
                docstring=ast.get_docstring(tree, clean=False),
                structure_hash=hashlib.sha256(module_dump.encode("utf-8")).hexdigest(),
            )
            definitions = _DefinitionCollector(relative, module_qualified_name, module_symbol)
            definitions.visit(tree)
            references = _ReferenceCollector(
                source_id, module_symbol, definitions.node_symbols, relative
            )
            references.visit(tree)
            shadowed_names = _scope_shadowed_names(
                tree, module_symbol, definitions.node_symbols, definitions.symbol_nodes
            )
            return ParsedFile(
                relative_path=relative,
                source_id=source_id,
                parser_name=self.parser_name,
                # 【学习区补充】成功解析会把模块、符号、引用和作用域信息一起返回。
                status="parsed",
                error=None,
                module_name=module_name,
                package_name=package_name,
                is_package_init=is_init,
                symbols=tuple(definitions.symbols),
                references=tuple(references.references),
                scope_shadowed_names=shadowed_names,
            )
        # 【学习区补充】语法错误和资源限制分开分类，后续才能区分源码问题与安全上限。
        except SyntaxError as exc:
            error = f"SyntaxError: {exc.msg} at line {exc.lineno or 1}"
        # 【学习区补充】资源限制单独分类，不与语法错误混为一谈。
        except _ParseLimitExceeded as exc:
            error = f"ResourceLimitExceeded: {exc}"
        except (ValueError, RecursionError, MemoryError) as exc:
            error = f"{type(exc).__name__}: Python source could not be parsed safely"
        return ParsedFile(
            relative_path=relative,
            source_id=source_id,
            parser_name=self.parser_name,
            status="failed",
            error=error,
            module_name=module_name,
            package_name=package_name,
            is_package_init=is_init,
            symbols=(),
            references=(),
        )
```

**逐段讲解。** 入口先约束文本规模，再解析 AST；只有成功解析才返回结构化符号和引用。解析失败仍可保留文本检索可能性，但不能声称关系已解析。

## AST 为检索提供结构，但不会执行程序

解析器把 Python 文本交给语法分析器，得到函数、类、参数、装饰器和调用等节点。访问这些节点时，作用域栈记录当前位于哪个模块、类或函数中，于是同名的 `forward` 可以分别成为 `A.forward` 和 `B.forward`。符号记录再附带源码行范围和签名，精确符号查询就能跳到定义，而不是停留在注释、字符串或普通文本命中。

结构化解析会失败，原因可能是语法错误，也可能是资源限制。两者必须分开记录：语法错误表示源码当前无法解析成 AST，资源限制则表示解析过程主动停止以保护运行资源。文件仍可能进入词法检索，但此时系统不应把纯文本命中说成已经确认的函数定义或静态调用关系。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `node` | AST 函数、异步函数或类定义节点。 | `_signature` 根据节点类型提取参数或基类；传入其他节点类型会明确报不支持。 |
| `name`、`kind` | 定义名称以及符号种类，例如类或函数。 | 与父级限定名组合后区分作用域；种类不同的同名对象不会被混成同一符号。 |
| `relative_path` | 源码在仓库内的位置。 | 参与符号身份计算；不同文件中的同名定义因此可以分开。 |
| `source_text` | 待解析的完整源码文本。 | AST 从它构建；语法错误和资源上限会导致结构化解析失败，而不是猜测结构。 |
| `parent_symbol_id`、限定名、签名 | 这些是解析过程中生成的关联字段。 | 它们让方法归属类、重载同名符号和源码范围可以被稳定区分。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
