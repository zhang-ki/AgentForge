import importlib

def visible(name):
    return importlib.import_module(name)
