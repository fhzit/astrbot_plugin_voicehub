"""VoiceHub 侧接口契约常量：插件与 VoiceHub 共用同一份路径定义。"""

# VoiceHub 调用插件的入站路径
PUSH_PATH = "/voicehub/push"
HEALTH_PATH = "/voicehub/health"

# 插件回查 VoiceHub 的路径
BIND_PATH = "/api/bot/voicehub/bind"
UNBIND_PATH = "/api/bot/voicehub/unbind"
VERIFY_TARGETS_PATH = "/api/bot/voicehub/verify-targets"

# 双方约定的请求头
TOKEN_HEADER = "X-VoiceHub-Token"
