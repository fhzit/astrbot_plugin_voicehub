"""VoiceHub 侧接口契约常量：插件与 VoiceHub 共用同一份路径定义。"""

# VoiceHub 调用插件的入站路径
PUSH_PATH = "/voicehub/push"
HEALTH_PATH = "/voicehub/health"

# 插件回查 VoiceHub 的路径
BIND_PATH = "/api/bot/voicehub/bind"
UNBIND_PATH = "/api/bot/voicehub/unbind"
VERIFY_TARGETS_PATH = "/api/bot/voicehub/verify-targets"

# 拉取模式：插件主动取件与回报投递结果（VoiceHub 无需访问插件）
PULL_PATH = "/api/bot/voicehub/pull"
ACK_PATH = "/api/bot/voicehub/ack"

# 点歌：插件回查 VoiceHub 的搜索与投稿接口
SONG_SEARCH_PATH = "/api/bot/voicehub/song-search"
SONG_REQUEST_PATH = "/api/bot/voicehub/song-request"

# 播出时段列表：站点既有的公开接口（与站点前端 RequestForm 同源）
PLAY_TIMES_PATH = "/api/play-times"

# 本周排期图片
WEEKLY_SCHEDULE_PATH = "/api/bot/voicehub/weekly-schedule"

# 双方约定的请求头
TOKEN_HEADER = "X-VoiceHub-Token"
