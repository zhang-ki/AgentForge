# Day 2｜系统中的数据对象为什么要分层

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 同一段代码在流程中会有不同身份

当系统找到一段源码时，它首先只是一个候选。候选可能重复、过期、来自错误仓库或与问题关系不大。经过身份核验和选择后，它才成为可引用的证据。证据再被格式化进一次模型请求；模型返回的文本又要经过引用及回答合同检查。若把所有这些都塞进一个字符串，程序就很难分清“搜索命中”“实际给模型看过”“回答引用过”分别发生没有。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 这套分层有什么用

这条数据边界让系统能准确回答几个审计问题：检索过什么？哪些内容真的发给模型？模型用了哪些请求内引用？回答合同通过了什么检查？用户因此看到的输出才有机会被追溯，而不是一段无来源的字符串。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 检索候选保存内容和来源身份
源码原文：[agentforge/models.py，第 7–19 行](../../agentforge/models.py#L7-L19)
```python
class SearchResult:
    repository: str
    relative_path: str
    start_line: int
    end_line: int
    text: str
    # 【学习区补充】哈希标识片段内容；路径和行号标识来源位置，两者不能互相替代。
    content_hash: str
    score: float
    evidence_roles: tuple[str, ...] = ()

    @property
    def citation(self) -> str:
        # 【学习区补充】引用位置由应用从路径和行号生成，不让模型自报可被信任的位置。
        return f"{self.relative_path}:{self.start_line}-{self.end_line}"
```

**逐段讲解。** 每条候选同时保存仓库、相对路径、行范围、哈希和分数。路径加行号派生 citation，避免下游只持有无法追溯的纯文本。

### 2. 一次模型请求的结构化上下文
源码原文：[agentforge/models.py，第 33–39 行](../../agentforge/models.py#L33-L39)
```python
class BuiltContext:
    question: str
    evidence: tuple[Evidence, ...]
    text: str
    estimated_tokens: int
    budget: int
    # 【学习区补充】该字段说明这是 UTF-8 字节上界估算，不是模型 Tokenizer 的精确计数。
    estimate_kind: str = "UTF-8 byte upper-bound proxy; not a tokenizer count"
```

**逐段讲解。** 问题、Evidence 对象、格式化内容、估算量与上限并存。这样模型看到文本，校验器仍能用结构数据找回 E1 的真实来源。

### 3. 共同接口只统一调用方式
源码原文：[agentforge/providers/base.py，第 6–9 行](../../agentforge/providers/base.py#L6-L9)
```python
# 【学习区补充】Protocol 只约定调用接口；实现类不必继承它，也不会因此获得额外权限。
class Provider(Protocol):
    def answer(self, context: BuiltContext) -> Answer:
        """返回结构化引用属于当前上下文的回答。"""
        ...
```

**逐段讲解。** 协议只约定 answer 接收 BuiltContext 并返回 Answer；具体 Provider 是否真实请求模型，须检查实现和当前配置。

## 把候选、上下文和回答看成三个阶段

`SearchResult` 描述的是检索器找到的候选片段：它带着仓库、路径、行范围、文本和来源分数。它还不是最终证据，因为候选可能过期、重复，或不适合当前问题。应用从候选构造本次请求的 `Evidence`，再把这些证据编号并格式化到 `BuiltContext`。所以文本虽然送给模型，应用手里仍保留结构化的路径和行号，之后可以核对模型引用的 `[E1]` 究竟映射到什么。

接口协议只统一调用约定，不会让每个 Provider 自动拥有相同能力。离线 Provider 可以从已有证据构造确定性拒答或摘要；聊天 Provider 才会访问本机服务。调用者应根据返回类型、请求统计和配置判断运行到了哪条分支，而不要仅凭方法名或统一的 `answer()` 接口推断发生了真实推理。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `SearchResult.repository`、`relative_path` | 记录候选来自哪个仓库及仓库内路径。 | 仓库身份用于隔离；相对路径便于在仓库中定位，不能单独代替仓库身份。 |
| `start_line`、`end_line`、`text` | 记录源码范围和该范围的内容。 | 行号让用户回到原文，文本是检索/生成材料；两者需要与当前源码一致。 |
| `content_hash`、`score`、`evidence_roles` | 分别标识内容版本、检索排序分数和证据承担的角色。 | 哈希用于识别旧内容；分数只用于排序；角色可表达调用点或定义等要求。 |
| `BuiltContext.question`、`evidence`、`text` | 保留原问题、结构化证据以及发给模型的格式化文本。 | `text` 不取代 `evidence`；验证器需要结构化对象找回 E 编号的真实来源。 |
| `estimated_tokens`、`budget`、`estimate_kind` | 记录估算用量、预算和估算方法。 | 字段名中的 tokens 不代表一定由 tokenizer 精确计数；本项目这里明确使用字节代理估算。 |
| `answer(context)` 的 `context` | 把本轮问题与证据交给 Provider。 | Provider 不应另行使用其他会话或仓库的证据补足回答。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
