"""拉取模式：插件主动向 VoiceHub 取件并回报投递结果。

用于 VoiceHub 无法访问插件的部署（插件在内网或 NAT 后）：推送方向倒转为
插件按周期轮询 VoiceHub 的待投递队列。取件与回执都是插件发起的出站请求，
因此 VoiceHub 侧不需要任何指向插件的可达性。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

from .config import VoiceHubConfig, parse_umo, umo_message_type
from .contract import ACK_PATH, PULL_PATH, TOKEN_HEADER
from .push import PushResult


@dataclass
class PullItem:
    """一条待投递通知。"""

    id: int
    title: str
    content: str
    url: Optional[str]
    umos: List[str]
    broadcast: bool


def parse_pull_items(payload: Any) -> List[PullItem]:
    """解析取件响应，丢弃形状非法的条目。

    逐条校验而不是整体信任：单条脏数据只应被跳过，不能让整批通知都无法投递。

    Args:
        payload: VoiceHub 返回的 JSON 对象。

    Returns:
        合法的待投递条目列表。
    """
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return []
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return []

    items: List[PullItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_id = raw.get("id")
        content = raw.get("content")
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
            continue
        if not isinstance(content, str) or not content:
            continue
        broadcast = raw.get("broadcast") is True
        umos: List[str] = []
        raw_umos = raw.get("umos")
        if isinstance(raw_umos, list):
            for umo in raw_umos:
                # 会话类型必须可解析，且与入站推送同一套白名单：插件只投递
                # 私聊与群聊，OtherMessage 一律跳过。
                parsed = parse_umo(umo)
                if parsed is not None and parsed[1] in {"FriendMessage", "GroupMessage"}:
                    umos.append(umo)
        # 既非广播又无有效目标：投递无从下手，跳过以免反复失败重试。
        if not broadcast and not umos:
            continue
        title = raw.get("title")
        url = raw.get("url")
        items.append(PullItem(
            id=item_id,
            title=title if isinstance(title, str) else "",
            content=content,
            url=url if isinstance(url, str) and url else None,
            umos=umos,
            broadcast=broadcast,
        ))
    return items


class VoiceHubPullClient:
    """轮询 VoiceHub 待投递队列的后台任务。"""

    def __init__(self, config: VoiceHubConfig, push_service: Any, logger: Any, voicehub_client: Any = None):
        self.config = config
        self.push_service = push_service
        self.logger = logger
        # 群目标授权由 VoiceHub 判定；未注入客户端时群投递会被拒绝（fail closed）。
        self.voicehub_client = voicehub_client
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        """是否具备运行条件：需要站点地址、令牌与轮询间隔。"""
        return bool(
            self.config.voicehub_base_url
            and self.config.voicehub_token
            and self.config.pull_interval_seconds > 0
        )

    async def start(self) -> None:
        """启动轮询任务。"""
        if not self.enabled:
            self.logger.warning(
                "[VoiceHub] 拉取模式未启动：需要配置 voicehub_base_url、令牌与轮询间隔。"
            )
            return
        self._task = asyncio.create_task(self._run())
        self.logger.info(
            f"[VoiceHub] 拉取模式已启动，间隔 {self.config.pull_interval_seconds} 秒。"
        )

    async def stop(self) -> None:
        """停止轮询任务，释放网络连接。"""
        if not self._task:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self.logger.info("[VoiceHub] 拉取模式已停止。")

    async def _run(self) -> None:
        """按固定间隔取件，直到被取消。

        单轮异常只记录并继续：网络抖动或 VoiceHub 重启不应终止常驻轮询。
        """
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 轮询不得因单次失败退出
                self.logger.warning(f"[VoiceHub] 拉取通知失败: {exc}")
            await asyncio.sleep(self.config.pull_interval_seconds)

    async def run_once(self) -> int:
        """执行一轮取件与投递。

        Returns:
            本轮处理的条目数。
        """
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        headers = {TOKEN_HEADER: self.config.voicehub_token}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.config.voicehub_base_url}{PULL_PATH}",
                json={},
                headers=headers,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return 0
                payload = await self._read_json(response)

            items = parse_pull_items(payload)
            if not items:
                return 0

            results = []
            for item in items:
                results.append(await self._deliver(session, headers, item))
            await self._ack(session, headers, results)
            return len(items)

    async def _deliver(
        self, session: aiohttp.ClientSession, headers: Dict[str, str], item: PullItem
    ) -> Dict[str, Any]:
        """投递一条通知。

        Args:
            session: 复用的 HTTP 会话。
            headers: 带令牌的请求头。
            item: 待投递条目。

        Returns:
            回执项。广播条目在无有效群目标时按失败回报，避免被静默丢弃。
        """
        try:
            targets = list(self.push_service.group_targets()) if item.broadcast else []
            targets = [umo for umo in targets if parse_umo(umo) is not None]
            if item.broadcast:
                targets.extend(item.umos)
            else:
                targets = item.umos
            # 去重但保持顺序，同一会话不重复投递。
            deduped: List[str] = []
            for umo in targets:
                if umo not in deduped:
                    deduped.append(umo)
            if not deduped:
                return {"id": item.id, "success": False, "reason": "没有可用的群广播目标"}

            # 群目标必须由 VoiceHub 白名单确认后再投递：拉取模式下 VoiceHub 虽已按
            # 白名单过滤过，但队列条目可能是在授权被撤销前入队的，因此这里再回查一次，
            # 回查不通过就按失败回报，避免向已撤销授权的群发消息。
            group_targets = [umo for umo in deduped if umo_message_type(umo) == "GroupMessage"]
            if group_targets:
                # 第二道闸门：本机 group_umos 非空时只放行其中的群。
                local_groups = set(self.push_service.group_targets())
                if local_groups and any(umo not in local_groups for umo in group_targets):
                    return {"id": item.id, "success": False, "reason": "群目标未获授权或无法验证"}
                if self.voicehub_client is None:
                    # 未注入客户端时无从确认授权，宁可不发也不越权。
                    return {"id": item.id, "success": False, "reason": "群目标未获授权或无法验证"}
                try:
                    authorized = await self.voicehub_client.verify_group_targets(group_targets)
                except Exception as exc:  # noqa: BLE001 - 回查故障时不得投递
                    self.logger.warning(f"[VoiceHub] 群目标回查失败: {exc}")
                    authorized = False
                if not authorized:
                    return {"id": item.id, "success": False, "reason": "群目标未获授权或无法验证"}

            outcome: PushResult = await self.push_service.push_text(
                deduped, item.title, item.content, item.url
            )
            if outcome.sent <= 0:
                reason = outcome.failed[0].get("reason") if outcome.failed else "投递失败"
                return {"id": item.id, "success": False, "reason": str(reason)[:200]}
            if outcome.failed:
                # 部分失败仍算投递完成，避免整条通知被重复推送。
                self.logger.warning(
                    f"[VoiceHub] 通知 {item.id} 部分目标投递失败: {outcome.failed}"
                )
            return {"id": item.id, "success": True}
        except Exception as exc:  # noqa: BLE001 - 单条失败不得中断整批
            return {"id": item.id, "success": False, "reason": str(exc)[:200]}

    async def _ack(
        self, session: aiohttp.ClientSession, headers: Dict[str, str], results: List[Dict[str, Any]]
    ) -> None:
        """回报投递结果；失败仅记录，由租约过期后重新领取。"""
        if not results:
            return
        try:
            async with session.post(
                f"{self.config.voicehub_base_url}{ACK_PATH}",
                json={"results": results},
                headers=headers,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    self.logger.warning(
                        f"[VoiceHub] 投递回执未接受（HTTP {response.status}），将由租约重试。"
                    )
        except Exception as exc:  # noqa: BLE001 - 回执失败由租约兜底
            self.logger.warning(f"[VoiceHub] 投递回执发送失败: {exc}")

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse) -> Any:
        """读取响应体，非 JSON 时返回空字典。"""
        try:
            return await response.json(content_type=None)
        except Exception:  # noqa: BLE001 - 上游返回非 JSON 时按无正文处理
            return {}
