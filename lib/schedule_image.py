"""用 Pillow 绘制本周排期图片。纯函数，不依赖 AstrBot 框架。

显示项、排版样式（classic / table）、列数与站点/学校 Logo 均来自 VoiceHub
后台的 ``astrbotWeeklyConfig``，插件只负责按配置出图，不自行发明样式。
"""

from __future__ import annotations

import asyncio
import io
import math
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
from PIL import Image, ImageDraw, ImageFont

# ── 画布参数 ────────────────────────────────────────────────
IMG_W        = 800
PAD_H        = 40   # 左右 padding
PAD_V        = 40   # 上下 padding
ROW_H        = 56   # 每首歌行高（封面 40 + 上下 8）
COVER_SIZE   = 40
COVER_RADIUS = 4
SEQ_SIZE     = 30
COL_GAP      = 16   # 双列布局的列间距
FOOTER_H     = 70   # 页脚与底部留白
DATE_GROUP_H = 36
PLAYTIME_H   = 32

# ── 表格排版（layoutStyle=table）参数 ───────────────────────
TABLE_HEADER_H  = 34
TABLE_PLAYTIME_H = 28
TABLE_ROW_H     = 68
TABLE_CELL_COVER = 32

# ── Logo ────────────────────────────────────────────────────
LOGO_SIZE = 44
LOGO_GAP  = 12

# ── 颜色 ────────────────────────────────────────────────────
C_BG          = (255, 255, 255)          # #ffffff
C_DIVIDER     = (209, 213, 219)          # #d1d5db  2px 标题区分割线
C_DIVIDER_ROW = (209, 213, 219)          # #d1d5db  1px 行底分割线
C_GROUP_BG    = (248, 249, 250)          # #f8f9fa
C_GROUP_TEXT  = (124, 58, 237)           # #7c3aed  日期组标题
C_GROUP_BAR   = (139, 92, 246)           # #8b5cf6  左侧紫色边框
C_TIME_TEXT   = (37, 99, 235)            # #2563eb  时段标题
C_TIME_BAR    = (11, 90, 254)            # #0b5afe  左侧蓝色边框
C_SONG        = (26, 26, 26)             # #1a1a1a  歌名
C_SUB         = (99, 99, 102)            # #636366  歌手 / 投稿人
C_SEQ_BG      = (240, 240, 240)          # #f0f0f0  序号圆圈背景
C_COVER_PH    = (245, 245, 245)          # #f5f5f5  封面占位
C_TABLE_HEAD  = (243, 244, 246)          # #f3f4f6  表头背景
C_TABLE_LINE  = (209, 213, 219)          # #d1d5db  表格线

# ── 字号 ────────────────────────────────────────────────────
FS_TITLE      = 22
FS_SUBTITLE   = 14
FS_GROUP      = 15
FS_SONG       = 16
FS_ARTIST     = 14
FS_REQ        = 12
FS_FOOTER     = 12
FS_SEQ        = 14
FS_TABLE_CELL = 13
FS_TABLE_SUB  = 11

COVER_TIMEOUT = aiohttp.ClientTimeout(total=5)

