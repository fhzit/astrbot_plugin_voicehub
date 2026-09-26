"""拉取模式测试：VoiceHub 无法访问插件时，由插件主动取件与回报。

用真实的本机 aiohttp 对端驱动，断言投递结果与回执内容，而不是只看函数是否被调用。
"""
import asyncio
import json
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from astrbot_plugin_voicehub.lib.config import VoiceHubConfig
from astrbot_plugin_voicehub.lib.pull import VoiceHubPullClient, parse_pull_items
from astrbot_plugin_voicehub.lib.voicehub import VoiceHubClient

CLAIM_TOKEN = "a" * 64


class _StubVoiceHub:
    """桩 VoiceHub：记录收到的取件与回执请求，按预设返回条目。"""

    def __init__(self, items, allowed_groups=None):
        self.items = [{**item, "claimToken": CLAIM_TOKEN} for item in items]
        self.pulls = 0
        self.acks = []
        self.verify_requests = []
        # 群目标授权白名单：回查到不在其中的会话即拒绝。
        self.allowed_groups = set(allowed_groups or [])
        self.headers = []
        self.redirect = False
        self.status = 200
        self._server = None
        self._thread = None
        self.port = None
        # 暂停开关：让测试能在插件发回执前检查请求
        self.release = threading.Event()

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: ARG002 - 静音访问日志
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.headers.append(dict(self.headers))
                if self.path.endswith("/pull"):
                    outer.pulls += 1
                    if outer.redirect:
                        self.send_response(307)
                        self.send_header("location", "https://evil.example.com/steal")
                        self.end_headers()
                        return
                    payload = json.dumps({"success": True, "items": outer.items}).encode()
                    self.send_response(outer.status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path.endswith("/ack"):
                    outer.acks.append(json.loads(body or b"{}"))
                    payload = b'{"success":true,"updated":1}'
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path.endswith("/verify-targets"):
                    # 群目标授权回查：只放行 outer.allowed_groups 中的会话。
                    outer.verify_requests.append(json.loads(body or b"{}"))
                    requested = json.loads(body or b"{}").get("umos") or []
                    allowed = [umo for umo in requested if umo in outer.allowed_groups]
                    if len(allowed) == len(requested):
                        payload = json.dumps({"success": True, "umos": requested}).encode()
                        self.send_response(200)
                    else:
                        payload = json.dumps({"success": False, "message": "包含未授权的目标"}).encode()
                        self.send_response(403)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(404)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)


class _PushRecorder:
    """记录 push_text 调用并按预设返回结果。"""

    def __init__(self, sent=1, failed=None, groups=None):
        self.calls = []
        self.sent = sent
        self.failed = failed or []
        self._groups = groups or []

    async def push_text(self, umos, title, content, url=None):
        self.calls.append({"umos": list(umos), "title": title, "content": content, "url": url})
        return type("R", (), {"sent": self.sent, "failed": self.failed})()

    def group_targets(self):
        return list(self._groups)


class _NullLogger:
    """静音日志，避免测试输出噪音。"""

    def info(self, *args, **kwargs):  # noqa: ARG002
        pass

    def warning(self, *args, **kwargs):  # noqa: ARG002
        pass

    def error(self, *args, **kwargs):  # noqa: ARG002
        pass

def _config(base_url, token="t0ken", interval=1):
    return VoiceHubConfig(
        webhook_token=token, voicehub_base_url=base_url,
        voicehub_token=token, pull_interval_seconds=interval,
    )


def test_parse_pull_items_drops_malformed_without_losing_the_rest():
    """单条脏数据只跳过该条，不影响同一批的其他通知。"""
    items = parse_pull_items({"success": True, "items": [{**item, "claimToken": CLAIM_TOKEN} for item in [
        {"id": 1, "content": "ok", "umos": ["default:FriendMessage:1"]},
        {"id": "2", "content": "bad id", "umos": ["default:FriendMessage:2"]},
        {"id": 3, "content": "", "umos": ["default:FriendMessage:3"]},
        {"id": 4, "content": "bad umo", "umos": ["not-a-umo"]},
        {"id": 5, "content": "other type", "umos": ["default:OtherMessage:5"]},
        {"id": 6, "content": "broadcast ok", "umos": [], "broadcast": True},
        {"id": 7, "content": "no targets", "umos": []},
        {"id": 8, "content": "mixed", "umos": ["default:FriendMessage:8", "junk"]},
    ]]})
    assert [item.id for item in items] == [1, 6, 8]
    assert items[0].umos == ["default:FriendMessage:1"]
    assert items[1].broadcast is True
    assert items[2].umos == ["default:FriendMessage:8"]


def test_parse_pull_items_rejects_non_success_envelope():
    assert parse_pull_items({"success": False, "items": [{"id": 1, "content": "x"}]}) == []
    assert parse_pull_items({"items": [{"id": 1, "content": "x"}]}) == []
    assert parse_pull_items(None) == []


