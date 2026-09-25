"""Real loopback callback contract tests."""
import asyncio
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
from astrbot_plugin_voicehub.lib.config import VoiceHubConfig
from astrbot_plugin_voicehub.lib.voicehub import VoiceHubClient


def test_callbacks_use_shared_header_paths_and_fail_closed():
    async def run():
        requests = []

        async def callback(request):
            requests.append((request.path, request.headers.get("X-VoiceHub-Token"), await request.json()))
            return web.json_response({"message": "unexpected"})

        app = web.Application()
        app.router.add_post("/api/bot/voicehub/bind", callback)
        app.router.add_post("/api/bot/voicehub/unbind", callback)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            config = VoiceHubConfig.from_mapping({"webhook_token": "secret", "voicehub_base_url": f"http://127.0.0.1:{port}"})
            client = VoiceHubClient(config)
            assert not (await client.verify_binding_code("0123456789abcdef01234567", "aiocqhttp:FriendMessage:user", "aiocqhttp")).ok
            assert not (await client.unbind("aiocqhttp:FriendMessage:user")).ok
            assert requests == [
                ("/api/bot/voicehub/bind", "secret", {"code": "0123456789abcdef01234567", "umo": "aiocqhttp:FriendMessage:user", "platform": "aiocqhttp"}),
                ("/api/bot/voicehub/unbind", "secret", {"umo": "aiocqhttp:FriendMessage:user"}),
            ]
        finally:
            await runner.cleanup()
    asyncio.run(run())

def test_callback_refuses_redirect_without_forwarding_token():
    async def run():
        forwarded = []
        async def redirect(request):
            raise web.HTTPTemporaryRedirect(location="/stolen")
        async def stolen(request):
            forwarded.append(request.headers.get("X-VoiceHub-Token"))
            return web.json_response({"success": True})
        app = web.Application()
        app.router.add_post("/api/bot/voicehub/bind", redirect)
        app.router.add_post("/stolen", stolen)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            client = VoiceHubClient(VoiceHubConfig.from_mapping({"webhook_token": "secret", "voicehub_base_url": f"http://127.0.0.1:{port}"}))
            result = await client.verify_binding_code("0123456789abcdef01234567", "aiocqhttp:FriendMessage:user", "aiocqhttp")
            assert not result.ok
            assert forwarded == []
        finally:
            await runner.cleanup()
    asyncio.run(run())

def test_target_lookup_requires_exact_verified_list_and_refuses_redirect():
    async def run():
        leaked = []
        async def verify(request):
            body = await request.json()
            if body["umos"] == ["aiocqhttp:FriendMessage:redirect"]:
                raise web.HTTPTemporaryRedirect(location="/stolen")
            return web.json_response({"success": True, "umos": ["aiocqhttp:FriendMessage:user1"]})
        async def stolen(request):
            leaked.append(request.headers.get("X-VoiceHub-Token"))
            return web.json_response({"success": True, "umos": ["aiocqhttp:FriendMessage:redirect"]})
        app = web.Application()
        app.router.add_post("/api/bot/voicehub/verify-targets", verify)
        app.router.add_post("/stolen", stolen)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            client = VoiceHubClient(VoiceHubConfig.from_mapping({"webhook_token": "secret", "voicehub_base_url": f"http://127.0.0.1:{port}"}))
            assert await client.verify_private_targets(["aiocqhttp:FriendMessage:user1"])
            assert not await client.verify_private_targets(["aiocqhttp:FriendMessage:other"])
            assert not await client.verify_private_targets(["aiocqhttp:FriendMessage:redirect"])
            assert leaked == []
        finally:
            await runner.cleanup()
    asyncio.run(run())
