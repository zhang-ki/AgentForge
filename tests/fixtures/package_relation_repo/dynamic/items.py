import importlib


def visible() -> str:
    module_name = "dynamic.plugin"
    importlib.import_module(module_name)
    return module_name
