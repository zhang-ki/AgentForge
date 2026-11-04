# Day 4｜切块、重叠和行号

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 为什么不能把整个仓库都交给模型

仓库可能远大于一次请求能容纳的输入。索引阶段会把文件切成片段，检索时只选择与当前问题有关的片段。切块太大，命中后会带入许多无关代码；切块太小，函数定义可能被拆开，片段失去理解所需的语境。重叠窗口是在两者之间折中：相邻片段共享少量行，降低重要语句恰好落在边界两侧的风险，但也会产生重复内容。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 为什么行范围是证据的一部分

检索命中某个片段后，回答需要给出相对路径和行号。行范围让人能快速回到真实源文件，也让应用能在回答前确认索引摘录仍对应当前内容。哈希帮助发现“索引时文本”和“现在文件文本”不一致。若文件变了，旧片段不能仅因为数据库里仍有记录就继续作为当前证据。

所以 chunk 不只是“供向量库搜索的一块文本”，而是带身份、范围和版本线索的证据载体。它把索引效率与可追溯回答连接起来。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 片段保留路径、范围和内容身份
源码原文：[agentforge/chunker.py，第 11–16 行](../../agentforge/chunker.py#L11-L16)
```python
class Chunk:
    relative_path: str
    start_line: int
    end_line: int
    text: str
    content_hash: str
```

**逐段讲解。** Chunk 是索引存储的基本材料：行号支持引用，哈希支持变更检测，chunk index 标识同一文件内片段顺序。

### 2. 窗口和重叠如何形成行区间
源码原文：[agentforge/chunker.py，第 19–48 行](../../agentforge/chunker.py#L19-L48)
```python
def split_source_lines(text: str) -> list[str]:
    """只按 CRLF、LF 或 CR 换行；其他 Unicode/控制分隔符仍视为行内字符。

    末尾的换行符只结束前一行，不会凭空增加另一行源码行。
    非空文件若只包含一个行终止符，就有一行空白行。
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not text:
        return []
    lines: list[str] = []
    start = index = 0
    length = len(text)
    while index < length:
        char = text[index]
        # 【学习区补充】单独处理 CR，下面再区分 CRLF，确保一组换行只切一次。
        if char == "\r":
            lines.append(text[start:index])
            # 【学习区补充】CRLF 两个字符共同构成一个换行，所以一次跳过两个位置。
            index += 2 if index + 1 < length and text[index + 1] == "\n" else 1
            start = index
        # 【学习区补充】单独 LF 在这里结束当前行；CRLF 已在前一个分支整体消费。
        elif char == "\n":
            lines.append(text[start:index])
            index += 1
            start = index
        else:
            index += 1
    # 【学习区补充】只有末尾确有未终止文本时才追加最后一行，避免多造空行。
    if start < length:
        lines.append(text[start:])
    elif text[-1] not in ("\r", "\n"):
        lines.append("")
    return lines
```

**逐段讲解。** 参数校验后按固定步长产出文本段及起止行。相邻段略有重叠，减少函数跨边界被截断后无法理解的情况。

### 3. 先拒绝不安全的相对路径再切块
源码原文：[agentforge/chunker.py，第 59–100 行](../../agentforge/chunker.py#L59-L100)
```python
def chunk_text(
    relative_path: str,
    text: str,
    window_lines: int = 80,
    overlap_lines: int = 10,
) -> list[Chunk]:
    if (not isinstance(relative_path, str) or not relative_path
            or relative_path.startswith(("/", "\\")) or ":" in relative_path):
        raise ValueError("relative_path must be a non-empty repository-relative path")
    if ".." in relative_path.replace("\\", "/").split("/"):
        raise ValueError("relative_path must not traverse outside the repository")
    if window_lines <= 0:
        raise ValueError("window_lines must be greater than zero")
    if overlap_lines < 0 or overlap_lines >= window_lines:
        raise ValueError("overlap_lines must be at least zero and smaller than window_lines")
    lines = split_source_lines(text)
    if not lines:
        return []

    # 【学习区补充】步长小于窗口长度时会产生重叠，让跨块边界的代码仍能保留上下文。
    step = window_lines - overlap_lines
    # 【学习区补充】先估算窗口数量并检查上限，不必创建所有块后再发现超量。
    count = 1 + max(0, (len(lines) - window_lines + step - 1) // step)
    # 【学习区补充】先估算切块数量并设上限，避免大输入创建过多对象。
    if count > MAX_TOTAL_CHUNKS:
        raise ValueError("Chunk limit exceeded; use a larger line window.")
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        end = min(start + window_lines, len(lines))
        content = "\n".join(lines[start:end])
        # 【学习区补充】哈希对应块的实际文本，内容相同的片段可稳定识别。
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        chunks.append(
            Chunk(
                relative_path=relative_path.replace("\\", "/"),
                start_line=start + 1,
                end_line=end,
                text=content,
                content_hash=digest,
            )
        )
        if end == len(lines):
            break
        start += step
    return chunks
```

**逐段讲解。** 路径检查确保片段身份属于仓库内文件；切块后对每段内容计算哈希。相关性由检索器决定，chunker 不负责挑答案。

## 分块同时解决模型输入和来源定位

扫描得到的文件通常比单次模型上下文大得多，因此切块负责把文件拆成可检索的窗口。窗口太大，会让无关代码占用预算；窗口太小，函数头、判断分支和返回语句可能落在不同片段里，读者难以理解完整逻辑。重叠让相邻块共享少量内容，缓解边界处的上下文断裂，但也会造成重复候选，所以检索阶段仍需要去重。

每个块不只是字符串：相对路径、起止行、块序号和内容哈希共同说明它来自哪里、在文件中的位置以及文本是否变化。换行格式也会影响行号映射，因此切块器对 LF、CRLF 和 CR 作明确处理。后续引用验证会重新对照当前源码；如果文件已经改变，旧块不能继续被当成当前版本的证据。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `text` | 要按源码行拆分或切块的文件文本。 | 换行符会影响行号边界，因此解析器和切块器必须采用一致的换行规则。 |
| `relative_path` | 块所属文件在仓库内的相对路径。 | 让每个块可以回到原文件；不应使用任意绝对路径作为对模型展示的来源。 |
| `window_lines` | 每个块最多覆盖的行窗口，默认值为 80 行。 | 增大能保留更多上下文但会占用更多检索/生成预算；减小则可能拆开一个逻辑。 |
| `overlap_lines` | 相邻窗口重复包含的行数，默认值为 10 行。 | 适度重叠能保住边界附近的上下文；过大则带来重复候选，且必须小于窗口大小。 |
| `Chunk.start_line`、`end_line`、`content_hash` | 记录块位置和内容版本。 | 块文本更新后哈希变化；引用位置由行号决定，不能从内容相似度推测。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
