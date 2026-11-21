"""统一的 aiohttp 会话工厂与网络错误文案。

aiohttp 默认把响应行/单个响应头的长度上限设为 8190 字节（``max_line_size`` /
``max_field_size``）。VoiceHub 开启 Nuxt/Nitro 的 ``nitro.timing`` 后，每个响应
都会附带逐 hook 的 ``Server-Timing`` 头，条目多时轻易超过该上限，客户端解析
响应头直接失败（表现为 ``ClientResponseError(400, 'Got more than 8190 bytes…')``）。

插件对站点的所有请求都必须走 :func:`make_session`，统一放大这两个上限，避免
散落的 ``ClientSession`` 逐处遗漏；聊天回复只用 :func:`describe_client_error`
的可读文案，原始异常由 :func:`log_network_error` 写日志。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from urllib.parse import urlsplit

import aiohttp

# 64 KB 足够容纳超长的 Server-Timing 头，同时不给恶意响应留下无上限的解析成本。
MAX_LINE_SIZE = 65536
MAX_FIELD_SIZE = 65536

TEXT_HEADER_TOO_LARGE = (
    "站点响应头过长，客户端无法解析（VoiceHub 的 Server-Timing 调试头超出上限），"
    "请联系站点管理员关闭 nitro.timing 后重试"
)
TEXT_TIMEOUT = "连接站点超时，请稍后重试"
TEXT_CONNECTION = "无法连接站点，请检查网络或站点地址"
TEXT_REQUEST_FAILED = "请求站点失败"

_TIMEOUT_TYPES = (asyncio.TimeoutError, TimeoutError, aiohttp.ServerTimeoutError)


def make_session(
    timeout: Optional[aiohttp.ClientTimeout] = None, **kwargs: Any
) -> aiohttp.ClientSession:
    """创建带放大 header 上限的 aiohttp 会话。

    Args:
        timeout: 请求超时；默认沿用 aiohttp 自带的 5 分钟 total。
        **kwargs: 透传给 ``aiohttp.ClientSession``，显式传入的
            ``max_line_size`` / ``max_field_size`` 优先。

    Returns:
        新的 ``aiohttp.ClientSession``（调用方负责 ``async with`` 关闭）。
    """
    kwargs.setdefault("max_line_size", MAX_LINE_SIZE)
    kwargs.setdefault("max_field_size", MAX_FIELD_SIZE)
    return aiohttp.ClientSession(timeout=timeout, **kwargs)


def is_header_limit_error(exc: BaseException) -> bool:
    """判断异常是否为「响应头超出客户端解析上限」。

    aiohttp 对这类失败抛 ``ClientResponseError``，状态码与文案由底层 http
    parser 决定，因此按文案特征识别，避免版本差异导致漏判。
    """
    text = str(exc)
    return "Got more than" in text and "bytes when reading" in text


def describe_client_error(exc: BaseException) -> str:
    """把网络异常收敛成可直接回复用户的可读文案。

    绝不回显原始异常文本（可能包含站点地址、Server-Timing 片段等内部信息）；
    需要排查细节时由调用方用 :func:`log_network_error` 写日志。

    Args:
        exc: 捕获到的异常。

    Returns:
        非空的中文提示。
    """
    if is_header_limit_error(exc):
        return TEXT_HEADER_TOO_LARGE
    if isinstance(exc, _TIMEOUT_TYPES):
        return TEXT_TIMEOUT
    if isinstance(exc, aiohttp.ClientConnectionError):
        return TEXT_CONNECTION
    name = type(exc).__name__
    return TEXT_REQUEST_FAILED if not name else f"{TEXT_REQUEST_FAILED}（{name}）"


def log_network_error(
    logger: Any, scope: str, exc: BaseException, context: str = ""
) -> None:
    """把完整异常写日志，供管理员排查。

    header 超限时原始报文里带 Server-Timing 片段，warning 级别只保留前 400 字符
    并注明原因，避免刷屏；其余异常用 ``%r`` 输出类型与文案。
    """
    where = f"{scope} {context}".strip()
    if is_header_limit_error(exc):
        logger.error("[VoiceHub] %s 响应头超出 aiohttp 上限：%s", where, str(exc)[:400])
        return
    logger.warning("[VoiceHub] %s 网络请求失败：%r", where, exc)


def redact_url(url: str) -> str:
    """只保留协议与主机名，日志里避免带出令牌化的查询串。"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else url
