"""字体下载的多源回退、可诊断报错与并发去重。

这里的「网络」用本地 socket 服务模拟：只关心插件侧的下载/校验/回退逻辑，
真实镜像可达性由部署时的实际网络决定。
"""

import asyncio
import io
import logging
import socket
import tarfile
import threading
from pathlib import Path

import pytest

from lib import font_manager as fm
from lib.config import VoiceHubConfig, parse_font_urls

LOGGER = logging.getLogger("font-test")

GOOD_MAGIC = b"\x00\x01\x00\x00"


def _fake_font(seed: bytes = b"") -> bytes:
    """构造一个体积达标、文件头合法的假字体。"""
    return GOOD_MAGIC + seed + b"\x00" * fm.MIN_FONT_BYTES


class _StubServer:
    """最小的 HTTP/1.1 服务，按路径返回预设状态码与正文，并统计请求次数。"""

    def __init__(self, routes: dict[str, tuple[int, bytes]]):
        self.routes = routes
        self.counts: dict[str, int] = {}
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._server.settimeout(0.2)
        self.port = self._server.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "_StubServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._server.close()
        self._thread.join(timeout=2)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    request = conn.recv(4096).decode("latin-1")
                    path = request.split(" ")[1] if " " in request else "/"
                    self.counts[path] = self.counts.get(path, 0) + 1
                    status, body = self.routes.get(path, (404, b"missing"))
                    reason = b"OK" if status == 200 else b"Error"
                    conn.sendall(
                        b"HTTP/1.1 %d %s\r\nContent-Length: %d\r\n"
                        b"Connection: close\r\n\r\n"
                        % (status, reason, len(body))
                        + body
                    )
                except OSError:
                    pass


