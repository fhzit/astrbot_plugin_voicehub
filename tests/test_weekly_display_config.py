"""测试插件对 VoiceHub 本周歌单新增显示配置的适配。

覆盖 PR 新增能力：
- ``format=text`` 纯文本输出（插件直接转发 VoiceHub 生成的文本）
- ``layoutStyle``（classic / table）与 ``listColumns``（1 / 2）
- ``showLogo`` / ``showSchoolLogo`` / ``showTitle`` / ``showArtist`` 开关
- ``imageConfig`` 中的站点 Logo / 学校 Logo 地址

策略与 test_schedule_image.py 一致：mock 字体加载与封面下载，不发网络请求。
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image as PILImage
from PIL import ImageFont


def _default_font() -> ImageFont.ImageFont:
    return ImageFont.load_default()


def _mock_fonts() -> dict:
    from astrbot_plugin_voicehub.lib.schedule_image import (
        FS_ARTIST, FS_FOOTER, FS_GROUP, FS_REQ, FS_SEQ,
        FS_SONG, FS_SUBTITLE, FS_TABLE_CELL, FS_TABLE_SUB, FS_TITLE,
    )
    f = _default_font()
    return {
        "regular": {s: f for s in (FS_SUBTITLE, FS_ARTIST, FS_REQ, FS_FOOTER, FS_GROUP, FS_TABLE_SUB)},
        "bold": {s: f for s in (FS_TITLE, FS_SONG, FS_SEQ, FS_GROUP, FS_TABLE_CELL)},
    }


@pytest.fixture(autouse=True)
def patch_load_fonts():
    from astrbot_plugin_voicehub.lib import schedule_image as si
    with patch.object(si, "_load_fonts", return_value=_mock_fonts()):
        yield


@pytest.fixture(autouse=True)
def no_cover_download():
    """默认禁止真实封面下载，避免测试触网。"""
    from astrbot_plugin_voicehub.lib import schedule_image as si

    async def _none(url: str):
        return None

    with patch.object(si, "_fetch_cover", new=_none):
        yield


def _make_data(
    layout: str = "classic",
    columns: int = 1,
    schedules: list | None = None,
    **overrides,
) -> dict:
    if schedules is None:
        schedules = [
            {
                "date": "2026/09/21 周一",
                "playTime": "午间广播",
                "sequence": 1,
                "title": "告白气球",
                "artist": "周杰伦",
                "cover": "",
                "requester": "张三",
                "requesterGrade": "高一",
                "requesterClass": "2班",
                "voteCount": 3,
                "played": False,
            },
            {
                "date": "2026/09/21 周一",
                "playTime": "午间广播",
                "sequence": 2,
                "title": "晴天",
                "artist": "周杰伦",
                "cover": "",
                "requester": "李四",
                "requesterGrade": "高二",
                "requesterClass": "1班",
                "voteCount": 0,
                "played": False,
            },
        ]

    display = {
        "layoutStyle": layout,
        "listColumns": columns,
        "showLogo": True,
        "showSchoolLogo": False,
        "showCover": True,
        "showTitle": True,
        "showArtist": True,
        "showRequester": True,
        "showVotes": False,
        "showSequence": True,
        "showPlayTime": True,
        "showDate": True,
    }
    display.update(overrides)

    return {
        "success": True,
        "weekRange": "2026/09/21 - 2026/09/27",
        "generatedAt": "2026/09/26 11:00:00",
        "siteTitle": "VoiceHub校园广播站",
        "schedules": schedules,
        "displayConfig": display,
        "imageConfig": {
            **display,
            "siteLogoUrl": "https://example.com/logo.png",
            "schoolLogoUrl": "",
        },
    }


def _render(data: dict) -> PILImage.Image:
    from astrbot_plugin_voicehub.lib.schedule_image import generate_weekly_schedule_image
    raw = asyncio.run(generate_weekly_schedule_image(data, Path("/mock/fonts")))
    assert raw[:4] == b"\x89PNG"
    return PILImage.open(io.BytesIO(raw))


# ──────────────────────────────────────────────────────────────
# 1. 纯文本接口
# ──────────────────────────────────────────────────────────────

def test_voicehub_client_requests_text_format():
    """插件取纯文本时必须带上 format=text 查询参数。"""
    from astrbot_plugin_voicehub.lib.voicehub import VoiceHubClient
    from astrbot_plugin_voicehub.lib import voicehub as vh

    captured = {}

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):
            return {"success": True, "text": "VoiceHub校园广播站 本周歌单"}

        async def text(self):
            return "VoiceHub校园广播站 本周歌单\n2026/09/21 - 2026/09/27"

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _Resp()

    config = type("C", (), {
        "voicehub_base_url": "https://vh.example.com",
        "voicehub_token": "tok",
        "request_timeout_seconds": 5,
    })()

    with patch.object(vh.aiohttp, "ClientSession", _Session):
        result = asyncio.run(VoiceHubClient(config).get_weekly_schedule_text())

    assert result["ok"] is True
    assert "format=text" in captured["url"]
    assert "VoiceHub校园广播站" in result["text"]


def test_weekly_command_uses_plain_text_when_requested(plugin):
    """VoiceHub 返回纯文本时，命令必须直接发送文本而不是渲染图片。"""
    from tests.test_plugin import FakeEvent

    async def run():
        p = plugin.VoiceHubPlugin(type("NS", (), {})(), {"webhook_token": "t"})

        async def fake_text():
            return {"ok": True, "text": "VoiceHub校园广播站 本周歌单\n1. 告白气球 - 周杰伦"}

        async def forbidden_image(*a, **k):
            raise AssertionError("纯文本模式下不应生成图片")

        p.voicehub_client.get_weekly_schedule_text = fake_text
        p.voicehub_client.get_weekly_schedule = forbidden_image
        p.plugin_config.voicehub_base_url = "https://vh.example.com"

        # 配置为文本模式
        p.plugin_config.weekly_output_mode = "text"
        results = [item async for item in p.vh_weekly(FakeEvent("/广播 本周歌单"), "")]
        assert results and "告白气球" in results[0]

        # 参数显式指定文本模式同样生效（即使配置为图片）
        p.plugin_config.weekly_output_mode = "image"
        results = [item async for item in p.vh_weekly(FakeEvent("/广播 本周歌单 文本"), "文本")]
        assert results and "告白气球" in results[0]
    asyncio.run(run())


def test_weekly_command_renders_image_by_default(plugin):
    """默认（图片模式）必须走出图路径，不能误走纯文本。"""
    async def run():
        p = plugin.VoiceHubPlugin(type("NS", (), {})(), {"webhook_token": "t"})

        async def forbidden_text():
            raise AssertionError("图片模式下不应请求纯文本")

        p.voicehub_client.get_weekly_schedule_text = forbidden_text
        p.plugin_config.weekly_output_mode = "image"
        assert p._weekly_use_text("") is False
        assert p._weekly_use_text("文本") is True
    asyncio.run(run())


# ──────────────────────────────────────────────────────────────
# 2. 布局样式与列数
# ──────────────────────────────────────────────────────────────

def test_table_layout_renders_without_error():
    """layoutStyle=table 时仍能输出合法 PNG。"""
    img = _render(_make_data(layout="table"))
    assert img.width > 0 and img.height > 0


def test_classic_two_columns_is_wider_or_shorter():
    """双列布局的相对几何应不同于单列（宽度不变、行数减半）。"""
    one = _render(_make_data(layout="classic", columns=1))
    two = _render(_make_data(layout="classic", columns=2))
    assert (one.width, one.height) != (two.width, two.height)


def test_two_columns_only_applies_to_classic():
    """表格排版忽略 listColumns，两个取值应产出同一几何。"""
    a = _render(_make_data(layout="table", columns=1))
    b = _render(_make_data(layout="table", columns=2))
    assert (a.width, a.height) == (b.width, b.height)


# ──────────────────────────────────────────────────────────────
# 3. 显示开关
# ──────────────────────────────────────────────────────────────

def test_show_title_false_hides_title():
    """关闭标题显示后，不应再出现歌名文本块（高度随之变化）。"""
    shown = _render(_make_data(showTitle=True))
    hidden = _render(_make_data(showTitle=False))
    assert shown.height != hidden.height or shown.tobytes() != hidden.tobytes()


def test_show_artist_false_hides_artist():
    shown = _render(_make_data(showArtist=True))
    hidden = _render(_make_data(showArtist=False))
    assert shown.tobytes() != hidden.tobytes()


def test_show_logo_toggles_header_logo():
    """站点 Logo 开关影响头部区域。"""
    data = _make_data(showLogo=False)
    without = _render(data)
    data["displayConfig"]["showLogo"] = True
    data["imageConfig"]["showLogo"] = True
    withlogo = _render(data)
    # Logo 关闭时头部更紧凑（或缺省不占位）
    assert without.height <= withlogo.height


def test_header_shows_site_title_top_left_beside_logo():
    """站点标题应像打印排期一样显示在图片左上角，并与 Logo 同排。"""
    from astrbot_plugin_voicehub.lib import schedule_image as si

    data = _make_data()
    data["siteTitle"] = "校广播站"
    img = _render(data)

    # 标题与 Logo 处于同一水平带：标题基线应落在 Logo 占据的纵向范围内
    assert si.LOGO_SIZE > 0
    title_y = si._header_title_top(True if data["imageConfig"]["siteLogoUrl"] else False)
    assert 0 <= title_y < si.LOGO_SIZE + si.PAD_V, "标题应与 Logo 同排，而不是压在 Logo 上方或下方"


def test_header_site_title_falls_back_when_missing():
    """站点标题缺失时回退 VoiceHub，且不得影响出图。"""
    from astrbot_plugin_voicehub.lib.schedule_image import resolve_site_title

    assert resolve_site_title({"siteTitle": "  "}) == "VoiceHub"
    assert resolve_site_title({}) == "VoiceHub"
    assert resolve_site_title({"siteTitle": "校园广播站"}) == "校园广播站"
    assert resolve_site_title({"imageConfig": {"siteTitle": "后台标题"}}) == "后台标题"


def test_header_without_logo_still_places_title_top_left():
    """关闭 Logo 时标题仍占据左上角，不留空位。"""
    from astrbot_plugin_voicehub.lib import schedule_image as si

    data = _make_data(showLogo=False)
    data["imageConfig"]["showLogo"] = False
    data["imageConfig"]["siteLogoUrl"] = ""
    img = _render(data)
    assert img.width == 800
    assert si._header_title_top(False) == si.PAD_V


def test_table_layout_also_shows_site_title():
    """表格排版同样在左上角展示站点标题。"""
    from astrbot_plugin_voicehub.lib import schedule_image as si

    data = _make_data(layout="table")
    data["siteTitle"] = "校广播站"
    img = _render(data)
    assert img.width == 800
    assert si.resolve_site_title(data) == "校广播站"


def test_requester_text_omits_missing_grade_and_class():
    """服务端不下发年级/班级时，投稿人只显示姓名，不补占位符。"""
    from astrbot_plugin_voicehub.lib.schedule_image import _requester_text

    assert _requester_text({"requester": "张*"}) == "张*"
    assert _requester_text({"requesterGrade": "", "requesterClass": "", "requester": "李*"}) == "李*"
    assert _requester_text({"requesterGrade": "高一", "requesterClass": "2班", "requester": "王五"}) == "高一 2班 王五"


def test_missing_display_config_falls_back_to_defaults():
    """displayConfig 缺失时按默认值渲染，不得抛异常。"""
    data = _make_data()
    data.pop("displayConfig")
    data.pop("imageConfig")
    img = _render(data)
    assert img.width == 800


def test_unknown_layout_falls_back_to_classic():
    """未知 layoutStyle 按经典列表处理，不得抛异常。"""
    data = _make_data(layout="unknown-style")
    img = _render(data)
    assert img.width > 0


def test_unknown_columns_falls_back_to_one():
    """listColumns 非 1/2 时按单列处理。"""
    data = _make_data(columns=7)
    img = _render(data)
    assert img.width > 0


# ──────────────────────────────────────────────────────────────
# 4. 文档与配置契约：新增显示项必须被说明
# ──────────────────────────────────────────────────────────────

def test_readme_documents_layout_and_display_options():
    """README 必须说明排版样式、列数、站点/学校 Logo 与文本输出。"""
    from pathlib import Path as _Path

    text = (_Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    for keyword in ("经典列表", "表格排版", "列表列数", "站点 Logo", "学校 Logo", "纯文本"):
        assert keyword in text, f"README 缺少对 {keyword} 的说明"


def test_schema_documents_weekly_output_mode():
    """_conf_schema.json 必须暴露 weekly_output_mode，且默认图片、取值受约束。"""
    import json
    from pathlib import Path as _Path

    schema = json.loads(
        (_Path(__file__).resolve().parent.parent / "_conf_schema.json").read_text(encoding="utf-8")
    )
    entry = schema.get("weekly_output_mode")
    assert entry is not None, "_conf_schema.json 缺少 weekly_output_mode"
    assert entry["default"] == "image"
    assert set(entry["options"]) == {"image", "text"}


def test_display_defaults_match_site_defaults():
    """插件回退默认值必须与 VoiceHub 服务端 astrbotWeeklyConfig 默认一致。"""
    from astrbot_plugin_voicehub.lib.schedule_image import DISPLAY_DEFAULTS

    assert DISPLAY_DEFAULTS["layoutStyle"] == "classic"
    assert DISPLAY_DEFAULTS["listColumns"] == 1
    assert DISPLAY_DEFAULTS["showLogo"] is True
    assert DISPLAY_DEFAULTS["showSchoolLogo"] is False
    assert DISPLAY_DEFAULTS["showTitle"] is True
    assert DISPLAY_DEFAULTS["showArtist"] is True
    assert DISPLAY_DEFAULTS["showVotes"] is False


def test_logo_slots_respect_switches_and_resolve_relative_urls():
    """Logo 槽位只在下发地址且开关开启时产生，相对路径补全为绝对地址。"""
    from astrbot_plugin_voicehub.lib.schedule_image import logo_slots, normalize_display_config

    display = normalize_display_config({"showLogo": True, "showSchoolLogo": False})
    slots = logo_slots(display, {"siteLogoUrl": "/assets/logo.png", "schoolLogoUrl": "/school.png"},
                       "https://vh.example.com")
    assert slots == [("site", "https://vh.example.com/assets/logo.png")]

    display = normalize_display_config({"showLogo": False, "showSchoolLogo": True})
    slots = logo_slots(display, {"siteLogoUrl": "/assets/logo.png", "schoolLogoUrl": "https://cdn.example.com/s.png"},
                       "https://vh.example.com")
    assert slots == [("school", "https://cdn.example.com/s.png")]

    # 开关开启但未配置地址时不留空位
    assert logo_slots(normalize_display_config({"showLogo": True}), {"siteLogoUrl": ""}) == []


def test_text_request_uses_text_endpoint_not_json():
    """纯文本取件失败时返回错误，不得把 JSON 结构当作文本发送。"""
    from astrbot_plugin_voicehub.lib.voicehub import VoiceHubClient
    from astrbot_plugin_voicehub.lib import voicehub as vh

    class _Resp:
        status = 500

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self, content_type=None):
            return {"message": "机器人令牌无效"}

        async def text(self):
            return ""

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            return _Resp()

    config = type("C", (), {
        "voicehub_base_url": "https://vh.example.com",
        "voicehub_token": "tok",
        "request_timeout_seconds": 5,
    })()

    with patch.object(vh.aiohttp, "ClientSession", _Session):
        result = asyncio.run(VoiceHubClient(config).get_weekly_schedule_text())

    assert result["ok"] is False
    assert "令牌无效" in result["message"]
