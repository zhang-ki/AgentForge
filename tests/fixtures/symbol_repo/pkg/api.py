"""Synthetic API module."""

from .helpers import shared_name as imported_shared
import pkg.helpers as helpers
from . import helpers as helper_module
from .missing import missing_symbol
import importlib


def use(value):
    left = imported_shared(value)
    right = helpers.shared_name(value)
    third = helper_module.shared_name(value)
    return left, right, third


def unresolved():
    return absent_symbol()


def dynamic(name):
    return __import__(name)


def dynamic_importlib(name):
    return importlib.import_module(name)


def shadowed_alias(helpers):
    return helpers.shared_name(3)


def shadowed_by_local_definition():
    def helpers():
        return 1
    return helpers.shared_name()