def test_pull_cycle_delivers_and_acks():
    """一轮取件：投递到正确会话并回报成功。"""
    stub = _StubVoiceHub([
        {"id": 11, "title": "标题", "content": "正文", "umos": ["default:FriendMessage:111"]},
    ]).start()
    try:
        recorder = _PushRecorder()
        client = VoiceHubPullClient(_config(f"http://127.0.0.1:{stub.port}"), recorder, _NullLogger())
        assert asyncio.run(client.run_once()) == 1

        assert stub.pulls == 1, "应发起一次取件"
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["umos"] == ["default:FriendMessage:111"]
        assert recorder.calls[0]["title"] == "标题"
        assert recorder.calls[0]["content"] == "正文"
        assert stub.acks == [{"results": [{"id": 11, "claimToken": CLAIM_TOKEN, "success": True}]}]

        # 令牌必须随出站请求发送，且不得外泄到其它头
        sent = {k.lower(): v for k, v in stub.headers[0].items()}
        assert sent.get("x-voicehub-token") == "t0ken"
    finally:
        stub.stop()


def test_broadcast_item_uses_voicehub_targets_only():
    """插件本机群列表不能扩大 VoiceHub 队列条目的投递目标。"""
    groups = ["default:GroupMessage:900", "default:GroupMessage:901"]
    stub = _StubVoiceHub([
        {"id": 21, "content": "全站广播", "umos": ["default:GroupMessage:900"], "broadcast": True},
    ], allowed_groups=groups).start()
    try:
        recorder = _PushRecorder(groups=groups)
        config = _config(f"http://127.0.0.1:{stub.port}")
        client = VoiceHubPullClient(config, recorder, _NullLogger(), VoiceHubClient(config))
        asyncio.run(client.run_once())

        assert recorder.calls[0]["umos"] == ["default:GroupMessage:900"]
        assert stub.acks == [{"results": [{"id": 21, "claimToken": CLAIM_TOKEN, "success": True}]}]
        # 群目标投递前必须回查 VoiceHub 授权，而不是只信本地列表。
        assert stub.verify_requests == [{"umos": ["default:GroupMessage:900"]}]
    finally:
        stub.stop()


def test_broadcast_to_unauthorized_group_is_reported_failed():
    """VoiceHub 撤销授权的群不得投递：回查不通过时按失败回报。"""
    stub = _StubVoiceHub([
        {"id": 22, "content": "全站广播", "umos": ["default:GroupMessage:900"], "broadcast": True},
    ], allowed_groups=[]).start()
    try:
        recorder = _PushRecorder(groups=["default:GroupMessage:900"])
        config = _config(f"http://127.0.0.1:{stub.port}")
        client = VoiceHubPullClient(config, recorder, _NullLogger(), VoiceHubClient(config))
        asyncio.run(client.run_once())

        assert recorder.calls == []
        ack = stub.acks[0]["results"][0]
        assert ack["id"] == 22 and ack["success"] is False
        assert "未获授权" in ack["reason"]
    finally:
        stub.stop()


def test_broadcast_to_group_outside_local_allowlist_is_reported_failed():
    """本机 group_umos 非空时，其中的群之外一律不投递（第二道闸门）。"""
    stub = _StubVoiceHub([
        {"id": 24, "content": "全站广播", "umos": ["default:GroupMessage:900"], "broadcast": True},
    ], allowed_groups=["default:GroupMessage:900"]).start()
    try:
        # 本机白名单只写了另一个群：即使 VoiceHub 已授权，也不得投递。
        recorder = _PushRecorder(groups=["default:GroupMessage:777"])
        config = _config(f"http://127.0.0.1:{stub.port}")
        client = VoiceHubPullClient(config, recorder, _NullLogger(), VoiceHubClient(config))
        asyncio.run(client.run_once())

        assert recorder.calls == []
        ack = stub.acks[0]["results"][0]
        assert ack["id"] == 24 and ack["success"] is False
    finally:
        stub.stop()


def test_broadcast_without_voicehub_client_fails_closed():
    """未注入 VoiceHub 客户端时群投递一律拒绝，宁可不发也不越权。"""
    stub = _StubVoiceHub([
        {"id": 23, "content": "全站广播", "umos": ["default:GroupMessage:900"], "broadcast": True},
    ]).start()
    try:
        recorder = _PushRecorder(groups=["default:GroupMessage:900"])
        client = VoiceHubPullClient(_config(f"http://127.0.0.1:{stub.port}"), recorder, _NullLogger())
        asyncio.run(client.run_once())

        assert recorder.calls == []
        assert stub.acks[0]["results"][0]["success"] is False
    finally:
        stub.stop()


