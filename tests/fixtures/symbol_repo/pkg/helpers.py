"""Helpers for synthetic symbol-index tests."""


def shared_name(value):
    """Return the value unchanged."""
    return value


class Worker:
    """Simple worker."""

    def run(self, value):
        return shared_name(value)

    class Nested:
        def render(self):
            return "nested"


def outer():
    def inner(value):
        return shared_name(value)
    return inner(1)


def duplicate():
    return 1


def duplicate():
    return 2


def ambiguous_call():
    return duplicate()
