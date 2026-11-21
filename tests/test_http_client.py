"""统一 aiohttp 会话工厂与错误映射的回归测试。

VoiceHub 开启 ``nitro.timing`` 后单个响应头可超过 aiohttp 默认的 8190 字节上限，
插件必须放大该限制，并且不把原始异常（可能是几 KB 的 header 片段）回显给用户。
"""

import asyncio
import socket
import threading
from pathlib import Path

import aiohttp
import pytest

from lib import voicehub as vh
from lib.http import (
    TEXT_CONNECTION,
    TEXT_HEADER_TOO_LARGE,
    TEXT_TIMEOUT,
    describe_client_error,
    make_session,
)

LIB = Path(__file__).resolve().parent.parent / "lib"


class _RawHeaderServer:
    """返回一个超长 ``Server-Timing`` 头的最小 HTTP 服务。"""

    def __init__(self, header_value: bytes):
        self._header_value = header_value
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self._server.settimeout(0.2)
        self.port = self._server.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "_RawHeaderServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._server.close()
        self._thread.join(timeout=2)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/bot/voicehub/weekly-schedule"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    conn.recv(4096)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nServer-Timing: "
                        + self._header_value
                        + b"\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                    )
                except OSError:
                    pass


@pytest.fixture(scope="module")
def big_header_server():
    with _RawHeaderServer(b"x" * 20000) as server:
        yield server


async def _fetch_header_exception(url: str) -> BaseException:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                await response.read()
    except BaseException as exc:  # noqa: BLE001 - 测试就是在断言这个异常
        return exc
    raise AssertionError("aiohttp 默认配置本应拒绝超长响应头")


def test_default_session_rejects_and_shared_factory_accepts_big_header(
    big_header_server,
):
    exc = asyncio.run(_fetch_header_exception(big_header_server.url))
    assert isinstance(exc, aiohttp.ClientError)
    assert "Got more than" in str(exc)
    assert describe_client_error(exc) == TEXT_HEADER_TOO_LARGE

    async def fetch():
        async with make_session() as session:
            async with session.get(big_header_server.url) as response:
                return await response.text()

    assert asyncio.run(fetch()) == "ok"


def test_factory_passes_explicit_limits_and_keeps_overrides():
    async def check():
        async with make_session(max_field_size=1234) as session:
            return session

    session = asyncio.run(check())
    # 显式传入的值不被覆盖（其余仍走放大后的默认值）
    assert session._max_field_size == 1234
    assert session._max_line_size >= 65536


def test_describe_client_error_never_returns_empty_text():
    assert describe_client_error(asyncio.TimeoutError()) == TEXT_TIMEOUT
    assert describe_client_error(aiohttp.ClientConnectionError()) == TEXT_CONNECTION

    class EmptyStrError(Exception):
        def __str__(self) -> str:
            return ""

    text = describe_client_error(EmptyStrError())
    assert text and "EmptyStrError" in text


def test_weekly_schedule_maps_header_error_to_readable_message(
    big_header_server, monkeypatch
):
    """本周歌单走统一会话后，header 超限时回复可读文案而不是原始异常。"""
    exc = asyncio.run(_fetch_header_exception(big_header_server.url))
    assert isinstance(exc, aiohttp.ClientError)

    class _RaisingSession:
        async def __aenter__(self):
            raise exc

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(vh, "make_session", lambda *a, **kw: _RaisingSession())
    config = vh.VoiceHubConfig(
        webhook_token="t", voicehub_base_url="https://voicehub.example.com", voicehub_token="t"
    )
    result = asyncio.run(vh.VoiceHubClient(config).get_weekly_schedule())

    assert result["ok"] is False
    assert result["message"] == TEXT_HEADER_TOO_LARGE
    assert "Got more than" not in result["message"]


def test_all_plugin_requests_go_through_the_shared_session_factory():
    """散落的 ClientSession 会漏掉 header 上限；插件内只允许 lib/http.py 构造。"""
    offenders = []
    for path in sorted(LIB.glob("*.py")):
        if path.name == "http.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "aiohttp.ClientSession(" in text:
            offenders.append(path.name)
    assert not offenders, f"这些模块绕过了统一会话工厂：{offenders}"