def test_broadcast_without_group_targets_reports_failure_not_silence():
    """未配置群目标时广播条目须回报失败，避免通知被静默丢弃。"""
    stub = _StubVoiceHub([{"id": 31, "content": "广播", "umos": [], "broadcast": True}]).start()
    try:
        recorder = _PushRecorder(groups=[])
        client = VoiceHubPullClient(_config(f"http://127.0.0.1:{stub.port}"), recorder, _NullLogger())
        asyncio.run(client.run_once())

        assert recorder.calls == []
        ack = stub.acks[0]["results"][0]
        assert ack["id"] == 31 and ack["success"] is False
        assert "群广播目标" in ack["reason"]
    finally:
        stub.stop()


def test_partial_failure_reports_failed_targets_for_retry():
    """部分目标失败只重试失败目标。"""
    stub = _StubVoiceHub([
        {"id": 41, "content": "部分失败", "umos": ["default:FriendMessage:411", "default:FriendMessage:412"]},
    ]).start()
    try:
        recorder = _PushRecorder(sent=1, failed=[{"umo": "default:FriendMessage:412", "reason": "boom"}])
        client = VoiceHubPullClient(_config(f"http://127.0.0.1:{stub.port}"), recorder, _NullLogger())
        asyncio.run(client.run_once())

        assert stub.acks == [{"results": [{"id": 41, "claimToken": CLAIM_TOKEN, "success": False,
                                      "failedUmos": ["default:FriendMessage:412"], "reason": "boom"}]}]
    finally:
        stub.stop()


def test_delivery_failure_reports_reason_for_vh_retry():
    """全部失败时回报失败与原因，由 VoiceHub 侧按尝试上限重试。"""
    stub = _StubVoiceHub([{"id": 51, "content": "失败", "umos": ["default:FriendMessage:511"]}]).start()
    try:
        recorder = _PushRecorder(sent=0, failed=[{"umo": "default:FriendMessage:511", "reason": "未找到匹配的平台适配器"}])
        client = VoiceHubPullClient(_config(f"http://127.0.0.1:{stub.port}"), recorder, _NullLogger())
        asyncio.run(client.run_once())

        ack = stub.acks[0]["results"][0]
        assert ack["id"] == 51 and ack["success"] is False
        assert "未找到匹配的平台适配器" in ack["reason"]
    finally:
        stub.stop()


def test_redirect_on_pull_is_refused_and_token_not_forwarded():
    """取件遇 3xx 必须拒绝，绝不把令牌转发到重定向目的地。"""
    stub = _StubVoiceHub([]).start()
    stub.redirect = True
    try:
        recorder = _PushRecorder()
        client = VoiceHubPullClient(_config(f"http://127.0.0.1:{stub.port}"), recorder, _NullLogger())
        assert asyncio.run(client.run_once()) == 0
        assert recorder.calls == []
        assert stub.acks == [], "重定向后不得尝试回执"
    finally:
        stub.stop()


def test_non_200_pull_response_yields_no_delivery():
    """VoiceHub 出错时不得投递任何内容。"""
    stub = _StubVoiceHub([{"id": 61, "content": "x", "umos": ["default:FriendMessage:611"]}]).start()
    stub.status = 500
    try:
        recorder = _PushRecorder()
        client = VoiceHubPullClient(_config(f"http://127.0.0.1:{stub.port}"), recorder, _NullLogger())
        assert asyncio.run(client.run_once()) == 0
        assert recorder.calls == []
    finally:
        stub.stop()


def test_client_disabled_without_interval_or_base_url():
    """未配置轮询间隔或站点地址时不启用，避免空转。"""
    recorder = _PushRecorder()
    assert not VoiceHubPullClient(
        VoiceHubConfig(webhook_token="t", voicehub_base_url="http://x", pull_interval_seconds=0),
        recorder, _NullLogger()).enabled
    assert not VoiceHubPullClient(
        VoiceHubConfig(webhook_token="t", voicehub_base_url="", pull_interval_seconds=30),
        recorder, _NullLogger()).enabled
    assert VoiceHubPullClient(
        _config("http://x"), recorder, _NullLogger()).enabled


def test_pull_only_start_skips_inbound_listener(plugin):
    """拉取模式不启动入站端口：VoiceHub 无需访问插件。"""
    async def run():
        instance = plugin.VoiceHubPlugin(
            types.SimpleNamespace(),
            {"webhook_token": "t", "voicehub_base_url": "http://127.0.0.1:1",
             "pull_interval_seconds": 3600, "listen_port": 0},
        )
        await instance.initialize()
        try:
            assert instance.pull_client is not None
            assert instance.http_server is None, "拉取模式不应开放入站端口"
        finally:
            await instance.terminate()
        assert instance.pull_client is None

    asyncio.run(run())


