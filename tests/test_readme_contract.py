"""README 与配置说明的一致性检查。

文档示例最容易悄悄失真：`_conf_schema.json` 里曾把群 UMO 示例写成适配器名前缀，
README 示例里的会话串也可能与 `parse_umo` 的实现脱节。这里把两者绑定起来，
让文档与代码一起被测试。
"""

import json
import re
from pathlib import Path

from lib.config import dropped_umo_values, parse_umo, umo_message_type

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SCHEMA = ROOT / "_conf_schema.json"


def test_readme_json_examples_are_valid():
    """README 中所有 JSON 示例必须能解析。"""
    blocks = re.findall(r"```json\n(.*?)```", README.read_text(encoding="utf-8"), re.S)
    assert blocks, "README 应至少包含一个 JSON 示例"
    for block in blocks:
        json.loads(block)


def test_readme_push_example_matches_server_rules():
    """推送示例须符合入站服务的校验规则（含 UMO 形状）。"""
    push = json.loads(re.findall(r"```json\n(.*?)```", README.read_text(encoding="utf-8"), re.S)[0])
    assert isinstance(push.get("content"), str) and push["content"]

    targets = push["targets"]
    # server.py 只接受这两个键，且不接受调用方自造的群会话
    assert set(targets) <= {"group", "umo"}

    for umo in targets.get("umo", []):
        parsed = parse_umo(umo)
        assert parsed is not None, f"README 示例中的 UMO 无法被实现解析: {umo}"
        assert parsed[1] == "FriendMessage", f"私聊示例的会话类型应为 FriendMessage: {umo}"


def test_readme_group_umo_example_is_wellformed():
    """README 给出的群 UMO 示例必须合法，且不被配置过滤器丢弃。"""
    text = README.read_text(encoding="utf-8")
    examples = re.findall(r"^([A-Za-z0-9_-]+:GroupMessage:[0-9]+)$", text, re.M)
    assert examples, "README 应给出群 UMO 的正例"
    for umo in examples:
        assert umo_message_type(umo) == "GroupMessage", umo
        assert not dropped_umo_values(umo, "GroupMessage"), umo


def test_malformed_umo_examples_are_actually_dropped():
    """README 描述的「会被忽略」的取值，实现必须真的丢弃。"""
    for malformed in ["myroom", "default:FriendMessage:1", "default:GroupMessage"]:
        assert dropped_umo_values(malformed, "GroupMessage"), (
            f"README 称此类取值会被忽略，但实现未丢弃: {malformed}"
        )


def test_readme_documents_every_config_option():
    """`_conf_schema.json` 里的每个配置项都要在 README 中有说明。"""
    text = README.read_text(encoding="utf-8")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    undocumented = [key for key in schema if f"`{key}`" not in text]
    assert not undocumented, f"README 未说明这些配置项: {undocumented}"


def test_group_umos_hint_warns_against_adapter_name_prefix():
    """配置提示须说明群 UMO 前缀是平台实例 ID，不能照抄适配器名。"""
    hint = json.loads(SCHEMA.read_text(encoding="utf-8"))["group_umos"]["hint"]
    assert "实例" in hint, "hint 应说明前缀是平台实例 ID"
    assert "适配器" in hint, "hint 应提醒不要写成适配器名"
    # 示例本身必须是「实例ID:GroupMessage:会话ID」形状
    assert re.search(r"[A-Za-z0-9_-]+:GroupMessage:\d+", hint), hint
