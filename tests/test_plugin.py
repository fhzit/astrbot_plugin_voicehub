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
    def group(name):
        def decorate(fn):
            fn.command = lambda sub: lambda method: method
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
        self.unified_msg_origin = "bot:FriendMessage:user1"
        self.group = group

    def get_group_id(self):
        return self.group

    def get_platform_name(self):
        return "bot"

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
        assert "绑定成功" in (await collect(p.vh_bind(FakeEvent("vh bind ABC"), "ABC")))[0]
        assert captured == [("ABC", "bot:FriendMessage:user1", "bot")]
        assert "用法" in (await collect(p.vh_bind(FakeEvent("vh bind ABC DEF"), "ABC DEF")))[0]
    asyncio.run(run())


async def collect(generator):
    return [item async for item in generator]


@pytest.fixture
def server(plugin):
    config = plugin.VoiceHubConfig.from_mapping({"webhook_token": "secret", "listen_host": "127.0.0.1", "listen_port": 0, "allowed_ips": "127.0.0.1", "group_umos": "bot:GroupMessage:room"})
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
    return plugin.VoiceHubHttpServer(config, sender, None, logging.getLogger("test")), sender


def test_live_auth_and_spoofed_forwarded_header(server):
    async def run():
        service, sender = server
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/voicehub/push"
        try:
            async with aiohttp.ClientSession() as client:
                body = {"content": "hello", "targets": {"umo": ["bot:FriendMessage:user1"]}}
                response = await client.post(url, json=body, headers={"X-Forwarded-For": "127.0.0.1"})
                assert response.status == 401
                service.config.allowed_ips = ["203.0.113.1"]
                response = await client.post(url, json=body, headers={"X-VoiceHub-Token": "secret", "X-Forwarded-For": "203.0.113.1"})
                assert response.status == 403
                service.config.allowed_ips = ["127.0.0.1"]
                response = await client.post(url, json=body, headers={"X-VoiceHub-Token": "secret"})
                assert response.status == 200
                assert sender.calls[0][0] == ["bot:FriendMessage:user1"]
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
    {"umo": {"evil": "obj"}}, {"umo": ["bad"]}, {"umo": ["bot:GroupMessage:unapproved"]},
    {"umo": ["bot:FriendMessage:"]}, {"umo": ["bot:OtherMessage:id"]},
    {"umo": ["bot:FriendMessage:user"], "umos": ["bot:FriendMessage:other"]},
])
def test_targets_reject_invalid_or_unapproved(plugin, server, targets):
    service, _ = server
    assert service._resolve_targets(targets)[1]


def test_targets_accept_private_and_configured_group(server):
    service, _ = server
    assert service._resolve_targets({"umo": ["bot:FriendMessage:user", "bot:FriendMessage:user"], "group": True}) == (["bot:GroupMessage:room", "bot:FriendMessage:user"], "")

def test_live_voicehub_payload_contract(server):
    async def run():
        service, sender = server
        await service.start()
        port = service._site._server.sockets[0].getsockname()[1]
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(
                    f"http://127.0.0.1:{port}/voicehub/push",
                    json={"title": "VoiceHub", "content": "通知", "targets": {"umo": ["bot:FriendMessage:user1"], "group": True}},
                    headers={"X-VoiceHub-Token": "secret"},
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["sent"] == 2
            assert sender.calls[-1][0] == ["bot:GroupMessage:room", "bot:FriendMessage:user1"]
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
