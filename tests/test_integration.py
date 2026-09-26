"""跨仓端到端契约测试：插件入站服务 + 桩 VoiceHub 服务。

用一个实现了 bind/unbind/verify-targets 的桩 VoiceHub 站点驱动插件的入站推送接口，
验证真实 HTTP 往返（令牌、回查、群目标、重定向拒绝），无需数据库与聊天平台。
"""
import asyncio
import logging
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
from astrbot_plugin_voicehub.lib.config import VoiceHubConfig
from astrbot_plugin_voicehub.lib.server import VoiceHubHttpServer
from astrbot_plugin_voicehub.lib.voicehub import VoiceHubClient

TOKEN = "secret"
VALID_CODE = "0123456789abcdef01234567"
BOUND_UMO = "aiocqhttp:FriendMessage:user1"
GROUP_UMO = "aiocqhttp:GroupMessage:room"


class StubVoiceHub:
    """带令牌校验的桩 VoiceHub：只认可预先登记的私聊绑定。"""

    def __init__(self, bound, redirect_verify=False):
        self.bound = list(bound)
        self.redirect_verify = redirect_verify
        self.requests = []
        self.leaked = []

    def _authorized(self, request):
        return request.headers.get("X-VoiceHub-Token") == TOKEN

    async def bind(self, request):
        body = await request.json()
        self.requests.append(("bind", body))
        if not self._authorized(request):
            return web.json_response({"success": False, "message": "令牌无效"}, status=401)
        if body.get("code") == VALID_CODE:
            return web.json_response({"success": True, "username": "alice"})
        return web.json_response({"success": False, "message": "绑定码无效"}, status=400)

    async def unbind(self, request):
        self.requests.append(("unbind", await request.json()))
        if not self._authorized(request):
            return web.json_response({"success": False, "message": "令牌无效"}, status=401)
        return web.json_response({"success": True})

    async def verify(self, request):
        body = await request.json()
        self.requests.append(("verify", body))
        if not self._authorized(request):
            return web.json_response({"success": False, "message": "令牌无效"}, status=401)
        if self.redirect_verify:
            raise web.HTTPTemporaryRedirect(location="/stolen")
        umos = body.get("umos") or []
        # 群目标的作者是 VoiceHub 后台白名单：桩里只授权 GROUP_UMO。
        if umos and all(umo in self.bound or umo == GROUP_UMO for umo in umos):
            return web.json_response({"success": True, "umos": umos})
        return web.json_response({"success": False, "message": "包含未授权的目标"}, status=403)

    async def stolen(self, request):
        self.leaked.append(request.headers.get("X-VoiceHub-Token"))
        return web.json_response({"success": True, "umos": []})


class Sender:
    """记录发送调用的桩推送服务。"""

    def __init__(self, groups):
        self._groups = groups
        self.calls = []

    def group_targets(self):
        return self._groups

    async def push_text(self, targets, title, content, url):
        self.calls.append((list(targets), title, content, url))
        return types.SimpleNamespace(sent=len(targets), failed=[])


@asynccontextmanager
async def stack(redirect_verify=False):
    """同时启动桩 VoiceHub 与插件入站服务，产出可用端点与记录器。"""
    stub = StubVoiceHub([BOUND_UMO], redirect_verify=redirect_verify)
    stub_app = web.Application()
    stub_app.router.add_post("/api/bot/voicehub/bind", stub.bind)
    stub_app.router.add_post("/api/bot/voicehub/unbind", stub.unbind)
    stub_app.router.add_post("/api/bot/voicehub/verify-targets", stub.verify)
    stub_app.router.add_post("/stolen", stub.stolen)
    stub_runner = web.AppRunner(stub_app)
    await stub_runner.setup()
    stub_site = web.TCPSite(stub_runner, "127.0.0.1", 0)
    await stub_site.start()
    stub_base = f"http://127.0.0.1:{stub_site._server.sockets[0].getsockname()[1]}"

    config = VoiceHubConfig.from_mapping({
        "webhook_token": TOKEN,
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "allowed_ips": "127.0.0.1",
        "group_umos": GROUP_UMO,
        "voicehub_base_url": stub_base,
    })
    sender = Sender(config.group_umos)
    server = VoiceHubHttpServer(config, sender, VoiceHubClient(config), logging.getLogger("test"))
    await server.start()
    port = server._site._server.sockets[0].getsockname()[1]
    try:
        yield stub, sender, VoiceHubClient(config), f"http://127.0.0.1:{port}/voicehub/push"
    finally:
        await server.stop()
        await stub_runner.cleanup()


def test_bound_private_target_is_verified_through_voicehub():
    async def run():
        async with stack() as (stub, sender, _client, url):
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "title": "注册待审核", "content": "有新用户注册待审核",
                    "targets": {"umo": BOUND_UMO},
                }, headers={"X-VoiceHub-Token": TOKEN}) as response:
                    assert response.status == 200
                    assert (await response.json())["sent"] == 1
            assert sender.calls == [([BOUND_UMO], "注册待审核", "有新用户注册待审核", None)]
            assert stub.requests == [("verify", {"umos": [BOUND_UMO]})]

    asyncio.run(run())


def test_unbound_private_target_rejects_whole_batch_including_group():
    async def run():
        async with stack() as (stub, sender, _client, url):
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "content": "hello",
                    "targets": {"umo": "aiocqhttp:FriendMessage:attacker", "group": True},
                }, headers={"X-VoiceHub-Token": TOKEN}) as response:
                    assert response.status == 403
            assert sender.calls == []
            assert [entry[0] for entry in stub.requests] == ["verify"]

    asyncio.run(run())


def test_explicit_group_push_is_authorized_through_voicehub():
    """群推送不再依赖绑定表，但必须经 VoiceHub 白名单确认。"""
    async def run():
        async with stack() as (stub, sender, _client, url):
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "content": "全校通知", "targets": {"group": True},
                }, headers={"X-VoiceHub-Token": TOKEN}) as response:
                    assert response.status == 200
            assert sender.calls == [([GROUP_UMO], "", "全校通知", None)]
            assert stub.requests == [("verify", {"umos": [GROUP_UMO]})]

    asyncio.run(run())


def test_unauthorized_group_push_is_rejected():
    """VoiceHub 未授权的群目标必须 403，且不投递、不泄露令牌。"""
    async def run():
        async with stack() as (stub, sender, _client, url):
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "content": "全校通知",
                    "targets": {"umo": "aiocqhttp:GroupMessage:not-allowed"},
                }, headers={"X-VoiceHub-Token": TOKEN}) as response:
                    assert response.status == 403
            assert sender.calls == []
            assert stub.leaked == []

    asyncio.run(run())


def test_missing_targets_is_rejected_without_broadcast():
    async def run():
        async with stack() as (_stub, sender, _client, url):
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={"content": "hello"},
                                       headers={"X-VoiceHub-Token": TOKEN}) as response:
                    assert response.status == 400
            assert sender.calls == []

    asyncio.run(run())


def test_verify_redirect_blocks_delivery_and_leaks_no_token():
    async def run():
        async with stack(redirect_verify=True) as (stub, sender, _client, url):
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json={
                    "content": "hello", "targets": {"umo": BOUND_UMO},
                }, headers={"X-VoiceHub-Token": TOKEN}) as response:
                    assert response.status == 403
            assert sender.calls == []
            assert stub.leaked == []

    asyncio.run(run())


def test_bind_and_unbind_round_trip_against_stub_voicehub():
    async def run():
        async with stack() as (stub, _sender, client, _url):
            ok = await client.verify_binding_code(VALID_CODE, BOUND_UMO, "aiocqhttp")
            assert ok.ok and ok.username == "alice"
            assert not (await client.verify_binding_code("wrong-code", BOUND_UMO, "aiocqhttp")).ok
            assert (await client.unbind(BOUND_UMO)).ok
            assert [entry[0] for entry in stub.requests] == ["bind", "bind", "unbind"]
            assert stub.requests[0][1] == {
                "code": VALID_CODE, "umo": BOUND_UMO, "platform": "aiocqhttp"
            }

    asyncio.run(run())
