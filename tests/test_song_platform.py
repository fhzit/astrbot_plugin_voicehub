"""批次 1 新功能：`-平台` 参数（序号 + 名称词）、音源列表、点歌地址指令。"""

import asyncio
import json
import types

import pytest

from lib.config import VoiceHubConfig
from lib.song import (
    PLATFORM_NAMES,
    PasswordResetOutcome,
    SongCandidate,
    SongRequestOutcome,
    SongSearchOutcome,
    SongService,
    VoiceHubSongClient,
    format_platform,
    parse_pick_args,
    resolve_platform_argument,
    TEXT_PICK_INVALID,
    TEXT_PICK_MANUAL_ARTIST_REQUIRED,
    TEXT_PICK_MANUAL_USAGE,
    TEXT_RESOLVE_UNRECOGNIZED,
)
from tests.test_song import UMO, _RecordingClient, _config, _search_outcome


# ----------------------------------------------------------------------
# -平台 参数解析：名称词优先级表
# ----------------------------------------------------------------------


def test_platform_alias_names_map_to_keys():
    """用户输入 B站/b站/bilibili/哔哩哔哩 等名称词映射到平台键。"""
    for word, key in (
        ("B站", "bilibili"), ("b站", "bilibili"), ("bilibili", "bilibili"),
        ("哔哩哔哩", "bilibili"), ("BILIBILI", "bilibili"),
        ("网易云", "netease"), ("网易云音乐", "netease"), ("网易", "netease"),
        ("QQ音乐", "tencent"), ("qq音乐", "tencent"), ("tencent", "tencent"),
        ("咪咕", "migu"), ("migu", "migu"),
    ):
        assert resolve_platform_argument(word) == key, word


def test_platform_alias_unknown_word_returns_none():
    """不是名称词也不是数字的取值返回 None。"""
    assert resolve_platform_argument("spotify") is None
    assert resolve_platform_argument("") is None
    assert resolve_platform_argument(None) is None


def test_platform_alias_number_defers_to_sources_order():
    """纯数字不是别名表职责：返回 None，由启用顺序映射处理。"""
    assert resolve_platform_argument("1") is None
    assert resolve_platform_argument("3") is None


def test_platform_names_cover_all_keys():
    """每个平台键都有中文名。"""
    assert set(PLATFORM_NAMES) >= {"netease", "tencent", "bilibili", "migu"}


# ----------------------------------------------------------------------
# 音源列表客户端（GET /api/bot/voicehub/song-sources）
# ----------------------------------------------------------------------


