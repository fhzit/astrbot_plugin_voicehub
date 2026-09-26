"""Contract and live HTTP regression tests without a running AstrBot instance."""
import asyncio
import importlib
import logging
import sys
import types
from pathlib import Path

import aiohttp
import pytest


@pytest.fixture(scope="module")
def plugin():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root.parent))
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("test")
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    def group(name, **kwargs):
        def decorate(fn):
            fn.command = lambda sub, **options: lambda method: method
            return fn
        return decorate
    event.filter = types.SimpleNamespace(command_group=group)
    event.MessageChain = type("MessageChain", (), {"message": lambda self, text: text})
    star = types.ModuleType("astrbot.api.star")
    star.Context = type("Context", (), {})
    star.Star = type("Star", (), {"__init__": lambda self, context: setattr(self, "context", context)})
    star.register = lambda *args: lambda cls: cls
    original = {key: sys.modules.get(key) for key in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star")}
    sys.modules["astrbot"] = types.ModuleType("astrbot")
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star
    command = types.ModuleType("astrbot.core.star.filter.command")
    command.GreedyStr = type("GreedyStr", (str,), {})
    sys.modules["astrbot.core.star.filter.command"] = command
    module = importlib.import_module("astrbot_plugin_voicehub.main")
    yield module
    for key, value in original.items():
        if value is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = value
    sys.modules.pop("astrbot.core.star.filter.command", None)


class FakeEvent:
    def __init__(self, message="vh bind", group=""):
        self.message_str = message
        self.unified_msg_origin = "aiocqhttp:FriendMessage:user1"
        self.group = group

    def get_group_id(self):
        return self.group

    def get_platform_name(self):
        return "aiocqhttp"

    def plain_result(self, text):
        return text


@pytest.mark.parametrize("message", ["vh bind", "/vh bind", "vh bind   "])
def test_bind_without_code_never_calls_voicehub(plugin, message):
    async def run():
        p = plugin.VoiceHubPlugin(types.SimpleNamespace(), {"webhook_token": "secret"})
        async def forbidden(*args):
            raise AssertionError("empty binding code sent upstream")
        p.voicehub_client.verify_binding_code = forbidden
        result = [item async for item in p.vh_bind(FakeEvent(message), "")]
        assert "用法" in result[0]
    asyncio.run(run())


def test_bind_passes_only_one_code(plugin):
    async def run():
        p = plugin.VoiceHubPlugin(types.SimpleNamespace(), {"webhook_token": "secret"})
        captured = []
        async def verify(code, umo, platform):
            captured.append((code, umo, platform))
            return types.SimpleNamespace(ok=True, username="u")
        p.voicehub_client.verify_binding_code = verify
        assert "绑定成功" in (await collect(p.vh_bind(FakeEvent("vh bind 0123456789abcdef01234567"), "0123456789abcdef01234567")))[0]
        assert captured == [("0123456789abcdef01234567", "aiocqhttp:FriendMessage:user1", "aiocqhttp")]
        assert "用法" in (await collect(p.vh_bind(FakeEvent("vh bind 0123456789abcdef01234567 DEADBEEF"), "0123456789abcdef01234567 DEADBEEF")))[0]
    asyncio.run(run())


async def collect(generator):
    return [item async for item in generator]


@pytest.fixture
def server(plugin):
    config = plugin.VoiceHubConfig.from_mapping({"webhook_token": "secret", "listen_host": "127.0.0.1", "listen_port": 0, "allowed_ips": "127.0.0.1", "group_umos": "aiocqhttp:GroupMessage:room"})
    class Sender:
        context = types.SimpleNamespace(platform_manager=types.SimpleNamespace(platform_insts=[]))
        def __init__(self):
            self.calls = []
        def group_targets(self):
            return config.group_umos
        async def push_text(self, targets, title, content, url):
            self.calls.append((targets, title, content, url))
            return types.SimpleNamespace(sent=len(targets), failed=[])
    sender = Sender()
    class Verifier:
        async def verify_private_targets(self, umos):
            return umos == ["aiocqhttp:FriendMessage:user1"]
        async def verify_group_targets(self, umos):
            # 群授权由 VoiceHub 判定：桩里只放行配置中的那个群。
            return umos == ["aiocqhttp:GroupMessage:room"]
    return plugin.VoiceHubHttpServer(config, sender, Verifier(), logging.getLogger("test")), sender


def test_live_auth_and_spoofed_forwarded_header(server):
    async def run():
        service, sender = server
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/voicehub/push"
        try:
            async with aiohttp.ClientSession() as client:
                body = {"content": "hello", "targets": {"umo": ["aiocqhttp:FriendMessage:user1"]}}
                response = await client.post(url, json=body, headers={"X-Forwarded-For": "127.0.0.1"})
                assert response.status == 401
                service.config.allowed_ips = ["203.0.113.1"]
                response = await client.post(url, json=body, headers={"X-VoiceHub-Token": "secret", "X-Forwarded-For": "203.0.113.1"})
                assert response.status == 403
                service.config.allowed_ips = ["127.0.0.1"]
                response = await client.post(url, json=body, headers={"X-VoiceHub-Token": "secret"})
                assert response.status == 200
                assert sender.calls[0][0] == ["aiocqhttp:FriendMessage:user1"]
        finally:
            await service.stop()
    asyncio.run(run())


def test_constant_time_token_compare(plugin, server, monkeypatch):
    from astrbot_plugin_voicehub.lib import server as server_module
    calls = []
    def compare(left, right):
        calls.append((left, right))
        return False
    monkeypatch.setattr(server_module.hmac, "compare_digest", compare)
    service, _ = server
    request = types.SimpleNamespace(headers={"X-VoiceHub-Token": "wrong"}, remote="127.0.0.1")
    assert service._guard(request).status == 401
    assert calls == [("wrong", "secret")]


@pytest.mark.parametrize("targets", [
    {"user_ids": ["1"]}, {"group": "false"}, {"group": 1}, {"umo": [42]},
    {"umo": {"evil": "obj"}}, {"umo": ["bad"]},
    {"umo": ["aiocqhttp:FriendMessage:"]}, {"umo": ["aiocqhttp:OtherMessage:id"]},
    {"umo": ["aiocqhttp:FriendMessage:user"], "umos": ["aiocqhttp:FriendMessage:other"]},
])
def test_targets_reject_invalid_or_unapproved(plugin, server, targets):
    """形态/字段非法的 targets 属于参数错误：参数校验阶段即拒绝。

    不在本机 group_umos 里的群属于「未授权」，走授权阶段的 403，
    由 test_group_target_outside_local_allowlist_is_forbidden 覆盖。
    """
    service, _ = server
    assert service._resolve_targets(targets)[1]


@pytest.mark.parametrize("malformed", [
    "myroom",                       # 缺少冒号：曾触发 IndexError -> HTTP 500
    "aiocqhttp",                    # 只有一段
    "aiocqhttp:Group",              # 只有两段
    ":GroupMessage:room",           # 平台标识为空
    "aiocqhttp:Bogus:room",         # 未知会话类型
    "aiocqhttp:GroupMessage:",      # 会话 ID 为空
    "aiocqhttp:GroupMessage:a b",   # 含空白
    "x" * 600 + ":GroupMessage:r",  # 超长
])
def test_malformed_group_umos_are_dropped_at_load(plugin, malformed):
    """畸形 group_umos 在加载时即被丢弃，绝不进入推送链路（曾导致未捕获 500）。"""
    config = plugin.VoiceHubConfig.from_mapping({"group_umos": malformed})
    assert config.group_umos == [], f"{malformed!r} 不应成为群广播目标"
    assert plugin.dropped_umo_values(malformed, "GroupMessage") == [malformed]


def test_valid_group_umo_is_kept_and_foreign_types_dropped(plugin):
    config = plugin.VoiceHubConfig.from_mapping({
        "group_umos": "aiocqhttp:GroupMessage:room, aiocqhttp:FriendMessage:user, plainroom",
    })
    assert config.group_umos == ["aiocqhttp:GroupMessage:room"]


def test_initialize_warns_about_dropped_groups_but_still_starts(plugin):
    """畸形 group_umos 不得阻止服务启动，且合法项仍然生效。"""
    async def run():
        instance = plugin.VoiceHubPlugin(types.SimpleNamespace(), {
            "webhook_token": "secret", "listen_host": "127.0.0.1", "listen_port": 0,
            "group_umos": "myroom, aiocqhttp:GroupMessage:room",
        })
        await instance.initialize()
        try:
            assert instance.http_server is not None
            assert instance.plugin_config.group_umos == ["aiocqhttp:GroupMessage:room"]
        finally:
            await instance.terminate()
    asyncio.run(run())


def test_malformed_target_from_request_is_rejected_not_crashing(plugin):
    """请求体里出现畸形 UMO 必须返回 400，而不是抛异常。"""
    config = plugin.VoiceHubConfig.from_mapping({"webhook_token": "secret", "group_umos": "aiocqhttp:GroupMessage:room"})
    sender = types.SimpleNamespace(group_targets=lambda: config.group_umos)
    service = plugin.VoiceHubHttpServer(config, sender, None, logging.getLogger("test"))
    for malformed in ["myroom", "aiocqhttp", "aiocqhttp:Group", "aiocqhttp:Bogus:room"]:
        _, error = service._resolve_targets({"umo": [malformed]})
        assert error, f"{malformed!r} 应被拒绝"


def test_malformed_group_umo_does_not_500_on_live_push(plugin, server):
    """回归：管理员把 group_umos 填成无冒号值时，群广播不得变成 500。"""
    async def run():
        service, sender = server
        # 模拟配置被写坏后的运行时状态（绕过加载期过滤）。
        service.config.group_umos = ["myroom"]
        service.push_service.group_targets = lambda: ["myroom"]
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(
                    f"http://127.0.0.1:{port}/voicehub/push",
                    json={"content": "hello", "targets": {"group": True}},
                    headers={"X-VoiceHub-Token": "secret"},
                ) as response:
                    assert response.status in (200, 400), f"不得为 500，实际 {response.status}"
        finally:
            await service.stop()
    asyncio.run(run())


def test_targets_accept_private_and_configured_group(server):
    service, _ = server
    assert service._resolve_targets({"umo": ["aiocqhttp:FriendMessage:user", "aiocqhttp:FriendMessage:user"], "group": True}) == (["aiocqhttp:GroupMessage:room", "aiocqhttp:FriendMessage:user"], "")

def test_missing_targets_never_broadcasts(server):
    service, _ = server
    assert service._resolve_targets(None)[1]

def test_explicit_group_only_sends_after_voicehub_authorization(server):
    """群推送必须先由 VoiceHub 确认授权，确认通过后正常投递。"""
    async def run():
        service, sender = server
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(f"http://127.0.0.1:{port}/voicehub/push", json={"content": "hello", "targets": {"group": True}}, headers={"X-VoiceHub-Token": "secret"}) as response:
                    assert response.status == 200
            assert sender.calls == [(["aiocqhttp:GroupMessage:room"], "", "hello", None)]
        finally:
            await service.stop()
    asyncio.run(run())


def test_group_target_rejected_when_voicehub_does_not_authorize(server):
    """VoiceHub 未授权的群目标必须 403，且一条消息都不发。"""
    async def run():
        service, sender = server
        service.push_service.group_targets = lambda: ["aiocqhttp:GroupMessage:other"]
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(f"http://127.0.0.1:{port}/voicehub/push", json={"content": "hello", "targets": {"group": True}}, headers={"X-VoiceHub-Token": "secret"}) as response:
                    assert response.status == 403
            assert sender.calls == []
        finally:
            await service.stop()
    asyncio.run(run())


def test_group_target_outside_local_allowlist_is_forbidden(server):
    """本机 group_umos 非空时，不在其中的群目标按授权失败拒绝（403，不是 400）。"""
    async def run():
        service, sender = server
        service.push_service.group_targets = lambda: ["aiocqhttp:GroupMessage:room"]
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(
                    f"http://127.0.0.1:{port}/voicehub/push",
                    json={"content": "hello", "targets": {"umo": "aiocqhttp:GroupMessage:elsewhere"}},
                    headers={"X-VoiceHub-Token": "secret"},
                ) as response:
                    assert response.status == 403
            assert sender.calls == []
        finally:
            await service.stop()
    asyncio.run(run())


def test_group_target_without_verifier_fails_closed(server):
    """未注入 VoiceHub 客户端时群推送一律拒绝，不得越权发出。"""
    async def run():
        service, sender = server
        service.voicehub_client = None
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(f"http://127.0.0.1:{port}/voicehub/push", json={"content": "hello", "targets": {"group": True}}, headers={"X-VoiceHub-Token": "secret"}) as response:
                    assert response.status == 403
            assert sender.calls == []
        finally:
            await service.stop()
    asyncio.run(run())

def test_private_target_without_verifier_fails_closed(server):
    async def run():
        service, sender = server
        service.voicehub_client = None
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(f"http://127.0.0.1:{port}/voicehub/push", json={"content": "hello", "targets": {"umo": "aiocqhttp:FriendMessage:user1"}}, headers={"X-VoiceHub-Token": "secret"}) as response:
                    assert response.status == 403
            assert sender.calls == []
        finally:
            await service.stop()
    asyncio.run(run())

def test_unverified_private_target_never_sends(server):
    async def run():
        service, sender = server
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(f"http://127.0.0.1:{port}/voicehub/push", json={"content": "hello", "targets": {"umo": ["aiocqhttp:FriendMessage:attacker"], "group": True}}, headers={"X-VoiceHub-Token": "secret"}) as response:
                    assert response.status == 403
            assert not sender.calls
        finally:
            await service.stop()
    asyncio.run(run())

def test_live_voicehub_payload_contract(server):
    async def run():
        service, sender = server
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(
                    f"http://127.0.0.1:{port}/voicehub/push",
                    json={"title": "VoiceHub", "content": "通知", "targets": {"umo": ["aiocqhttp:FriendMessage:user1"], "group": True}},
                    headers={"X-VoiceHub-Token": "secret"},
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["sent"] == 2
            assert sender.calls[-1][0] == ["aiocqhttp:GroupMessage:room", "aiocqhttp:FriendMessage:user1"]
        finally:
            await service.stop()
    asyncio.run(run())


def test_failed_start_releases_runner_and_port(server):
    async def run():
        first, _ = server
        await first.start()
        port = first._site._server.sockets[0].getsockname()[1]
        second = type(first)(type(first.config)(**{**vars(first.config), "listen_port": port}), first.push_service, None, logging.getLogger("test"))
        try:
            with pytest.raises(OSError):
                await second.start()
            assert second._runner is None
        finally:
            await second.stop()
            await first.stop()
        await second.start()
        await second.stop()
    asyncio.run(run())
