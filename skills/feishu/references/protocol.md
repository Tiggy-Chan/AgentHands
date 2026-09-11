# 飞书内部协议参考（cookie 会话）

本文件记录 feishu skill 用的内部端点。凡标「待验证」的条目都来自开源实现的
源码分析，还没在我们租户的真实会话上复核；真实网页流量的观察日期会注明。

来源：`scripts/vendor/lark_bridge/`（github.com/ryanwx/lark-bridge，MIT）
和 github.com/echowxsy/lark-cli 的源码。

## 认证

| 用途 | 请求 | 备注 |
|---|---|---|
| CSRF | `POST https://internal-api-lark-api.feishu.cn/accounts/csrf` | 响应 Set-Cookie 里的 `swp_csrf_token` |
| 当前用户 | `GET https://internal-api-lark-api.feishu.cn/accounts/web/user?app_id=12` | `data.user.id` / `name`；@我过滤的前提；**兼作探活** |
| 必需 cookie 字段 | `session`、`passport_web_did` | WS 握手要 `passport_web_did`（device_id） |

cookie 寿命：无活动 12–24h。daemon 每 5 分钟打一次 `accounts/web/user` 保活。

## IM gateway（protobuf over HTTP）

统一端点：`POST https://internal-api-lark-api.feishu.cn/im/gateway/`
请求头是 `x-command`/`x-web-version` 等（见 `transport.gateway_call`），
body 是 `Packet{cmd, payload, cid}` 序列化。

