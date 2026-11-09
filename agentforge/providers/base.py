from typing import Protocol

from ..models import Answer, BuiltContext


class Provider(Protocol):
    def answer(self, context: BuiltContext) -> Answer:
        """返回不可信模型文本；来源引用由应用层 Answer Contract 解析和验证。"""
        ...

