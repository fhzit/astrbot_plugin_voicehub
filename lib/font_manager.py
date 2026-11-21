"""字体管理：首次运行时下载 HarmonyOS Sans SC，后续复用本地缓存。

下载源按「中国大陆可达性」排序：

1. ``cdn.npmmirror.com`` 的 npm 归档（阿里 npmmirror，整包含全部字重，一次下载供两个字体复用）
2. 腾讯云 npm 镜像
3. 华为云 npm 镜像
4. jsDelivr（cdn / fastly 两域名）、unpkg（海外或可直连的环境，单文件更小）

任一源失败会按指数退避重试，全部失败时抛出 :class:`FontDownloadError`：异常
含每个源的具体失败原因与「手动放置字体」的目录，避免用户只看到一个空报错。
管理员可用插件配置 ``font_urls`` 覆盖/追加下载源，``font_dir`` 指定离线字体目录。

并发安全：同一字体文件的下载用 ``asyncio.Lock`` 去重（in-flight 复用），临时文件
用 ``tempfile.mkstemp`` 生成唯一名，下载完成后再 ``os.replace`` 原子落盘。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tarfile
import tempfile
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import aiohttp

from .http import make_session, redact_url

FONT_FILES: Dict[str, str] = {
    "regular": "HarmonyOS_Sans_SC_Regular.ttf",
    "bold": "HarmonyOS_Sans_SC_Bold.ttf",
}

# 字体包（@fontpkg/harmony-os-sans-sc@1.0.3）在 npm 各镜像上的归档地址。
# 归档内成员形如 ``package/HarmonyOS_Sans_SC_Regular.ttf``。
FONT_PACKAGE_TGZ = "harmony-os-sans-sc-1.0.3.tgz"

DEFAULT_SOURCES: Tuple[str, ...] = (
    # 中国大陆优先：npm 归档 CDN（npmmirror / 腾讯云 / 华为云）
    "https://cdn.npmmirror.com/packages/%40fontpkg/harmony-os-sans-sc/1.0.3/"
    + FONT_PACKAGE_TGZ
    + "#package/{filename}",
    "https://mirrors.cloud.tencent.com/npm/@fontpkg/harmony-os-sans-sc/-/"
    + FONT_PACKAGE_TGZ
    + "#package/{filename}",
    "https://mirrors.huaweicloud.com/repository/npm/@fontpkg/harmony-os-sans-sc/-/"
    + FONT_PACKAGE_TGZ
    + "#package/{filename}",
    # 海外网络或可直连 jsDelivr 的环境：单文件只有约 8 MB
    "https://cdn.jsdelivr.net/npm/@fontpkg/harmony-os-sans-sc@1.0.3/{filename}",
    "https://fastly.jsdelivr.net/npm/@fontpkg/harmony-os-sans-sc@1.0.3/{filename}",
    "https://unpkg.com/@fontpkg/harmony-os-sans-sc@1.0.3/{filename}",
)

# 单个字重约 8 MB；低于 1 MB 必然是半截文件或错误页，不能当成缓存。
MIN_FONT_BYTES = 1024 * 1024
MIN_ARCHIVE_BYTES = 8 * 1024 * 1024

# PIL 只接受 TrueType/OpenType；TTF 头 0x00010000、'true'、'ttcf'，OTF 为 'OTTO'。
FONT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO")

DOWNLOAD_ATTEMPTS = 3
RETRY_DELAYS = (2, 5)
# 单个源的整体超时：33 MB 归档在慢速链路上需要宽裕的时间。
FONT_TIMEOUT = aiohttp.ClientTimeout(total=900, sock_connect=20, sock_read=120)

_TIMEOUT_TYPES = (asyncio.TimeoutError, TimeoutError, aiohttp.ServerTimeoutError)

# 同一字体文件的并发下载锁；按事件循环隔离，避免跨 loop 复用已绑定的 Lock。
_LOCK_REGISTRY: "weakref.WeakKeyDictionary[Any, Dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


class FontSourceError(RuntimeError):
    """单个下载源失败；``retryable`` 决定是否还有必要重试同一源。"""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class FontDownloadError(RuntimeError):
    """所有下载源都失败。

    ``str()`` 给出完整原因（供日志），``short`` 是可放进聊天回复的一行提示。
    """

    def __init__(self, filename: str, font_dir: Path, reasons: Sequence[str]):
        self.filename = filename
        self.font_dir = font_dir
        self.reasons: List[str] = [reason for reason in reasons if reason]
        detail = "；".join(self.reasons) or "没有可用的下载源"
        attempts = len(self.reasons)
        super().__init__(
            f"{filename} 下载失败（{attempts} 次尝试）：{detail}。"
            f"可手动下载该字体放到 {font_dir} 后重试，"
            f"或在插件配置 font_urls 里填可用的下载地址"
        )
        self.short = (
            f"{filename} 的所有下载源都不可用（{attempts} 次尝试，原因见日志）。"
            f"可手动把字体放到 {font_dir}，或用插件配置 font_urls 指定可用源"
        )


@dataclass(frozen=True)
class FontSource:
    """一个下载源；``member`` 非空表示从 npm 归档里取出该成员。"""

    url: str
    member: str = ""

    @classmethod
    def parse(cls, value: str) -> Optional["FontSource"]:
        """解析 ``url`` 或 ``url#归档内路径``；非法取值返回 None。"""
        text = str(value or "").strip()
        if not text:
            return None
        url, _, member = text.partition("#")
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return None
        return cls(url=url, member=member.strip())

    @property
    def label(self) -> str:
        """日志用短标签：只显示协议+主机名，避免整条 URL 刷屏。"""
        host = redact_url(self.url)
        if self.member:
            return f"{host}（归档 {Path(self.member).name}）"
        return host


