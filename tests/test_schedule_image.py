"""测试 schedule_image 纯函数（不依赖 AstrBot，不依赖字体下载）。

策略：
- mock _load_fonts（返回 ImageFont.load_default()，不需要真实 .ttf 文件）
- mock _fetch_cover（控制封面下载行为，不发网络请求）
- 所有测试都用 asyncio.run()，无需 pytest-asyncio
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image as PILImage
from PIL import ImageFont


# ──────────────────────────────────────────────────────────────
# 辅助：构造默认字体映射（用 load_default 代替真实 .ttf）
# ──────────────────────────────────────────────────────────────

def _default_font() -> ImageFont.ImageFont:
    return ImageFont.load_default()


def _mock_fonts() -> dict:
    """返回与 _load_fonts 相同结构的假字体字典。"""
    from astrbot_plugin_voicehub.lib.schedule_image import (
        FS_ARTIST, FS_FOOTER, FS_GROUP, FS_REQ, FS_SEQ,
        FS_SONG, FS_SUBTITLE, FS_TITLE,
    )
    f = _default_font()
    return {
        "regular": {s: f for s in (FS_SUBTITLE, FS_ARTIST, FS_REQ, FS_FOOTER, FS_GROUP)},
        "bold":    {s: f for s in (FS_TITLE, FS_SONG, FS_SEQ, FS_GROUP)},
    }


# ──────────────────────────────────────────────────────────────
# autouse fixture：给所有测试 patch _load_fonts，避免真实 IO
# ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_load_fonts():
    """所有测试都 mock 字体加载，不依赖本地 .ttf 文件。"""
    from astrbot_plugin_voicehub.lib import schedule_image as si
    with patch.object(si, "_load_fonts", return_value=_mock_fonts()):
        yield


# ──────────────────────────────────────────────────────────────
# 测试数据构造
# ──────────────────────────────────────────────────────────────

def _make_data(
    schedules: list | None = None,
    show_cover: bool = True,
    show_requester: bool = True,
    show_votes: bool = False,
) -> dict:
    if schedules is None:
        schedules = [
            {
                "date": "2026/09/21 周一",
                "playTime": "午间广播",
                "sequence": 1,
                "title": "告白气球",
                "artist": "周杰伦",
                "cover": "https://example.com/cover1.jpg",
                "requester": "张三",
                "requesterGrade": "高一",
                "requesterClass": "2班",
                "voteCount": 3,
                "played": False,
            },
            {
                "date": "2026/09/22 周二",
                "playTime": "午间广播",
                "sequence": 2,
                "title": "晴天",
                "artist": "周杰伦",
                "cover": "",           # 无封面 URL
                "requester": "李四",
                "requesterGrade": "高二",
                "requesterClass": "1班",
                "voteCount": 0,
                "played": False,
            },
        ]
    return {
        "success": True,
        "weekRange": "2026/09/21 - 2026/09/27",
        "generatedAt": "2026/09/26 11:00:00",
        "siteTitle": "VoiceHub校园广播站",
        "schedules": schedules,
        "displayConfig": {
            "showCover": show_cover,
            "showSequence": True,
            "showRequester": show_requester,
            "showVotes": show_votes,
            "showPlayTime": True,
            "showDate": True,
        },
    }


# ──────────────────────────────────────────────────────────────
# 测试
# ──────────────────────────────────────────────────────────────

def test_generate_returns_bytes():
    """正常数据下 generate_weekly_schedule_image 必须返回非空 bytes。"""
    from astrbot_plugin_voicehub.lib.schedule_image import generate_weekly_schedule_image
    data = _make_data()
    result = asyncio.run(generate_weekly_schedule_image(data, Path("/mock/fonts")))
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_output_is_valid_png():
    """返回值必须是合法 PNG 文件（以 PNG magic bytes 开头）。"""
    from astrbot_plugin_voicehub.lib.schedule_image import generate_weekly_schedule_image
    data = _make_data()
    result = asyncio.run(generate_weekly_schedule_image(data, Path("/mock/fonts")))
    # PNG magic bytes: 0x89 0x50 0x4E 0x47
    assert result[:4] == b"\x89PNG"


def test_show_cover_false_skips_cover_download():
    """showCover=False 时不应尝试下载封面。"""
    from astrbot_plugin_voicehub.lib import schedule_image as si
    from astrbot_plugin_voicehub.lib.schedule_image import generate_weekly_schedule_image

    call_count = 0

    async def counting_fetch(url: str) -> None:
        nonlocal call_count
        call_count += 1
        return None

    data = _make_data(show_cover=False)
    with patch.object(si, "_fetch_cover", new=counting_fetch):
        asyncio.run(generate_weekly_schedule_image(data, Path("/mock/fonts")))

    # showCover=False 时 _fetch_covers 直接返回空字典，不应调用 _fetch_cover
    assert call_count == 0


def test_show_cover_true_attempts_fetch():
    """showCover=True 时应为每条有封面 URL 的条目尝试下载。"""
    from astrbot_plugin_voicehub.lib import schedule_image as si
    from astrbot_plugin_voicehub.lib.schedule_image import generate_weekly_schedule_image

    call_count = 0

    async def counting_fetch(url: str) -> None:
        nonlocal call_count
        if url:
            call_count += 1
        return None

    data = _make_data(show_cover=True)
    # data 里第一条 cover 非空，期望至少调用 1 次
    with patch.object(si, "_fetch_cover", new=counting_fetch):
        asyncio.run(generate_weekly_schedule_image(data, Path("/mock/fonts")))

    assert call_count >= 1


def test_show_requester_and_votes_flags():
    """displayConfig 里的 showRequester / showVotes 控制字段不影响图片生成成功。"""
    from astrbot_plugin_voicehub.lib.schedule_image import generate_weekly_schedule_image
    for show_req, show_votes in [(True, True), (False, False), (True, False)]:
        data = _make_data(show_requester=show_req, show_votes=show_votes)
        result = asyncio.run(generate_weekly_schedule_image(data, Path("/mock/fonts")))
        assert isinstance(result, bytes) and len(result) > 0


def test_cover_fetch_failure_does_not_raise():
    """封面下载失败（抛异常）不影响整体图片生成。"""
    from astrbot_plugin_voicehub.lib import schedule_image as si
    from astrbot_plugin_voicehub.lib.schedule_image import generate_weekly_schedule_image

    async def failing_fetch(url: str) -> None:
        raise RuntimeError("模拟网络错误")

    data = _make_data(show_cover=True)
    with patch.object(si, "_fetch_cover", new=failing_fetch):
        # _fetch_covers 内部 try/except 应吞掉单个失败
        result = asyncio.run(generate_weekly_schedule_image(data, Path("/mock/fonts")))
    assert isinstance(result, bytes) and len(result) > 0


def test_empty_schedule_returns_image_with_notice():
    """空排期时应返回包含「本周暂无排期」提示的图片（bytes 非空，且尺寸合法）。"""
    from astrbot_plugin_voicehub.lib.schedule_image import generate_weekly_schedule_image
    data = _make_data(schedules=[])
    result = asyncio.run(generate_weekly_schedule_image(data, Path("/mock/fonts")))
    assert isinstance(result, bytes) and len(result) > 0
    # 能被 Pillow 正常解析
    img = PILImage.open(io.BytesIO(result))
    assert img.width == 800


def test_multiple_dates_and_playtimes():
    """多日期、多时段数据正常渲染。"""
    from astrbot_plugin_voicehub.lib.schedule_image import generate_weekly_schedule_image
    schedules = [
        {
            "date": f"2026/09/2{i} 周{c}",
            "playTime": "午间广播" if i % 2 == 0 else "下午广播",
            "sequence": i + 1,
            "title": f"歌曲{i}",
            "artist": f"歌手{i}",
            "cover": "",
            "requester": f"同学{i}",
            "requesterGrade": "高一",
            "requesterClass": "1班",
            "voteCount": i,
            "played": False,
        }
        for i, c in enumerate(["一", "二", "三", "四", "五"])
    ]
    data = _make_data(schedules=schedules)
    result = asyncio.run(generate_weekly_schedule_image(data, Path("/mock/fonts")))
    assert isinstance(result, bytes) and len(result) > 0
