"""VoiceHub 侧回调客户端：绑定码校验与解绑。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from .config import VoiceHubConfig
from .contract import BIND_PATH, UNBIND_PATH, TOKEN_HEADER


@dataclass
class BindResult:
    """绑定/解绑调用结果。"""

    ok: bool = False
    message: str = ""
    username: Optional[str] = None


class VoiceHubClient:
    """调用 VoiceHub 提供的机器人绑定接口。"""

    def __init__(self, config: VoiceHubConfig):
        self.config = config

    async def _post(self, path: str, payload: dict) -> BindResult:
        """向 VoiceHub 发起一次带令牌的 POST。

        Args:
            path: 相对 VoiceHub 站点的接口路径。
            payload: JSON 请求体。

        Returns:
            归一化的调用结果；网络异常与超时都收敛为失败结果。
        """
        if not self.config.voicehub_base_url:
            return BindResult(ok=False, message="插件未配置 VoiceHub 站点地址")

        url = f"{self.config.voicehub_base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            TOKEN_HEADER: self.config.voicehub_token,
        }
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    body = await self._read_json(response)
                    if response.status >= 400:
                        return BindResult(ok=False, message=self._error_message(body, response.status))
                    return BindResult(
                        ok=body.get("success") is True,
                        message=str(body.get("message") or ""),
                        username=body.get("username"),
                    )
        except aiohttp.ClientError as exc:
            return BindResult(ok=False, message=f"无法连接 VoiceHub：{exc}")
        except Exception as exc:  # noqa: BLE001 - 插件侧不得因回调失败抛异常
            return BindResult(ok=False, message=f"请求 VoiceHub 失败：{exc}")

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse) -> dict:
        """读取响应体，非 JSON 时返回空字典。"""
        try:
            body: Any = await response.json(content_type=None)
            return body if isinstance(body, dict) else {}
        except Exception:  # noqa: BLE001 - 上游返回非 JSON 时按无正文处理
            return {}

    @staticmethod
    def _error_message(body: dict, status: int) -> str:
        """提取 VoiceHub 返回的错误文案。"""
        message = body.get("message") or body.get("statusMessage")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return f"VoiceHub 返回状态码 {status}"

    async def verify_binding_code(self, code: str, umo: str, platform: str) -> BindResult:
        """提交绑定码完成用户与会话的绑定。

        Args:
            code: VoiceHub 生成的一次性绑定码。
            umo: 当前会话的 unified_msg_origin。
            platform: 当前平台适配器名。

        Returns:
            绑定结果。
        """
        return await self._post(
            BIND_PATH,
            {"code": code, "umo": umo, "platform": platform},
        )

    async def unbind(self, umo: str) -> BindResult:
        """解除会话绑定。

        Args:
            umo: 当前会话的 unified_msg_origin。

        Returns:
            解绑结果。
        """
        return await self._post(UNBIND_PATH, {"umo": umo})