@dataclass
class _RunContext:
    """一次 ``ensure_fonts`` 调用内的共享状态（归档只下载一次）。"""

    font_dir: Path
    logger: Any
    archive: Optional[Path] = None


def _lock_for(dest: Path) -> asyncio.Lock:
    """返回同一字体文件在当前事件循环内的下载锁。"""
    loop = asyncio.get_running_loop()
    per_loop = _LOCK_REGISTRY.setdefault(loop, {})
    key = str(dest)
    lock = per_loop.get(key)
    if lock is None:
        lock = asyncio.Lock()
        per_loop[key] = lock
    return lock


def _new_temp(directory: Path, filename: str) -> Path:
    """在目标目录内创建唯一临时文件，保证 ``os.replace`` 不跨挂载点。"""
    fd, name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".part", dir=directory)
    os.close(fd)
    return Path(name)


def _is_complete_font(path: Path, min_bytes: int = MIN_FONT_BYTES) -> bool:
    """判断字体文件是否可用：存在、体积达标、文件头是 TTF/OTF。"""
    try:
        if not path.is_file() or path.stat().st_size < min_bytes:
            return False
        with path.open("rb") as fp:
            return fp.read(4) in FONT_MAGIC
    except OSError:
        return False


def _validate_font_file(path: Path) -> None:
    """校验已下载文件，避免半截文件或 HTML 错误页被当成字体。"""
    size = path.stat().st_size
    if size < MIN_FONT_BYTES:
        raise FontSourceError(f"文件仅 {size} 字节，不是有效字体", retryable=False)
    with path.open("rb") as fp:
        magic = fp.read(4)
    if magic not in FONT_MAGIC:
        raise FontSourceError(f"文件头不是 TTF/OTF（{magic!r}）", retryable=False)


def _validate_archive(path: Path) -> None:
    """校验 npm 归档：gzip 头 + 体积下限。"""
    size = path.stat().st_size
    if size < MIN_ARCHIVE_BYTES:
        raise FontSourceError(f"归档仅 {size} 字节，不是有效的字体包", retryable=False)
    with path.open("rb") as fp:
        if fp.read(2) != b"\x1f\x8b":
            raise FontSourceError("归档不是 gzip 格式", retryable=False)


