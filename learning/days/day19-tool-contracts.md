# Day 19｜工具调用必须经过执行器验证

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 模型建议不等于程序执行

Agent 使用工具时，模型或 Planner 产生的是一个“想调用什么”的结构化请求。若系统收到请求就照做，错误参数可能越界访问文件，未知工具甚至可能执行不应开放的操作。安全系统把提议和执行分开：先检查工具名、参数、路径和范围，只有符合白名单合同的请求才会进入具体实现。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## Prompt 为什么不够

提示词可以告诉模型“不要访问仓库外”，但模型可能误解，也可能被仓库中恶意文本影响。真正的安全约束必须在工具执行代码里再次验证。仓库文件只作为不可信数据；其中的指令不能增添工具、放宽路径或改变用户授权。

AgentForge 当前的工具循环是受限只读，不提供 shell、任意 Python 代码执行、网络访问或补丁写入。这个限制由执行器合同实现，而不是依靠模型自觉。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 参数对象按严格 schema 检查
源码原文：[agentforge/agent_loop.py，第 78–90 行](../../agentforge/agent_loop.py#L78-L90)
```python
def _strict_arguments(
    arguments: Any, *, required: set[str], optional: set[str]
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object.")
    keys = set(arguments)
    missing = required - keys
    unexpected = keys - required - optional
    # 【学习区补充】漏掉必需参数就停止，不用默认猜测替调用者补值。
    if missing:
        raise ValueError("Missing tool arguments: " + ", ".join(sorted(missing)) + ".")
    # 【学习区补充】额外字段也拒绝，避免未声明参数悄悄改变工具行为。
    if unexpected:
        raise ValueError("Unexpected tool arguments: " + ", ".join(sorted(unexpected)) + ".")
    return arguments
```

**逐段讲解。** 必需键、额外键和类型都会检查；模型多给一个字段也不能扩大工具权限。

### 2. 工具执行入口只分派白名单动作
源码原文：[agentforge/agent_loop.py，第 134–140 行](../../agentforge/agent_loop.py#L134-L140)
```python
    def execute(self, call: ToolCall) -> Any:
        if not isinstance(call, ToolCall):
            raise ValueError("Planner output must be a ToolCall.")
        # 【学习区补充】先通过白名单再动态调用，规划结果不能访问任意 Python 方法。
        if call.name not in ALLOWED_AGENT_TOOLS:
            raise ValueError(f"Unknown or forbidden tool: {call.name!r}.")
        _nonempty_text(call.origin, "tool origin", maximum=128)
        return getattr(self, "_tool_" + call.name.replace("-", "_"))(call.arguments)
```

**逐段讲解。** 名称在允许集合之外就拒绝，之后才执行对应函数。Prompt 是指引，执行器才是权限边界。

### 3. 文件工具的路径不能逃出仓库
源码原文：[agentforge/agent_loop.py，第 110–115 行](../../agentforge/agent_loop.py#L110-L115)
```python
def _normalized_relative_path(value: Any) -> str:
    path = _nonempty_text(value, "path", maximum=1024).replace("\\", "/")
    parts = path.split("/")
    # 【学习区补充】拒绝绝对路径、盘符和 ..，工具只能读取规范化的仓库内相对路径。
    if path.startswith("/") or ":" in path or any(part in ("", ".", "..") for part in parts):
        raise ValueError("path must be a normalized repository-relative path.")
    return path
```

**逐段讲解。** 格式检查拒绝绝对路径和父目录跳转，执行时再做实际路径边界检查，避免只信字符串。

## 工具参数和执行权限是两道门

参数 schema 先检查模型或调用者提供的输入形状：必需字段是否存在、类型是否正确、额外字段是否允许。通过 schema 只说明参数符合格式，不代表操作本身获准。执行入口再按白名单把动作分派给具体工具，因此工具名、路径范围和动作类型由程序控制，而不是由模型自由拼出一条命令。

文件读取还需要独立的路径归一化和仓库边界验证。用户给出 `../`、绝对路径或链接目标时，不能只凭字符串前缀判断安全。工具返回值也应保持有限且可审计，避免把整个仓库无约束地塞入上下文。当前设计是受限只读工具合同，不等于通用代码执行能力。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `arguments` | 模型或调用方提交的参数对象。 | 先检查它是否为允许的结构；不符合时在工具执行前拒绝。 |
| `required`、`optional` | 必须提供的字段名集合和额外允许的字段名集合。 | 缺少必需项或出现白名单之外的项都会失败，避免忽略意外参数。 |
| `call` | 一次工具调用请求，包含工具名及参数。 | 执行器按白名单分派；调用对象不能直接转化为任意命令。 |
| `value` | 需要规范化和检查的路径输入。 | 需要拒绝绝对路径、父目录跳转和越界目标，再转换为仓库内相对路径。 |
| `ToolCall.name`、`arguments` | 选择动作及其输入。 | 名称控制可用能力，参数控制作用对象，两项都要独立校验。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