def _build_tgz(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_font_sources_put_configured_overrides_first():
    sources = fm.font_sources(
        "regular", {"regular": ["https://mirror.example/a.ttf"]}
    )
    assert sources[0].url == "https://mirror.example/a.ttf"
    # 内置源仍保留，作为配置源失败后的兜底
    assert any("cdn.npmmirror.com" in source.url for source in sources)
    assert any("cdn.jsdelivr.net" in source.url for source in sources)


def test_default_sources_are_mainland_friendly_and_cover_archive():
    """默认源必须优先走中国大陆可达的 npm 镜像，而不是只依赖 jsDelivr。"""
    sources = fm.font_sources("bold")
    hosts = [source.label for source in sources]

    mainland = [
        source
        for source in sources
        if "npmmirror" in source.url or "tencent" in source.url or "huaweicloud" in source.url
    ]
    assert mainland, "默认源缺少中国大陆镜像"
    assert mainland[0] is sources[0], "中国大陆镜像应排在最前"
    assert mainland[0].member == "package/HarmonyOS_Sans_SC_Bold.ttf"
    # 归档型源会复用同一份下载，避免两个字体各下一次 33 MB
    assert len({source.url for source in mainland}) == 3
    assert any("jsdelivr" in host for host in hosts)


def test_parse_font_urls_accepts_multiple_lines_and_drops_junk():
    parsed = parse_font_urls(
        "\n".join(
            [
                "regular=https://a.example/reg.ttf",
                "regular=https://b.example/reg.tgz#package/HarmonyOS_Sans_SC_Regular.ttf",
                "bold=https://a.example/bold.ttf",
                "bold=ftp://bad.example/bold.ttf",
                "unknown=https://a.example/x.ttf",
                "没有等号的一行",
            ]
        )
    )
    assert parsed["regular"] == [
        "https://a.example/reg.ttf",
        "https://b.example/reg.tgz#package/HarmonyOS_Sans_SC_Regular.ttf",
    ]
    assert parsed["bold"] == ["https://a.example/bold.ttf"]


def test_config_exposes_font_dir_and_urls():
    config = VoiceHubConfig.from_mapping(
        {
            "webhook_token": "t",
            "font_dir": "/srv/fonts",
            "font_urls": "regular=https://a.example/reg.ttf",
        }
    )
    assert config.font_dir == "/srv/fonts"
    assert config.font_urls["regular"] == ["https://a.example/reg.ttf"]


def test_fonts_already_exist_rejects_truncated_or_foreign_files(tmp_path: Path):
    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    for filename in fm.FONT_FILES.values():
        (font_dir / filename).write_bytes(GOOD_MAGIC + b"\x00" * 100)
    assert fm.fonts_already_exist(font_dir) is False

    # 体积够大但不是字体（例如 HTML 错误页）也不能算缓存
    for filename in fm.FONT_FILES.values():
        (font_dir / filename).write_bytes(b"<html>" + b"\x00" * fm.MIN_FONT_BYTES)
    assert fm.fonts_already_exist(font_dir) is False

    for filename in fm.FONT_FILES.values():
        (font_dir / filename).write_bytes(_fake_font())
    assert fm.fonts_already_exist(font_dir) is True


def test_ensure_fonts_downloads_via_configured_source(tmp_path: Path):
    regular = _fake_font(b"R")
    bold = _fake_font(b"B")
    with _StubServer(
        {"/regular.ttf": (200, regular), "/bold.ttf": (200, bold)}
    ) as server:
        sources = {
            "regular": [server.url("/regular.ttf")],
            "bold": [server.url("/bold.ttf")],
        }
        result = asyncio.run(fm.ensure_fonts(tmp_path, LOGGER, sources))

    assert result["regular"].read_bytes() == regular
    assert result["bold"].read_bytes() == bold
    # 原子落盘：临时文件不得残留
    assert list(tmp_path.glob("*.part")) == []


def test_ensure_fonts_falls_back_to_next_source(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fm, "RETRY_DELAYS", (0, 0))
    regular = _fake_font()
    with _StubServer(
        {
            "/broken.ttf": (503, b"busy"),
            "/ok.ttf": (200, regular),
        }
    ) as server:
        sources = {
            "regular": [server.url("/broken.ttf"), server.url("/ok.ttf")],
            "bold": [server.url("/ok.ttf")],
        }
        asyncio.run(fm.ensure_fonts(tmp_path, LOGGER, sources))

    assert (tmp_path / fm.FONT_FILES["regular"]).read_bytes() == regular
    assert server.counts["/ok.ttf"] >= 1


def test_ensure_fonts_extracts_from_npm_archive_once(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fm, "MIN_ARCHIVE_BYTES", 1024)
    archive = _build_tgz(
        {
            "package/HarmonyOS_Sans_SC_Regular.ttf": _fake_font(b"R"),
            "package/HarmonyOS_Sans_SC_Bold.ttf": _fake_font(b"B"),
        }
    )
    with _StubServer({"/fontpkg.tgz": (200, archive)}) as server:
        url = server.url("/fontpkg.tgz")
        sources = {
            "regular": [f"{url}#package/HarmonyOS_Sans_SC_Regular.ttf"],
            "bold": [f"{url}#package/HarmonyOS_Sans_SC_Bold.ttf"],
        }
        asyncio.run(fm.ensure_fonts(tmp_path, LOGGER, sources))

    assert (tmp_path / fm.FONT_FILES["regular"]).stat().st_size >= fm.MIN_FONT_BYTES
    assert (tmp_path / fm.FONT_FILES["bold"]).stat().st_size >= fm.MIN_FONT_BYTES
    # 两个字体共用同一份归档，只下载一次
    assert server.counts["/fontpkg.tgz"] == 1
    assert list(tmp_path.glob("*.part")) == []


def test_ensure_fonts_error_lists_reasons_and_manual_hint(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fm, "RETRY_DELAYS", (0, 0))
    # 只验证配置源：清空内置源，避免测试环境真的从公网下载到字体
    monkeypatch.setattr(fm, "DEFAULT_SOURCES", ())
    with _StubServer({"/fail.ttf": (500, b"boom")}) as server:
        sources = {
            "regular": [server.url("/fail.ttf")],
            "bold": [server.url("/fail.ttf")],
        }
        with pytest.raises(fm.FontDownloadError) as excinfo:
            asyncio.run(fm.ensure_fonts(tmp_path, LOGGER, sources))

    error = excinfo.value
    message = str(error)
    assert "HTTP 500" in message
    assert str(tmp_path) in message
    assert error.short and str(tmp_path) in error.short
    assert error.filename == fm.FONT_FILES["regular"]
    assert list(tmp_path.glob("*.part")) == []


def test_ensure_fonts_reports_timeout_type_when_message_is_empty(tmp_path: Path, monkeypatch):
    """str(TimeoutError()) 为空：汇总原因必须回退到类型名，不能留空。"""

    async def boom(source, dest, ctx):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(fm, "_fetch_source", boom)
    monkeypatch.setattr(fm, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(fm, "DEFAULT_SOURCES", ())
    sources = {
        "regular": ["https://mirror.example/reg.ttf"],
        "bold": ["https://mirror.example/bold.ttf"],
    }
    with pytest.raises(fm.FontDownloadError) as excinfo:
        asyncio.run(fm.ensure_fonts(tmp_path, LOGGER, sources))

    assert "下载超时" in str(excinfo.value)


def test_ensure_fonts_redownloads_incomplete_cache(tmp_path: Path):
    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    broken = fm.FONT_FILES["regular"]
    (font_dir / broken).write_bytes(GOOD_MAGIC + b"\x00" * 64)  # 半截文件

    regular = _fake_font()
    with _StubServer({"/regular.ttf": (200, regular)}) as server:
        sources = {
            "regular": [server.url("/regular.ttf")],
            "bold": [server.url("/regular.ttf")],
        }
        asyncio.run(fm.ensure_fonts(font_dir, LOGGER, sources))

    assert (font_dir / broken).read_bytes() == regular


def test_ensure_fonts_deduplicates_concurrent_first_downloads(tmp_path: Path):
    regular = _fake_font(b"R")
    bold = _fake_font(b"B")
    with _StubServer(
        {"/regular.ttf": (200, regular), "/bold.ttf": (200, bold)}
    ) as server:
        sources = {
            "regular": [server.url("/regular.ttf")],
            "bold": [server.url("/bold.ttf")],
        }

        async def run_twice():
            return await asyncio.gather(
                fm.ensure_fonts(tmp_path, LOGGER, sources),
                fm.ensure_fonts(tmp_path, LOGGER, sources),
            )

        first, second = asyncio.run(run_twice())

    assert first["regular"].read_bytes() == second["regular"].read_bytes() == regular
    # 并发触发首次出图时，同一文件只下载一次
    assert server.counts["/regular.ttf"] == 1
    assert server.counts["/bold.ttf"] == 1


def test_default_timeout_is_generous_for_archive_downloads():
    assert fm.FONT_TIMEOUT.total is None or fm.FONT_TIMEOUT.total >= 300
