<div align="center">

# 📻 VoiceHub 广播助手

*将校园广播通知、点歌与本周排期带进聊天窗口*

[![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![VoiceHub](https://img.shields.io/badge/service-VoiceHub-7c3aed?style=flat-square)](https://github.com/laoshuikaixue/VoiceHub)

</div>

## ✨ 简介

这是 [VoiceHub](https://github.com/laoshuikaixue/VoiceHub) 的 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：用户可在机器人私聊绑定账号、点歌，发送 `/广播 本周歌单` 获取排期图片；VoiceHub 也能把账户通知送到已绑定的私聊会话。插件不保存绑定关系，账号与排期数据由 VoiceHub 管理。

- 插件仓库：<https://github.com/fhzit/astrbot_plugin_voicehub>
- 适配器：`aiocqhttp`（OneBot v11）、`qq_official` / `qq_official_webhook`、`wecom_ai_bot`、`lark`、`dingtalk`；实际可用功能取决于平台发送能力。
- 通知支持 VoiceHub 主动推送（`push`）和插件主动取件（`pull`），两侧模式必须一致。
- Python 依赖见 [`requirements.txt`](./requirements.txt)；歌单图片由 Pillow 绘制，中文字体首次使用时下载并缓存。

## ✨ 功能特性

- 🔗 **私聊绑定**：VoiceHub 生成一次性绑定码，在机器人私聊完成绑定或解绑。
- 🔔 **精准通知**：仅投递当前有效的私聊绑定，QQ、企微、钉钉、飞书可分别启用。
- 🎵 **私聊点歌**：搜索歌曲、按序号投稿，可选播出时段和点歌券，遵循站点投稿规则。
- 🖼️ **本周歌单**：读取已发布排期，插件用 Pillow 生成图片，显示项由 VoiceHub 后台独立配置。
- 🛡️ **安全投递**：共享令牌、可选 IP 白名单、目标回查与重定向拒绝。
- 🔄 **适配 NAT**：`pull` 模式只要求插件能出站访问 VoiceHub，不必暴露插件端口。

> **导出方案说明**：打印排期的方案存于浏览器 localStorage，插件不能直接选择它；后台「本周歌单图片显示项」是独立配置。Pillow 图片也不是网页打印样式的像素级复刻。

---

## 🔒 安全与投递细节

- **出站/入站分离**：`push` 模式在 AstrBot 进程内监听独立端口；`pull` 模式由插件定时取件，不开放入站端口。
- **令牌 + IP 白名单**：入站请求必须携带共享令牌（常量时间比较），可选按真实 TCP 对端做 IP 白名单。
- **显式目标，不做隐式群发**：请求必须写明目标会话，缺失 `targets` 直接拒绝，避免把通知洒进机器人所在的所有群。
- **私聊目标需回查**：私聊会话在推送前向 VoiceHub 逐个核对**当前有效绑定**，核对不通过则整批拒绝（含其中的群目标），防止把通知推给已解绑的用户。
- **群目标需 VoiceHub 授权**：群会话由管理员在 **VoiceHub 后台**「群聊推送 → 群目标白名单」显式添加（含平台归属）；插件投递前回查该白名单，用户无法通过任何指令自行选择群。
- **不跟随重定向**：回查/绑定/解绑请求一律拒绝 3xx，避免共享令牌被转发到重定向目的地。
- **一次性绑定码**：绑定码的生成、过期与一次性消费全部在 VoiceHub 侧实现，插件只做传递。
- **`/广播 自检`**：不依赖 VoiceHub 也能验证当前会话能否收到推送。

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
        ├─ 用户在机器人私聊发送:  /广播 绑定 <绑定码>
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

群目标白名单**唯一来源是 VoiceHub 后台**：`AstrBot 通知配置 → 群聊推送 → 群目标白名单` 里显式添加 `{UMO, 平台, 备注}`。插件侧的 `group_umos` 只作为可选的第二道闸门（留空即不限制），群授权一律以 VoiceHub 回查结果为准。

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
- `public_base_url`：对外展示的 HTTPS 地址（不带 `/voicehub/push`），仅用于在 `/广播 状态` 和日志里显示可填写的推送地址。
- `allowed_ips`：可选，逗号分隔。**只匹配实际 TCP 对端**，不信任 `X-Forwarded-For`；反向代理部署时填代理地址，并在代理层另行实施上游 IP 白名单。
- `message_prefix`：可选，推送时单独占一行，位于标题和正文之前，例如 `校园广播站`。
- `include_url`：默认开启。关闭后只推送标题与正文，不带 VoiceHub 站点链接。
- `voicehub_base_url`：VoiceHub 站点地址，例如 `https://voicehub.example.com`。**绑定/解绑/私聊回查都依赖它**；留空则绑定指令不可用（推送仍可用）。
- `voicehub_token`：回查 VoiceHub 用的令牌，留空复用 `webhook_token`（一般无需单独填写）。
- `pull_interval_seconds`：**拉取模式**开关，默认 `0`（关闭）。填大于 0 的值（建议 15–60）即启用，见下节。
- `group_umos`：本机附加群白名单（可选），见下节。
- `verify_cache_seconds`：群目标授权回查结果的缓存秒数，默认 30，填 `0` 表示每次投递都回查。
- `request_timeout_seconds`：回调 VoiceHub 的超时，默认 15 秒。
- `song_enabled`：默认开启。关闭后 `/广播 点歌` 与 `/广播 选歌` 回复「点歌功能未启用。」。
- `song_result_count`：点歌搜索展示的候选条数，默认 5，**上限 5**（超出按 5 处理）。

首次生成歌单图片会自动下载 HarmonyOS Sans SC 字体到插件数据目录并缓存；若下载失败，指令会报错，请检查 AstrBot 出站网络后重试。无需额外部署浏览器或生图服务。

生产环境请用 TLS 反向代理，不要在公网明文传输令牌。

### 3. 在 VoiceHub 后台启用机器人推送

管理员 → 站点设置 → `AstrBot 通知配置`：

1. 勾选 **启用机器人通知总开关**；
2. 在 **启用 QQ / 企业微信 / 钉钉 / 飞书** 中勾选要开放的平台，勾选后用户账号页才会出现对应平台的绑定卡片；
3. **推送方向**按部署方式选：VoiceHub 能访问插件用 `push`（默认）；插件在内网/NAT 后用 `pull`，并与插件侧 `pull_interval_seconds` 配合；
4. `AstrBot 服务地址` 填插件的对外地址，例如 `https://astrbot.example.com:6199`。**`pull` 模式下可留空**（只作展示，不参与投递；地址仅在 `push` 模式下必填）；
5. `访问令牌` 填与插件 `webhook_token` **完全相同**的值（页面不会回显已有密钥，留空表示保持不变）；
6. 在 **本周歌单图片显示项** 中分别设置封面、序号、投稿人、票数、播出时段和日期，点击保存。此处为全站共享设置，**不是**打印排期页面浏览器本地的导出方案。

通知只投递到**用户自己已绑定并启用**的私聊会话；群通知只投递到**管理员在 VoiceHub 后台添加的群目标白名单**（带平台归属，见 4 节）。用户侧在 `账号设置 → 账号绑定` 里为每个已启用平台分别生成一次性绑定码，然后在**对应平台的机器人私聊**中发送 `/广播 绑定 <绑定码>` 完成绑定。

### 4. 配置群聊推送

群聊推送（点歌通知、注册待审核、备份失败、系统异常等）在 **VoiceHub 后台 → `AstrBot 通知配置` → 群聊推送** 配置：

1. **群目标白名单**：逐条填写 `{UMO, 平台, 备注}`。UMO 必须是 `平台实例ID:GroupMessage:会话ID`；把机器人拉进目标群，在群里发送 `/广播 状态` 取整串最保险，例如：

```
default:GroupMessage:123456789
```

> 前缀是 AstrBot 中该平台的**实例 ID**（配置里的 `id`，默认常为 `default`），**不是适配器类型名**。别手写 `aiocqhttp:GroupMessage:...` —— 除非你确实把实例 ID 改成了 `aiocqhttp`。用 `/广播 状态` 取到的整串最保险。

2. **事件开关**：按需开启。默认开启「新点歌投稿」「注册待审核」「备份失败」「系统异常」；「歌曲已播放」「重播申请」默认关闭。
3. **CD 防刷屏**：设置合并窗口（同群同事件的合并秒数）与单群频率上限，参数可在后台调整。

插件侧 `group_umos` 留空即可；只有当你希望在本机再叠加一层限制时才填写（格式同上，形状不合法的条目会在加载时被丢弃并记入启动日志）。

## 聊天指令

指令组名为 **`/广播`**；旧写法 `/vh` 作为**别名**保留，两种写法完全等价。

| 指令 | 说明 |
| --- | --- |
| `/广播 绑定 <绑定码>` | 绑定当前**私聊**会话。群聊中会被拒绝，避免个人通知被推到群里。 |
| `/广播 解绑` | 解除当前会话的绑定。 |
| `/广播 状态` | 显示平台、会话类型、会话 ID（UMO）与服务地址。取值、排查都用它。 |
| `/广播 自检` | 向当前会话发送一条测试通知，不经过 VoiceHub。 |
| `/广播 点歌 <关键词>` | 搜索歌曲并列出候选（见下节）。**仅私聊可用。** |
| `/广播 选歌 <序号> [时段=<时段序号>] [点歌券=<券码>]` | 按序号投稿。**仅私聊可用。** |
| `/广播 本周歌单` | 返回本周已发布排期的图片；图片字段由 VoiceHub 后台配置。 |

**兼容旧写法**：英文旧名（`bind` / `unbind` / `status` / `test` / `song` / `pick` / `weekly`）只作为别名存在，例如 `/vh bind`；文档一律以中文指令为准。`/广播 选歌` 的参数键也兼容英文旧键 `time=` / `card=`。

指令唤醒前缀由 AstrBot 配置控制（默认 `/`），实际消息需符合唤醒规则。**不要在群里公开一次性绑定码。**

### 点歌（仅私聊可用）

点歌是**私聊专用**的交互式流程，在群里一律回复「为避免刷屏，请在机器人私聊中点歌。」：

1. **搜索**：私聊发送 `/广播 点歌 告白气球`，机器人回候选列表：

   ```
   点歌搜索：告白气球（音源：网易云音乐）
   1. 告白气球 - 周杰伦（03:35）
   2. ...
   回复「/广播 选歌 序号」完成点歌。
   ```

   音源默认网易云音乐；`durationSeconds` 为空时显示 `未知时长`。搜不到时回复「没有找到「关键词」的歌曲，换个关键词试试。」

2. **投稿**：`/广播 选歌 2`。成功率回复站点返回的文案，例如「点歌成功：告白气球 - 周杰伦」。

3. **播出时段**：`/广播 点歌 时段` 列出可选时段（`时段` 是保留字，不会当作关键词搜索）：

   ```
   可选播出时段：
   1. 午间广播（12:00-12:30）
   2. ...
   回复「/广播 选歌 序号 时段=时段序号」选择时段。
   ```

   站点未开放播出时段选择时回复「当前未开放播出时段选择。」。带时段投稿：`/广播 选歌 3 时段=2`。

4. **点歌券**：`/广播 选歌 3 点歌券=ABCD1234`（券码统一大写后提交）。参数可任意组合、顺序无关：`/广播 选歌 3 时段=2 点歌券=ABCD1234`。

约束与提示：

- 搜索结果的有效期是 **10 分钟**；过期后需重新搜索，否则回复「请先用「/广播 点歌 关键词」搜索歌曲。」
- 同一会话两次投稿之间至少间隔 **5 秒**，命中冷却回复「点歌太频繁，请稍后再试。」
- 未配置 `voicehub_base_url` 时回复「插件未配置 VoiceHub 站点地址，无法点歌。」
- 站点的错误文案（如未绑定、券码无效、同曲限制）原样回给用户；网络异常回复「无法连接 VoiceHub，请稍后重试。」
- 待选状态只存在内存，AstrBot 重启后需重新搜索。

### 本周歌单图片

发送 `/广播 本周歌单`，插件向 VoiceHub 查询**北京时间本周已发布**的排期（草稿不展示），并返回一张 PNG；没有排期时显示「本周暂无排期」。需要插件启用 `song_enabled`、填写 `voicehub_base_url`，以及配置与 VoiceHub 一致的访问令牌。首次调用会下载中文字体，此后复用缓存。

管理员在 VoiceHub 后台 **机器人通知配置 → 本周歌单图片显示项** 勾选封面、序号、投稿人、票数、播出时段和日期后保存。设置对下一次查询生效；它不读取打印排期浏览器中的导出方案。

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
- `targets.umo` 里的 `platform_id` 同样是平台**实例 ID**，请用 `/广播 状态` 显示的整串 UMO。
- 群广播用 `{"targets": {"group": true}}`，投递到 VoiceHub 后台「群目标白名单」中已授权的群（插件侧 `group_umos` 若非空则再作一层限制）；也可以把已授权的群 UMO 直接放进 `umo` 数组。
- 出现在 `umo` 中但未获 VoiceHub 授权的群会话会被拒绝（`群会话未被管理员授权`）。
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
- `POST /api/bot/voicehub/song-search`：点歌搜索，`{"umo": "...", "keyword": "告白气球", "platform": "netease", "page": 1}`。返回 `{"success": true, "platform": "netease", "keyword": "告白气球", "sessionToken": "<密封票据>", "items": [{"index": 1, "title": "...", "artist": "...", "durationSeconds": 215}]}`；候选不出现在列表里时 `items` 为空数组。
- `POST /api/bot/voicehub/song-request`：点歌投稿，`{"umo": "...", "sessionToken": "<song-search 返回的票据>", "index": 2, "playTimeId": 2, "cardCode": "ABCD1234", "note": "..."}`（后三项可选）。成功返回 `{"success": true, "songId": 123, "message": "点歌成功：告白气球 - 周杰伦"}`；站点侧错误（未绑定、票据过期、序号越界、券码无效、同曲限制等）的文案原样回给用户。
- `GET /api/bot/voicehub/weekly-schedule`：查询北京时间本周已发布排期，返回 `weekRange`、`siteTitle`、`schedules` 和管理员保存的 `displayConfig` 六个布尔显示项；由插件绘制图片，接口本身不返回图片。

点歌接口不走公共白名单，与其它机器人回调一样只认 `X-VoiceHub-Token`。播出时段列表用站点既有的公开接口 `GET /api/play-times`（与站点前端同源）；若站点未开放时段选择，插件回复「当前未开放播出时段选择。」。

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
| VoiceHub 报 400「群会话未被管理员授权」 | 该群不在 VoiceHub 后台的群目标白名单里（或插件 `group_umos` 非空但不含它）。 |
| 群推送收不到 | 按顺序查：① VoiceHub 后台「群聊推送」对应事件开关是否开启；② 群目标白名单里 UMO 是否正确（用 `/广播 状态` 取整串）；③ 是否被 CD 节流拦下（合并窗口内会延后投递）；④ 启动日志有无「形状非法，已忽略」告警；⑤ 插件 `group_umos` 是否非空且不含该群。 |
| 拉取模式不工作 | 检查 `pull_interval_seconds` > 0、`voicehub_base_url` 与令牌齐全；VoiceHub 后台推送方向也必须为 `pull`。 |
| `/广播 本周歌单` 提示字体下载失败 | 检查 AstrBot 容器访问字体 CDN 的出站网络和插件数据目录写权限，稍后重试。 |
| 歌单图片显示项与打印方案不一致 | 图片字段在 VoiceHub 后台「本周歌单图片显示项」单独设置，不读取浏览器本地打印方案。 |
| 绑定失败「插件未配置 VoiceHub 站点地址」 | 填 `voicehub_base_url`。 |
| 通知「发送成功」但没收到 | 适配器返回成功只说明 AstrBot 找到了对应平台实例，不保证第三方平台最终送达；用 `/广播 自检` 和 `/广播 状态` 先排除会话问题，再查平台侧限制。 |
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
lib/song.py          点歌：搜索/投稿客户端与内存待选状态
lib/font_manager.py  中文字体按需下载与缓存
lib/schedule_image.py 本周歌单的 Pillow 图片绘制
_conf_schema.json    管理面板配置项定义
tests/               桩 astrbot + 桩 VoiceHub 的测试套件
```
