"""插件配置读取。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

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

            group_umos=_as_list(data.get("group_umos")),

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
