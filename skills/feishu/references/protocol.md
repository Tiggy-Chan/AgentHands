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
| 8 | 按 id 批量取消息 | 重复 field1 = msg_id | 已验证（开源实现） |
| 13 | 建私聊 | `PutChatRequest` | **永久拒绝** |
| 64 | 群名 | field1 string chatId | 响应是 UserInfo proto，字段号未收，wire-walk 启发式取名，**待验证** |
| 5023 | 用户名 | f1 varint userId, f2 varint chatId, f3=1 | 同上，**待验证** |
| 11021 | 消息搜索 | chat/时间段/发送人/**mention_user_id** | 分页 token 是 JSON；时间参数已在 2026-08-31 当前租户用 epoch 秒实测可用 |

消息字段（`entities.Message`）：`chatId`/`fromId`/`createTime`/`parentId`/
`rootId`/`threadId`/`atOutChatterIds`(f38)/`isRemoved`/`isTruncated`/`chatType`
(1 私聊 2 群 3 话题群)。

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

## 文档 / 云盘（REST，走 `{FEISHU_DOMAIN}`，默认 www.feishu.cn）

| 用途 | 请求 |
|---|---|
| 我的空间根节点 | `GET /space/api/explorer/v3/my_space/folder/`；`data.has_more` + `last_label` 分页 |
| 共享空间根节点 | `GET /space/api/explorer/v2/share/folder/list/`；`data.has_more` + `last_label` 分页 |
| 子节点 | `GET /space/api/explorer/v3/children/list/?token=<folder>`；分页响应 `data.has_more` + `data.last_label`，下一页带 `last_label` |
| wiki→obj_token | `GET /space/api/wiki/v2/tree/get_node/?wiki_token=<t>&expand_shortcut=true`（obj_type: 22 docx / 3 sheet / 2 doc） |
| 建导出任务 | `POST /space/api/export/create/`（docx→markdown/docx/pdf；sheet→xlsx；读语义，白名单放行） |
| 轮询导出 | `GET /space/api/export/result/<ticket>?token=…&type=…`（job_status 0=完成 1/2=进行中） |
| 下载 | `GET https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/all/<file_token>/` |

REST 大多要带 `x-csrftoken: <_csrf_token cookie 值>`（注意：不是 swp_csrf_token）。

## 待抓包确认的缺口

1. 完整会话列表的稳定 RPC schema（当前 DOM provider 可用，但不保证虚拟列表全量）。
2. 文档搜索结果对应的稳定 token/URL 字段（当前网页 provider 能返回结果文本）。
3. cmd=64/5023 的 UserInfo 字段语义；当前仍是 wire-walk 启发式。
4. WS ticket/app_key 轮换和长时间断线恢复的更多样本。

抓包方法（别把凭证落盘）：

```
1. feishu.py login 之后，FEISHU 浏览器 profile 已登录
2. 用 playwright 开 https://www.feishu.cn/messenger/，挂 page.on("request")
3. 只记录 method/host/path/x-command 头 —— 不记 Cookie、不存 HAR、
   WS URL 里的 ticket/access_key 不落盘
4. 确认的端点写回本文件，实现进 _feishu/chats.py 或 docs_search.py
```

## 已踩过的坑

- 上游 `proto_pb2.py` 是过期编译产物（缺 `messagesAtMe`/`atOutChatterIds`），
  已用 libprotoc 35.1 重新编译。上游改 .proto 不重编的话，这里要跟着重编。
- protobuf ≥5 的 FieldDescriptor 没有 `.label`，用 `.is_repeated`。
- `SimpleCookie` 遇到一个坏段会整串返回 `{}`——cookie 解析用容错分段解析。
- venv 的 python 是符号链接，`realpath(sys.executable)` 会解析成系统解释器，
  判断「在不在 venv 里」要用未解析的路径。
