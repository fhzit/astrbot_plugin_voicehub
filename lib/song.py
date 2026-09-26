"""点歌指令的服务端交互层：搜索、投稿与会话状态。

点歌是**私聊专用**的交互式流程：用户先搜索拿到候选列表（序号与密封票据都由
VoiceHub 返回），再按序号投稿。插件侧只保留每个会话最近一次搜索的结果，供
随后的 `/广播 选歌` 使用；票据本身由 VoiceHub 密封，插件无法伪造候选。

会话状态只存在内存（AstrBot 重启即失效），与插件「不落盘业务状态」的定位一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

from .config import VoiceHubConfig
from .contract import PLAY_TIMES_PATH, SONG_REQUEST_PATH, SONG_SEARCH_PATH, TOKEN_HEADER

# 会话有效期与投稿冷却（逐字要求：10 分钟 / 5 秒）
SONG_SESSION_TTL_SECONDS = 600
PICK_COOLDOWN_SECONDS = 5.0

# 结果条数默认 5，上限 5（聊天里刷屏没有意义，VoiceHub 也只回前 5 条）
DEFAULT_RESULT_COUNT = 5
MAX_RESULT_COUNT = 5
MIN_RESULT_COUNT = 1

# 音源平台的中文名（逐字）
PLATFORM_NAMES: Dict[str, str] = {
    "netease": "网易云音乐",
    "tencent": "QQ音乐",
    "migu": "咪咕音乐",
    "bilibili": "哔哩哔哩",
}

DEFAULT_PLATFORM = "netease"

# `/广播 点歌 <关键词>` 的保留字：查到时段列表而不是搜索歌曲
PLAY_TIME_KEYWORD = "时段"

# 用户可见文案（逐字，除标注为补充的几条外均由实现规格规定）
TEXT_GROUP_ONLY = "为避免刷屏，请在机器人私聊中点歌。"
TEXT_NO_SESSION = "请先用「/广播 点歌 关键词」搜索歌曲。"
TEXT_COOLDOWN = "点歌太频繁，请稍后再试。"
TEXT_NO_BASE_URL = "插件未配置 VoiceHub 站点地址，无法点歌。"
TEXT_NETWORK = "无法连接 VoiceHub，请稍后重试。"
TEXT_FAILED = "点歌失败，请稍后重试。"
TEXT_NO_PLAY_TIME = "当前未开放播出时段选择。"
TEXT_INDEX_INVALID = "序号必须是 1-5 之间的整数。"

# `/广播 选歌` 的参数键名（中文为主，英文旧键仅作兼容别名）
PICK_PLAY_TIME_KEYS = frozenset({"时段", "time"})
PICK_CARD_KEYS = frozenset({"点歌券", "card"})

# 以下几条为规格未覆盖处的补充文案（见 README「点歌」一节）
TEXT_SONG_DISABLED = "点歌功能未启用。"
TEXT_USAGE_SONG = "用法：/广播 点歌 <关键词>（发送 /广播 点歌 时段 查看播出时段）。"
TEXT_USAGE_PICK = "用法：/广播 选歌 <序号>。"
TEXT_PICK_INVALID = "点歌参数不正确。用法：/广播 选歌 <序号> [时段=时段序号] [点歌券=券码]。"
TEXT_PLAY_TIME_MISSING = "找不到该播出时段，请先发送「/广播 点歌 时段」查看。"

MAX_KEYWORD_LENGTH = 100
MAX_CARD_LENGTH = 100


@dataclass
class SongCandidate:
    """一首待选歌曲。"""

    index: int
    title: str
    artist: str
    duration_seconds: Optional[int] = None


@dataclass
class PlayTime:
    """一个可选播出时段。"""

    id: int
    name: str
    start_time: str = ""
    end_time: str = ""


@dataclass
class SongSearchOutcome:
    """搜索调用结果。"""

    ok: bool = False
    message: str = ""
    session_token: str = ""
    platform: str = DEFAULT_PLATFORM
    keyword: str = ""
    candidates: List[SongCandidate] = field(default_factory=list)


@dataclass
class SongRequestOutcome:
    """投稿调用结果。"""

    ok: bool = False
    message: str = ""
    song_id: Optional[int] = None


@dataclass
class PlayTimeOutcome:
    """播出时段查询结果。"""

    ok: bool = False
    message: str = ""
    enabled: bool = False
    play_times: List[PlayTime] = field(default_factory=list)


@dataclass
class PickArgs:
    """`/vh pick` 的解析结果。"""

    index: int
    play_time_index: Optional[int] = None
    card_code: Optional[str] = None


@dataclass
class SongSession:
    """某个会话最近一次搜索的待选状态。"""

    session_token: str
    platform: str
    keyword: str
    candidates: List[SongCandidate]
    created_at: float
    play_times: List[PlayTime] = field(default_factory=list)


# ----------------------------------------------------------------------
# 纯函数：解析与渲染
# ----------------------------------------------------------------------


def format_duration(seconds: Any) -> str:
    """把秒数渲染为 mm:ss；取值缺失或非法时返回「未知时长」。

    Args:
        seconds: VoiceHub 返回的时长（秒），可能为 None。

    Returns:
        补零到两位分钟的 `mm:ss`，或 `未知时长`。
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "未知时长"
    total = int(round(seconds))
    if total < 0:
        return "未知时长"
    return f"{total // 60:02d}:{total % 60:02d}"


