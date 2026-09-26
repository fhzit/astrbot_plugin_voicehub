"""日志规范契约：插件代码的日志记录器必须来自 AstrBot 框架。

AstrBot 插件市场要求：日志记录器**必须且只能**从 ``astrbot.api`` 导入
（``from astrbot.api import logger``），不得使用 Python 内置 ``logging``
模块自建 logger —— 后者绕过框架的日志配置，日志级别与格式不受控。

本插件中的纯函数模块（``lib/font_manager.py``、``lib/schedule_image.py``
等不依赖框架的模块）采用**参数注入**：由 ``main.py`` 把框架 logger 传进去，
与 ``lib/push.py`` / ``lib/pull.py`` 的做法一致。这样既满足规范，
又保持这些模块可脱离 AstrBot 单独测试。

这里把该规范写成可执行断言，避免后续新增代码又把内置 logging 带回来。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"

# 允许自带 logger 定义的位置：仅测试目录
EXEMPT_PARTS = ("tests", ".venv")


def _plugin_python_files():
    """插件自身的 Python 文件（排除测试与虚拟环境）。"""
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in EXEMPT_PARTS for part in rel.parts):
            continue
        yield path, rel


def test_no_module_self_creates_logger():
    """插件代码不得用内置 logging 自建 logger，必须从 astrbot.api 取。"""
    offenders = []
    for path, rel in _plugin_python_files():
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*import logging\b", text, re.M):
            offenders.append(f"{rel}: import logging")
        if "logging.getLogger(" in text:
            offenders.append(f"{rel}: logging.getLogger(...)")
    assert not offenders, (
        "以下位置使用内置 logging 自建 logger，应改为 from astrbot.api import logger "
        f"或参数注入：{offenders}"
    )


def test_main_py_uses_framework_logger():
    """插件入口必须从 astrbot.api 导入 logger。"""
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "from astrbot.api import logger" in text, "main.py 应从 astrbot.api 导入 logger"


def test_pure_modules_take_logger_by_injection():
    """不依赖框架的模块应以参数接收 logger，而不是自建。"""
    for name in ("font_manager.py", "schedule_image.py"):
        text = (LIB / name).read_text(encoding="utf-8")
        assert "logging.getLogger(" not in text, f"lib/{name} 不应自建 logger"
        assert "import logging" not in text, f"lib/{name} 不应导入内置 logging"

    font_text = (LIB / "font_manager.py").read_text(encoding="utf-8")
    assert re.search(r"async def ensure_fonts\([^)]*\blogger\b", font_text, re.S), (
        "lib/font_manager.py 的 ensure_fonts 应以参数接收 logger"
    )

    # 参考实现：push.py / pull.py 的注入式做法应保持
    for name in ("push.py", "pull.py"):
        text = (LIB / name).read_text(encoding="utf-8")
        assert "logger: Any" in text, f"lib/{name} 应保持 logger 参数注入"


def test_main_passes_logger_to_ensure_fonts():
    """调用点必须把框架 logger 传给 ensure_fonts。"""
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert re.search(r"ensure_fonts\(\s*font_dir\s*,\s*logger\s*\)", text), (
        "main.py 调用 ensure_fonts 时应传入 logger"
    )
