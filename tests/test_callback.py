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
            assert not (await client.verify_binding_code("ABC", "bot:FriendMessage:user", "bot")).ok
            assert not (await client.unbind("bot:FriendMessage:user")).ok
            assert requests == [
                ("/api/bot/voicehub/bind", "secret", {"code": "ABC", "umo": "bot:FriendMessage:user", "platform": "bot"}),
                ("/api/bot/voicehub/unbind", "secret", {"umo": "bot:FriendMessage:user"}),
            ]
        finally:
            await runner.cleanup()
    asyncio.run(run())
