"""推送正文格式与跨平台 UMO 透传测试。"""
import asyncio
import logging

import pytest

from astrbot_plugin_voicehub.lib.config import VoiceHubConfig
from astrbot_plugin_voicehub.lib.push import PushService


@pytest.mark.parametrize("prefix,title,content,url,expected", [
    ("校园广播站", "活动通知", "正文", "https://example.org/news", "校园广播站\n活动通知\n正文\nhttps://example.org/news"),
    ("校园广播站", "", "正文", None, "校园广播站\n正文"),
    ("", "活动通知", "正文", None, "活动通知\n正文"),
    ("校园广播站", "", "正文", None, "校园广播站\n正文"),
])
def test_prefix_occupies_its_own_line(prefix, title, content, url, expected):
    config = VoiceHubConfig.from_mapping({"message_prefix": prefix})
    service = PushService(None, config, logging.getLogger("test"))
    assert service.build_chain(title, content, url) == expected


@pytest.mark.parametrize("umo", [
    "default:FriendMessage:123",  # OneBot v11
    "官方实例:FriendMessage:openid",  # QQ 官方
    "企业微信:GroupMessage:conversation",  # 企业微信
    "飞书实例:GroupMessage:oc_123",  # 飞书
    "钉钉实例:GroupMessage:cid",  # 钉钉
])
def test_push_uses_astrbot_for_platform_delivery(umo):
    class Context:
        def __init__(self):
            self.calls = []

        async def send_message(self, target, chain):
            self.calls.append((target, chain))
            return True

    context = Context()
    service = PushService(context, VoiceHubConfig(message_prefix="通知"), logging.getLogger("test"))
    result = asyncio.run(service.push_text([umo], "标题", "正文"))
    assert context.calls == [(umo, "通知\n标题\n正文")]
    assert result.sent == 1 and result.failed == []
