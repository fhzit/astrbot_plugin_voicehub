"""在收集测试模块前安装最小 astrbot 桩模块与包搜索路径。

插件代码在导入期即引用 `astrbot.api`，因此桩必须在任何测试模块被导入前就位。
"""
import importlib
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def _install_astrbot_stubs() -> None:
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("test")
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})

    def command_group(name, **kwargs):  # noqa: ARG001 - 桩只关心装饰器链
        def decorate(fn):
            fn.command = lambda sub, **options: (lambda method: method)
            return fn
        return decorate

    event.filter = types.SimpleNamespace(command_group=command_group)
    event.MessageChain = type("MessageChain", (), {"message": lambda self, text: text})
    star = types.ModuleType("astrbot.api.star")
    star.Context = type("Context", (), {})
    star.Star = type("Star", (), {"__init__": lambda self, context: setattr(self, "context", context)})
    star.register = lambda *args: (lambda cls: cls)
    command = types.ModuleType("astrbot.core.star.filter.command")
    command.GreedyStr = type("GreedyStr", (str,), {})

    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.star": types.ModuleType("astrbot.core.star"),
        "astrbot.core.star.filter": types.ModuleType("astrbot.core.star.filter"),
        "astrbot.core.star.filter.command": command,
    }
    for name, module in modules.items():
        sys.modules[name] = module


_install_astrbot_stubs()


@pytest.fixture(scope="session")
def plugin():
    """加载插件主模块，供各测试模块共用。"""
    return importlib.import_module("astrbot_plugin_voicehub.main")
