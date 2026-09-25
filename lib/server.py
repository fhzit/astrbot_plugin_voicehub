"""入站 HTTP 服务：接收 VoiceHub 的推送请求。"""

from __future__ import annotations

import hmac
from typing import Any, List, Tuple

from aiohttp import web

from .config import VoiceHubConfig, parse_umo, umo_message_type
from .push import PushService
from .voicehub import VoiceHubClient
from .contract import PUSH_PATH, HEALTH_PATH, TOKEN_HEADER

MAX_BODY_BYTES = 64 * 1024
MAX_TARGETS = 200


class VoiceHubHttpServer:
    """基于 aiohttp 的轻量入站接口。

    运行在 AstrBot 进程内，AstrBot 为常驻服务，因此这里可以使用长驻监听；
    插件本身不发起任何定时任务或长连接。
    """

    def __init__(
        self,
        config: VoiceHubConfig,
        push_service: PushService,
        voicehub_client: VoiceHubClient,
        logger: Any,
    ):
        self.config = config
        self.push_service = push_service
        self.voicehub_client = voicehub_client
        self.logger = logger
        self._runner: Any = None
        self._site: Any = None

    async def start(self) -> None:
        """启动监听。"""
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        app.router.add_post(PUSH_PATH, self.handle_push)
        app.router.add_get(HEALTH_PATH, self.handle_health)

        runner = web.AppRunner(app)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.config.listen_host, self.config.listen_port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site
        self.logger.info(
            f"[VoiceHub] 推送接口已启动：{self.config.display_endpoint}"
        )

    async def stop(self) -> None:
        """停止监听并释放端口。"""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self.logger.info("[VoiceHub] 推送接口已停止。")

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    async def handle_health(self, request: web.Request) -> web.Response:
        """健康检查，供 VoiceHub 后台「测试连接」使用。"""
        denied = self._guard(request)
        if denied:
            return denied

        return web.json_response(
            {
                "success": True,
                "service": "astrbot_plugin_voicehub",
                "platforms": self._platform_names(),
                "group_targets": len(self.push_service.group_targets()),
            }
        )

    async def handle_push(self, request: web.Request) -> web.Response:
        """接收一条推送请求。

        请求体：
            title: 通知标题。
            content: 通知正文。
            targets: 目标筛选，支持 ``{"umo": [...]}`` / ``{"group": true}``。
            url: 可选来源链接。
        """
        denied = self._guard(request)
        if denied:
            return denied

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 请求体非法统一按 400 处理
            return web.json_response({"success": False, "message": "请求体必须是合法 JSON"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"success": False, "message": "请求体必须是 JSON 对象"}, status=400)

        if not isinstance(payload.get("content"), str) or not isinstance(payload.get("title", ""), str):
            return web.json_response({"success": False, "message": "标题和正文必须是字符串"}, status=400)
        title = payload.get("title", "").strip()
        content = payload["content"].strip()
        if not content:
            return web.json_response({"success": False, "message": "content 不能为空"}, status=400)

        umos, error = self._resolve_targets(payload.get("targets"))
        if error:
            return web.json_response({"success": False, "message": error}, status=400)
        if not umos:
            return web.json_response({"success": False, "message": "没有可用的推送目标"}, status=400)

        # 会话类型解析失败的目标直接判为非法，不得进入推送链路。
        private_umos = [umo for umo in umos if umo_message_type(umo) == "FriendMessage"]
        if private_umos:
            try:
                authorized = await self.voicehub_client.verify_private_targets(private_umos)
            except Exception as exc:  # noqa: BLE001 - 回查故障时不得推送
                self.logger.warning(f"[VoiceHub] 私聊目标回查失败: {exc}")
                authorized = False
            if not authorized:
                return web.json_response({"success": False, "message": "私聊目标未获授权或无法验证"}, status=403)

        url = payload.get("url")
        result = await self.push_service.push_text(
            umos,
            title,
            content,
            str(url).strip() if isinstance(url, str) and url.strip() else None,
        )

        self.logger.info(
            f"[VoiceHub] 推送完成：成功 {result.sent}，失败 {len(result.failed)}"
        )
        return web.json_response(
            {
                "success": result.sent > 0,
                "sent": result.sent,
                "failed": result.failed,
            }
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _guard(self, request: web.Request) -> Any:
        """校验令牌与来源 IP。

        Args:
            request: 入站请求。

        Returns:
            校验通过时返回 None，否则返回可直接响应的错误对象。
        """
        token = request.headers.get(TOKEN_HEADER, "")
        if not self.config.webhook_token or not hmac.compare_digest(token, self.config.webhook_token):
            self.logger.warning("[VoiceHub] 拒绝了一次令牌无效的请求。")
            return web.json_response({"success": False, "message": "令牌无效"}, status=401)

        if self.config.allowed_ips:
            peer = request.remote or ""
            # Forwarded headers are caller-controlled unless a trusted proxy
            # rewrites them. Only compare the actual socket peer.
            if peer not in self.config.allowed_ips:
                self.logger.warning(f"[VoiceHub] 拒绝了白名单外来源的请求：{peer}")
                return web.json_response({"success": False, "message": "来源 IP 不在白名单内"}, status=403)

        return None


    def _resolve_targets(self, raw: Any) -> Tuple[List[str], str]:
        """解析目标会话。

        Args:
            raw: 请求体中的 targets 字段。

        Returns:
            (目标会话列表, 错误文案)。错误文案非空时表示参数非法。
        """
        umos: List[str] = []

        if raw is None:
            return [], "必须明确指定 targets"

        if not isinstance(raw, dict):
            return [], "targets 必须是对象"

        if not raw or set(raw) - {"group", "umo"}:
            return [], "targets 字段不支持"
        if "group" in raw and not isinstance(raw["group"], bool):
            return [], "group 必须是布尔值"

        if raw.get("group"):
            # 只接受形状合法的 GroupMessage；配置被写坏时不得进入推送链路。
            umos.extend(
                target for target in self.push_service.group_targets()
                if umo_message_type(target) == "GroupMessage"
            )

        if "umo" in raw:
            value = raw["umo"]
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or len(value) > MAX_TARGETS:
                return [], "umo 必须是字符串或不超过 200 个字符串的数组"
            for item in value:
                if not isinstance(item, str) or item != item.strip() or not item:
                    return [], "umo 包含无效会话"
                # Private sessions are selected from VoiceHub's binding store;
                # group sessions must additionally be explicitly admin-approved.
                pieces = parse_umo(item)
                if pieces is None:
                    return [], "umo 包含无效会话"
                if pieces[1] == "GroupMessage" and item not in self.push_service.group_targets():
                    return [], "群会话未被管理员授权"
                if pieces[1] not in {"FriendMessage", "GroupMessage"}:
                    return [], "不支持的会话类型"
                umos.append(item)

        deduped: List[str] = []
        for umo in umos:
            candidate = umo.strip()
            if candidate and candidate not in deduped:
                deduped.append(candidate)

        if len(deduped) > MAX_TARGETS:
            return [], f"单次推送目标不能超过 {MAX_TARGETS} 个"

        return deduped, ""

    def _platform_names(self) -> List[str]:
        """列出当前已加载的平台适配器名，便于排查。"""
        try:
            insts = self.push_service.context.platform_manager.platform_insts
            return sorted({inst.meta().name for inst in insts})
        except Exception:  # noqa: BLE001 - 诊断信息不可用不影响主流程
            return []
