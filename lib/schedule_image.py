"""用 Pillow 绘制本周排期图片。纯函数，不依赖 AstrBot 框架。"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ── 画布参数 ────────────────────────────────────────────────
IMG_W        = 800
PAD_H        = 40   # 左右 padding
PAD_V        = 40   # 上下 padding
ROW_H        = 56   # 每首歌行高（封面 40 + 上下 8）
COVER_SIZE   = 40
COVER_RADIUS = 4
SEQ_SIZE     = 30

# ── 颜色 ────────────────────────────────────────────────────
C_BG          = (255, 255, 255)          # #ffffff
C_DIVIDER     = (209, 213, 219)          # #d1d5db  2px 标题区分割线
C_DIVIDER_ROW = (209, 213, 219)          # #d1d5db  1px 行底分割线
C_GROUP_BG    = (248, 249, 250)          # #f8f9fa
C_GROUP_TEXT  = (124, 58, 237)           # #7c3aed  日期组标题
C_GROUP_BAR   = (139, 92, 246)          # #8b5cf6  左侧紫色边框
C_TIME_TEXT   = (37, 99, 235)            # #2563eb  时段标题
C_TIME_BAR    = (11, 90, 254)            # #0b5afe  左侧蓝色边框
C_SONG        = (26, 26, 26)             # #1a1a1a  歌名
C_SUB         = (99, 99, 102)            # #636366  歌手 / 投稿人
C_SEQ_BG      = (240, 240, 240)          # #f0f0f0  序号圆圈背景
C_COVER_PH    = (245, 245, 245)          # #f5f5f5  封面占位

# ── 字号 ────────────────────────────────────────────────────
FS_TITLE      = 22
FS_SUBTITLE   = 14
FS_GROUP      = 15
FS_SONG       = 16
FS_ARTIST     = 14
FS_REQ        = 12
FS_FOOTER     = 12
FS_SEQ        = 14

COVER_TIMEOUT = aiohttp.ClientTimeout(total=5)


# ──────────────────────────────────────────────────────────────
# 字体加载（同步，供线程池调用）
# ──────────────────────────────────────────────────────────────

def _load_fonts(font_dir: Path) -> dict[str, dict[int, ImageFont.FreeTypeFont]]:
    """加载所有需要的字号，返回 {style: {size: font}}。"""
    reg_path  = font_dir / "HarmonyOS_Sans_SC_Regular.ttf"
    bold_path = font_dir / "HarmonyOS_Sans_SC_Bold.ttf"

    def _f(path: Path, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(path), size)

    reg_sizes  = (FS_SUBTITLE, FS_ARTIST, FS_REQ, FS_FOOTER, FS_GROUP)
    bold_sizes = (FS_TITLE, FS_SONG, FS_SEQ, FS_GROUP)
    return {
        "regular": {s: _f(reg_path, s)  for s in reg_sizes},
        "bold":    {s: _f(bold_path, s) for s in bold_sizes},
    }


# ──────────────────────────────────────────────────────────────
# 封面下载（异步，并发）
# ──────────────────────────────────────────────────────────────

async def _fetch_cover(url: str) -> Image.Image | None:
    """下载单张封面，超时或失败返回 None。"""
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


def _paste_cover(
    canvas: Image.Image,
    cover_img: Image.Image | None,
    x: int,
    y: int,
) -> None:
    """把封面贴到画布，带圆角遮罩；失败时画占位方块。"""
    size = COVER_SIZE
    if cover_img is None:
        draw = ImageDraw.Draw(canvas)
        _draw_rounded_rect(draw, (x, y, x + size, y + size), COVER_RADIUS, C_COVER_PH)
        return

    # 缩放到 40×40
    cover = cover_img.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")

    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle((0, 0, size - 1, size - 1), radius=COVER_RADIUS, fill=255)

    canvas.paste(cover, (x, y), mask)


# ──────────────────────────────────────────────────────────────
# 主绘图函数（同步，跑在线程池）
# ──────────────────────────────────────────────────────────────

def _draw_image(
    data: dict,
    covers: dict[int, Image.Image | None],
    font_dir: Path,
) -> bytes:
    """纯 Pillow 绘图，返回 PNG bytes。"""
    schedules: list[dict] = data.get("schedules") or []
    display: dict        = data.get("displayConfig") or {}
    show_cover    = display.get("showCover",    True)
    show_seq      = display.get("showSequence", True)
    show_req      = display.get("showRequester", True)
    show_votes    = display.get("showVotes",    False)
    show_playtime = display.get("showPlayTime", True)
    show_date     = display.get("showDate",     True)

    site_title = data.get("siteTitle", "VoiceHub")
    week_range = data.get("weekRange", "")

    fonts = _load_fonts(font_dir)
    R = fonts["regular"]
    B = fonts["bold"]

    # ── 预先计算高度 ──────────────────────────────────────────
    # 标题区：站点名行 + 日期范围行 + 分割线 = 约 80px
    HEADER_H = PAD_V + 28 + 6 + 20 + PAD_V // 2  # 约 110
    # 每个日期组：组标题 36px + 若干时段（时段标题 32px + 若干行）
    # 先做一遍扫描计算总高度
    # 按 date 分组，组内再按 playTime 分组
    def _group_key(s: dict) -> str:
        return s.get("date", "")

    def _time_key(s: dict) -> str:
        return s.get("playTime", "")

    date_groups: list[tuple[str, list[dict]]] = []
    for date, items in groupby(schedules, key=_group_key):
        date_groups.append((date, list(items)))

    total_h = HEADER_H
    if not schedules:
        total_h += 60  # 空状态提示
    else:
        for _date, items in date_groups:
            if show_date:
                total_h += 36  # 日期组标题
            time_groups: list[tuple[str, list[dict]]] = []
            for pt, tms in groupby(items, key=_time_key):
                time_groups.append((pt, list(tms)))
            for _pt, songs in time_groups:
                if show_playtime:
                    total_h += 32  # 时段标题
                total_h += len(songs) * ROW_H
    total_h += 40  # 页脚
    total_h += PAD_V

    # ── 创建画布 ──────────────────────────────────────────────
    img = Image.new("RGB", (IMG_W, total_h), C_BG)
    draw = ImageDraw.Draw(img)

    y = PAD_V

    # ── 标题区 ────────────────────────────────────────────────
    title_font = B[FS_TITLE]
    draw.text((PAD_H, y), site_title, font=title_font, fill=C_SONG)
    y += 28 + 6

    if week_range:
        draw.text((PAD_H, y), week_range, font=R[FS_SUBTITLE], fill=C_SUB)
        y += 20

    y += PAD_V // 2
    # 分割线
    draw.line([(PAD_H, y), (IMG_W - PAD_H, y)], fill=C_DIVIDER, width=2)
    y += 10

    # ── 空状态 ────────────────────────────────────────────────
    if not schedules:
        draw.text(
            (IMG_W // 2, y + 20),
            "本周暂无排期",
            font=B[FS_SONG],
            fill=C_SUB,
            anchor="mm",
        )
        y += 60
    else:
        idx_global = 0  # 用于索引 covers 字典

        for date, items in date_groups:
            if show_date:
                draw.rectangle([(0, y), (IMG_W, y + 36)], fill=C_GROUP_BG)
                draw.rectangle([(PAD_H, y + 4), (PAD_H + 3, y + 32)], fill=C_GROUP_BAR)
                draw.text((PAD_H + 12, y + 10), date, font=B[FS_GROUP], fill=C_GROUP_TEXT)
                y += 36

            time_groups: list[tuple[str, list[dict]]] = []
            for pt, tms in groupby(items, key=_time_key):
                time_groups.append((pt, list(tms)))

            for playtime, songs in time_groups:
                # ── 时段标题 ──────────────────────────────────
                if show_playtime and playtime:
                    draw.rectangle([(PAD_H, y + 6), (PAD_H + 3, y + 26)], fill=C_TIME_BAR)
                    draw.text(
                        (PAD_H + 12, y + 8),
                        playtime,
                        font=R[FS_GROUP],
                        fill=C_TIME_TEXT,
                    )
                    y += 32

                for song in songs:
                    row_y = y
                    cover_x = PAD_H
                    content_x = PAD_H

                    # ── 序号圆圈 ──────────────────────────────
                    if show_seq:
                        seq = song.get("sequence", idx_global + 1)
                        cx = cover_x + SEQ_SIZE // 2
                        cy = row_y + ROW_H // 2
                        r = SEQ_SIZE // 2
                        draw.ellipse(
                            [(cx - r, cy - r), (cx + r, cy + r)],
                            fill=C_SEQ_BG,
                        )
                        seq_text = str(seq)
                        draw.text(
                            (cx, cy),
                            seq_text,
                            font=B[FS_SEQ],
                            fill=C_SONG,
                            anchor="mm",
                        )
                        content_x += SEQ_SIZE + 8

                    # ── 封面图 ────────────────────────────────
                    if show_cover:
                        cover_img = covers.get(idx_global)
                        _paste_cover(img, cover_img, content_x, row_y + 8)
                        content_x += COVER_SIZE + 10

                    # ── 歌名 + 歌手 ───────────────────────────
                    title_text  = song.get("title", "")
                    artist_text = song.get("artist", "")
                    draw.text(
                        (content_x, row_y + 8),
                        title_text,
                        font=B[FS_SONG],
                        fill=C_SONG,
                    )
                    draw.text(
                        (content_x, row_y + 28),
                        artist_text,
                        font=R[FS_ARTIST],
                        fill=C_SUB,
                    )

                    # ── 右侧：投稿人 + 票数 ───────────────────
                    right_x = IMG_W - PAD_H
                    right_y = row_y + 12

                    if show_votes:
                        vote_text = f"▲ {song.get('voteCount', 0)}"
                        vw = _text_w(draw, vote_text, R[FS_REQ])
                        draw.text(
                            (right_x - vw, right_y + 16),
                            vote_text,
                            font=R[FS_REQ],
                            fill=C_SUB,
                        )

                    if show_req:
                        requester = song.get("requester", "")
                        grade     = song.get("requesterGrade", "")
                        cls_      = song.get("requesterClass", "")
                        req_parts = [p for p in (grade, cls_, requester) if p]
                        req_text  = " ".join(req_parts) if req_parts else ""
                        if req_text:
                            rw = _text_w(draw, req_text, R[FS_REQ])
                            draw.text(
                                (right_x - rw, right_y),
                                req_text,
                                font=R[FS_REQ],
                                fill=C_SUB,
                            )

                    # ── 行底分割线 ────────────────────────────
                    draw.line(
                        [(PAD_H, row_y + ROW_H - 1), (IMG_W - PAD_H, row_y + ROW_H - 1)],
                        fill=C_DIVIDER_ROW,
                        width=1,
                    )

                    y += ROW_H
                    idx_global += 1

    # ── 页脚 ──────────────────────────────────────────────────
    y += 10
    now_str = datetime.now().strftime("生成时间：%Y/%m/%d %H:%M:%S")
    fw = _text_w(draw, now_str, R[FS_FOOTER])
    draw.text(
        (IMG_W - PAD_H - fw, y),
        now_str,
        font=R[FS_FOOTER],
        fill=C_SUB,
    )

    # ── 输出 PNG ──────────────────────────────────────────────
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────
# 对外入口
# ──────────────────────────────────────────────────────────────

async def generate_weekly_schedule_image(data: dict, font_dir: Path) -> bytes:
    """生成本周排期图片，返回 PNG bytes。

    Args:
        data:     VoiceHub ``/api/bot/voicehub/weekly-schedule`` 的响应 dict。
        font_dir: 存放 HarmonyOS Sans SC .ttf 文件的目录。

    Returns:
        PNG 图片的原始字节。
    """
    schedules: list[dict] = data.get("schedules") or []
    display: dict         = data.get("displayConfig") or {}
    show_cover = display.get("showCover", True)

    # 并发下载封面（异步 IO）
    covers = await _fetch_covers(schedules, show_cover)

    # Pillow 绘图是同步 IO，放到线程池
    loop = asyncio.get_event_loop()
    png_bytes: bytes = await loop.run_in_executor(
        None,
        _draw_image,
        data,
        covers,
        font_dir,
    )
    return png_bytes