# 显示项默认值：与服务端 astrbotWeeklyConfig 的默认一致。
# 服务端未下发某项时按此回退，保证旧版站点也能正常出图。
DISPLAY_DEFAULTS: dict[str, Any] = {
    "layoutStyle": "classic",
    "listColumns": 1,
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

WEEKLY_LAYOUTS = ("classic", "table")

BOOLEAN_KEYS = (
    "showLogo", "showSchoolLogo", "showCover", "showTitle", "showArtist",
    "showRequester", "showVotes", "showSequence", "showPlayTime", "showDate",
)


# ──────────────────────────────────────────────────────────────
# 配置归一化
# ──────────────────────────────────────────────────────────────

def normalize_display_config(display: Any) -> dict[str, Any]:
    """把服务端 displayConfig 归一化为绘图配置。

    非法或缺失的取值一律回退默认，避免上游配置异常导致出图失败。

    Args:
        display: VoiceHub 响应的 ``displayConfig``，可能缺失或类型异常。

    Returns:
        含全部显示项与排版参数的字典。
    """
    source = display if isinstance(display, dict) else {}
    layout = source.get("layoutStyle")
    result: dict[str, Any] = {
        "layoutStyle": layout if layout in WEEKLY_LAYOUTS else DISPLAY_DEFAULTS["layoutStyle"],
        "listColumns": 2 if source.get("listColumns") == 2 else 1,
    }
    for key in BOOLEAN_KEYS:
        value = source.get(key, DISPLAY_DEFAULTS[key])
        result[key] = value if isinstance(value, bool) else DISPLAY_DEFAULTS[key]
    return result


# ──────────────────────────────────────────────────────────────
# 字体加载（同步，供线程池调用）
# ──────────────────────────────────────────────────────────────

def _load_fonts(font_dir: Path) -> dict[str, dict[int, ImageFont.FreeTypeFont]]:
    """加载所有需要的字号，返回 {style: {size: font}}。"""
    reg_path  = font_dir / "HarmonyOS_Sans_SC_Regular.ttf"
    bold_path = font_dir / "HarmonyOS_Sans_SC_Bold.ttf"

    def _f(path: Path, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(path), size)

    reg_sizes  = (FS_SUBTITLE, FS_ARTIST, FS_REQ, FS_FOOTER, FS_GROUP, FS_TABLE_SUB)
    bold_sizes = (FS_TITLE, FS_SONG, FS_SEQ, FS_GROUP, FS_TABLE_CELL)
    return {
        "regular": {s: _f(reg_path, s)  for s in reg_sizes},
        "bold":    {s: _f(bold_path, s) for s in bold_sizes},
    }


# ──────────────────────────────────────────────────────────────
# 图片下载（异步，并发）
# ──────────────────────────────────────────────────────────────

async def _fetch_cover(url: str) -> Image.Image | None:
    """下载单张图片，超时或失败返回 None。"""
    if not url:
        return None
    try:
        async with aiohttp.ClientSession(timeout=COVER_TIMEOUT) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        return img
    except Exception:  # noqa: BLE001
        return None


async def _fetch_covers(schedules: list[dict], show_cover: bool) -> dict[int, Image.Image | None]:
    """并发拉取所有封面；show_cover=False 时直接返回空字典。"""
    if not show_cover:
        return {}
    tasks = {
        i: asyncio.create_task(_fetch_cover(s.get("cover", "")))
        for i, s in enumerate(schedules)
    }
    results: dict[int, Image.Image | None] = {}
    for i, task in tasks.items():
        try:
            results[i] = await task
        except Exception:  # noqa: BLE001
            results[i] = None
    return results


def _absolute_url(url: str, base_url: str) -> str:
    """把站点返回的相对 Logo 地址补全为绝对地址。"""
    if not url or not base_url or url.startswith(("http://", "https://")):
        return url
    try:
        return urljoin(base_url.rstrip("/") + "/", url.lstrip("/"))
    except Exception:  # noqa: BLE001
        return url


def resolve_site_title(data: Any) -> str:
    """取站点标题：优先响应顶层 ``siteTitle``，回退 ``imageConfig.siteTitle``，最后 VoiceHub。

    与打印排期的 ``siteTitle`` 同源（systemSettings.siteTitle），缺失或空白时用
    默认站点名，避免左上角出现空白标题。
    """
    source = data if isinstance(data, dict) else {}
    for candidate in (source.get("siteTitle"), (source.get("imageConfig") or {}).get("siteTitle")
                      if isinstance(source.get("imageConfig"), dict) else None):
        text = str(candidate or "").strip()
        if text:
            return text
    return "VoiceHub"


def logo_slots(display: dict[str, Any], image_config: Any, base_url: str = "") -> list[tuple[str, str]]:
    """返回需要下载的 Logo 槽位 ``[(kind, url), ...]``。

    仅在开关开启且地址非空时占位；关闭时头部不留空位。

    Args:
        display: 归一化后的显示配置。
        image_config: VoiceHub 响应的 ``imageConfig``。
        base_url: VoiceHub 站点地址，用于补全相对路径。

    Returns:
        槽位列表，kind 为 ``site`` / ``school``。
    """
    config = image_config if isinstance(image_config, dict) else {}
    slots: list[tuple[str, str]] = []
    if display.get("showLogo"):
        url = str(config.get("siteLogoUrl") or "").strip()
        if url:
            slots.append(("site", _absolute_url(url, base_url)))
    if display.get("showSchoolLogo"):
        url = str(config.get("schoolLogoUrl") or "").strip()
        if url:
            slots.append(("school", _absolute_url(url, base_url)))
    return slots


async def _fetch_logos(slots: list[tuple[str, str]]) -> dict[str, Image.Image | None]:
    """并发下载 Logo 图片。"""
    tasks = {kind: asyncio.create_task(_fetch_cover(url)) for kind, url in slots}
    results: dict[str, Image.Image | None] = {}
    for kind, task in tasks.items():
        try:
            results[kind] = await task
        except Exception:  # noqa: BLE001
            results[kind] = None
    return results


# ──────────────────────────────────────────────────────────────
# 绘图工具
# ──────────────────────────────────────────────────────────────

def _text_w(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bb = draw.textbbox((0, 0), text, font=font)
    return int(bb[2] - bb[0])


def _draw_rounded_rect(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: tuple,
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill)


def _paste_image(
    canvas: Image.Image,
    source: Image.Image | None,
    x: int,
    y: int,
    size: int,
    radius: int,
) -> None:
    """把图片贴到画布，带圆角遮罩；失败时画占位方块。"""
    if source is None:
        draw = ImageDraw.Draw(canvas)
        _draw_rounded_rect(draw, (x, y, x + size, y + size), radius, C_COVER_PH)
        return

    image = source.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    canvas.paste(image, (x, y), mask)


def _paste_cover(canvas: Image.Image, cover_img: Image.Image | None, x: int, y: int) -> None:
    _paste_image(canvas, cover_img, x, y, COVER_SIZE, COVER_RADIUS)


# ──────────────────────────────────────────────────────────────
# 分组工具
# ──────────────────────────────────────────────────────────────

def _group_by_date(schedules: list[dict]) -> list[tuple[str, list[dict]]]:
    return [(date, list(items)) for date, items in groupby(schedules, key=lambda s: s.get("date", ""))]


def _group_by_playtime(items: list[dict]) -> list[tuple[str, list[dict]]]:
    return [(pt, list(songs)) for pt, songs in groupby(items, key=lambda s: s.get("playTime", ""))]


def _requester_text(song: dict) -> str:
    """投稿人展示文本：年级 班级 姓名（缺项自动跳过）。"""
    parts = [p for p in (song.get("requesterGrade", ""), song.get("requesterClass", ""), song.get("requester", "")) if p]
    return " ".join(parts)


# ──────────────────────────────────────────────────────────────
# 头部与页脚
# ──────────────────────────────────────────────────────────────

def _header_title_top(has_logo: bool) -> int:
    """站点标题的纵向起点。

    有 Logo 时与 Logo 同排（对齐到 Logo 带顶部），无 Logo 时贴头部留白，
    两种情况标题都落在左上角，与打印排期的页头一致。
    """
    return PAD_V + 2 if has_logo else PAD_V


def _header_height(has_logo: bool, has_week_range: bool) -> int:
    block_h = FS_TITLE + 4 + (20 if has_week_range else 0)
    row_h = max(LOGO_SIZE, block_h) if has_logo else block_h
    return PAD_V + row_h + PAD_V // 2 + 10  # 分割线后留白


def _draw_header(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    fonts: dict,
    site_title: str,
    week_range: str,
    logos: dict[str, Image.Image | None],
    slots: list[tuple[str, str]],
) -> int:
    """绘制头部（Logo、站点标题、周范围、分割线），返回正文起始 y。

    版式对齐打印排期页头：左上角依次是站点 Logo、竖线、学校 Logo、标题块，
    标题块第一行是站点标题，第二行是周范围；关闭 Logo 时标题仍占据左上角。
    """
    R = fonts["regular"]
    B = fonts["bold"]
    top = PAD_V
    block_h = FS_TITLE + 4 + (20 if week_range else 0)
    row_h = max(LOGO_SIZE, block_h) if slots else block_h

    x = PAD_H
    if slots:
        for index, (kind, _url) in enumerate(slots):
            if index:
                x += 8
            _paste_image(img, logos.get(kind), x, top, LOGO_SIZE, COVER_RADIUS)
            x += LOGO_SIZE
        x += 12
        # 竖线分隔（与打印排期的 logo-divider 一致）
        draw.line([(x, top + 6), (x, top + row_h - 6)], fill=C_DIVIDER, width=2)
        x += 12

    y = _header_title_top(bool(slots))
    draw.text((x, y), site_title, font=B[FS_TITLE], fill=C_SONG)
    y += FS_TITLE + 4

    if week_range:
        draw.text((x, y), week_range, font=R[FS_SUBTITLE], fill=C_SUB)

    y = top + row_h + PAD_V // 2
    draw.line([(PAD_H, y), (IMG_W - PAD_H, y)], fill=C_DIVIDER, width=2)
    y += 10
    return y


def _draw_footer(draw: ImageDraw.ImageDraw, fonts: dict, y: int, generated_at: str) -> None:
    R = fonts["regular"]
    text = generated_at or datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    label = f"生成时间：{text}"
    width = _text_w(draw, label, R[FS_FOOTER])
    draw.text((IMG_W - PAD_H - width, y + 10), label, font=R[FS_FOOTER], fill=C_SUB)


# ──────────────────────────────────────────────────────────────
# 经典列表排版
# ──────────────────────────────────────────────────────────────

def _classic_content_height(date_groups: list[tuple[str, list[dict]]], display: dict) -> int:
    columns = display["listColumns"]
    height = 0
    for _date, items in date_groups:
        if display["showDate"]:
            height += DATE_GROUP_H
        for playtime, songs in _group_by_playtime(items):
            if display["showPlayTime"] and playtime:
                height += PLAYTIME_H
            height += math.ceil(len(songs) / columns) * ROW_H
    return height


def _draw_song_item(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    fonts: dict,
    song: dict,
    cover: Image.Image | None,
    x_left: int,
    x_right: int,
    y: int,
    display: dict,
) -> None:
    """绘制单条歌曲：序号、封面、标题/歌手、右侧投稿人与热度。"""
    R = fonts["regular"]
    B = fonts["bold"]
    content_x = x_left

    if display["showSequence"]:
        cx = content_x + SEQ_SIZE // 2
        cy = y + ROW_H // 2
        r = SEQ_SIZE // 2
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=C_SEQ_BG)
        draw.text(
            (cx, cy),
            str(song.get("sequence", "")),
            font=B[FS_SEQ],
            fill=C_SONG,
            anchor="mm",
        )
        content_x += SEQ_SIZE + 8

    if display["showCover"]:
        _paste_cover(img, cover, content_x, y + 8)
        content_x += COVER_SIZE + 10

    if display["showTitle"]:
        title = str(song.get("title", ""))
        if title:
            draw.text((content_x, y + 8), title, font=B[FS_SONG], fill=C_SONG)

    if display["showArtist"]:
        artist = str(song.get("artist", ""))
        if artist:
            draw.text((content_x, y + 28), artist, font=R[FS_ARTIST], fill=C_SUB)

    if display["showVotes"]:
        vote_text = f"▲ {song.get('voteCount', 0)}"
        vw = _text_w(draw, vote_text, R[FS_REQ])
        draw.text((x_right - vw, y + 28), vote_text, font=R[FS_REQ], fill=C_SUB)

    if display["showRequester"]:
        req_text = _requester_text(song)
        if req_text:
            rw = _text_w(draw, req_text, R[FS_REQ])
            draw.text((x_right - rw, y + 12), req_text, font=R[FS_REQ], fill=C_SUB)


def _draw_classic(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    fonts: dict,
    date_groups: list[tuple[str, list[dict]]],
    cover_of: dict[int, Image.Image | None],
    display: dict,
    start_y: int,
) -> None:
    """按日期/时段分组绘制经典列表，双列时组内左右并排。"""
    R = fonts["regular"]
    B = fonts["bold"]
    columns = display["listColumns"]
    column_width = (IMG_W - 2 * PAD_H - COL_GAP * (columns - 1)) // columns

    y = start_y
    for date, items in date_groups:
        if display["showDate"]:
            draw.rectangle([(0, y), (IMG_W, y + DATE_GROUP_H)], fill=C_GROUP_BG)
            draw.rectangle([(PAD_H, y + 4), (PAD_H + 3, y + DATE_GROUP_H - 4)], fill=C_GROUP_BAR)
            draw.text((PAD_H + 12, y + 10), date, font=B[FS_GROUP], fill=C_GROUP_TEXT)
            y += DATE_GROUP_H

        for playtime, songs in _group_by_playtime(items):
            if display["showPlayTime"] and playtime:
                draw.rectangle([(PAD_H, y + 6), (PAD_H + 3, y + 26)], fill=C_TIME_BAR)
                draw.text((PAD_H + 12, y + 8), playtime, font=R[FS_GROUP], fill=C_TIME_TEXT)
                y += PLAYTIME_H

            rows = math.ceil(len(songs) / columns)
            for index, song in enumerate(songs):
                column = index % columns
                row = index // columns
                x_left = PAD_H + column * (column_width + COL_GAP)
                row_y = y + row * ROW_H
                _draw_song_item(
                    img, draw, fonts, song, cover_of.get(id(song)),
                    x_left, x_left + column_width, row_y, display,
                )

            for row in range(rows):
                line_y = y + (row + 1) * ROW_H - 1
                draw.line([(PAD_H, line_y), (IMG_W - PAD_H, line_y)], fill=C_DIVIDER_ROW, width=1)

            y += rows * ROW_H


# ──────────────────────────────────────────────────────────────
# 表格排版
# ──────────────────────────────────────────────────────────────

def _table_dates(schedules: list[dict]) -> list[str]:
    return list(dict.fromkeys(s.get("date", "") for s in schedules))


def _table_playtimes(schedules: list[dict]) -> list[str]:
    return list(dict.fromkeys(s.get("playTime", "") for s in schedules))


def _table_uses_playtime(schedules: list[dict], display: dict) -> bool:
    """多时段时才按播出时段分行，与服务端表格一致。"""
    playtimes = _table_playtimes(schedules)
    if not display["showPlayTime"]:
        return False
    if len(playtimes) > 1:
        return True
    return len(playtimes) == 1 and playtimes[0] not in ("", "未指定时段")


def _table_cell_songs(
    songs: list[dict], date: str, playtime: str, use_playtime: bool
) -> list[dict]:
    return [
        s for s in songs
        if s.get("date", "") == date and (not use_playtime or s.get("playTime", "") == playtime)
    ]


def _table_row_count(
    songs: list[dict], dates: list[str], playtime: str, use_playtime: bool
) -> int:
    counts = [len(_table_cell_songs(songs, date, playtime, use_playtime)) for date in dates]
    return max(counts) if counts else 0


def _table_content_height(schedules: list[dict], display: dict) -> int:
    dates = _table_dates(schedules)
    height = TABLE_HEADER_H + 6
    if not dates:
        return height
    if _table_uses_playtime(schedules, display):
        for playtime in _table_playtimes(schedules):
            height += TABLE_PLAYTIME_H + _table_row_count(schedules, dates, playtime, True) * TABLE_ROW_H
    else:
        height += _table_row_count(schedules, dates, "", False) * TABLE_ROW_H
    return height


def _draw_table_cell(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    fonts: dict,
    song: dict,
    cover: Image.Image | None,
    x: int,
    y: int,
    width: int,
    display: dict,
) -> None:
    R = fonts["regular"]
    B = fonts["bold"]
    content_x = x + 6
    if display["showCover"]:
        _paste_image(img, cover, content_x, y + 8, TABLE_CELL_COVER, COVER_RADIUS)
        content_x += TABLE_CELL_COVER + 8

    if display["showTitle"]:
        title = str(song.get("title", ""))
        if title:
            draw.text((content_x, y + 8), title, font=B[FS_TABLE_CELL], fill=C_SONG)

    if display["showArtist"]:
        artist = str(song.get("artist", ""))
        if artist:
            draw.text((content_x, y + 26), artist, font=R[FS_TABLE_SUB], fill=C_SUB)

    meta_parts = []
    if display["showRequester"]:
        requester = _requester_text(song)
        if requester:
            meta_parts.append(requester)
    if display["showVotes"]:
        meta_parts.append(f"▲ {song.get('voteCount', 0)}")
    if meta_parts:
        draw.text((content_x, y + 42), " | ".join(meta_parts), font=R[FS_TABLE_SUB], fill=C_SUB)


def _draw_table(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    fonts: dict,
    schedules: list[dict],
    cover_of: dict[int, Image.Image | None],
    display: dict,
    start_y: int,
) -> None:
    """按日期列 × 序号行绘制表格（对应站点「表格排版」）。"""
    R = fonts["regular"]
    B = fonts["bold"]
    dates = _table_dates(schedules)
    if not dates:
        return

    seq_w = SEQ_SIZE + 10 if display["showSequence"] else 0
    body_width = IMG_W - 2 * PAD_H - seq_w
    date_w = body_width // len(dates)
    row_x = PAD_H

    y = start_y
    # 表头：日期列
    draw.rectangle([(PAD_H, y), (IMG_W - PAD_H, y + TABLE_HEADER_H)], fill=C_TABLE_HEAD)
    if display["showSequence"]:
        draw.rectangle([(row_x, y), (row_x + seq_w, y + TABLE_HEADER_H)], outline=C_TABLE_LINE, width=1)
    for index, date in enumerate(dates):
        x = row_x + seq_w + index * date_w
        draw.rectangle([(x, y), (x + date_w, y + TABLE_HEADER_H)], outline=C_TABLE_LINE, width=1)
        tw = _text_w(draw, date, B[FS_TABLE_CELL])
        draw.text((x + max(4, (date_w - tw) // 2), y + 9), date, font=B[FS_TABLE_CELL], fill=C_SONG)
    y += TABLE_HEADER_H + 6

    use_playtime = _table_uses_playtime(schedules, display)
    playtime_keys = _table_playtimes(schedules) if use_playtime else [""]

    for playtime in playtime_keys:
        if use_playtime:
            draw.rectangle([(PAD_H, y), (IMG_W - PAD_H, y + TABLE_PLAYTIME_H)], fill=C_GROUP_BG)
            draw.text((PAD_H + 8, y + 6), playtime, font=B[FS_TABLE_CELL], fill=C_TIME_TEXT)
            y += TABLE_PLAYTIME_H

        rows = _table_row_count(schedules, dates, playtime, use_playtime)
        for row in range(rows):
            band_y = y + row * TABLE_ROW_H
            if display["showSequence"]:
                draw.rectangle(
                    [(row_x, band_y), (row_x + seq_w, band_y + TABLE_ROW_H)],
                    outline=C_TABLE_LINE, width=1,
                )
                draw.text(
                    (row_x + seq_w // 2, band_y + TABLE_ROW_H // 2),
                    str(row + 1),
                    font=B[FS_SEQ],
                    fill=C_SONG,
                    anchor="mm",
                )
            for index, date in enumerate(dates):
                cell_x = row_x + seq_w + index * date_w
                draw.rectangle(
                    [(cell_x, band_y), (cell_x + date_w, band_y + TABLE_ROW_H)],
                    outline=C_TABLE_LINE, width=1,
                )
                songs = _table_cell_songs(schedules, date, playtime, use_playtime)
                if row < len(songs):
                    song = songs[row]
                    _draw_table_cell(
                        img, draw, fonts, song, cover_of.get(id(song)),
                        cell_x, band_y, date_w, display,
                    )
        y += rows * TABLE_ROW_H


# ──────────────────────────────────────────────────────────────
# 主绘图函数（同步，跑在线程池）
# ──────────────────────────────────────────────────────────────

def _draw_image(
    data: dict,
    covers: dict[int, Image.Image | None],
    logos: dict[str, Image.Image | None],
    slots: list[tuple[str, str]],
    font_dir: Path,
    base_url: str = "",
) -> bytes:
    """纯 Pillow 绘图，返回 PNG bytes。"""
    schedules: list[dict] = data.get("schedules") or []
    display = normalize_display_config(data.get("displayConfig"))

    site_title = resolve_site_title(data)
    week_range = str(data.get("weekRange") or "")

    fonts = _load_fonts(font_dir)
    cover_of = {id(s): covers.get(i) for i, s in enumerate(schedules)}

    date_groups = _group_by_date(schedules)
    content_height = (
        _table_content_height(schedules, display)
        if display["layoutStyle"] == "table" and schedules
        else _classic_content_height(date_groups, display)
    )
    if not schedules:
        content_height += 60

    header_height = _header_height(bool(slots), bool(week_range))
    total_h = header_height + content_height + FOOTER_H

    img = Image.new("RGB", (IMG_W, total_h), C_BG)
    draw = ImageDraw.Draw(img)

    y = _draw_header(img, draw, fonts, site_title, week_range, logos, slots)

    if not schedules:
        draw.text(
            (IMG_W // 2, y + 20),
            "本周暂无排期",
            font=fonts["bold"][FS_SONG],
            fill=C_SUB,
            anchor="mm",
        )
    elif display["layoutStyle"] == "table":
        _draw_table(img, draw, fonts, schedules, cover_of, display, y)
    else:
        _draw_classic(img, draw, fonts, date_groups, cover_of, display, y)

    _draw_footer(draw, fonts, total_h - FOOTER_H, str(data.get("generatedAt") or ""))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────
# 对外入口
# ──────────────────────────────────────────────────────────────

async def generate_weekly_schedule_image(
    data: dict, font_dir: Path, base_url: str = ""
) -> bytes:
    """生成本周排期图片，返回 PNG bytes。

    Args:
        data:     VoiceHub ``/api/bot/voicehub/weekly-schedule`` 的响应 dict。
        font_dir: 存放 HarmonyOS Sans SC .ttf 文件的目录。
        base_url: VoiceHub 站点地址，用于补全相对路径的站点/学校 Logo。

    Returns:
        PNG 图片的原始字节。
    """
    schedules: list[dict] = data.get("schedules") or []
    display = normalize_display_config(data.get("displayConfig"))
    slots = logo_slots(display, data.get("imageConfig"), base_url)

    # 并发下载封面与 Logo（异步 IO）
    covers = await _fetch_covers(schedules, display["showCover"])
    logos = await _fetch_logos(slots)

    # Pillow 绘图是同步 IO，放到线程池
    loop = asyncio.get_event_loop()
    png_bytes: bytes = await loop.run_in_executor(
        None,
        _draw_image,
        data,
        covers,
        logos,
        slots,
        font_dir,
        base_url,
    )
    return png_bytes
