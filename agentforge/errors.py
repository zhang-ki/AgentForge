"""预期且可向用户展示的 AgentForge 错误。"""


class AgentForgeError(Exception):
    """可在不显示堆栈信息的情况下报告的错误基类。"""


class ScanError(AgentForgeError):
    """无法安全扫描指定的仓库。"""


class StoreError(AgentForgeError):
    """无法打开或更新本地索引。"""


class IndexConflictError(StoreError):
    """固定本地索引已绑定到另一个仓库。"""


class ProviderError(AgentForgeError):
    """Provider 执行失败或返回了无效响应。"""


class CitationValidationError(ProviderError):
    """模型引用违反了本次请求的证据协议。"""

    def __init__(self, message: str, *, code: str, diagnostic: dict):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic

