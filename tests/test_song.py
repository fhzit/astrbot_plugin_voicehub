"""点歌指令测试：参数解析、列表渲染、会话状态与 HTTP 客户端映射。

不启动真实 AstrBot：直接测 `lib/song.py` 的纯函数与服务状态机，
HTTP 客户端用本机桩 VoiceHub 驱动（与 `tests/test_pull.py` 同样的风格）。
"""
import asyncio
import json
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from astrbot_plugin_voicehub.lib.config import VoiceHubConfig
from astrbot_plugin_voicehub.lib.song import (
    DEFAULT_RESULT_COUNT,
    MAX_RESULT_COUNT,
    PICK_COOLDOWN_SECONDS,
    SONG_SESSION_TTL_SECONDS,
    PlayTime,
    SongCandidate,
    SongSearchOutcome,
    SongService,
    VoiceHubSongClient,
    format_duration,
    format_platform,
    parse_pick_args,
    render_play_times,
    render_song_list,
)

UMO = "aiocqhttp:FriendMessage:user1"
GROUP = "123456789"

TEXT_GROUP_ONLY = "为避免刷屏，请在机器人私聊中点歌。"
TEXT_NO_SESSION = "请先用「/vh song 关键词」搜索歌曲。"
TEXT_COOLDOWN = "点歌太频繁，请稍后再试。"
TEXT_NO_BASE_URL = "插件未配置 VoiceHub 站点地址，无法点歌。"
TEXT_NETWORK = "无法连接 VoiceHub，请稍后重试。"
TEXT_FAILED = "点歌失败，请稍后重试。"
TEXT_NO_PLAY_TIME = "当前未开放播出时段选择。"
TEXT_INDEX_INVALID = "序号必须是 1-5 之间的整数。"


# ----------------------------------------------------------------------
# 桩 VoiceHub 站点：记录请求体，按预设返回
# ----------------------------------------------------------------------


class _StubSongApi:
    """桩 VoiceHub：实现 song-search / song-request / play-times。"""

    def __init__(self):
        self.search_bodies = []
        self.request_bodies = []
        self.headers = []
        self.search_status = 200
        self.search_response = {
            "success": True,
            "platform": "netease",
            "keyword": "告白气球",
            "sessionToken": "sealed-ticket-1",
            "items": [
                {"index": 1, "title": "告白气球", "artist": "周杰伦", "durationSeconds": 215},
                {"index": 2, "title": "晴天", "artist": "周杰伦", "durationSeconds": 269},
                {"index": 3, "title": "无时长", "artist": "某人", "durationSeconds": None},
            ],
        }
        self.request_status = 200
        self.request_response = {
            "success": True,
            "songId": 123,
            "title": "告白气球",
            "artist": "周杰伦",
            "message": "点歌成功：告白气球 - 周杰伦",
        }
        self.play_times_status = 200
        self.play_times_response = {
            "enabled": True,
            "playTimes": [
                {"id": 1, "name": "午间广播", "startTime": "12:00", "endTime": "12:30", "enabled": True},
                {"id": 2, "name": "晚间广播", "startTime": "18:00", "endTime": "18:30", "enabled": True},
            ],
        }
        self._server = None
        self._thread = None

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: ARG002 - 静音访问日志
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.headers.append(dict(self.headers))
                if self.path.endswith("/play-times"):
                    self._send(outer.play_times_status, outer.play_times_response)
                    return
                self._send(404, {"success": False, "message": "not found"})

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                outer.headers.append(dict(self.headers))
                body = json.loads(raw or b"{}")
                if self.path.endswith("/song-search"):
                    outer.search_bodies.append(body)
                    self._send(outer.search_status, outer.search_response)
                    return
                if self.path.endswith("/song-request"):
                    outer.request_bodies.append(body)
                    self._send(outer.request_status, outer.request_response)
                    return
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


class _RecordingClient:
    """记录调用的桩客户端，供服务状态机测试使用（不发网络请求）。"""

    def __init__(self, search=None, request=None, play_times=None):
        self.search_calls = []
        self.request_calls = []
        self.play_time_calls = 0
        self._search = search
        self._request = request
        self._play_times = play_times

    async def search(self, umo, keyword, platform="netease", page=1):
        self.search_calls.append({"umo": umo, "keyword": keyword, "platform": platform, "page": page})
        return self._search

    async def request_song(self, umo, session_token, index, play_time_id=None, card_code=None, note=None):
        self.request_calls.append({
            "umo": umo, "session_token": session_token, "index": index,
            "play_time_id": play_time_id, "card_code": card_code, "note": note,
        })
        return self._request

    async def fetch_play_times(self):
        self.play_time_calls += 1
        return self._play_times


def _config(base_url="", **overrides):
    data = {"voicehub_base_url": base_url, "webhook_token": "t0ken"}
    data.update(overrides)
    return VoiceHubConfig.from_mapping(data)


def _search_outcome(candidates=None, keyword="告白气球", platform="netease"):
    return SongSearchOutcome(
        ok=True,
        message="",
        session_token="sealed-ticket-1",
        platform=platform,
        keyword=keyword,
        candidates=candidates if candidates is not None else [
            SongCandidate(index=1, title="告白气球", artist="周杰伦", duration_seconds=215),
            SongCandidate(index=2, title="晴天", artist="周杰伦", duration_seconds=269),
        ],
    )


def _ok_song_request():
    return types.SimpleNamespace(ok=True, message="点歌成功：告白气球 - 周杰伦", song_id=123)


# ----------------------------------------------------------------------
# 1. 参数解析
# ----------------------------------------------------------------------


def test_parse_pick_args_accepts_index_time_and_card_in_any_order():
    """`/vh pick 3 time=2 card=ABCD1234` 解析出序号、时段序号与券码。"""
    args, error = parse_pick_args("3 time=2 card=ABCD1234")
    assert error == ""
    assert (args.index, args.play_time_index, args.card_code) == (3, 2, "ABCD1234")

    # 键名顺序无关
    args, error = parse_pick_args("/vh pick 3 card=ABCD1234 time=2")
    assert error == ""
    assert (args.index, args.play_time_index, args.card_code) == (3, 2, "ABCD1234")

    # 单独使用
    assert parse_pick_args("1")[0].play_time_index is None
    assert parse_pick_args("2 card=abc")[0].card_code == "ABC"
    assert parse_pick_args("vh pick 1 time=3")[0].play_time_index == 3


@pytest.mark.parametrize("text", ["abc", "0", "6", "-1", "3.5", "", "time=2"])
def test_parse_pick_args_rejects_bad_index(text):
    """非 1-5 整数序号的报错文案逐字。"""
    args, error = parse_pick_args(text)
    assert args is None
    assert error == "序号必须是 1-5 之间的整数。"


# ----------------------------------------------------------------------
# 2/3. 渲染格式
# ----------------------------------------------------------------------


def test_duration_and_platform_formatting():
    """时长 mm:ss 补零，音源中文名逐字。"""
    assert format_duration(215) == "03:35"
    assert format_duration(5) == "00:05"
    assert format_duration(None) == "未知时长"
    assert format_platform("netease") == "网易云音乐"
    assert format_platform("tencent") == "QQ音乐"
    assert format_platform("migu") == "咪咕音乐"
    assert format_platform("bilibili") == "哔哩哔哩"


def test_render_song_list_is_verbatim():
    """曲目列表逐字：标题行、编号行、结尾提示行。"""
    text = render_song_list("告白气球", "netease", [
        SongCandidate(index=1, title="告白气球", artist="周杰伦", duration_seconds=215),
        SongCandidate(index=2, title="无时长", artist="某人", duration_seconds=None),
    ])
    assert text == (
        "点歌搜索：告白气球（音源：网易云音乐）\n"
        "1. 告白气球 - 周杰伦（03:35）\n"
        "2. 无时长 - 某人（未知时长）\n"
        "回复「/vh pick 序号」完成点歌。"
    )


def test_render_play_times_is_verbatim():
    """播出时段列表逐字。"""
    text = render_play_times([
        PlayTime(id=1, name="午间广播", start_time="12:00", end_time="12:30"),
        PlayTime(id=2, name="晚间广播", start_time="18:00", end_time="18:30"),
    ])
    assert text == (
        "可选播出时段：\n"
        "1. 午间广播（12:00-12:30）\n"
        "2. 晚间广播（18:00-18:30）\n"
        "回复「/vh pick 序号 time=时段序号」选择时段。"
    )


def test_no_result_text_is_verbatim():
    """无结果文案逐字，且不写入待选会话。"""
    async def run():
        client = _RecordingClient(search=_search_outcome(candidates=[]))
        service = SongService(_config("http://stub.invalid"), client=client)
        assert await service.song(UMO, "", "告白气球") == "没有找到「告白气球」的歌曲，换个关键词试试。"
        assert client.search_calls[0]["keyword"] == "告白气球"
        # 无候选时不应留下待选状态
        assert await service.pick(UMO, "", "1") == TEXT_NO_SESSION

    asyncio.run(run())


# ----------------------------------------------------------------------
# 4-7. 服务状态机
# ----------------------------------------------------------------------


def test_group_chat_is_refused_for_song_and_pick():
    """群聊一律拒绝，且不发起任何上游请求。"""
    async def run():
        client = _RecordingClient(search=_search_outcome(), request=_ok_song_request())
        service = SongService(_config("http://stub.invalid"), client=client)
        assert await service.song(UMO, GROUP, "告白气球") == TEXT_GROUP_ONLY
        assert await service.pick(UMO, GROUP, "1") == TEXT_GROUP_ONLY
        assert await service.song_time(UMO, GROUP) == TEXT_GROUP_ONLY
        assert client.search_calls == [] and client.request_calls == []
        assert client.play_time_calls == 0

    asyncio.run(run())


def test_session_expires_after_ten_minutes():
    """时钟推进超过 600 秒后投稿提示重新搜索。"""
    async def run():
        clock = [1000.0]
        client = _RecordingClient(search=_search_outcome(), request=_ok_song_request())
        service = SongService(_config("http://stub.invalid"), client=client, now=lambda: clock[0])

        await service.song(UMO, "", "告白气球")
        clock[0] += 599
        assert await service.pick(UMO, "", "1") == "点歌成功：告白气球 - 周杰伦"

        clock[0] += 601  # 距上次搜索超过 600 秒
        assert await service.pick(UMO, "", "1") == TEXT_NO_SESSION
        # 过期后不再发起投稿请求
        assert len(client.request_calls) == 1

    asyncio.run(run())


def test_session_ttl_constant_is_ten_minutes():
    assert SONG_SESSION_TTL_SECONDS == 600
    assert PICK_COOLDOWN_SECONDS == 5
    assert DEFAULT_RESULT_COUNT == 5 and MAX_RESULT_COUNT == 5


def test_missing_session_replies_with_search_hint():
    """未搜索直接投稿：提示先搜索。"""
    async def run():
        client = _RecordingClient(request=_ok_song_request())
        service = SongService(_config("http://stub.invalid"), client=client)
        assert await service.pick(UMO, "", "1") == TEXT_NO_SESSION
        assert client.request_calls == []

    asyncio.run(run())


def test_second_pick_within_five_seconds_is_cooled_down():
    """同一会话 5 秒内第二次投稿被冷却拦截。"""
    async def run():
        clock = [500.0]
        client = _RecordingClient(search=_search_outcome(), request=_ok_song_request())
        service = SongService(_config("http://stub.invalid"), client=client, now=lambda: clock[0])

        await service.song(UMO, "", "告白气球")
        assert await service.pick(UMO, "", "1") == "点歌成功：告白气球 - 周杰伦"
        clock[0] += 4.9
        assert await service.pick(UMO, "", "2") == TEXT_COOLDOWN
        assert len(client.request_calls) == 1

        # 冷却只针对本会话；另一会话不受影响
        assert await service.pick("aiocqhttp:FriendMessage:user2", "", "1") == TEXT_NO_SESSION

        clock[0] += 0.2  # 距上次投稿正好 5.1 秒
        assert await service.pick(UMO, "", "2") == "点歌成功：告白气球 - 周杰伦"
        assert len(client.request_calls) == 2

    asyncio.run(run())


def test_unconfigured_site_url_replies_verbatim():
    """未配置站点地址时的文案逐字，且不发请求。"""
    async def run():
        client = _RecordingClient(search=_search_outcome(), request=_ok_song_request())
        service = SongService(_config(""), client=client)
        assert await service.song(UMO, "", "告白气球") == TEXT_NO_BASE_URL
        assert await service.pick(UMO, "", "1") == TEXT_NO_BASE_URL
        assert await service.song_time(UMO, "") == TEXT_NO_BASE_URL
        assert client.search_calls == [] and client.request_calls == []

    asyncio.run(run())


def test_play_time_listing_when_disabled_or_empty():
    """未开放播出时段选择时逐字回复固定文案。"""
    async def run():
        disabled = _RecordingClient(play_times=types.SimpleNamespace(
            ok=True, message="", enabled=False, play_times=[]))
        service = SongService(_config("http://stub.invalid"), client=disabled)
        assert await service.song_time(UMO, "") == TEXT_NO_PLAY_TIME
        assert await service.song(UMO, "", "time") == TEXT_NO_PLAY_TIME

        empty = _RecordingClient(play_times=types.SimpleNamespace(
            ok=True, message="", enabled=True, play_times=[]))
        service = SongService(_config("http://stub.invalid"), client=empty)
        assert await service.song_time(UMO, "") == TEXT_NO_PLAY_TIME

    asyncio.run(run())


# ----------------------------------------------------------------------
# 8/9. HTTP 客户端映射
# ----------------------------------------------------------------------


def test_search_maps_response_and_sends_token_and_body():
    """搜索请求体与响应映射，令牌只出现在约定请求头里。"""
    stub = _StubSongApi().start()
    try:
        client = VoiceHubSongClient(_config(stub.base_url))
        outcome = asyncio.run(client.search(UMO, "  告白气球  ", platform="netease"))

        assert stub.search_bodies == [{
            "umo": UMO, "keyword": "告白气球", "platform": "netease", "page": 1,
        }]
        sent = {k.lower(): v for k, v in stub.headers[0].items()}
        assert sent.get("x-voicehub-token") == "t0ken"

        assert outcome.ok is True
        assert outcome.session_token == "sealed-ticket-1"
        assert outcome.keyword == "告白气球" and outcome.platform == "netease"
        assert [(c.index, c.title, c.artist, c.duration_seconds) for c in outcome.candidates] == [
            (1, "告白气球", "周杰伦", 215),
            (2, "晴天", "周杰伦", 269),
            (3, "无时长", "某人", None),
        ]
    finally:
        stub.stop()


def test_search_renders_full_flow_through_service():
    """搜索→列表渲染→投稿的端到端映射（走真实 HTTP 客户端）。"""
    stub = _StubSongApi().start()
    try:
        service = SongService(_config(stub.base_url))
        listing = asyncio.run(service.song(UMO, "", "告白气球"))
        assert listing == (
            "点歌搜索：告白气球（音源：网易云音乐）\n"
            "1. 告白气球 - 周杰伦（03:35）\n"
            "2. 晴天 - 周杰伦（04:29）\n"
            "3. 无时长 - 某人（未知时长）\n"
            "回复「/vh pick 序号」完成点歌。"
        )
        assert asyncio.run(service.pick(UMO, "", "1")) == "点歌成功：告白气球 - 周杰伦"
        assert stub.request_bodies == [{
            "umo": UMO, "sessionToken": "sealed-ticket-1", "index": 1,
        }]
    finally:
        stub.stop()


def test_request_body_carries_uppercased_card_time_and_note():
    """投稿请求体：券码大写、preferredPlayTimeId 取时段、submissionNote 取 note。"""
    stub = _StubSongApi().start()
    try:
        client = VoiceHubSongClient(_config(stub.base_url))
        outcome = asyncio.run(client.request_song(
            UMO, "sealed-ticket-1", 2, play_time_id=2,
            card_code="abcd1234", note="送给三班的同学",
        ))
        assert outcome.ok is True
        assert stub.request_bodies == [{
            "umo": UMO, "sessionToken": "sealed-ticket-1", "index": 2,
            "playTimeId": 2, "cardCode": "ABCD1234", "submissionNote": "送给三班的同学",
        }]
        assert outcome.message == "点歌成功：告白气球 - 周杰伦"

        # 可选字段缺省时不出现，避免把 null 传给站点
        asyncio.run(client.request_song(UMO, "sealed-ticket-1", 1))
        assert stub.request_bodies[1] == {
            "umo": UMO, "sessionToken": "sealed-ticket-1", "index": 1,
        }
    finally:
        stub.stop()