def format_platform(platform: Any) -> str:
    """把音源键渲染为中文名；未知取值原样返回。"""
    name = str(platform or "").strip()
    return PLATFORM_NAMES.get(name, name)


def render_song_list(keyword: str, platform: str, candidates: List[SongCandidate]) -> str:
    """渲染搜索结果。

    Args:
        keyword: 用户搜索的关键词。
        platform: 音源键（`netease` 等）。
        candidates: 待选歌曲（已按 index 排序）。

    Returns:
        逐字格式的列表文案。
    """
    lines = [f"点歌搜索：{keyword}（音源：{format_platform(platform)}）"]
    for candidate in candidates:
        lines.append(
            f"{candidate.index}. {candidate.title} - {candidate.artist}"
            f"（{format_duration(candidate.duration_seconds)}）"
        )
    lines.append("回复「/广播 选歌 序号」完成点歌。")
    return "\n".join(lines)


def render_play_times(play_times: List[PlayTime]) -> str:
    """渲染播出时段列表（编号即 `/广播 选歌 序号 时段=N` 里的 N）。"""
    lines = ["可选播出时段："]
    for number, play_time in enumerate(play_times, start=1):
        lines.append(f"{number}. {play_time.name}（{_format_range(play_time)}）")
    lines.append("回复「/广播 选歌 序号 时段=时段序号」选择时段。")
    return "\n".join(lines)


def _format_range(play_time: PlayTime) -> str:
    """时段范围展示，与站点 `formatPlayTimeRange` 保持一致。"""
    start = (play_time.start_time or "").strip()
    end = (play_time.end_time or "").strip()
    if start and end:
        return f"{start}-{end}"
    return start or end


def parse_pick_args(text: Any) -> Tuple[Optional[PickArgs], str]:
    """解析 `/广播 选歌` 的参数。

    参数键名以中文为准（`时段=` 播出时段序号、`点歌券=` 券码），同时兼容英文旧键
    `time=` / `card=`。两键可任意顺序出现。

    Args:
        text: 指令参数原文，可含 `/广播 选歌` 或 `/vh pick` 前缀。

    Returns:
        `(PickArgs, "")`，或 `(None, 错误文案)`。
    """
    raw = str(text or "").strip()
    tokens = raw.split()
    # 容忍参数里带上指令名本身（中文指令名或旧英文名）
    while tokens and tokens[0] in {"广播", "vh", "/广播", "/vh", "选歌", "pick"}:
        tokens.pop(0)
    if not tokens:
        return None, TEXT_INDEX_INVALID

    index = _parse_index(tokens[0])
    if index is None:
        return None, TEXT_INDEX_INVALID

    play_time_index: Optional[int] = None
    card_code: Optional[str] = None
    for token in tokens[1:]:
        if "=" not in token:
            return None, TEXT_PICK_INVALID
        key, _, value = token.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key in PICK_PLAY_TIME_KEYS:
            parsed = _parse_index(value)
            if parsed is None:
                return None, TEXT_PICK_INVALID
            play_time_index = parsed
        elif key in PICK_CARD_KEYS:
            if not value or len(value) > MAX_CARD_LENGTH:
                return None, TEXT_PICK_INVALID
            # 与站点 RequestForm 一致：券码统一大写后再提交
            card_code = value.upper()
        else:
            return None, TEXT_PICK_INVALID

    return PickArgs(index=index, play_time_index=play_time_index, card_code=card_code), ""


def _parse_index(value: Any) -> Optional[int]:
    """把 1..5 的十进制序号解析为整数；其余一律视为非法。"""
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    if number < 1 or number > MAX_RESULT_COUNT:
        return None
    return number