def fonts_already_exist(font_dir: Path, min_bytes: int = MIN_FONT_BYTES) -> bool:
    """两个字体文件是否都已完整缓存（用于提前给用户提示下载进度）。"""
    return all(
        _is_complete_font(font_dir / filename, min_bytes)
        for filename in FONT_FILES.values()
    )


def font_sources(
    key: str, overrides: Optional[Mapping[str, Sequence[str]]] = None
) -> Tuple[FontSource, ...]:
    """按优先级返回某个字重的下载源：配置的源在前，内置源在后。"""
    filename = FONT_FILES[key]
    result: List[FontSource] = []
    for raw in (overrides or {}).get(key, ()) or ():
        source = FontSource.parse(raw)
        if source is not None and source not in result:
            result.append(source)
    for template in DEFAULT_SOURCES:
        source = FontSource.parse(template.format(filename=filename))
        if source is not None and source not in result:
            result.append(source)
    return tuple(result)


def _describe_failure(exc: BaseException) -> str:
    """把下载异常收敛成非空、可诊断的一句原因。"""
    if isinstance(exc, FontSourceError):
        return str(exc).strip() or type(exc).__name__
    name = type(exc).__name__
    if isinstance(exc, _TIMEOUT_TYPES):
        return f"下载超时（{name}）"
    if isinstance(exc, aiohttp.ClientResponseError):
        return f"HTTP {exc.status}"
    if isinstance(exc, aiohttp.ClientConnectionError):
        detail = str(exc).strip()
        return f"连接被重置或中断（{detail}）" if detail else "连接被重置或中断"
    if isinstance(exc, OSError):
        detail = str(exc).strip() or name
        return f"写盘/网络错误：{detail}"
    detail = str(exc).strip()
    return f"{name}：{detail}" if detail else name


