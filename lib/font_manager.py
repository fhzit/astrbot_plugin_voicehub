"""字体管理：首次运行时下载 HarmonyOS Sans SC，后续复用。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiohttp

FONT_URLS: dict[str, str] = {
    "regular": "https://cdn.jsdelivr.net/npm/@fontpkg/harmony-os-sans-sc@1.0.3/HarmonyOS_Sans_SC_Regular.ttf",
    "bold":    "https://cdn.jsdelivr.net/npm/@fontpkg/harmony-os-sans-sc@1.0.3/HarmonyOS_Sans_SC_Bold.ttf",
}

FONT_FILES: dict[str, str] = {
    "regular": "HarmonyOS_Sans_SC_Regular.ttf",
    "bold":    "HarmonyOS_Sans_SC_Bold.ttf",
}


async def _download(url: str, dest: Path, label: str, logger: Any) -> None:
    """下载单个字体文件到 dest，带进度日志。"""
    logger.info("[VoiceHub] 正在下载字体 %s → %s", label, dest.name)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            tmp = dest.with_suffix(".tmp")
            with tmp.open("wb") as fp:
                async for chunk in resp.content.iter_chunked(65536):
                    fp.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        # 每 25% 打一次进度
                        if pct % 25 == 0:
                            logger.info(
                                "[VoiceHub] 字体 %s 下载进度：%d%%（%d/%d 字节）",
                                label, pct, downloaded, total,
                            )
            tmp.rename(dest)
    logger.info("[VoiceHub] 字体 %s 下载完成（%d 字节）", label, downloaded)


def fonts_already_exist(font_dir: Path) -> bool:
    """判断两个字体文件是否已存在（用于提前给用户提示下载进度）。"""
    return all((font_dir / filename).exists() for filename in FONT_FILES.values())


async def ensure_fonts(font_dir: Path, logger: Any) -> dict[str, Path]:
    """确保两个字体文件存在，首次运行时从 CDN 下载。

    Args:
        font_dir: 字体保存目录（不存在时自动创建）。
        logger: 日志记录器（由调用方注入，通常为 ``astrbot.api.logger``）。

    Returns:
        ``{"regular": Path, "bold": Path}``，指向本地 .ttf 文件。
    """
    font_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for key, filename in FONT_FILES.items():
        dest = font_dir / filename
        result[key] = dest
        if dest.exists():
            logger.debug("[VoiceHub] 字体已存在，跳过下载：%s", dest)
        else:
            await _download(FONT_URLS[key], dest, key, logger)
    return result
