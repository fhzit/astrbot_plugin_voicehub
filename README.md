# AstrBot VoiceHub 推送插件

把 [VoiceHub](https://github.com/laoshuikaixue/VoiceHub) 的通知投递到 AstrBot 已加载的聊天平台会话。

VoiceHub 常驻在服务器上，本插件作为它与聊天平台之间的**出站网关**：VoiceHub 通过插件的独立 HTTP 端口把通知交给 AstrBot，插件再用 AstrBot 的 `context.send_message` 发到指定会话。插件不保存任何业务状态（用户与会话的绑定关系全部由 VoiceHub 维护）。

- 插件仓库：<https://github.com/fhzit/astrbot_plugin_voicehub>
- 适配器：`aiocqhttp`（OneBot v11）、`qq_official` / `qq_official_webhook`、`wecom_ai_bot`、`lark`、`dingtalk`
- 开发与测试基于 AstrBot `4.28.x`；Python 依赖仅 `aiohttp`

## 特性

- **出站/入站分离**：插件在 AstrBot 进程内监听一个独立端口接收推送；插件自身不发起定时任务或长连接。
- **令牌 + IP 白名单**：入站请求必须携带共享令牌（常量时间比较），可选按真实 TCP 对端做 IP 白名单。
- **显式目标，不做隐式群发**：请求必须写明目标会话，缺失 `targets` 直接拒绝，避免把通知洒进机器人所在的所有群。
- **私聊目标需回查**：私聊会话在推送前向 VoiceHub 逐个核对**当前有效绑定**，核对不通过则整批拒绝（含其中的群目标），防止把通知推给已解绑的用户。
- **群目标需管理员授权**：群会话只能来自插件配置中管理员显式填写的 `group_umos`，用户无法通过任何指令自行选择群。
- **不跟随重定向**：回查/绑定/解绑请求一律拒绝 3xx，避免共享令牌被转发到重定向目的地。
- **一次性绑定码**：绑定码的生成、过期与一次性消费全部在 VoiceHub 侧实现，插件只做传递。
- **`/vh test` 自检**：不依赖 VoiceHub 也能验证当前会话能否收到推送。

## 工作方式

```
                    ┌──────────────────────────────────────────┐
                    │ AstrBot 进程                              │
                    │                                          │
  VoiceHub  ──POST──▶  插件入站 HTTP  :6199                     │
  (出站 HTTPS)          /voicehub/push   /voicehub/health       │
                    │        │                                 │
                    │        ├─ 令牌 + IP 白名单校验             │
                    │        ├─ 解析 targets / 形状校验          │
                    │        ├─ 私聊目标 ──POST──▶ VoiceHub 回查 │
                    │        └─ context.send_message ──▶ 适配器  │
                    │                                    │     │
                    └────────────────────────────────────┼─────┘
                                                         ▼
                                        QQ / 企业微信 / 飞书 / 钉钉 …
```

反向的绑定流程：

```
用户在 VoiceHub 生成绑定码
        │
        ├─ 用户在机器人私聊发送:  /vh bind <绑定码>
        │
        └─ 插件 ──POST /api/bot/voicehub/bind──▶ VoiceHub 校验并保存 UMO
```

因此 **VoiceHub 必须能访问插件的监听端口**，**插件必须能访问 VoiceHub 站点**，两个方向都要放行。

### 两种推送方向

上面的入站方式要求 VoiceHub 能访问插件。如果插件跑在内网 / NAT 之后（家宽、只出不进的容器网络、只开了出站白名单的服务器），VoiceHub 打不进来，改用**拉取模式**：

```
push 模式（默认）           pull 模式（pull_interval_seconds > 0）
VoiceHub ──POST──▶ 插件     插件 ──POST /api/bot/voicehub/pull──▶ VoiceHub 取件
                           插件 ──POST /api/bot/voicehub/ack───▶ VoiceHub 回执
插件必须可达                 插件只需能出站访问 VoiceHub
```

拉取模式下：

- 插件**不再开放入站端口**（`listen_host` / `listen_port` / `allowed_ips` 均不生效）；
- 通知先入 VoiceHub 的待投递队列，插件每 `pull_interval_seconds` 秒取一批投递并回报结果；
- 领取带租约，插件崩溃或重启后未回执的通知会被重新领取，不会丢；
- 单条通知投递失败会回报 VoiceHub 重试，超过 3 次后标记失败停止重试；
- **VoiceHub 后台的「推送方向」必须同时选 `pull`**，两边设置要一致，否则 VoiceHub 仍会尝试直连插件。

`group_umos` 仅在插件侧生效，且**只对历史遗留的广播条目有效**：VoiceHub 的四平台独立开关下已无法按平台校验群接收目标，因此 VoiceHub 不再发起群广播。当前插件保留对 `broadcast` 条目的处理，仅用于兼容旧队列数据。

## 快速开始

### 1. 安装插件

将本目录放到 AstrBot 的插件目录：

```sh
cd AstrBot/data/plugins
git clone https://github.com/fhzit/astrbot_plugin_voicehub.git
```

也可以用 AstrBot 管理面板的 `插件市场` / `安装插件`（支持仓库地址或上传 zip）安装，然后在面板中重载插件。

依赖会在插件加载时按 `requirements.txt` 安装；也可以手动装：

```sh
AstrBot/.venv/bin/pip install -r astrbot_plugin_voicehub/requirements.txt
```

### 2. 配置插件

在 AstrBot 管理面板 → `插件` → `VoiceHub 推送` 中填写。**最小可用配置只有一项：`webhook_token`。**

- `webhook_token`（必填）：高熵共享密钥，与 VoiceHub 后台「机器人推送」里的令牌完全一致。留空时插件不会启动 HTTP 服务（会记录错误日志）。
- `listen_host` / `listen_port`：默认 `0.0.0.0:6199`。VoiceHub 与 AstrBot 同机时建议填 `127.0.0.1`；跨机部署必须让防火墙/反向代理放行该端口。
- `public_base_url`：对外展示的 HTTPS 地址（不带 `/voicehub/push`），仅用于在 `/vh status` 和日志里显示可填写的推送地址。
- `allowed_ips`：可选，逗号分隔。**只匹配实际 TCP 对端**，不信任 `X-Forwarded-For`；反向代理部署时填代理地址，并在代理层另行实施上游 IP 白名单。
- `message_prefix`：可选，推送时单独占一行，位于标题和正文之前，例如 `校园广播站`。
- `include_url`：默认开启。关闭后只推送标题与正文，不带 VoiceHub 站点链接。
- `voicehub_base_url`：VoiceHub 站点地址，例如 `https://voicehub.example.com`。**绑定/解绑/私聊回查都依赖它**；留空则绑定指令不可用（推送仍可用）。
- `voicehub_token`：回查 VoiceHub 用的令牌，留空复用 `webhook_token`（一般无需单独填写）。
- `pull_interval_seconds`：**拉取模式**开关，默认 `0`（关闭）。填大于 0 的值（建议 15–60）即启用，见下节。
- `group_umos`：群广播目标，逗号或换行分隔，见下节。
- `request_timeout_seconds`：回调 VoiceHub 的超时，默认 15 秒。

生产环境请用 TLS 反向代理，不要在公网明文传输令牌。

### 3. 在 VoiceHub 后台启用机器人推送

管理员 → 站点设置 → `AstrBot 通知配置`：

1. 勾选 **启用机器人通知总开关**；
2. 在 **启用 QQ / 企业微信 / 钉钉 / 飞书** 中勾选要开放的平台，勾选后用户账号页才会出现对应平台的绑定卡片；
3. **推送方向**按部署方式选：VoiceHub 能访问插件用 `push`（默认）；插件在内网/NAT 后用 `pull`，并与插件侧 `pull_interval_seconds` 配合；
4. `AstrBot 服务地址` 填插件的对外地址，例如 `https://astrbot.example.com:6199`。**`pull` 模式下可留空**（只作展示，不参与投递；地址仅在 `push` 模式下必填）；
5. `访问令牌` 填与插件 `webhook_token` **完全相同**的值（页面不会回显已有密钥，留空表示保持不变）；
6. 保存。

通知只投递到**用户自己已绑定并启用**的私聊会话；群广播在四平台独立开关下已停用（无法按平台校验群接收目标）。用户侧在 `账号设置 → 账号绑定` 里为每个已启用平台分别生成一次性绑定码，然后在**对应平台的机器人私聊**中发送 `/vh bind <绑定码>` 完成绑定。

### 4. 配置群广播目标（仅历史遗留兼容）

VoiceHub 已不再发起群广播：四平台独立开关下无法按平台校验群接收目标。`group_umos` 现在只对**旧队列里已存在的广播条目**生效，用于兼容升级前入队、尚未投递的数据。配置方式（如需人工排查历史条目）：

1. 把机器人拉进目标群；
2. 在群里发送 `/vh status`；
3. 复制输出的「会话 ID」整串，填到 `group_umos`。

**格式必须是 `平台实例ID:GroupMessage:会话ID`**，例如：

```
default:GroupMessage:123456789
```

> 前缀是 AstrBot 中该平台的**实例 ID**（配置里的 `id`，默认常为 `default`），**不是适配器类型名**。别手写 `aiocqhttp:GroupMessage:...` —— 除非你确实把实例 ID 改成了 `aiocqhttp`。用 `/vh status` 取到的整串最保险。

形状不合法的条目（漏写冒号、会话类型写成 `FriendMessage` 等）会在加载时被丢弃，并在启动日志中列出，不会进入推送链路 —— 群广播收不到时先看这条日志。

## 聊天指令

| 指令 | 说明 |
| --- | --- |
| `/vh bind <绑定码>` | 绑定当前**私聊**会话。群聊中会被拒绝，避免个人通知被推到群里。 |
| `/vh unbind` | 解除当前会话的绑定。 |
| `/vh status` | 显示平台、会话类型、会话 ID（UMO）与服务地址。取值、排查都用它。 |
| `/vh test` | 向当前会话发送一条测试通知，不经过 VoiceHub。 |

指令唤醒前缀由 AstrBot 配置控制（默认 `/`），实际消息需符合唤醒规则。**不要在群里公开一次性绑定码。**

## HTTP 接口契约

所有入站请求携带 `X-VoiceHub-Token: <webhook_token>`；可选 IP 白名单仅匹配真实 TCP 对端。请求体上限 64 KiB，单次最多 200 个目标。

### `POST /voicehub/push`

```json
{
  "title": "点歌已通过",
  "content": "你点的《xxx》已排期，播放时间 12:30。",
  "url": "https://voicehub.example.com/my/songs",
  "targets": { "umo": ["default:FriendMessage:10001"] }
}
```

- `content` 必填；`title`、`url` 可选。
- `targets.umo` 里的 `platform_id` 同样是平台**实例 ID**，请用 `/vh status` 显示的整串 UMO。
- 群广播用 `{"targets": {"group": true}}`，只发送到管理员配置的 `group_umos`；也可以把已配置的群 UMO 直接放进 `umo` 数组。
- 出现在 `umo` 中但不在 `group_umos` 里的群会话会被拒绝（`群会话未被管理员授权`）。
- 响应：`{"success": true, "sent": 1, "failed": []}`；失败项形如 `{"umo": "...", "reason": "..."}`。**部分失败时 `success` 仍为 true，调用方须检查 `failed`。**

状态码：

- `400` 请求体非法 / 缺少 `targets` / 无可推送目标 / 目标形状非法或群会话未授权
- `401` 令牌无效
- `403` 来源 IP 不在白名单内，或私聊目标未通过回查
- `200` 已处理（逐目标结果见 `failed`）

### `GET /voicehub/health`

同样要求令牌与 IP 白名单，供 VoiceHub 的「测试连接」使用：

```json
{ "success": true, "service": "astrbot_plugin_voicehub", "platforms": ["aiocqhttp"], "group_targets": 2 }
```

### 插件回调 VoiceHub（入站方向）

插件向 `voicehub_base_url` 发起，均带 `X-VoiceHub-Token`，均不跟随重定向：

- `POST /api/bot/voicehub/bind`：`{"code": "一次性绑定码", "umo": "...", "platform": "适配器名"}`
- `POST /api/bot/voicehub/unbind`：`{"umo": "..."}`
- `POST /api/bot/voicehub/verify-targets`：`{"umos": ["..."]}`。VoiceHub **必须验证令牌，并且只对当前有效的私聊绑定逐个核对**；全部有效时返回 HTTP 200 `{"success": true, "umos": [...]}`，列表需与请求完全一致（顺序可不同）。插件要求严格的成功标志、完整精确列表和 200，任一不符或不可用时整批拒绝。
- `POST /api/bot/voicehub/pull`：拉取模式取件，`{}`。返回 `{"success": true, "items": [{"id": 1, "title": "...", "content": "...", "url": "...", "umos": ["..."], "broadcast": false}]}`；**每个目标在入队时已确认为有效绑定，VoiceHub 在领取时会再核对绑定归属**（解绑、平台停用或会话易主的目标不会出现在取件结果里），插件不在此处回查。
- `POST /api/bot/voicehub/ack`：拉取模式回执，`{"results": [{"id": 1, "success": true}]}`；失败项可带 `reason`。VoiceHub 据此标记已投递或安排重试（超过尝试上限后停止）。

VoiceHub 回调应返回 `{"success": true, "username": "可选名称"}` 或 `{"success": false, "message": "原因"}`；缺少 `success: true` 按失败处理。令牌校验、绑定码有效期、私聊归属与重绑规则都由 VoiceHub 负责。

## 安全模型

- **令牌**：全链路共用 `webhook_token`（`voicehub_token` 可单独覆盖回查方向的令牌），两侧都必须使用高熵随机值。校验采用常量时间比较。
- **传输**：请确保令牌经 TLS 传输；不要在公网明文暴露 `listen_port`。
- **重定向**：绑定、解绑与目标回查三类请求均设 `allow_redirects=False`，3xx 一律视为失败，避免令牌被转发到第三方。
- **目标授权**：私聊来自 VoiceHub 绑定表，群来自插件管理员配置，插件不接受调用方任意指定的会话。
- **IP 白名单**：只比对 socket 对端地址，`X-Forwarded-For` 可被伪造故不采信。
- **无状态**：插件不落盘任何绑定关系与消息内容。

## 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 日志出现「未配置推送令牌，已跳过启动 HTTP 服务」 | `webhook_token` 为空，填上并重载插件。 |
| 日志出现「HTTP 服务启动失败」 | 端口被占用或地址不可绑定，改 `listen_port` 或 `listen_host`。 |
| VoiceHub 报 401 | 两侧令牌不一致，或反向代理把 `X-VoiceHub-Token` 过滤掉了。 |
| VoiceHub 报 403「私聊目标未获授权」 | 用户未绑定、绑定已失效，或插件没配 `voicehub_base_url`，或回查超时/被重定向。 |
| VoiceHub 报 400「群会话未被管理员授权」 | 该群不在 `group_umos` 中。 |
| 群广播收不到 | VoiceHub 已停用群广播（无法按平台校验群接收目标），只有升级前入队的旧广播条目仍会投递到 `group_umos`；先看启动日志的「形状非法，已忽略」告警，用 `/vh status` 重新取群会话串。 |
| 拉取模式不工作 | 检查 `pull_interval_seconds` > 0、`voicehub_base_url` 与令牌齐全；`/vh status` 的服务行会显示「拉取模式（每 N 秒…）」。 |
| 绑定失败「插件未配置 VoiceHub 站点地址」 | 填 `voicehub_base_url`。 |
| 通知「发送成功」但没收到 | 适配器返回成功只说明 AstrBot 找到了对应平台实例，不保证第三方平台最终送达；用 `/vh test` 和 `/vh status` 先排除会话问题，再查平台侧限制。 |
| 想确认插件在监听 | `curl -H "X-VoiceHub-Token: <token>" http://127.0.0.1:6199/voicehub/health`。 |

## 开发与测试

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest tests -q
```

测试不需要真实聊天平台：`tests/conftest.py` 在收集前注入最小 `astrbot.*` 桩模块，`tests/test_integration.py` 会同时启动桩 VoiceHub（实现 bind / unbind / verify-targets）与插件的入站服务，覆盖令牌校验、绑定往返、已绑定与未绑定私聊目标、显式群广播、缺失 `targets` 拒绝、回查重定向拒绝与令牌不外泄。

未覆盖：真实适配器的端到端送达、VoiceHub 侧的数据库查询。

## 项目结构

```
main.py              插件入口：指令定义、initialize/terminate
lib/config.py        配置解析、UMO 形状校验与过滤
lib/contract.py      两侧共用的路径与请求头常量
lib/server.py        入站 HTTP 服务（令牌/IP 校验、目标解析）
lib/push.py          消息链构造与多目标推送
lib/pull.py          拉取模式：主动取件、投递与回执
lib/voicehub.py      回调 VoiceHub 的客户端（绑定/解绑/回查）
_conf_schema.json    管理面板配置项定义
tests/               桩 astrbot + 桩 VoiceHub 的测试套件
```
