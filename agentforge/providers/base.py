from typing import Protocol

from ..models import Answer, BuiltContext


class Provider(Protocol):
    def answer(self, context: BuiltContext) -> Answer:
        """返回结构化引用属于当前上下文的回答。"""
        ...