# ----------------------------------------------------------------------
# HTTP 客户端
# ----------------------------------------------------------------------


class VoiceHubSongClient:
    """调用 VoiceHub 的点歌接口（搜索 / 投稿 / 播出时段）。"""

    def __init__(self, config: VoiceHubConfig):
        self.config = config

    @property
    def timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            TOKEN_HEADER: self.config.voicehub_token,
        }

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
        """提取站点错误文案：优先 message，其次 statusMessage，否则兜底。

        `status` 只用于兜底判断，绝不回显给用户，避免把内部状态码写进回复。
        """
        del status  # 兜底文案与状态码无关，保留参数以统一调用形态
        for key in ("message", "statusMessage"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return TEXT_FAILED

    async def _post(self, path: str, payload: dict, error_factory: Callable[[str], Any]) -> Any:
        """向 VoiceHub 发起一次带令牌的 POST。

        Args:
            path: 相对 VoiceHub 站点的接口路径。
            payload: JSON 请求体。
            error_factory: 由失败文案构造结果对象的回调。

        Returns:
            解析后的结果对象；网络异常与超时都收敛为失败结果。
        """
        if not self.config.voicehub_base_url:
            return error_factory(TEXT_NO_BASE_URL)

        url = f"{self.config.voicehub_base_url}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    url, json=payload, headers=self._headers(), allow_redirects=False
                ) as response:
                    if 300 <= response.status < 400:
                        # 不跟随重定向，也不把令牌带到别处；对外只给统一失败文案
                        return error_factory(TEXT_FAILED)
                    body = await self._read_json(response)
                    if response.status >= 400:
                        return error_factory(self._error_message(body, response.status))
                    return body
        except (aiohttp.ClientError, TimeoutError, OSError):
            return error_factory(TEXT_NETWORK)
        except Exception:  # noqa: BLE001 - 插件侧不得因点歌失败抛异常
            return error_factory(TEXT_FAILED)

    async def search(
        self, umo: str, keyword: str, platform: str = DEFAULT_PLATFORM, page: int = 1
    ) -> SongSearchOutcome:
        """搜索候选歌曲。

        Args:
            umo: 当前私聊会话的 unified_msg_origin。
            keyword: 搜索关键词。
            platform: 音源平台键。
            page: 页码，默认 1。

        Returns:
            归一化的搜索结果；失败时 `ok` 为 False 且 `message` 为可展示文案。
        """
        payload = {
            "umo": umo,
            "keyword": str(keyword or "").strip(),
            "platform": platform or DEFAULT_PLATFORM,
            "page": page,
        }
        body = await self._post(
            SONG_SEARCH_PATH, payload, lambda message: SongSearchOutcome(ok=False, message=message)
        )
        if isinstance(body, SongSearchOutcome):
            return body
        if body.get("success") is not True:
            return SongSearchOutcome(ok=False, message=self._error_message(body, 200))

        return SongSearchOutcome(
            ok=True,
            message="",
            session_token=str(body.get("sessionToken") or ""),
            platform=str(body.get("platform") or payload["platform"]),
            keyword=str(body.get("keyword") or payload["keyword"]),
            candidates=parse_candidates(body.get("items")),
        )

    async def request_song(
        self,
        umo: str,
        session_token: str,
        index: int,
        play_time_id: Optional[int] = None,
        card_code: Optional[str] = None,
        note: Optional[str] = None,
    ) -> SongRequestOutcome:
        """按序号投稿。

        Args:
            umo: 当前私聊会话的 unified_msg_origin。
            session_token: 搜索接口返回的密封票据。
            index: 候选序号（1..5）。
            play_time_id: 播出时段的**真实 id**（不是展示序号）。
            card_code: 点歌券码，提交前统一大写。
            note: 附言。

        Returns:
            归一化的投稿结果。
        """
        payload: Dict[str, Any] = {
            "umo": umo,
            "sessionToken": session_token,
            "index": index,
        }
        if play_time_id is not None:
            payload["playTimeId"] = play_time_id
        if card_code:
            # 与站点 RequestForm.vue 提交前大写化保持一致
            payload["cardCode"] = card_code.strip().upper()
        if note:
            payload["note"] = str(note).strip()

        body = await self._post(
            SONG_REQUEST_PATH, payload, lambda message: SongRequestOutcome(ok=False, message=message)
        )
        if isinstance(body, SongRequestOutcome):
            return body
        if body.get("success") is not True:
            return SongRequestOutcome(ok=False, message=self._error_message(body, 200))

        song_id = body.get("songId")
        return SongRequestOutcome(
            ok=True,
            message=str(body.get("message") or ""),
            song_id=song_id if isinstance(song_id, int) and not isinstance(song_id, bool) else None,
        )

    async def fetch_play_times(self) -> PlayTimeOutcome:
        """读取站点开放的播出时段。

        VoiceHub 的机器人回调没有对应的取时段端点，这里复用站点既有公开接口
        `GET /api/play-times`（与站点前端 RequestForm 同源）。

        Returns:
            归一化的时段列表结果。
        """
        if not self.config.voicehub_base_url:
            return PlayTimeOutcome(ok=False, message=TEXT_NO_BASE_URL)

        url = f"{self.config.voicehub_base_url}{PLAY_TIMES_PATH}"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(
                    url, headers=self._headers(), allow_redirects=False
                ) as response:
                    if response.status != 200:
                        body = await self._read_json(response)
                        if response.status >= 400:
                            return PlayTimeOutcome(
                                ok=False, message=self._error_message(body, response.status)
                            )
                        return PlayTimeOutcome(ok=False, message=TEXT_FAILED)
                    body = await self._read_json(response)
        except (aiohttp.ClientError, TimeoutError, OSError):
            return PlayTimeOutcome(ok=False, message=TEXT_NETWORK)
        except Exception:  # noqa: BLE001 - 时段读取失败不得影响投稿链路
            return PlayTimeOutcome(ok=False, message=TEXT_FAILED)

        if body.get("success") is False:
            return PlayTimeOutcome(ok=False, message=self._error_message(body, 200))

        enabled = body.get("enabled") is True
        play_times = parse_play_times(body.get("playTimes")) if enabled else []
        return PlayTimeOutcome(ok=True, message="", enabled=enabled, play_times=play_times)