def test_pick_maps_play_time_index_to_id_and_card_to_body():
    """`time=2` 映射为第二个时段的真实 id；券码大写后进入请求体。"""
    stub = _StubSongApi().start()
    try:
        service = SongService(_config(stub.base_url))
        asyncio.run(service.song(UMO, "", "告白气球"))
        assert asyncio.run(service.pick(UMO, "", "2 time=2 card=abcd1234")) == "点歌成功：告白气球 - 周杰伦"
        body = stub.request_bodies[0]
        assert (body["index"], body["playTimeId"], body["cardCode"]) == (2, 2, "ABCD1234")
    finally:
        stub.stop()


def test_play_time_index_uses_real_ids_not_positions():
    """时段 id 与展示序号不一致时，必须换算成真实 id。"""
    stub = _StubSongApi().start()
    stub.play_times_response = {"enabled": True, "playTimes": [
        {"id": 7, "name": "午间广播", "startTime": "12:00", "endTime": "12:30", "enabled": True},
        {"id": 9, "name": "晚间广播", "startTime": "18:00", "endTime": "18:30", "enabled": True},
    ]}
    try:
        service = SongService(_config(stub.base_url))
        asyncio.run(service.song(UMO, "", "告白气球"))
        asyncio.run(service.pick(UMO, "", "1 time=2"))
        assert stub.request_bodies[0]["playTimeId"] == 9
    finally:
        stub.stop()


def test_play_times_are_fetched_and_cached_in_session():
    """时段列表按 /vh song time 拉取并缓存，供后续 time= 换算。"""
    stub = _StubSongApi().start()
    try:
        service = SongService(_config(stub.base_url))
        assert asyncio.run(service.song_time(UMO, "")) == (
            "可选播出时段：\n"
            "1. 午间广播（12:00-12:30）\n"
            "2. 晚间广播（18:00-18:30）\n"
            "回复「/vh pick 序号 time=时段序号」选择时段。"
        )
        asyncio.run(service.song(UMO, "", "告白气球"))
        asyncio.run(service.pick(UMO, "", "1 time=1"))
        assert stub.request_bodies[0]["playTimeId"] == 1
    finally:
        stub.stop()


def test_http_error_message_prefers_message_then_status_message():
    """4xx/5xx：优先 message，其次 statusMessage，都没有时用兜底文案。"""
    stub = _StubSongApi().start()
    try:
        client = VoiceHubSongClient(_config(stub.base_url))

        stub.search_status = 403
        stub.search_response = {"message": "该会话未绑定 VoiceHub 账号"}
        assert asyncio.run(client.search(UMO, "告白气球")).message == "该会话未绑定 VoiceHub 账号"

        stub.search_response = {"statusMessage": "ASTRBOT_UMO_UNBOUND"}
        assert asyncio.run(client.search(UMO, "告白气球")).message == "ASTRBOT_UMO_UNBOUND"

        stub.search_response = {"error": "boom"}
        assert asyncio.run(client.search(UMO, "告白气球")).message == TEXT_FAILED

        stub.request_status = 400
        stub.request_response = {"message": "序号超出可点歌曲范围"}
        outcome = asyncio.run(client.request_song(UMO, "sealed-ticket-1", 4))
        assert outcome.ok is False and outcome.message == "序号超出可点歌曲范围"
    finally:
        stub.stop()


def test_http_error_text_reaches_user_verbatim():
    """站点错误文案原样回给用户（不包装、不带状态码）。"""
    stub = _StubSongApi().start()
    try:
        service = SongService(_config(stub.base_url))
        stub.search_status = 403
        stub.search_response = {"message": "该会话未绑定 VoiceHub 账号"}
        assert asyncio.run(service.song(UMO, "", "告白气球")) == "该会话未绑定 VoiceHub 账号"

        stub.search_status = 200
        asyncio.run(service.song(UMO, "", "告白气球"))
        stub.request_status = 400
        stub.request_response = {"message": "点歌券无效或已使用"}
        assert asyncio.run(service.pick(UMO, "", "1")) == "点歌券无效或已使用"
    finally:
        stub.stop()


