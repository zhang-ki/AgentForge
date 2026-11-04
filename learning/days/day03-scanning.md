# Day 3｜扫描器如何安全读取仓库

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## “遍历目录”不是简单的递归

一个仓库可能包含源码、缓存、密钥、超大文件、链接和不断变化的文件。扫描器必须先决定哪些路径属于允许范围，再决定哪些文件适合索引。仅检查路径字符串是不够的：扫描后、实际打开前，文件可能已被换成符号链接或其他对象。因此安全边界至少有两个时点：枚举路径时检查一次，读取文件时再确认一次。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 怎样理解链接和身份检查

符号链接可以让仓库内路径指向仓库外；硬链接则可能让仓库里的另一个名字指向同一文件对象。它们是不同的文件系统机制，所以不能只检查“最终路径是否在仓库内”。扫描器还要根据平台提供的信息识别重解析点、链接计数和打开句柄对应的文件。无法可靠确认时，安全做法是拒绝读取。

读 [test_scanner.py](../../tests/test_scanner.py) 时重点看测试保护的行为，不必运行它：普通文件应正常进入候选；链接逃逸、硬链接或检查后的替换应被挡住。这里体现的是“先枚举、再在使用点核验”的安全设计。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 扫描资源边界在开始前校验
源码原文：[agentforge/scanner.py，第 45–55 行](../../agentforge/scanner.py#L45-L55)
```python
class ScanLimits:
    max_file_bytes: int = MAX_FILE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_files: int = MAX_FILES
    max_entries: int = MAX_ENTRIES

    # 【学习区补充】配置对象创建时就验证上限，避免无效值进入扫描循环。
    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0 or self.max_total_bytes <= 0:
            raise ValueError("byte limits must be greater than zero")
        if self.max_files <= 0 or self.max_entries <= 0:
            raise ValueError("file and entry limits must be greater than zero")
```

**逐段讲解。** 限制对象避免扫描文件数、字节数和目录条目数无限增长；无效的零或负数配置立即失败。

### 2. 路径边界是目录关系而非字符串前缀
源码原文：[agentforge/scanner.py，第 106–126 行](../../agentforge/scanner.py#L106-L126)
```python
def _canonical_inside(root: Path, candidate: Path) -> Path:
    """在使用路径前，重新解析并核验其是否仍位于授权范围内。"""
    try:
        normalized = Path(os.path.abspath(os.fspath(candidate)))
        # 【学习区补充】先检查路径的目录关系，避免把 /repo-other 误认成 /repo 子目录。
        if not _is_within(root, normalized):
            raise ScanReadError("outside_root")
        current = normalized
        # 【学习区补充】从候选路径逐级检查到仓库根目录，验证每个目录组件都没有重解析点。
        while True:
            # 【学习区补充】逐个检查路径组件的 reparse 属性；链接式跳转按安全策略拒绝。
            if _has_reparse_attribute(current.lstat()):
                raise ScanReadError("reparse_point")
            if current == root:
                break
            current = current.parent
        # 【学习区补充】解析符号链接后的真实位置，再次确认它仍在授权仓库内。
        resolved = normalized.resolve(strict=True)
        if not _is_within(root, resolved):
            raise ScanReadError("outside_root")
        return resolved
    except ScanReadError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ScanReadError("unavailable_path") from None
```

**逐段讲解。** 真实路径被规范化后再判断是否处于仓库根下。这样能够避开同前缀目录和路径别名造成的错误判断。

### 3. 读取时重新核验扫描候选
源码原文：[agentforge/scanner.py，第 302–349 行](../../agentforge/scanner.py#L302-L349)
```python
def read_candidate(candidate: ScanCandidate, root: Path, max_file_bytes: int = MAX_FILE_BYTES) -> str:
    """读取一个已发现的文本文件，并重新验证文件类型与仓库边界。"""
    relative_parts = candidate.relative_path.replace("\\", "/").split("/")
    if not relative_parts or any(part in ("", ".", "..") for part in relative_parts):
        raise ScanReadError("invalid_relative_path")
    root_real = root.resolve(strict=True)
    expected = root_real.joinpath(*relative_parts)
    # 【学习区补充】由相对路径重新拼出预期绝对路径，再与扫描时的路径比对。
    if os.path.normcase(str(expected)) != os.path.normcase(str(candidate.absolute_path)):
        raise ScanReadError("invalid_relative_path")
    try:
        path = _canonical_inside(root_real, candidate.absolute_path)
        info = path.lstat()
    except (OSError, RuntimeError, ScanReadError):
        raise ScanReadError("unavailable_path") from None
    if _has_reparse_attribute(info):
        raise ScanReadError("reparse_point")
    if not stat_module.S_ISREG(info.st_mode):
        raise ScanReadError("not_regular_file")
    # 【学习区补充】同时核对硬链接数和设备/文件号，拒绝同一文件的其他路径别名。
    if (info.st_nlink != 1 or not info.st_dev or not info.st_ino
            or (candidate.device is not None and candidate.device != info.st_dev)
            or (candidate.inode is not None and candidate.inode != info.st_ino)):
        raise ScanReadError("file_changed_or_hard_link")
    # 【学习区补充】先用元数据拒绝超大文件，避免为明确超限对象读取正文。
    if info.st_size > max_file_bytes:
        raise ScanReadError("file_size_limit")
    try:
        with open_source(root_real, path) as source:
            # 【学习区补充】对已打开句柄取文件身份，防止路径检查后文件被替换。
            opened = os.fstat(source.fileno())
            if opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ScanReadError("file_changed_or_hard_link")
            # 【学习区补充】多读一个字节才能区分“恰好到上限”和“实际超限”。
            raw = source.read(max_file_bytes + 1)
            # 【学习区补充】读取后再比对身份和修改时间，发现读取期间变化就丢弃结果。
            finished = os.fstat(source.fileno())
            if (finished.st_nlink != 1
                    or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    != (finished.st_dev, finished.st_ino, finished.st_size, finished.st_mtime_ns)):
                raise ScanReadError("file_changed_during_read")
    except OSError:
        raise ScanReadError("unreadable_file") from None
    if len(raw) > max_file_bytes:
        raise ScanReadError("file_size_limit")
    # 【学习区补充】空字节或异常控制字符通常意味着二进制内容，不送入文本解析。
    if b"\x00" in raw or any(byte < 9 or 13 < byte < 32 or byte == 127 for byte in raw):
        raise ScanReadError("binary_content")
    try:
        # 【学习区补充】严格解码失败就拒绝，不用替换字符掩盖损坏或未知编码。
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise ScanReadError("unsupported_encoding") from None
    # 【学习区补充】正文还要经过敏感文本检查，疑似凭证不进入索引。
    if any(pattern.search(text) for pattern in _SECRET_TEXT_PATTERNS):
        raise ScanReadError("secret_like_content")
    return text
```

**逐段讲解。** 从候选生成到读取可能有竞态；此处再次验证路径、文件类型、硬链接和设备/inode，打开句柄后复核身份，最后确认读取期间文件没有变化。

## 从目录枚举到可读取文本的完整路径

扫描不是一次性的“列出文件名”。第一步先用资源上限控制目录深度、文件数量和单文件大小；第二步规范化路径并判断它是否仍属于用户指定的仓库；第三步才尝试打开文件。打开后还要检查句柄对应的真实文件身份，再读取有上限的内容，并确认读取期间文件没有被替换或修改。这样处理是为了防止路径检查与真正读取之间发生变化，也就是常说的检查时与使用时不一致。

链接、硬链接、设备身份和文件号是不同层面的判断。路径在仓库内部，不代表它指向的内容也在仓库内部；两个路径名称不同，也可能指向同一个文件。实现无法可靠确认身份时采取拒绝策略，代价是个别合法文件可能无法入库，收益是不会为了“尽量扫描”而悄悄越过仓库边界。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `ScanLimits` 中的数量、深度和字节上限 | 限制最多扫描多少文件、进入多深目录、读取多大的文件或仓库总量。 | 上限需要在扫描开始前校验；放宽上限会增加耗时和内存，也扩大一次任务的资源范围。 |
| `_canonical_inside(root, candidate)` | `root` 是用户授权的仓库根，`candidate` 是准备检查的目标路径。 | 两者关系按真实规范路径判断，而不是比较字符串前缀；越界或链接跳转会被拒绝。 |
| `read_candidate(candidate, root, max_file_bytes)` | 候选携带扫描时记录的身份；`root` 重申授权边界；`max_file_bytes` 限定读取大小。 | 读取时再次核验身份可发现扫描之后的替换；超限文件不会被完整读入。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