def parse_candidates(raw_items: Any) -> List[SongCandidate]:
    """解析搜索响应的候选列表，丢弃形状非法的条目。

    Args:
        raw_items: 响应里的 `items` 取值。

    Returns:
        序号合法且标题非空的候选列表。
    """
    if not isinstance(raw_items, list):
        return []
    candidates: List[SongCandidate] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        title = raw.get("title")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            continue
        if not isinstance(title, str) or not title:
            continue
        artist = raw.get("artist")
        duration = raw.get("durationSeconds")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            duration = None
        candidates.append(SongCandidate(
            index=index,
            title=title,
            artist=artist if isinstance(artist, str) else "",
            duration_seconds=int(round(duration)) if duration is not None else None,
        ))
    return candidates


def parse_play_times(raw_items: Any) -> List[PlayTime]:
    """解析播出时段列表，丢弃形状非法的条目。"""
    if not isinstance(raw_items, list):
        return []
    play_times: List[PlayTime] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        time_id = raw.get("id")
        if isinstance(time_id, bool) or not isinstance(time_id, int) or time_id <= 0:
            continue
        name = raw.get("name")
        play_times.append(PlayTime(
            id=time_id,
            name=name if isinstance(name, str) and name else f"时段 {time_id}",
            start_time=raw.get("startTime") if isinstance(raw.get("startTime"), str) else "",
            end_time=raw.get("endTime") if isinstance(raw.get("endTime"), str) else "",
        ))
    return play_times


# ----------------------------------------------------------------------
# 会话状态与指令流程
# ----------------------------------------------------------------------