def test_network_failure_replies_verbatim_without_leaking_details():
    """网络异常时用固定文案，绝不外泄地址、令牌或堆栈。"""
    async def run():
        # 端口 1 上没有任何服务：连接立即失败
        config = _config("http://127.0.0.1:1")
        client = VoiceHubSongClient(config)
        outcome = await client.search(UMO, "告白气球")
        assert outcome.ok is False
        assert outcome.message == TEXT_NETWORK
        assert "t0ken" not in outcome.message and "127.0.0.1" not in outcome.message

        service = SongService(config)
        assert await service.song(UMO, "", "告白气球") == TEXT_NETWORK

        # 超时同样收敛为同一文案
        class SlowClient(VoiceHubSongClient):
            async def search(self, umo, keyword, platform="netease", page=1):  # noqa: ARG002
                raise asyncio.TimeoutError

        service = SongService(config, client=SlowClient(config))
        assert await service.song(UMO, "", "告白气球") == TEXT_NETWORK

    asyncio.run(run())


def test_redirect_is_refused_and_no_token_leaks():
    """3xx 一律拒绝，绝不把令牌转发到重定向目的地。"""
    stub = _StubSongApi().start()
    stub.search_status = 307
    stub.search_response = {}
    try:
        client = VoiceHubSongClient(_config(stub.base_url))
        outcome = asyncio.run(client.search(UMO, "告白气球"))
        assert outcome.ok is False
        assert outcome.message == TEXT_FAILED
    finally:
        stub.stop()


# ----------------------------------------------------------------------
# 配置项
# ----------------------------------------------------------------------


def test_song_config_defaults_and_clamping():
    """song_enabled 默认开启；song_result_count 默认 5 且上限 5。"""
    config = VoiceHubConfig.from_mapping({})
    assert config.song_enabled is True
    assert config.song_result_count == 5

    assert VoiceHubConfig.from_mapping({"song_enabled": "false"}).song_enabled is False
    assert VoiceHubConfig.from_mapping({"song_enabled": "on"}).song_enabled is True
    assert VoiceHubConfig.from_mapping({"song_result_count": 9}).song_result_count == 5
    assert VoiceHubConfig.from_mapping({"song_result_count": 0}).song_result_count == 1
    assert VoiceHubConfig.from_mapping({"song_result_count": "abc"}).song_result_count == 5


# ----------------------------------------------------------------------
# 指令接线（不启动真实 AstrBot，仅验证事件参数传递）
# ----------------------------------------------------------------------


class FakeEvent:
    """最小事件桩：只提供指令用到的取值。"""

    def __init__(self, group=""):
        self.unified_msg_origin = UMO
        self._group = group

    def get_group_id(self):
        return self._group

    def get_platform_name(self):
        return "aiocqhttp"

    def plain_result(self, text):
        return text


async def _collect(generator):
    return [item async for item in generator]


def test_commands_pass_group_id_through_to_refusal(plugin):
    """群聊中 /vh song 与 /vh pick 都逐字拒绝。"""
    async def run():
        p = plugin.VoiceHubPlugin(types.SimpleNamespace(), {
            "webhook_token": "secret", "voicehub_base_url": "http://stub.invalid",
        })
        event = FakeEvent(group=GROUP)
        assert (await _collect(p.vh_song(event, "告白气球")))[0] == TEXT_GROUP_ONLY
        assert (await _collect(p.vh_pick(event, "1")))[0] == TEXT_GROUP_ONLY

    asyncio.run(run())


def test_song_command_reports_unconfigured_site(plugin):
    """私聊中未配置站点地址时的文案逐字。"""
    async def run():
        p = plugin.VoiceHubPlugin(types.SimpleNamespace(), {"webhook_token": "secret"})
        assert (await _collect(p.vh_song(FakeEvent(), "告白气球")))[0] == TEXT_NO_BASE_URL
        assert (await _collect(p.vh_pick(FakeEvent(), "1")))[0] == TEXT_NO_BASE_URL
        assert (await _collect(p.vh_song(FakeEvent(), "time")))[0] == TEXT_NO_BASE_URL

    asyncio.run(run())
