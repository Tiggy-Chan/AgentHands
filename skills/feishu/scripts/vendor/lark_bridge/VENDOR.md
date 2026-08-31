# VENDOR.md — lark-bridge

## 来源

- 上游：<https://github.com/ryanwx/lark-bridge>
- 版本：commit `a165f59cc053216a40fe17fc6854924b29f8fc61`（2026-07-21）
- 许可：MIT（见同目录 `LICENSE`，Copyright (c) 2026 Ryan Zhu）
- 拷入时间：2026-08-28

这是一个逆向飞书网页客户端协议的 Python 实现：cookie 认证下的消息收发、
消息搜索、WebSocket 实时监听、云盘/文档操作。agenthands 的 feishu skill 以它
为协议底座。

上游 `mcp_server.py` 未拷入（skill 不走 MCP，走自己的 CLI）。

## 本地补丁台账（升级上游时必须逐条重新应用）

1. **`_http.py`（新增文件，不是上游代码）**：统一的 httpx client 工厂 +
   TLS 配置钩子 + 发送/资源写入闸门钩子（`require_send_allowed` /
   `require_write_allowed` / `write_disabled`）。
2. **所有 `httpx.AsyncClient(verify=False, ...)` → `_http.make_client(...)`**：
   `search.py`（2 处）、`listener.py`（1 处）、`sender.py`（1 处）、
   `drive.py`（10 处）。上游全局关 TLS 校验，这里恢复严格校验。
3. **`listener.py`**：
   - `Message` 新增字段 `at_me` / `at_out_chatter_ids` / `thread_id` /
     `is_removed` / `is_truncated` / `raw`（proto 里本来就有
     `messagesAtMe` 和 `atOutChatterIds`，上游没暴露；`raw` 让调用方
     能做结构化 mention 解码）；
   - ACK 移到一帧的所有消息 yield 完之后（事件先落盘再 ACK）。
4. **`sender.py`**：`send_message` 开头加 `_http.require_send_allowed()`，
   发送闸门未打开时抛 `SendBlockedError`。
5. **`drive.py`**：`create_folder` / `upload_file` / `import_document` /
   `delete_nodes` 四个写操作直接抛 `WriteDisabledError`（只读策略）。
   `export_document` 保留——导出是读语义。
6. **`search.py`**：`_build_search_params` 里三处 `bytes([len(x)])` 改成
   varint 长度 `_ev(len(x))`，长度 ≥128 时上游写法会产出非法 protobuf。
7. **`client.py`**：`_parse_cookies` 改为容错解析（按 `;` 分段、首个 `=`
   切分、跳过无 `=` 段）。上游的 `"; "` 硬分割对含空格的值不可靠；
   `http.cookies.SimpleCookie` 也试过并放弃——实测一个坏段会让整串
   解析结果为空 `{}`，session 静默丢失更危险。

8. **`proto/proto_pb2.py` 重新编译**：上游仓库里这个文件是过期产物——
   `proto.proto` 有 `messagesAtMe`/`atOutChatterIds` 等字段，编译产物里
   没有。已用 grpcio-tools（libprotoc 35.1）从 `proto.proto` 重新生成。
   上游如果只改 .proto 不重新编译，这里也要跟着重新编译。

9. **`_proto_utils.py`**：`field.label == field.LABEL_REPEATED` 改成
   `field.is_repeated`——protobuf ≥5（upb）的 FieldDescriptor 没有
   `.label` 实例属性，上游写法在新运行时直接 AttributeError。
10. **`_http.BoundedAsyncClient`**：控制面响应默认限制为 8 MiB，文档/文件下载
   显式放宽到 256 MiB，避免异常响应耗尽宿主机内存。

## 升级步骤

1. `git clone` 上游到 /tmp，`diff -r` 对比本目录，评估上游改动。
2. 拷入新代码后**逐条重打上面全部补丁**，不要只覆盖上游同名文件。
3. 跑 `./test.sh` 的单测（protobuf round-trip、发送闸门、解码）。
4. 用真实会话按 `references/protocol.md` 的清单复核关键命令号。

## 已知上游限制

- `pyproject.toml` 声明 Python ≥3.11；本 vendor 在 Python 3.10 上回归
  （语法均为 3.10 兼容）。
- 使用 protobuf 私有 API `google.protobuf.internal.decoder._DecodeVarint`；
  升级 protobuf 运行时后先跑单测。
- WebSocket 的 `APP_KEY`（`_urls.py`）是网页前端内置常量，飞书前端改版
  可能轮换；连不上时从浏览器 Network 面板的 WS 请求里重新取。
