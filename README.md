# AstrBot VoiceHub 推送插件

VoiceHub 通过独立 HTTP 端口把通知交给常驻 AstrBot；插件使用 AstrBot 的 `context.send_message` 向已绑定的私聊会话及管理员配置的群会话发送纯文本通知。支持具备主动发送能力的适配器（例如 OneBot、QQ 官方机器人、企业微信 AI 机器人、飞书、钉钉）；实际送达还取决于各适配器的配置和平台限制。VoiceHub 可以部署在 serverless 环境：它只需向 AstrBot 发起出站 HTTPS 请求，AstrBot 的插件端口仍需可从 VoiceHub 访问。

## 安装与配置

将本目录放入 AstrBot 插件目录，安装 `requirements.txt`，在 AstrBot 插件配置中填写：

- `webhook_token`：非空、高熵共享密钥，VoiceHub 调用推送接口时使用；缺失则 HTTP 服务不启动。
- `listen_host` / `listen_port`：默认 `0.0.0.0:6199`；只经本机反向代理访问时设 `127.0.0.1`。
- `public_base_url`：对外展示的 HTTPS 地址（不带 `/voicehub/push`）；与监听地址可不同。生产环境使用 TLS 反向代理，不要在公网明文传输 token。
- `allowed_ips`：可选逗号分隔的 IP 地址；只匹配实际 TCP 对端。反向代理部署时填代理地址，并在代理处单独实施上游 IP 白名单；`X-Forwarded-For` 不被插件信任。
- `voicehub_base_url`：VoiceHub 站点地址，绑定/解绑必须配置；`voicehub_token` 留空则复用 `webhook_token`。VoiceHub 必须支持下面的回调路径并验证同一密钥。
- `group_umos`：管理员指定的群广播会话 UMO，逗号分隔；在对应群里执行 `/vh status` 取得。用户不能通过绑定指令选择群，绑定指令始终只允许私聊。

**绑定码的生成、过期和一次性消费由 VoiceHub 实现**，插件不保存码或绑定状态。

## HTTP 契约

所有请求均携带 `X-VoiceHub-Token: <webhook_token>`。仅接受 TCP 对端符合可选的 IP 白名单。请求体最大 64 KiB，一次最多 200 个目标。

- `POST /voicehub/push`：`{"title":"标题","content":"正文","url":"https://...","targets":{"umo":["adapter:FriendMessage:session"]}}`。`content` 必填；`title`、`url` 可选。VoiceHub 从其绑定表取出用户私聊 UMO，不应允许终端用户指定任意 UMO。群广播使用 `{"targets":{"group":true}}`，只发送到插件管理员配置的 `group_umos`；也可把已配置的群 UMO 放在 `umo` 数组中。`targets` 缺失时默认群广播。无可用目标返回 400。响应为 `{"success":true,"sent":1,"failed":[]}`；每目标失败项含 `umo`、`reason`。若部分失败，`success` 表示至少一个成功，调用方须检查 `failed`。
- `GET /voicehub/health`：同样要求 token/IP 白名单；返回平台列表与群目标数。
- 插件向 VoiceHub `POST /api/bot/voicehub/bind`：`{"code":"一次性绑定码","umo":"adapter:FriendMessage:session","platform":"adapter"}`。
- 插件向 VoiceHub `POST /api/bot/voicehub/unbind`：`{"umo":"adapter:FriendMessage:session"}`。

VoiceHub 回调应明确返回 `{"success":true,"username":"可选名称"}` 或 `{"success":false,"message":"原因"}`；缺失 `success:true` 的响应按失败处理。回调使用 `X-VoiceHub-Token: <voicehub_token>`（默认同推送令牌）。VoiceHub 应验证令牌、一次性码的有效期、私聊归属与重绑规则。插件只传递会话标识，不存储账户绑定关系。

## 聊天指令

- `/vh bind <绑定码>`：默认只允许私聊；由 AstrBot 指令过滤器解析一个绑定码。
- `/vh unbind`：解绑当前会话。
- `/vh status`：显示平台、会话 UMO、接口状态。
- `/vh test`：向当前会话发送测试通知。

指令唤醒前缀由 AstrBot 配置控制，实际消息须符合其唤醒规则。不要在群内公开一次性绑定码。适配器返回成功只说明 AstrBot 找到了对应平台实例，不保证第三方平台最终送达。

## 本地测试

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt pytest
.venv/bin/python -m pytest tests -q
```

测试使用本机 aiohttp HTTP 服务与桩 AstrBot 事件，无需运行真实聊天平台。未覆盖 VoiceHub 端实现或真实适配器端到端送达。
