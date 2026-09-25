"""插件配置读取。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

DEFAULT_PORT = 6199
DEFAULT_TIMEOUT = 15



def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> List[str]:
    """把 WebUI 的 text 字段解析为去空列表。"""
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    elif isinstance(value, str):
        items = value.replace("\n", ",").split(",")
    else:
        return []

    result: List[str] = []
    for item in items:
        stripped = item.strip()
        if stripped and stripped not in result:
            result.append(stripped)
    return result


# AstrBot 支持的会话类型（见 astrbot.core.platform.message_type.MessageType）。
UMO_MESSAGE_TYPES = frozenset({"FriendMessage", "GroupMessage", "OtherMessage"})

MAX_UMO_LENGTH = 512


def parse_umo(value: Any) -> Optional[Tuple[str, str, str]]:
    """把字符串解析为 (平台标识, 消息类型, 会话 ID)。

    AstrBot 的 unified_msg_origin 形如 ``platform_id:MessageType:session_id``，
    其中 platform_id 是平台适配器实例 ID（不一定等于适配器名）。

    Args:
        value: 待解析的取值，通常来自配置或请求体。

    Returns:
        形状合法时返回三元组，否则返回 None。
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_UMO_LENGTH or any(c.isspace() for c in candidate):
        return None
    parts = candidate.split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    platform_id, message_type, session_id = parts
    if message_type not in UMO_MESSAGE_TYPES:
        return None
    return platform_id, message_type, session_id


def umo_message_type(value: Any) -> str:
    """返回会话类型；形状非法时返回空字符串。

    调用方据此判断目标类型，避免直接对字符串做下标访问而越界。

    Args:
        value: 待解析的 unified_msg_origin。

    Returns:
        ``FriendMessage`` / ``GroupMessage`` / ``OtherMessage`` 之一，非法时为空串。
    """
    parsed = parse_umo(value)
    return parsed[1] if parsed else ""


def _as_umo_list(value: Any, message_type: str) -> List[str]:
    """解析 UMO 列表，只保留指定消息类型的合法项。

    管理员在 WebUI 里手写的 UMO 很容易缺少冒号或写错会话类型；这里直接丢弃
    非法项（由调用方提示），避免畸形取值进入推送链路。

    Args:
        value: 配置中的原始取值。
        message_type: 期望的会话类型，如 ``GroupMessage``。

    Returns:
        去重后的合法 UMO 列表。
    """
    return [item for item in _as_list(value) if umo_message_type(item) == message_type]


def dropped_umo_values(value: Any, message_type: str) -> List[str]:
    """返回配置里被判定为指定会话类型之外、因而被忽略的取值。

    供启动日志使用：管理员写错 UMO 时原本会静默失效，这里让问题可见。

    Args:
        value: 配置中的原始取值。
        message_type: 期望的会话类型，如 ``GroupMessage``。

    Returns:
        被丢弃的原始字符串列表。
    """
    return [item for item in _as_list(value) if umo_message_type(item) != message_type]



@dataclass
class VoiceHubConfig:
    """插件运行配置。"""

    webhook_token: str = ""
    listen_host: str = "0.0.0.0"
    listen_port: int = DEFAULT_PORT
    public_base_url: str = ""
    allowed_ips: List[str] = field(default_factory=list)
    message_prefix: str = ""
    include_url: bool = True

    group_umos: List[str] = field(default_factory=list)

    request_timeout_seconds: int = DEFAULT_TIMEOUT
    voicehub_base_url: str = ""
    voicehub_token: str = ""

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "VoiceHubConfig":
        """从 AstrBot 注入的插件配置构造实例。

        Args:
            raw: 插件配置对象，缺失或类型异常时逐项回退默认值。

        Returns:
            归一化后的配置实例。
        """
        data: Dict[str, Any] = {}
        if isinstance(raw, Mapping):
            data = dict(raw)
        else:
            # AstrBotConfig 等映射类对象可能不支持 dict()，逐个键读取兜底
            for key in (
                "webhook_token",
                "listen_host",
                "listen_port",
                "public_base_url",
                "allowed_ips",
                "message_prefix",
                "include_url",

                "group_umos",

                "request_timeout_seconds",
                "voicehub_base_url",
                "voicehub_token",
            ):
                value = getattr(raw, "get", None)
                if callable(value):
                    data[key] = raw.get(key)

        token = str(data.get("webhook_token") or "").strip()
        voicehub_token = str(data.get("voicehub_token") or "").strip() or token

        return cls(
            webhook_token=token,
            listen_host=str(data.get("listen_host") or "0.0.0.0").strip() or "0.0.0.0",
            listen_port=_as_int(data.get("listen_port"), DEFAULT_PORT),
            public_base_url=str(data.get("public_base_url") or "").strip().rstrip("/"),
            allowed_ips=_as_list(data.get("allowed_ips")),
            message_prefix=str(data.get("message_prefix") or "").strip(),
            include_url=_as_bool(data.get("include_url"), True),

            group_umos=_as_umo_list(data.get("group_umos"), "GroupMessage"),

            request_timeout_seconds=_as_int(data.get("request_timeout_seconds"), DEFAULT_TIMEOUT),
            voicehub_base_url=str(data.get("voicehub_base_url") or "").strip().rstrip("/"),
            voicehub_token=voicehub_token,
        )

    @property
    def display_endpoint(self) -> str:
        """对外展示的推送地址，供管理员在 VoiceHub 中填写。"""
        if self.public_base_url:
            return f"{self.public_base_url}/voicehub/push"
        return f"http://{self.listen_host}:{self.listen_port}/voicehub/push"
