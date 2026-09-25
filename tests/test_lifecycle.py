"""Additional plugin lifecycle and boundary regression cases."""
import asyncio
import types

from test_plugin import FakeEvent, collect, plugin, server


def test_plugin_initialize_and_terminate_release_listener(plugin):
    async def run():
        p = plugin.VoiceHubPlugin(types.SimpleNamespace(), {"webhook_token": "secret", "listen_host": "127.0.0.1", "listen_port": 0})
        await p.initialize()
        assert p.http_server and p.http_server._site
        await p.terminate()
        assert p.http_server is None
        await p.initialize()
        assert p.http_server and p.http_server._site
        await p.terminate()
    asyncio.run(run())


def test_bind_rejects_group_by_default(plugin):
    async def run():
        p = plugin.VoiceHubPlugin(types.SimpleNamespace(), {"webhook_token": "secret"})
        async def forbidden(*args):
            raise AssertionError("group bind sent upstream")
        p.voicehub_client.verify_binding_code = forbidden
        result = await collect(p.vh_bind(FakeEvent("vh bind 0123456789abcdef01234567", group="room"), "0123456789abcdef01234567"))
        assert "私聊" in result[0]
    asyncio.run(run())


def test_targets_reject_excess_count(server):
    service, _ = server
    targets = [f"aiocqhttp:FriendMessage:user{i}" for i in range(201)]
    assert service._resolve_targets({"umo": targets})[1]