async def _download_raw(url: str, tmp: Path, logger: Any, label: str) -> int:
    """把 URL 流式下载到 tmp，返回字节数；下载不完整或 HTTP 非 200 时抛错。"""
    logger.info("[VoiceHub] 正在下载字体资源：%s", label)
    async with make_session(FONT_TIMEOUT) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                raise FontSourceError(
                    f"HTTP {resp.status}",
                    retryable=resp.status in {408, 425, 429} or resp.status >= 500,
                )
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            last_bucket = -1
            with tmp.open("wb") as fp:
                async for chunk in resp.content.iter_chunked(65536):
                    fp.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        bucket = downloaded * 100 // total // 20
                        if bucket > last_bucket:
                            last_bucket = bucket
                            logger.info(
                                "[VoiceHub] 字体资源 %s 下载进度：%d%%（%d/%d 字节）",
                                label,
                                min(100, downloaded * 100 // total),
                                downloaded,
                                total,
                            )
    if total and downloaded != total:
        raise FontSourceError(
            f"下载不完整（{downloaded}/{total} 字节）", retryable=True
        )
    return downloaded


def _extract_member(archive: Path, member: str, dest: Path) -> None:
    """从 npm 归档中取出字体成员并原子写入 dest（只读流式解包，不做路径落盘）。"""
    tmp = _new_temp(dest.parent, dest.name)
    expected = Path(member).name
    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                info = tar.getmember(member)
            except KeyError:
                # 兼容归档内前缀变化：按文件名后缀兜底匹配
                candidates = [
                    item
                    for item in tar.getmembers()
                    if item.name == expected or item.name.endswith("/" + expected)
                ]
                if not candidates:
                    raise FontSourceError(
                        f"归档中缺少 {expected}", retryable=False
                    ) from None
                info = candidates[0]
            handle = tar.extractfile(info)
            if handle is None:
                raise FontSourceError(f"归档成员不可读：{info.name}", retryable=False)
            with handle, tmp.open("wb") as fp:
                shutil.copyfileobj(handle, fp)
        _validate_font_file(tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


async def _archive_for(source: FontSource, ctx: _RunContext) -> Path:
    """下载（或复用本次运行已下载的）npm 归档。"""
    if ctx.archive is not None and ctx.archive.exists():
        return ctx.archive
    tmp = _new_temp(ctx.font_dir, "fontpkg.tgz")
    try:
        await _download_raw(source.url, tmp, ctx.logger, source.label)
        _validate_archive(tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    ctx.archive = tmp
    return tmp


async def _fetch_source(source: FontSource, dest: Path, ctx: _RunContext) -> int:
    """从单个源把字体取到 dest（原子替换），返回最终文件字节数。"""
    if source.member:
        archive = await _archive_for(source, ctx)
        await asyncio.to_thread(_extract_member, archive, source.member, dest)
    else:
        tmp = _new_temp(dest.parent, dest.name)
        try:
            await _download_raw(source.url, tmp, ctx.logger, source.label)
            _validate_font_file(tmp)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
    _validate_font_file(dest)
    return dest.stat().st_size


async def _download_with_fallback(
    key: str, dest: Path, sources: Sequence[FontSource], ctx: _RunContext
) -> None:
    """依次尝试各下载源；失败按指数退避重试，最终汇总原因抛出。"""
    reasons: List[str] = []
    for source in sources:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                size = await _fetch_source(source, dest, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 逐源收集失败原因后统一报错
                reason = _describe_failure(exc)
                reasons.append(f"{source.label}：{reason}")
                ctx.logger.warning(
                    "[VoiceHub] 字体 %s 第 %d/%d 次从 %s 获取失败：%s",
                    key,
                    attempt,
                    DOWNLOAD_ATTEMPTS,
                    source.label,
                    reason,
                )
                retryable = getattr(exc, "retryable", True)
                if not retryable or attempt == DOWNLOAD_ATTEMPTS:
                    break
                await asyncio.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
                continue
            ctx.logger.info(
                "[VoiceHub] 字体 %s 已就绪：%s（%d 字节，来源 %s）",
                key,
                dest.name,
                size,
                source.label,
            )
            return
    raise FontDownloadError(dest.name, dest.parent, reasons)


def _discard_incomplete(dest: Path, logger: Any) -> None:
    """删除不完整的历史字体文件，避免半截文件被永久当成缓存。"""
    try:
        if not dest.exists():
            return
        size = dest.stat().st_size
        logger.warning(
            "[VoiceHub] 字体文件不完整（%d 字节），重新下载：%s", size, dest.name
        )
        dest.unlink()
    except OSError as exc:  # noqa: BLE001 - 删除失败时下载会用原子替换覆盖它
        logger.warning("[VoiceHub] 无法删除不完整的字体文件 %s：%s", dest, exc)


async def ensure_fonts(
    font_dir: Path,
    logger: Any,
    sources: Optional[Mapping[str, Sequence[str]]] = None,
) -> Dict[str, Path]:
    """确保两个字体文件可用，缺失时按多源回退下载。

    Args:
        font_dir: 字体保存目录（不存在时自动创建）；可放预置字体实现离线部署。
        logger: 日志记录器（由调用方注入，通常为 ``astrbot.api.logger``）。
        sources: 可选的下载源覆盖，形如 ``{"regular": ["https://..."], ...}``；
            同一键可给多个地址，按顺序排在默认源之前。

    Returns:
        ``{"regular": Path, "bold": Path}``，指向本地 .ttf 文件。

    Raises:
        FontDownloadError: 所有下载源均失败；``short`` 为聊天用的简短提示。
    """
    font_dir.mkdir(parents=True, exist_ok=True)
    ctx = _RunContext(font_dir=font_dir, logger=logger)
    result: Dict[str, Path] = {}
    try:
        for key, filename in FONT_FILES.items():
            dest = font_dir / filename
            result[key] = dest
            if _is_complete_font(dest):
                logger.debug("[VoiceHub] 字体已存在，跳过下载：%s", dest)
                continue
            async with _lock_for(dest):
                # 等锁期间可能已被并发的首次出图任务下载完成
                if _is_complete_font(dest):
                    logger.debug("[VoiceHub] 字体已由并发任务下载完成：%s", dest)
                    continue
                _discard_incomplete(dest, logger)
                await _download_with_fallback(
                    key, dest, font_sources(key, sources), ctx
                )
    finally:
        if ctx.archive is not None:
            ctx.archive.unlink(missing_ok=True)
    return result