class SongService:
    """把 `/vh song`、`/vh song time`、`/vh pick` 映射为 VoiceHub 调用。"""

    def __init__(
        self,
        config: VoiceHubConfig,
        client: Optional[Any] = None,
        now: Optional[Callable[[], float]] = None,
    ):
        self.config = config
        self.client = client or VoiceHubSongClient(config)
        self._now = now or time.monotonic
        self._sessions: Dict[str, SongSession] = {}
        self._last_pick: Dict[str, float] = {}

    # -- 会话状态 ------------------------------------------------------

    def session(self, umo: str) -> Optional[SongSession]:
        """返回未过期的待选会话；过期即清理。"""
        session = self._sessions.get(umo)
        if session is None:
            return None
        if self._now() - session.created_at > SONG_SESSION_TTL_SECONDS:
            self._sessions.pop(umo, None)
            return None
        return session

    # -- 指令入口 ------------------------------------------------------

    async def song(self, umo: str, group_id: str, keyword: str) -> str:
        """`/广播 点歌 <关键词>`（关键词为保留字 `时段` 时改为查看播出时段）。"""
        refusal = self._guard(umo, group_id)
        if refusal:
            return refusal

        text = str(keyword or "").strip()
        if text in {PLAY_TIME_KEYWORD, "time"}:
            return await self.song_time(umo, group_id)
        if not text or len(text) > MAX_KEYWORD_LENGTH:
            return TEXT_USAGE_SONG

        outcome = await self._call(self.client.search(umo, text))
        if outcome is None:
            return TEXT_NETWORK
        if not outcome.ok:
            return outcome.message or TEXT_FAILED
        if not outcome.candidates:
            return f"没有找到「{outcome.keyword}」的歌曲，换个关键词试试。"

        candidates = outcome.candidates[: self.config.song_result_count]
        self._sessions[umo] = SongSession(
            session_token=outcome.session_token,
            platform=outcome.platform,
            keyword=outcome.keyword,
            candidates=candidates,
            created_at=self._now(),
        )
        return render_song_list(outcome.keyword, outcome.platform, candidates)

    async def song_time(self, umo: str, group_id: str) -> str:
        """`/vh song time`：列出可选播出时段。"""
        refusal = self._guard(umo, group_id)
        if refusal:
            return refusal

        outcome = await self._call(self.client.fetch_play_times())
        if outcome is None:
            return TEXT_NETWORK
        if not outcome.ok:
            return outcome.message or TEXT_FAILED
        if not outcome.enabled or not outcome.play_times:
            return TEXT_NO_PLAY_TIME

        session = self.session(umo)
        if session is not None:
            session.play_times = outcome.play_times
        return render_play_times(outcome.play_times)

    async def pick(self, umo: str, group_id: str, raw_args: str) -> str:
        """`/广播 选歌 <序号> [时段=时段序号] [点歌券=券码]`。"""
        refusal = self._guard(umo, group_id)
        if refusal:
            return refusal

        args, error = parse_pick_args(raw_args)
        if args is None:
            return error or TEXT_USAGE_PICK

        session = self.session(umo)
        if session is None:
            return TEXT_NO_SESSION

        if self._cooling_down(umo):
            return TEXT_COOLDOWN

        play_time_id: Optional[int] = None
        if args.play_time_index is not None:
            play_time_id = await self._resolve_play_time(umo, session, args.play_time_index)
            if play_time_id is None:
                return TEXT_PLAY_TIME_MISSING

        # 通过全部前置校验后才记录投稿时刻：被拒绝的尝试不应延长冷却。
        self._last_pick[umo] = self._now()

        outcome = await self._call(self.client.request_song(
            umo,
            session.session_token,
            args.index,
            play_time_id=play_time_id,
            card_code=args.card_code,
        ))
        if outcome is None:
            return TEXT_NETWORK
        if not outcome.ok:
            return outcome.message or TEXT_FAILED
        return outcome.message or TEXT_FAILED

    # -- 内部 ----------------------------------------------------------

    def _guard(self, umo: str, group_id: str) -> str:
        """私聊与会话可用性的前置校验，返回非空表示应直接回复该文案。"""
        del umo  # 会话标识只用于状态索引，校验阶段无需读取
        if group_id:
            return TEXT_GROUP_ONLY
        if not self.config.song_enabled:
            return TEXT_SONG_DISABLED
        if not self.config.voicehub_base_url:
            return TEXT_NO_BASE_URL
        return ""

    def _cooling_down(self, umo: str) -> bool:
        """同一会话两次投稿之间的最小间隔。"""
        last = self._last_pick.get(umo)
        return last is not None and (self._now() - last) < PICK_COOLDOWN_SECONDS

    async def _resolve_play_time(
        self, umo: str, session: SongSession, play_time_index: int
    ) -> Optional[int]:
        """把展示序号换算为时段的真实 id；序号非法或时段不可用时返回 None。"""
        if not session.play_times:
            outcome = await self.client.fetch_play_times()
            if outcome is None or not outcome.ok or not outcome.play_times:
                return None
            session.play_times = outcome.play_times
        if play_time_index < 1 or play_time_index > len(session.play_times):
            return None
        return session.play_times[play_time_index - 1].id

    @staticmethod
    async def _call(coroutine: Any) -> Any:
        """等待一次上游调用，把网络异常收敛为 None（由调用方给出固定文案）。

        上游返回的失败结果（含站点文案）原样返回；只有异常才映射为 None，
        这样站点给出的中文错误文案不会被插件覆盖。
        """
        try:
            return await coroutine
        except (aiohttp.ClientError, TimeoutError, OSError):
            return None
