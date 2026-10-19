"""Import the source-update modules that sit beside the engine package.

The launcher puts them on sys.path, but ComfyUI loads this folder as a package by file location and the
folder is not on sys.path, so they are loaded from the folder next to freevideo_engine under their own names.
"""
import importlib
import importlib.util
import sys
from pathlib import Path


def load(name):
    if name in sys.modules:
        return sys.modules[name]
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def update_modules():
    """(ed25519, update keys, source bootstrap); the bootstrap imports the first two by their own names."""
    return load('freevideo_ed25519'), load('freevideo_update_keys'), load('freevideo_source_bootstrap')
