"""VoiceHub 推送插件：把 VoiceHub 的通知投递到 AstrBot 各平台适配器会话。

插件定位是「出站网关」：不保存业务状态（用户与会话的绑定关系由 VoiceHub 维护），
只负责校验令牌、解析目标会话、构造消息链、调用 AstrBot 适配器发送，
并在指令中把绑定码回传给 VoiceHub 校验。
"""

import asyncio
import tempfile
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .lib.config import VoiceHubConfig, dropped_umo_values
from .lib.font_manager import ensure_fonts, fonts_already_exist
from .lib.pull import VoiceHubPullClient
from .lib.push import PushService
from .lib.schedule_image import generate_weekly_schedule_image
from .lib.server import VoiceHubHttpServer
from .lib.song import SongService
from .lib.voicehub import VoiceHubClient

_FONT_DIR = Path.home() / ".astrbot/data/plugin_data/astrbot_plugin_voicehub"


@register(
    "astrbot_plugin_voicehub",
    "fhzit",
    "接收 VoiceHub 通知并推送到聊天平台会话",
    "1.0.0",
)
class VoiceHubPlugin(Star):
    """VoiceHub 推送插件。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.raw_config = config
        self.plugin_config = VoiceHubConfig.from_mapping(config)
        self.voicehub_client = VoiceHubClient(self.plugin_config)
        self.push_service = PushService(self.context, self.plugin_config, logger)
        self.song_service = SongService(self.plugin_config)
        self.http_server: Optional[VoiceHubHttpServer] = None
        self.pull_client: Optional[VoiceHubPullClient] = None

    async def initialize(self):
        """启动通知通道。

        拉取模式（pull_interval_seconds > 0）下插件主动向 VoiceHub 取件，
        不需要 VoiceHub 能访问本机，因此不启动入站 HTTP 服务；否则保持原有
        入站推送模式（要求 VoiceHub 可访问插件的监听端口）。
        """
        if not self.plugin_config.webhook_token:
            logger.error(
                "[VoiceHub] 未配置推送令牌（webhook_token），已跳过启动通知通道。"
                "请在插件配置中填写与 VoiceHub 一致的令牌。"
            )
            return

        # group_umos 在加载时已过滤非法项；此处提示被丢弃的取值，避免管理员
        # 以为群广播已生效却收不到消息（例如漏写冒号或会话类型写错）。
        raw_config = self.raw_config if isinstance(self.raw_config, dict) else None
        raw_groups = raw_config.get("group_umos") if raw_config else None
        if raw_groups:
            dropped = dropped_umo_values(raw_groups, "GroupMessage")
            if dropped:
                logger.warning(
                    f"[VoiceHub] 以下群广播会话形状非法，已忽略：{dropped}。"
                    "正确格式示例：default:GroupMessage:123456（请在群内发送 /广播 状态 取值）。"
                )

        if self.plugin_config.pull_interval_seconds > 0:
            self.pull_client = VoiceHubPullClient(
                self.plugin_config, self.push_service, logger, self.voicehub_client
            )
            await self.pull_client.start()
            return

        self.http_server = VoiceHubHttpServer(
            self.plugin_config,
            self.push_service,
            self.voicehub_client,
            logger,
        )
        try:
            await self.http_server.start()
        except Exception as exc:  # noqa: BLE001 - 端口占用等启动失败不得影响 AstrBot 主流程
            logger.error(f"[VoiceHub] HTTP 服务启动失败: {exc}")
            self.http_server = None

    async def terminate(self):
        """插件卸载/停用时关闭通知通道。"""
        if self.pull_client:
            await self.pull_client.stop()
            self.pull_client = None
        if self.http_server:
            await self.http_server.stop()
            self.http_server = None

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command_group("广播", alias={"vh"})
    def vh(self):
        """VoiceHub 相关指令组（`/广播`，兼容旧写法 `/vh`）。"""

    @vh.command("绑定", alias={"bind"})
    async def vh_bind(self, event: AstrMessageEvent, code: GreedyStr):
        """绑定 VoiceHub 账号：/广播 绑定 <绑定码>"""
        if not self.plugin_config.webhook_token:
            yield event.plain_result("插件尚未配置推送令牌，请先在 AstrBot 插件配置中填写。")
            return

        if event.get_group_id():
            yield event.plain_result(
                "为避免个人通知被推送到群里，请在机器人私聊中发送绑定码。"
            )
            return

        # AstrBot's CommandFilter strips the wake prefix before matching; use
        # the parsed parameter instead of guessing from the raw message text.
        code = code.strip()
        if not code or len(code.split()) != 1:
            yield event.plain_result(
                "用法：/广播 绑定 <绑定码>。绑定码请在 VoiceHub 的「机器人推送」中生成。"
            )
            return

        result = await self.voicehub_client.verify_binding_code(
            code, event.unified_msg_origin, event.get_platform_name()
        )
        if result.ok:
            yield event.plain_result(
                f"绑定成功，VoiceHub 通知将推送到当前会话（{result.username or '已绑定账号'}）。"
            )
        else:
            yield event.plain_result(f"绑定失败：{result.message}")

    @vh.command("解绑", alias={"unbind"})
    async def vh_unbind(self, event: AstrMessageEvent):
        """解绑当前会话：/广播 解绑"""
        if not self.plugin_config.webhook_token:
            yield event.plain_result("插件尚未配置推送令牌，请先在 AstrBot 插件配置中填写。")
            return

        result = await self.voicehub_client.unbind(event.unified_msg_origin)
        if result.ok:
            yield event.plain_result("已解绑 VoiceHub 通知推送。")
        else:
            yield event.plain_result(f"解绑失败：{result.message}")

    @vh.command("状态", alias={"status"})
    async def vh_status(self, event: AstrMessageEvent):
        """查看当前会话的推送状态与会话 ID：/广播 状态"""
        platform = event.get_platform_name()
        message_type = "群聊" if event.get_group_id() else "私聊"
        if self.pull_client:
            endpoint = f"拉取模式（每 {self.plugin_config.pull_interval_seconds} 秒向 VoiceHub 取件）"
        elif self.http_server:
            endpoint = self.plugin_config.display_endpoint
        else:
            endpoint = "未启用（缺少推送令牌或启动失败）"
        yield event.plain_result(
            "VoiceHub 推送状态：\n"
            f"- 平台：{platform}\n"
            f"- 类型：{message_type}\n"
            f"- 会话 ID：{event.unified_msg_origin}\n"
            f"- 服务：{endpoint}"
        )

    @vh.command("自检", alias={"test"})
    async def vh_test(self, event: AstrMessageEvent):
        """向当前会话发送一条测试通知：/广播 自检"""
        result = await self.push_service.push_text(
            [event.unified_msg_origin],
            "VoiceHub 测试通知",
            "这是一条来自 VoiceHub 插件的测试推送。",
            None,
        )
        if result.failed:
            logger.warning(f"[VoiceHub] 测试推送存在失败目标: {result.failed}")
        yield event.plain_result(
            f"测试推送完成：成功 {result.sent} 个会话，失败 {len(result.failed)} 个。"
        )

    @vh.command("点歌", alias={"song"})
    async def vh_song(self, event: AstrMessageEvent, keyword: GreedyStr):
        """搜索歌曲：/广播 点歌 <关键词>（`时段` 为保留字，用于查看播出时段）"""
        yield event.plain_result(await self.song_service.song(
            event.unified_msg_origin, event.get_group_id(), keyword
        ))

    @vh.command("选歌", alias={"pick"})
    async def vh_pick(self, event: AstrMessageEvent, args: GreedyStr):
        """按序号投稿：/广播 选歌 <序号> [时段=时段序号] [点歌券=券码]"""
        yield event.plain_result(await self.song_service.pick(
            event.unified_msg_origin, event.get_group_id(), args
        ))

    @vh.command("本周歌单", alias={"weekly"})
    async def vh_weekly(self, event: AstrMessageEvent):
        """发送本周排期图片：/广播 本周歌单"""
        if not self.plugin_config.song_enabled:
            yield event.plain_result("点歌功能未启用，请在插件配置中开启。")
            return

        if not self.plugin_config.voicehub_base_url:
            yield event.plain_result("插件未配置 VoiceHub 站点地址，无法获取排期。")
            return

        data = await self.voicehub_client.get_weekly_schedule()
        if data.get("ok") is False:
            yield event.plain_result(f"获取排期失败：{data.get('message', '未知错误')}")
            return

        # 字体首次下载约需 15 秒，提前告知用户
        font_dir = _FONT_DIR
        font_dir.mkdir(parents=True, exist_ok=True)
        if not fonts_already_exist(font_dir):
            logger.info("[VoiceHub] 首次使用排期图片功能，正在下载字体（约 16 MB），请稍候……")

        try:
            fonts = await ensure_fonts(font_dir, logger)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"字体下载失败，无法生成排期图片：{exc}")
            return

        try:
            img_bytes = await generate_weekly_schedule_image(data, font_dir)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[VoiceHub] 生成排期图片失败: {exc}")
            yield event.plain_result(f"生成排期图片失败：{exc}")
            return

        # 写入临时文件，再用 event.image_result 发送
        with tempfile.NamedTemporaryFile(
            suffix=".png", prefix="vh_weekly_", delete=False
        ) as tmp:
            tmp.write(img_bytes)
            tmp_path = tmp.name

        try:
            yield event.image_result(tmp_path)
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