| cmd | 用途 | payload | 状态 |
|---|---|---|---|
| 5 | 发消息 | `PutMessageRequest` | 已按 lark-bridge 实现；**双重闸门后可用** |
| 8 | 按 id 批量取消息 | 重复 field1 = msg_id | 已验证（开源实现 + 本租户实测） |
| 13 | 建私聊 | `PutChatRequest` | **永久拒绝** |
| 64 | 群名 | field1 string chatId | **2026-09-07 实测已失效**：当前租户回 `payloadType=5` 空 Packet |
| 5023 | 用户名 | f1 varint userId, f2 varint chatId, f3=1 | 同上，**已失效**。名字改由 whoami（自己）+ Messenger DOM 会话名（chats 反哺缓存，p2p 会话名=对方名字）提供 |
| 11021 | 消息搜索 | chat/时间段/发送人/**mention_user_id** | 分页 token 是 JSON；时间参数已在 2026-08-31 当前租户用 epoch 秒实测可用 |

**消息归属（2026-09-07 起）**：发送者角色不靠名字解析——`is_me`/`direction` 由
whoami user_id 与 fromId 比对得出，p2p 会话渲染为「我/对方」，名字缺失只影响
显示、不会判反角色。历史缓存里的错误名字（如全量 "xi tong xiao xi"）会被
denylist 与同名 ≥3 用户规则自动清除。

**Content 字段布局（2026-09-07 原始 wire dump 验证，vendor schema 对 FILE/IMAGE 错位）**：

- FILE (type 3)：f1 str = key（`file_v3_<shard>_<uuid>g`）、f2 str = 文件名、
  f3 varint = 字节数、f4 str = mime、f7 str = 区域标记（`eu_nc`）。
  vendor 的 Content 会把 f2 文件名读成 imageKey、f4 mime 读成 attachments ——
  解码器已按上述布局走 wire 级解析，proto 派生的媒体 key 一律过合理性检查。
- IMAGE (type 5)：f2 nested { f1 str = key（`img_v3_<shard>_<uuid>g`）、
  f2 str = 区域（`eu_nc-cdn`）、f3 nested 加密材料 { f2 { f1 32B AES-256 key,
  f2 12B GCM nonce } }、f4 repeated 尺寸变体 { f1 slot, f2 { f1 宽, f2 高 } }
  （slot=1 为原图）}；根 f4 varint = 字节数。
- **消息媒体是 AES-256-GCM 加密的**（tag=密文最后 16B）。下载管线在
  `message_resources._decrypt_in_place` 透明解密；密钥只存在进程内
  `_resource_crypto`，绝不进公开输出/事件池/日志。

**媒体下载路由（2026-09-07 media-observe probe 实测）**：

| 种类 | 路由 | 状态 |
|---|---|---|
| image/audio/video (img_v3_/audio_v3_/media_v3_ key) | `GET https://internal-api-lark-file.feishu.cn/static-resource/v1/<完整 key 含前缀>~` | **已验证**：200 + image/jpeg，下载后 AES-256-GCM 解密得到与声明尺寸一致的 JPEG |
| sticker/头像 | 同路由，query 带 `format/image_size/sticker_format` | 已观察到，未登记（sticker 下载仍失败关闭） |
| 消息文件 (file_v3_ key) | 静态路由全灭（static-resource 400/404、drive-stream 404）；真实下载 URL 由网页端一次性签发（疑似 service worker，页面钩子看不到） | **已落地 UI 路径**：`media-file-download` 用受限浏览器点文件卡片下载按钮捕获真实 download 事件（2026-09-10 验证，mime/size 对账一致） |
| CDN 变体 | `s1-imfile.feishucdn.com/static-resource/v1/...` | 已观察到（免登录 CDN），未使用 |

消息字段（`entities.Message`）：`chatId`/`fromId`/`createTime`/`parentId`/
`rootId`/`threadId`/`atOutChatterIds`(f38)/`isRemoved`/`isTruncated`/`chatType`
(1 私聊 2 群 3 话题群)。

消息内容字段（`Content`）包括 `imageKey`、`fileKey`、`audioKey`、`fileName`、
`fileMime`、`fileSize`、`attachments`、`stickerSetId`、`stickerId` 和 `shareUserId`。
当前 `message` provider 将这些字段转换为资源元数据；资源 key 不进入普通输出。
`entities.Message.Type` 已覆盖 0、2–25 中定义的文本、文件、图片、音频、卡片、
合并转发、位置、用户卡片、日历和 Todo 类型，未来数值类型统一降级为 `opaque`。

## WebSocket 实时推送

- 地址：`wss://msg-frontier.feishu.cn/ws/v2?access_key=…&ticket=…&device_id=…`
- ticket：`GET https://login.feishu.cn/suite/passport/frontier_ticket/?local_device_id=<passport_web_did>`
- access_key = `md5("2" + appKey + device_id + "f8a69f1719916z")`，
  appKey 内置常量（`vendor/lark_bridge/_urls.py`），前端改版会轮换——
  连不上时从浏览器 Network 的 WS 请求里重新取。
- 推送帧：`Frame.payload` → `Packet{cmd=6}` → `PushMessagesRequest`：
  `messages` map + **`messagesAtMe` map（服务端直接标了 @我）**。
- 每帧要回 ACK（`Packet{cmd=1, sid}` 包进 `Frame`）。本 skill 的 ACK 在
  事件落盘**之后**发出。

## Messenger / 搜索网页观察（2026-08-31）

- Messenger 页面会请求 `POST /im/gateway/` 的 `x-command=1011741`；当前抓到的响应是
  feed 配置/分组数据，不能直接当作完整会话列表。
- 同页还观察到 `1102001`、`1102002`、`1102601`、`1102602`、`1103941` 等 feed 相关
  command；请求体和响应 schema 尚未稳定映射，不能凭 command 号猜 payload。
- 当前 `chats` 使用短生命周期、无 React 内部遍历的 DOM provider，读取
  `[data-feed-id]` 和会话卡片文本，结果带 `source=browser_dom`、`completeness=ui_snapshot`；
  `complete` 只表示本次滚动未发现更多卡片。
- 搜索入口在 `/next/messenger/` 的搜索面板；输入关键词后点击“云文档”，结果以
  `.search-doc-card-box` 卡片渲染。当前远程 `docs-search` 读取卡片标题、摘要、类型和
  修改信息；不保存正文或原始 URL，结果可能没有可复用 token。

## 邮件 / 日历网页观察（2026-08-31）

- 邮箱入口：`GET https://<tenant>.feishu.cn/mail`，页面标题为“飞书邮箱”；当前可见
  邮件列表使用 `FeedItem-module__mailFeedItem` 等普通 DOM class，但没有稳定的
  `message_id` data 属性。provider 因此返回 `id_kind=ui_fingerprint`，只能在相同可见
  列表中定位详情，不把指纹冒充服务端邮件 id。
- 邮箱页面加载后会产生 `cmd=3700/3601/3730/3791/3833` 等 protobuf 请求；字段语义和
  写入 payload 尚未确认，transport 不登记这些 command 的读写含义。
- 日历入口：`GET https://<tenant>.feishu.cn/calendar/`，最终页面为 `/calendar/week`；
  会议项使用 `.event-instance` 和 `data-key`、`data-start-time` 等属性，当前 provider
  可读取标题、时间、地点等可见字段，结果固定标记 `source=browser_dom`、`complete=false`。
- mail/calendar provider 只读取有界 DOM 文本；邮件和日历正文不写入事件池或长期缓存。

### 日历写接口（2026-09-11 headed 抓包登记，已实测打通）

- **创建日程 = gateway cmd 1002003**，已登记进 `transport.WRITE_GATEWAY_CMDS`（双闸门）。
  payload 为 `f1{ inner }`，inner 字段（逐字段与抓包样本核对，构造器见
  `calendar_ops._build_create_payload`）：f1 "0"、f2 客户端 event UUID（uuid4，幂等键）、
  f3 0、f4/f5/f6 calendar_id×3、f7/f8 空、f9..f12 0、f15 1、f16 -1(int64)、f17 1、
  f18 0、**f20 是空 bytes 不是 varint**、f21 1、f22 1、f23 0、f24 2、f25 start_epoch、
  f26 时区、f27 end_epoch、f28 时区、f29/f30 标题×2、f31 描述富文本 JSON、
  f32 {f1,f2 空, f4/f5 float32 360.0}、f34 {f1 空, f2 5, f3 3}、f35/f36 0。
- 描述 JSON（f31）形状：etherpad 风格 `initialAttributedText(s)` + `apool`；
  **apool 必须含 1 个 author + 7 个随机 6 位 lineguid（nextNum=8）**，最小化 apool
  会被拒（实测）。空描述（text 为空）服务端接受。
- **创建后紧跟 cmd 1010023 关联包**：payload = `f1{ f1 uuid, f2 calendar_id, f3 0 }`；
  f3=0 漏掉会被回 "PB payload in packet unmarshal error"（HTTP 900）。
- **删除日程 = cmd 1002005**（WRITE 已登记）：payload = f1 calendar_id、
  f2 客户端 event uuid、f3 0、f4 0、f5 start_epoch（定位重复日程实例）；
  成功应答同为 payloadType=1 ack。CLI `calendar delete` 双闸门，已实测删除 3 个事件。
- **增量同步 = cmd 1002002**（READ 已登记）：payload = `f1{ f1 calendar_id,
  f2 微秒时间戳字符串, f3 上限(100) }`；响应 f5.f1.f2 内重复 f1 事件记录：
  f1 服务端 id、f2 客户端 uuid、f25 start、f27 end、f29 标题（f37/f38 为参与人）。
  since=0 拉全量；CLI `calendar sync` 用来拿 event_uuid（DOM list 拿不到 uuid）。
- **事件详情 = cmd 1002006**（READ 已登记）：f1=服务端事件 id → 完整事件记录。
  UI 在删除后逐个拉它刷新，删除本身不需要它。
- **应答形态**（`_parse_create_reply`）：成功 = payloadType=1 短 ack（回显 cid，
  **不回显 event uuid**）；同 uuid 重发 = payloadType=0 + f1 错误串
  "event already exist"（幂等去重，视为成功）；**payloadType=5 + 空 payload + HTTP 401
  = 边缘拒绝，根因是请求没绑会话 Cookie**（CLI 的 create/mail 写分支在 auth.prepare()
  之前 return，已修：confirm 执行前必须先 `auth.prepare()`）。
- **头指纹与 Messenger 不同**：locale zh_CN、x-command-version 2.7.0、x-lgw-os-type 2、
  x-lsc-bizid 3、x-lsc-terminal web、x-web-version 3.40.0+1a01f3756ef，且无
  x-lgw-req-sdk-type（`_CALENDAR_HEADERS`，空字符串=移除该头）。实测 Messenger 默认头
  也能通过边缘校验，但保持真实指纹以防风控。
- 日历页面观察到的其他 command（1000014 疑似列表、1002002 事件同步、1002005/6 轮询、
  1103991 同步计数等）字段语义未确认，不登记。

## 受控 Browser Worker

- `chats`、远程 `docs-search`、`probe`、mail/calendar DOM 读取和扫码登录都通过一次性
  `systemd-run --user` worker 执行，Python、Node 和 Chromium 子进程共用同一 cgroup。
- 默认属性：`MemoryHigh=1536M`、`MemoryMax=2G`、`TasksMax=128`、有限 RuntimeMax。
  （2026-09-10 两次上调：384M/512M→768M/1G 解决 Messenger SPA 本体 ~700M 的 OOM；
  →1536M/2G 缓解 systemd-oomd 按**会话级 PSI**（user@1000.service，限值 50%/20s）
  整体 SIGKILL transient unit 的问题——紧 cgroup 自身的回收抖动会推高会话压力。
  oomd 杀的特征是 systemd-run 退出码 255 且 worker 无输出；worker 对该签名自动重试一次。
  `ManagedOOMPreference=avoid` 对非 root cgroup 无效（oomd 忽略），不可用。）
  Chromium 使用禁 GPU、禁扩展、单 renderer/低 raster 线程参数，避免在 128 tasks 上限下
  触发 fork 拒绝。worker 超时或父进程取消时会先 stop transient unit，再回收子进程。
- probe 只保存 method/host/path/command/status、query key、响应长度和 DOM 结构摘要；不
  读取或保存 Cookie、ticket、Authorization、正文、附件和 HAR。没有 systemd user/cgroup
  时浏览器能力失败关闭，不退回无上限启动。

## 文档 / 云盘（REST，走 `{FEISHU_DOMAIN}`，默认 www.feishu.cn）

| 用途 | 请求 |
|---|---|
| 我的空间根节点 | `GET /space/api/explorer/v3/my_space/folder/`；`data.has_more` + `last_label` 分页 |
| 共享空间根节点 | `GET /space/api/explorer/v2/share/folder/list/`；`data.has_more` + `last_label` 分页 |
| 子节点 | `GET /space/api/explorer/v3/children/list/?token=<folder>`；分页响应 `data.has_more` + `data.last_label`，下一页带 `last_label` |
| wiki→obj_token | `GET /space/api/wiki/v2/tree/get_node/?wiki_token=<t>&expand_shortcut=true`（obj_type: 22 docx / 3 sheet / 2 doc） |
| 建导出任务 | `POST /space/api/export/create/`（docx→markdown/docx/pdf；sheet→xlsx；csv 实测报 export file extension mismatch；读语义，白名单放行） |
| 轮询导出 | `GET /space/api/export/result/<ticket>?token=…&type=…`（job_status 0=完成 1/2=进行中） |
| 下载 | `GET https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/all/<file_token>/` |
| 导入·预备上传 | `POST /space/api/box/upload/prepare/`（`mount_point=ccm_import`，extra 带 obj_type/file_extension）→ upload_id |
| 导入·传字节 | `POST https://internal-api-drive-stream.feishu.cn/space/api/box/stream/upload/merge_block/`（octet-stream + adler32 头） |
| 导入·完成上传 | `POST /space/api/box/upload/finish/` → file_token |
| 导入·建任务 | `POST /space/api/import/create/`（file_token + type + 目标文件夹）→ ticket |
| 导入·轮询 | `GET /space/api/import/result/<ticket>`（job_status 0=完成）→ 新文档 token/url |

REST 大多要带 `x-csrftoken: <_csrf_token cookie 值>`（注意：不是 swp_csrf_token）。

导入源格式（官方 import-user-guide）：docx/doc/txt/md/mark/markdown/html/xlsx/xls/csv；
md 后缀必须与内容严格一致（否则 1069910）；md 语法保真度无官方承诺，以实测为准。

## 待抓包确认的缺口

1. 完整会话列表的稳定 RPC schema（当前 DOM provider 可用，但不保证虚拟列表全量）。
2. 文档搜索结果对应的稳定 token/URL 字段（当前网页 provider 能返回结果文本）。
3. ~~cmd=64/5023~~ 已失效（payloadType=5）；如前端恢复名字 RPC 需重新观察。
4. 邮件列表/详情的稳定 message id 和读 command 字段语义。
5. 日历事件详情字段、分页语义和读 command 字段语义。
6. 邮件发送/回复、日历修改的精确写 endpoint 和 payload。
   ~~日历创建~~ 已登记并实测打通（cmd 1002003 + 1010023，2026-09-11，见「日历写接口」）。
7. WS ticket/app_key 轮换和长时间断线恢复的更多样本。
8. ~~图片下载端点~~ 已登记（static-resource 路由 + AES-256-GCM 解密，2026-09-07 验证）。
   ~~消息文件（file_v3_ key）~~ 已落地 UI 下载路径（2026-09-10，见上表；静态路由确认不存在）。
   剩余：audio/video 的 content 布局与加密材料字段（需真实样本）、sticker 路由的
   query 参数语义（需真实 sticker 消息样本做一次 media-observe）。
9. 会话内历史加载的客户端行为：2026-09-07 观察打开会话时只看到 feed 类 command
   （1011741/1102001/1102002/1102601/1102602/1103941），消息体疑似走本地缓存
   （IndexedDB）；直接打开老消息所在位置还没有可靠的 UI 路径。

`probe --scope media-observe --msg-id <id> [--interact [--headed]]` 是观察媒体流量的受控入口：
打开目标消息所在会话（deep link → feed 卡片点击 → 消息搜索跳转三段 fallback），
只记录携带资源 key 的请求元数据与 `/static-resource/` 路由；client 没抓到时会用
消息自己的 key 对已观察路由做 GET 候选验证（headers-only，不读 body）。
`--interact` 会主动点击文件卡片/下载按钮（文件不会自动加载，这是 file_v3_ 路由
从未被观测到的原因），并记录 download 事件与 octet-stream/attachment 响应；
`--headed` 时留出 120 秒窗口让用户手动点下载。

写接口抓包（2026-09-10 新增）：`probe --scope calendar|mail --headed --capture --out <文件>`。
headed 浏览器里由用户本人手动执行一次写操作（建测试日程/发测试邮件），probe 以
observe 模式放行并记录非 GET 请求的元数据与 ≤4KiB 截断 body（结果
`contains_bodies=true`，必须落 `--out` 文件）。**endpoint 登记进 transport 白名单后
必须删除抓包文件**。observe 模式永不默认开启。

抓包方法（别把凭证落盘）：

```
1. feishu.py login 之后，FEISHU 浏览器 profile 已登录
2. 用 `feishu.py probe --scope chats|docs-search|mail|calendar`，或
   `feishu.py probe --scope message-media --msg-id <msg_id>`；不要直接启动 Playwright
3. 只记录 method/host/path/x-command 头和结构摘要 —— 不记 Cookie、不存 HAR、
   WS URL 里的 ticket/access_key 不落盘
4. 确认的端点写回本文件，实现进 `_feishu/chats.py`、`docs_search.py` 或消息资源 provider

## 消息资源与本机媒体处理

- `feishu.py message <msg_id>` 返回 schema-2 envelope；`history` 和 `thread` 使用相同模型。
- `feishu.py media <msg_id> --action metadata|download|inspect|ocr|transcribe` 只处理
  用户明确指定的消息资源。`download` 必须带 `--out` 或 `--keep-media`。
- 已验证的 Drive stream 可用于文件/云文件 token；图片、音频、视频和贴纸必须由受控
  probe 观察到精确 host/path 后再加入 transport 白名单。第三方 URL 不自动抓取。
- Media Worker 使用 `systemd-run --user`，默认 `MemoryHigh=768M`、`MemoryMax=1G`、
  `TasksMax=64`、`RuntimeMaxSec=600` 和 `KillMode=control-group`；其子进程包括
  ffmpeg、tesseract 和 faster-whisper。
- 原始附件默认只存在于受限临时文件，处理完成后删除；`--keep-media` 才写入加密媒体缓存。
- OCR 使用 `tesseract chi_sim+eng`，ASR 使用本机 `faster-whisper base` CPU/int8；禁止把消息
  内容上传到外部 OCR/ASR 服务。
```

## 已踩过的坑

- 上游 `proto_pb2.py` 是过期编译产物（缺 `messagesAtMe`/`atOutChatterIds`），
  已用 libprotoc 35.1 重新编译。上游改 .proto 不重编的话，这里要跟着重编。
- protobuf ≥5 的 FieldDescriptor 没有 `.label`，用 `.is_repeated`。
- `SimpleCookie` 遇到一个坏段会整串返回 `{}`——cookie 解析用容错分段解析。
- venv 的 python 是符号链接，`realpath(sys.executable)` 会解析成系统解释器，
  判断「在不在 venv 里」要用未解析的路径。
- **payloadType=5 + 空 payload 不是「命令已死」的充分判据**（2026-09-11）：它同时是
  边缘网关在 HTTP 401（请求没绑 Cookie）时的应答形状。CLI 的日历/邮件写分支曾在
  `auth.prepare()` 之前 return，导致写请求匿名发出全被 401 拒绝；判 5 先看响应
  HTTP 状态码再说命令失效。真正的命令失效（cmd=64/5023）是登录态下 200 + payloadType=5。
- 成功 ack（payloadType=1）只回显 cid 不回显业务 id——「响应里找请求 id」的校验
  思路在日历 gateway 上不适用；幂等去重靠服务端按客户端 uuid 报 "already exist"。
