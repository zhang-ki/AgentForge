from typing import Protocol

from ..models import Answer, BuiltContext


class Provider(Protocol):
    def answer(self, context: BuiltContext) -> Answer:
        """Return an answer whose structured citations belong to context."""
        ...