class _StubSourcesApi:
    """桩 VoiceHub：实现 GET /api/bot/voicehub/song-sources。"""

    def __init__(self):
        self.sources_status = 200
        self.sources_response = {
            "success": True,
            "defaultSource": "netease",
            "sources": [
                {"index": 1, "key": "netease", "name": "网易云音乐"},
                {"index": 2, "key": "bilibili", "name": "哔哩哔哩"},
            ],
        }
        self.paths = []
        self.headers = []
        self._server = None
        self._thread = None

    def start(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: ARG002
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.paths.append(self.path)
                outer.headers.append(dict(self.headers))
                if self.path.endswith("/song-sources"):
                    self._send(outer.sources_status, outer.sources_response)
                    return
                self._send(404, {"success": False, "message": "not found"})

            def do_POST(self):
                self._send(404, {"success": False, "message": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)


def test_fetch_sources_maps_response():
    """音源列表端点：路径、令牌头、响应映射。"""
    stub = _StubSourcesApi().start()
    try:
        client = VoiceHubSongClient(_config(stub.base_url))
        outcome = asyncio.run(client.fetch_sources())
        assert stub.paths[0].endswith("/api/bot/voicehub/song-sources")
        sent = {k.lower(): v for k, v in stub.headers[0].items()}
        assert sent.get("x-voicehub-token") == "t0ken"
        assert outcome.ok is True
        assert [(s.index, s.key, s.name) for s in outcome.sources] == [
            (1, "netease", "网易云音乐"),
            (2, "bilibili", "哔哩哔哩"),
        ]
    finally:
        stub.stop()


def test_fetch_sources_error_is_failure():
    """音源列表失败收敛为 ok=False，不抛异常。"""
    stub = _StubSourcesApi()
    stub.sources_status = 401
    stub.sources_response = {"success": False, "message": "机器人令牌无效"}
    stub = stub.start()
    try:
        client = VoiceHubSongClient(_config(stub.base_url))
        outcome = asyncio.run(client.fetch_sources())
        assert outcome.ok is False
        assert outcome.message == "机器人令牌无效"
    finally:
        stub.stop()


# ----------------------------------------------------------------------
# 序号 → 平台键映射（结合音源启用顺序）
# ----------------------------------------------------------------------


def test_resolve_platform_by_index_uses_sources_order():
    """`-平台 2` 按音源启用列表的序号取平台；无列表时回退缺省。"""
    from lib.song import SongSourcesOutcome

    class _SourcesClient:
        def __init__(self, sources):
            self._outcome = SongSourcesOutcome(
                ok=True,
                sources=[
                    types.SimpleNamespace(index=i + 1, key=key, name=name)
                    for i, (key, name) in enumerate(sources)
                ],
            )

        async def fetch_sources(self):
            return self._outcome

    async def run():
        client = _SourcesClient([("netease", "网易云音乐"), ("bilibili", "哔哩哔哩")])
        service = SongService(_config("http://stub.invalid"), client=client)
        # 序号 2 = 启用列表里的 bilibili
        assert await service.resolve_platform("2") == "bilibili"
        # 名称词不受列表影响
        assert await service.resolve_platform("B站") == "bilibili"
        assert await service.resolve_platform("网易云") == "netease"

    asyncio.run(run())


def test_resolve_platform_out_of_range_is_none():
    """序号超出启用列表范围时返回 None（上层给定点文案）。"""
    from lib.song import SongSourcesOutcome

    class _SourcesClient:
        def __init__(self, sources):
            self._outcome = SongSourcesOutcome(
                ok=True,
                sources=[
                    types.SimpleNamespace(index=i + 1, key=key, name=name)
                    for i, (key, name) in enumerate(sources)
                ],
            )

        async def fetch_sources(self):
            return self._outcome

    async def run():
        client = _SourcesClient([("netease", "网易云音乐")])
        service = SongService(_config("http://stub.invalid"), client=client)
        assert await service.resolve_platform("3") is None
        # 音源列表获取失败 → 序号路径不可用，返回 None
        class _BrokenClient:
            async def fetch_sources(self):
                return SongSourcesOutcome(ok=False, message="网络错误")

        service2 = SongService(_config("http://stub.invalid"), client=_BrokenClient())
        assert await service2.resolve_platform("1") is None

    asyncio.run(run())


# ----------------------------------------------------------------------
# 点歌指令带 -平台 参数
# ----------------------------------------------------------------------


def test_song_command_passes_platform_keyword_to_search():
    """`点歌 <词> -平台 B站`：解析为 bilibili 并传入搜索。"""
    async def run():
        client = _RecordingClient(search=_search_outcome(platform="bilibili"))
        service = SongService(_config("http://stub.invalid"), client=client)
        await service.song(UMO, "", "miku -平台 B站")
        assert client.search_calls[0]["platform"] == "bilibili"

    asyncio.run(run())


def test_song_command_platform_number_resolves_via_sources():
    """`点歌 <词> -平台 2`：序号按启用列表映射。"""
    from lib.song import SongSourcesOutcome

    class _SourcesClient:
        def __init__(self, outcome):
            self._outcome = outcome

        async def fetch_sources(self):
            return self._outcome

    async def run():
        sources = SongSourcesOutcome(
            ok=True,
            sources=[
                types.SimpleNamespace(index=1, key="netease", name="网易云音乐"),
                types.SimpleNamespace(index=2, key="bilibili", name="哔哩哔哩"),
            ],
        )
        client = _RecordingClient(search=_search_outcome(platform="bilibili"), sources=sources)
        service = SongService(_config("http://stub.invalid"), client=client)
        await service.song(UMO, "", "miku -平台 2")
        assert client.search_calls[0]["platform"] == "bilibili"

    asyncio.run(run())


def test_song_command_invalid_platform_gets_fixed_text():
    """无效平台给定点文案，且不发起搜索。"""
    async def run():
        client = _RecordingClient(search=_search_outcome())
        service = SongService(_config("http://stub.invalid"), client=client)
        text = await service.song(UMO, "", "miku -平台 spotify")
        assert "平台" in text and "spotify" not in text
        assert client.search_calls == []

    asyncio.run(run())


def test_song_without_platform_flag_uses_default():
    """不带 -平台 时行为与现状一致（netease 缺省）。"""
    async def run():
        client = _RecordingClient(search=_search_outcome())
        service = SongService(_config("http://stub.invalid"), client=client)
        await service.song(UMO, "", "告白气球")
        assert client.search_calls[0]["platform"] == "netease"

    asyncio.run(run())


# ----------------------------------------------------------------------
# 点歌地址指令
# ----------------------------------------------------------------------


def test_site_command_replies_base_url():
    """`点歌地址` 回复配置的站点地址。"""
    async def run():
        client = _RecordingClient(search=_search_outcome())
        service = SongService(_config("https://music.example.com"), client=client)
        text = await service.site(UMO, "")
        assert "https://music.example.com" in text

    asyncio.run(run())


def test_site_command_without_base_url_gets_fixed_text():
    """未配置站点地址时给定点文案。"""
    async def run():
        client = _RecordingClient(search=_search_outcome())
        service = SongService(_config(""), client=client)
        assert await service.site(UMO, "") == "插件未配置 VoiceHub 站点地址。"

    asyncio.run(run())


def test_site_command_group_chat_refused():
    """群聊拒绝（与点歌同口径）。"""
    async def run():
        client = _RecordingClient(search=_search_outcome())
        service = SongService(_config("https://music.example.com"), client=client)
        text = await service.site(UMO, "12345")
        assert "私聊" in text

    asyncio.run(run())


# ----------------------------------------------------------------------
# 渲染：搜索结果尾部提示可用 -平台
# ----------------------------------------------------------------------


def test_render_song_list_hint_mentions_platform_flag():
    """搜索列表尾部提示 -平台 参数的用法。"""
    from lib.song import render_song_list

    text = render_song_list("miku", "bilibili", [
        SongCandidate(index=1, title="メリュー", artist="n-buna", duration_seconds=253),
    ])
    assert "平台" in text
    assert "-平台" in text


def test_format_platform_unknown_key_passthrough():
    """未知平台键原样返回（现状兼容）。"""
    assert format_platform("netease") == "网易云音乐"
    assert format_platform("custom") == "custom"


# ----------------------------------------------------------------------
# 分享链接解析（点歌 <链接> → 单曲会话 → 选歌 1 确认）
# ----------------------------------------------------------------------


def _resolve_outcome(keyword="晴天", platform="netease"):
    return SongSearchOutcome(
        ok=True,
        message="",
        session_token="sealed-resolve-1",
        platform=platform,
        keyword=keyword,
        candidates=[SongCandidate(index=1, title=keyword, artist="周杰伦", duration_seconds=269)],
    )


class _ResolveRecordingClient(_RecordingClient):
    """在 _RecordingClient 上加 resolve 记录。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.resolve_calls = []
        self._resolve = _resolve_outcome()

    async def resolve(self, umo, text):
        self.resolve_calls.append({"umo": umo, "text": text})
        return self._resolve


def test_song_command_with_netease_link_goes_to_resolve():
    """`点歌 <网易云链接>`：走 resolve 端点，写单元素会话。"""
    async def run():
        client = _ResolveRecordingClient()
        service = SongService(_config("http://stub.invalid", share_link_auto_resolve=True), client=client)
        text = await service.song(UMO, "", "分享《晴天》 https://music.163.com/song?id=186016 快听")
        assert client.resolve_calls and "music.163.com" in client.resolve_calls[0]["text"]
        assert client.search_calls == []
        assert "已识别：晴天 - 周杰伦" in text
        assert "选歌 1" in text
        # 会话里只有一首：选歌 1 直接投稿
        session = service.session(UMO)
        assert session is not None and len(session.candidates) == 1
        assert session.session_token == "sealed-resolve-1"

    asyncio.run(run())


def test_song_command_with_bilibili_and_qq_links_detected():
    """B站与 QQ 音乐链接同样走解析。"""
    async def run():
        for fragment in ("https://b23.tv/abc123", "https://y.qq.com/n/ryqq/songDetail/001J5QJL", "163cn.tv/xy"):
            client = _ResolveRecordingClient()
            service = SongService(_config("http://stub.invalid", share_link_auto_resolve=True), client=client)
            await service.song(UMO, "", f"看看 {fragment}")
            assert len(client.resolve_calls) == 1, fragment

    asyncio.run(run())


def test_resolve_failure_returns_server_message():
    """解析端点拒绝时回站点文案原文。"""
    async def run():
        client = _ResolveRecordingClient()
        client._resolve = SongSearchOutcome(ok=False, message="无法识别的分享链接，支持网易云音乐、QQ音乐、哔哩哔哩")
        service = SongService(_config("http://stub.invalid", share_link_auto_resolve=True), client=client)
        text = await service.song(UMO, "", "https://music.163.com/song?id=1")
        assert "无法识别的分享链接" in text

    asyncio.run(run())


def test_resolve_renders_duration_when_known():
    """解析结果带时长行。"""
    from lib.song import render_resolved_song

    text = render_resolved_song("netease", SongCandidate(index=1, title="晴天", artist="周杰伦", duration_seconds=269))
    assert "已识别：晴天 - 周杰伦（音源：网易云音乐）" in text
    assert "04:29" in text
    assert "选歌 1" in text


# ----------------------------------------------------------------------
# 选歌 手动（手动投稿）
# ----------------------------------------------------------------------

def _service_with_client(client):
    return SongService(_config("http://stub.invalid"), client=client)


class _ManualStubClient(_RecordingClient):
    """手动投稿桩：记录调用并返回成功。"""

    def __init__(self, outcome=None):
        super().__init__()
        self.manual_calls = []
        self._manual = outcome or SongRequestOutcome(ok=True, message="点歌成功：晴天 - 周杰伦", song_id=9)

    async def request_song_manual(self, umo, title, artist, card_code=None, note=None):
        self.manual_calls.append({"umo": umo, "title": title, "artist": artist, "card_code": card_code, "note": note})
        return self._manual


def test_parse_manual_pick_basic():
    """`选歌 手动 歌名 -歌手 歌手` → manual dict，序号占位 1。"""
    args, error = parse_pick_args("手动 告白气球 -歌手 周杰伦")
    assert error == ""
    assert args is not None
    assert args.manual == {"title": "告白气球", "artist": "周杰伦"}
    assert args.index == 1


def test_parse_manual_pick_title_with_spaces():
    """歌名可含空格，直到第一个 `-` 参数。"""
    args, error = parse_pick_args("手动 Fix You 2011 Remaster -歌手 Coldplay")
    assert error == ""
    assert args is not None
    assert args.manual == {"title": "Fix You 2011 Remaster", "artist": "Coldplay"}


def test_parse_manual_pick_requires_artist():
    """缺 `-歌手` 报错。"""
    args, error = parse_pick_args("手动 告白气球")
    assert args is None
    assert error == TEXT_PICK_MANUAL_ARTIST_REQUIRED


def test_parse_manual_pick_artist_flag_without_value():
    args, error = parse_pick_args("手动 告白气球 -歌手")
    assert args is None
    assert error == TEXT_PICK_MANUAL_ARTIST_REQUIRED


def test_parse_manual_pick_rejects_link_and_cover():
    """手动投稿没有 -链接/-封面：出现即按未知参数报错。"""
    args, error = parse_pick_args("手动 告白气球 -歌手 周杰伦 -链接 https://x.com/1")
    assert args is None
    assert error == TEXT_PICK_INVALID
    args, error = parse_pick_args("手动 告白气球 -歌手 周杰伦 -封面 https://x.com/a.jpg")
    assert args is None
    assert error == TEXT_PICK_INVALID


def test_parse_manual_pick_with_optional_flags():
    """时段/点歌券/备注照常解析；备注含空格。"""
    args, error = parse_pick_args("手动 晴天 -歌手 周杰伦 -时段 2 -点歌券 ab12 -备注 生日快乐 玩得开心")
    assert error == ""
    assert args is not None
    assert args.manual == {"title": "晴天", "artist": "周杰伦"}
    assert args.play_time_index == 2
    assert args.card_code == "AB12"
    assert args.note == "生日快乐 玩得开心"


def test_parse_manual_pick_empty_after_keyword():
    args, error = parse_pick_args("手动")
    assert args is None
    assert error == TEXT_PICK_MANUAL_USAGE


def test_parse_manual_pick_duplicate_flag():
    args, error = parse_pick_args("手动 晴天 -歌手 周杰伦 -歌手 林俊杰")
    assert args is None
    assert error == TEXT_PICK_INVALID


def test_pick_manual_calls_request_song_manual_without_session():
    """手动投稿不要求点歌会话，且调用 request_song_manual。"""
    stub = _ManualStubClient()
    service = _service_with_client(stub)

    async def run():
        return await service.pick(UMO, "", "手动 晴天 -歌手 周杰伦 -备注 生日快乐")

    reply = asyncio.run(run())
    assert reply == "点歌成功：晴天 - 周杰伦"
    assert stub.manual_calls == [{
        "umo": UMO, "title": "晴天", "artist": "周杰伦", "card_code": None, "note": "生日快乐",
    }]


def test_pick_manual_failure_does_not_start_cooldown():
    """失败的投稿不进入冷却。"""
    stub = _ManualStubClient(SongRequestOutcome(ok=False, message="点歌券无效"))
    service = _service_with_client(stub)

    async def run():
        return await service.pick(UMO, "", "手动 晴天 -歌手 周杰伦 -点歌券 XXXX")

    reply = asyncio.run(run())
    assert reply == "点歌券无效"
    assert service._cooling_down(UMO) is False


# ----------------------------------------------------------------------
# share_link_auto_resolve 开关（默认关：链接当普通关键词搜索）
# ----------------------------------------------------------------------

_SHARE_TEXT = "【周杰伦《晴天》】https://music.163.com/song?id=186016 复制此链接"


class _ResolveStubClient(_RecordingClient):
    """resolve 桩：返回单条候选的搜索同形结果。"""

    def __init__(self):
        super().__init__(search=_search_outcome())
        self.resolve_calls = []
        self._resolve = SongSearchOutcome(
            ok=True,
            message="",
            session_token="sealed-resolve-1",
            platform="netease",
            keyword="晴天",
            candidates=[SongCandidate(index=1, title="晴天", artist="周杰伦", duration_seconds=269)],
        )

    async def resolve(self, umo, text):
        self.resolve_calls.append({"umo": umo, "text": text})
        return self._resolve


def test_share_link_disabled_by_default_goes_to_search():
    """开关默认关：链接文本按普通关键词走搜索。"""
    stub = _ResolveStubClient()
    service = SongService(_config("http://stub.invalid"), client=stub)

    async def run():
        return await service.song(UMO, "", _SHARE_TEXT)

    reply = asyncio.run(run())
    assert stub.resolve_calls == []
    assert stub.search_calls, "应走 search 而非 resolve"


def test_share_link_enabled_resolves_single_candidate():
    """开关开：链接走 resolve，会话写入单条候选（选歌 1 确认）。"""
    stub = _ResolveStubClient()
    cfg = _config("http://stub.invalid", share_link_auto_resolve=True)
    service = SongService(cfg, client=stub)

    async def run():
        return await service.song(UMO, "", _SHARE_TEXT)

    reply = asyncio.run(run())
    assert stub.resolve_calls == [{"umo": UMO, "text": _SHARE_TEXT}]
    assert "已识别：晴天 - 周杰伦（音源：网易云音乐）" in reply
    assert "选歌 1" in reply
    session = service.session(UMO)
    assert session is not None
    assert len(session.candidates) == 1
    assert session.session_token == "sealed-resolve-1"


def test_share_link_resolve_unrecognized_message():
    """解析不出候选时回复专用文案。"""
    stub = _ResolveStubClient()
    stub._resolve = SongSearchOutcome(ok=True, message="", session_token="t", platform="netease", keyword="", candidates=[])
    service = SongService(_config("http://stub.invalid", share_link_auto_resolve=True), client=stub)

    async def run():
        return await service.song(UMO, "", _SHARE_TEXT)

    reply = asyncio.run(run())
    assert reply == TEXT_RESOLVE_UNRECOGNIZED


def test_share_link_failed_resolution_returns_message():
    """端点报错时透出错误文案。"""
    stub = _ResolveStubClient()
    stub._resolve = SongSearchOutcome(ok=False, message="无法识别的分享链接")
    service = SongService(_config("http://stub.invalid", share_link_auto_resolve=True), client=stub)

    async def run():
        return await service.song(UMO, "", _SHARE_TEXT)

    assert asyncio.run(run()) == "无法识别的分享链接"


# ----------------------------------------------------------------------
# 重置密码（两步确认，私聊专用）
# ----------------------------------------------------------------------

class _ResetStubClient(_RecordingClient):
    """重置密码桩：记录调用并按脚本返回。"""

    def __init__(self):
        super().__init__()
        self.reset_calls = []
        self._init = PasswordResetOutcome(
            ok=True, message="", pending_token="pending-token-1", expires_in_seconds=300,
        )
        self._confirm = PasswordResetOutcome(ok=True, message="密码重置成功")

    async def password_reset(self, umo, step, password, pending_token=None):
        self.reset_calls.append({
            "umo": umo, "step": step, "password": password, "pending_token": pending_token,
        })
        return self._init if step == "init" else self._confirm


def test_reset_password_group_chat_refused():
    async def run():
        stub = _ResetStubClient()
        service = _service_with_client(stub)
        reply = await service.reset_password(UMO, "12345", "N3w-Passw0rd!")
        assert "私聊" in reply
        assert stub.reset_calls == []

    asyncio.run(run())


def test_reset_password_two_step_flow():
    async def run():
        stub = _ResetStubClient()
        service = _service_with_client(stub)
        first = await service.reset_password(UMO, "", "N3w-Passw0rd!")
        # 第一步不改密码，只创建 pending
        assert stub.reset_calls == [{"umo": UMO, "step": "init", "password": "N3w-Passw0rd!", "pending_token": None}]
        assert "确认" in first
        second = await service.reset_password(UMO, "", "确认 N3w-Passw0rd!")
        assert second == "密码重置成功"
        assert stub.reset_calls[1] == {
            "umo": UMO, "step": "confirm", "password": "N3w-Passw0rd!", "pending_token": "pending-token-1",
        }

    asyncio.run(run())


def test_reset_password_mismatch_cancels_pending():
    async def run():
        stub = _ResetStubClient()
        service = _service_with_client(stub)
        await service.reset_password(UMO, "", "N3w-Passw0rd!")
        reply = await service.reset_password(UMO, "", "确认 Another-Pass1!")
        assert "不一致" in reply
        # pending 已作废：再确认也不行
        reply2 = await service.reset_password(UMO, "", "确认 N3w-Passw0rd!")
        assert "过期或不正确" in reply2

    asyncio.run(run())


def test_reset_password_confirm_without_pending():
    async def run():
        stub = _ResetStubClient()
        service = _service_with_client(stub)
        reply = await service.reset_password(UMO, "", "确认 N3w-Passw0rd!")
        assert "过期或不正确" in reply
        assert stub.reset_calls == []

    asyncio.run(run())


def test_reset_password_expired_pending():
    async def run():
        clock = [1000.0]
        stub = _ResetStubClient()
        service = SongService(_config("http://stub.invalid"), client=stub, now=lambda: clock[0])
        await service.reset_password(UMO, "", "N3w-Passw0rd!")
        clock[0] += 301.0  # 超过 5 分钟
        reply = await service.reset_password(UMO, "", "确认 N3w-Passw0rd!")
        assert "过期或不正确" in reply

    asyncio.run(run())


def test_reset_password_empty_args_usage():
    async def run():
        stub = _ResetStubClient()
        service = _service_with_client(stub)
        reply = await service.reset_password(UMO, "", "")
        assert "用法" in reply

    asyncio.run(run())


def test_reset_password_init_failure_returns_server_message():
    async def run():
        stub = _ResetStubClient()
        stub._init = PasswordResetOutcome(ok=False, message="密码太短")
        service = _service_with_client(stub)
        reply = await service.reset_password(UMO, "", "short")
        assert reply == "密码太短"

    asyncio.run(run())
