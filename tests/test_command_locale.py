"""对外指令本地化契约：插件指令必须全部为中文。

指令是插件最外层的界面。这里把「中文指令」写成可执行的断言，避免后续改动
悄悄把英文旧名重新提为主指令名（英文名只允许作为 alias 保留）。
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
README = ROOT / "README.md"
SCHEMA = ROOT / "_conf_schema.json"

# 不得再作为**主**指令名出现的英文旧名
RETIRED_COMMAND_NAMES = ("bind", "unbind", "status", "test", "song", "pick")

# 必须出现的中文指令名
CHINESE_COMMAND_NAMES = ("绑定", "解绑", "状态", "自检", "点歌", "选歌")


def test_main_py_uses_chinese_command_names_only():
    """`main.py` 不得把英文旧名注册为主指令名，且必须注册全部中文指令名。"""
    text = MAIN.read_text(encoding="utf-8")

    for name in RETIRED_COMMAND_NAMES:
        assert f'.command("{name}")' not in text, f"英文主指令名残留: {name}"
        assert f".command('{name}')" not in text, f"英文主指令名残留: {name}"

    for name in CHINESE_COMMAND_NAMES:
        assert f'.command("{name}"' in text, f"缺少中文指令名: {name}"


def test_english_names_are_only_aliases():
    """英文旧名只能出现在 `alias={...}` 里，作为兼容别名。"""
    text = MAIN.read_text(encoding="utf-8")
    aliases = set(re.findall(r"alias=\{([^}]*)\}", text))
    joined = " ".join(aliases)
    for name in RETIRED_COMMAND_NAMES:
        assert f'"{name}"' in joined, f"英文旧名未作为别名保留: {name}"


def test_command_group_is_chinese_with_vh_alias():
    """指令组注册为中文「广播」，并保留 `vh` 别名。"""
    text = MAIN.read_text(encoding="utf-8")
    assert 'command_group("广播"' in text, "指令组名应为中文「广播」"
    match = re.search(r"command_group\(\s*\"广播\"\s*,\s*alias=\{([^}]*)\}", text)
    assert match is not None, "指令组应带 alias 参数"
    assert '"vh"' in match.group(1), "指令组应保留 vh 别名"


def test_readme_recommends_chinese_commands():
    """README 的指令表必须用中文指令，旧英文写法只允许出现在「兼容旧写法」说明里。"""
    text = README.read_text(encoding="utf-8")

    for name in CHINESE_COMMAND_NAMES:
        assert f"/广播 {name}" in text, f"README 缺少中文指令: /广播 {name}"

    for name in RETIRED_COMMAND_NAMES:
        legacy = f"/vh {name}"
        assert text.count(legacy) <= 1, (
            f"README 仍把 {legacy} 作为推荐用法出现 {text.count(legacy)} 次"
        )


def test_schema_hint_points_to_chinese_status_command():
    """`_conf_schema.json` 的 hint 不得再把 `/vh status` 作为取值方式。"""
    raw = SCHEMA.read_text(encoding="utf-8")
    assert "/vh status" not in raw, "配置提示仍指向旧写法 /vh status"
    schema = json.loads(raw)
    hints = [value.get("hint", "") for value in schema.values()]
    assert any("/广播 状态" in hint for hint in hints), "配置提示应指向 /广播 状态"
