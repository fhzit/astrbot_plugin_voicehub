"""消息链构造与推送。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from astrbot.api.event import MessageChain

from .config import VoiceHubConfig


@dataclass
class PushResult:
    """一次推送的结果。"""

    sent: int = 0
    failed: List[dict] = field(default_factory=list)


class PushService:
    """把文本通知投递到 AstrBot 会话。"""

    def __init__(self, context: Any, config: VoiceHubConfig, logger: Any):
        self.context = context
        self.config = config
        self.logger = logger

    def build_chain(self, title: str, content: str, url: Optional[str] = None) -> MessageChain:
        """构造纯文本消息链。

        Args:
            title: 通知标题。
            content: 通知正文。
            url: 可选的来源链接。

        Returns:
            可直接交给 AstrBot 发送的消息链。
        """
        lines: List[str] = []
        if self.config.message_prefix:
            lines.append(self.config.message_prefix)
        if title:
            lines.append(title)
        if content:
            lines.append(content)
        if url and self.config.include_url:
            lines.append(url)

        return MessageChain().message("\n".join(lines))

    async def push_text(
        self,
        umos: List[str],
        title: str,
        content: str,
        url: Optional[str] = None,
    ) -> PushResult:
        """向多个会话推送同一条通知。

        Args:
            umos: 目标会话列表。
            title: 通知标题。
            content: 通知正文。
            url: 可选的来源链接。

        Returns:
            成功计数与失败目标明细；单个目标失败不影响其余目标。
        """
        result = PushResult()
        chain = self.build_chain(title, content, url)

        for umo in umos:
            try:
                ok = await self.context.send_message(umo, chain)
                if ok:
                    result.sent += 1
                else:
                    result.failed.append({"umo": umo, "reason": "未找到匹配的平台适配器"})
            except Exception as exc:  # noqa: BLE001 - 单目标失败不得中断整批推送
                self.logger.warning(f"[VoiceHub] 推送到 {umo} 失败: {exc}")
                result.failed.append({"umo": umo, "reason": str(exc)})

        return result

    def group_targets(self) -> List[str]:
        """返回管理员配置的群广播会话。"""
        return list(self.config.group_umos)

    @property
    def http_server_enabled(self) -> bool:
        """HTTP 服务是否具备启动条件。"""
        return bool(self.config.webhook_token)
