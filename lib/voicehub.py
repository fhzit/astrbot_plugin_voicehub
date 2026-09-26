"""VoiceHub 侧回调客户端：绑定码校验与解绑。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from .config import VoiceHubConfig, umo_message_type
from .contract import BIND_PATH, UNBIND_PATH, VERIFY_TARGETS_PATH, WEEKLY_SCHEDULE_PATH, TOKEN_HEADER


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
        # 目标授权回查的短期缓存：键为目标集合，值为 (过期时刻, 是否授权)。
        self._verify_cache: dict = {}

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
                async with session.post(url, json=payload, headers=headers, allow_redirects=False) as response:
                    if 300 <= response.status < 400:
                        return BindResult(ok=False, message="VoiceHub 回调拒绝重定向")
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

    async def verify_private_targets(self, umos: list[str]) -> bool:
        """Only accept an authenticated, exact binding-store confirmation for every UMO."""
        return await self._verify_targets(umos, expected={"FriendMessage"})

    async def verify_group_targets(self, umos: list[str]) -> bool:
        """确认群目标均在 VoiceHub 后台白名单内。

        群目标无法由插件自行判定：会话串前缀是 AstrBot 平台实例 ID，插件本地
        列表无法证明该群属于哪一平台，也无法感知管理员在后台撤销授权的动作。
        因此授权判定完全交给 VoiceHub，插件只做「问一次、答不上就不发」的
        失败即关闭（fail closed）处理。
        """
        return await self._verify_targets(umos, expected={"GroupMessage"})

    async def _verify_targets(self, umos: list[str], expected: set) -> bool:
        """向 VoiceHub 回查目标授权，并要求返回集合与本批目标完全一致。"""
        if not umos or not self.config.voicehub_base_url or not self.config.voicehub_token:
            return False
        if self.config.verify_cache_seconds > 0:
            # 同一批目标在短时间内可能被 push 与 pull 两条路径分别确认，
            # 缓存只用于削峰；缓存项都带短 TTL，因此撤销授权最多延迟一个 TTL。
            cached = self._verify_cache.get(self._cache_key(umos))
            if cached is not None and cached[0] > time.monotonic():
                return cached[1]
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        verified_ok = False
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.config.voicehub_base_url}{VERIFY_TARGETS_PATH}",
                    json={"umos": umos},
                    headers={TOKEN_HEADER: self.config.voicehub_token},
                    allow_redirects=False,
                ) as response:
                    if response.status == 200:
                        body = await self._read_json(response)
                        verified = body.get("umos")
                        verified_ok = (body.get("success") is True and isinstance(verified, list)
                                       and len(verified) == len(umos) and len(set(umos)) == len(umos)
                                       and all(isinstance(item, str) and umo_message_type(item) in expected
                                               for item in verified)
                                       and set(verified) == set(umos))
        except Exception:  # noqa: BLE001 - network/timeout/invalid upstream response fails closed
            verified_ok = False
        if self.config.verify_cache_seconds > 0:
            self._verify_cache[self._cache_key(umos)] = (time.monotonic() + self.config.verify_cache_seconds, verified_ok)
        return verified_ok

    def _cache_key(self, umos: list[str]) -> tuple:
        """缓存键：排序后的目标集合，保证同一批目标顺序不同也命中同一条。"""
        return tuple(sorted(umos))

    async def get_weekly_schedule(self) -> dict:
        """拉取本周排期数据。

        Returns:
            VoiceHub 返回的排期 dict；网络异常或配置缺失时返回
            ``{"ok": False, "message": "..."}``.
        """
        if not self.config.voicehub_base_url:
            return {"ok": False, "message": "插件未配置 VoiceHub 站点地址"}
        if not self.config.voicehub_token:
            return {"ok": False, "message": "插件未配置 VoiceHub 令牌"}

        url = f"{self.config.voicehub_base_url}{WEEKLY_SCHEDULE_PATH}"
        headers = {TOKEN_HEADER: self.config.voicehub_token}
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers=headers, allow_redirects=False
                ) as response:
                    if 300 <= response.status < 400:
                        return {"ok": False, "message": "VoiceHub 拒绝重定向"}
                    body = await self._read_json(response)
                    if response.status >= 400:
                        return {"ok": False, "message": self._error_message(body, response.status)}
                    return body
        except aiohttp.ClientError as exc:
            return {"ok": False, "message": f"无法连接 VoiceHub：{exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"请求 VoiceHub 失败：{exc}"}
