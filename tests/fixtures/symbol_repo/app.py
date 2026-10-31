"""Synthetic application module."""


def shared_name(value):
    return value


def app_call():
    return shared_name(1)


class Controller:
    def execute(self, value):
        return value
