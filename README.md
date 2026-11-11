<p align="center">
  <img src="assets/logo.svg" width="130" alt="AgentHands Logo">
</p>

<h1 align="center">AgentHands (Agent 大手子)</h1>

<p align="center">
  <strong>Personal Account Action Framework for AI Agents without Developer APIs</strong><br>
  让 AI Agent 真正拥有受控的“手和脚”：基于个人会话接管主流平台个人账号，打破企业资质审批壁垒。
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square" alt="MIT License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://github.com/Tiggy-Chan/AgentHands/stargazers"><img src="https://img.shields.io/github/stars/Tiggy-Chan/AgentHands?style=flat-square" alt="GitHub Stars"></a>
</p>

<p align="center">
  <strong>简体中文</strong> · <a href="README_en.md">English</a>
</p>

---

## 为什么需要 AgentHands？

当今大语言模型（LLM）与代码智能体（Claude Code、Codex、Hermes Agent 等）具备了强大的推理与决策大脑，但在接入真实互联网服务时，开发者往往面临难以逾越的工程死局：

1. **企业门槛高**：官方开放平台 API 往往仅向企业认证开发者开放，要求提交营业执照、申请 App ID 与 App Secret，并经企业租户管理员逐级人工审批，个人开发者基本无法拿到权限；
2. **能力严重残缺**：现有的各类开放平台机器人无法以“本人身份”读写私聊、无法直接编辑个人云文档、无法进行卖家改价发货或社群管理；
3. **只读与行动的脱节**：只读工具（如 Agent-Reach）解决了 Agent 感知互联网信息的“眼睛与耳朵”，但 Agent 无法在这些平台上执行真实业务状态变更；
4. **失控与幻觉风险**：直接把账号写操作交给大模型极易因幻觉引发误删、误发或敏感凭证泄漏。

**AgentHands（Agent 大手子）专为打破这些壁垒而生**：
- **零官方 API 门槛**：基于个人网页会话 Cookie、内部 REST/GraphQL 网关或受控无头浏览器运行，能力等同本人在浏览器中操作；
- **工业级底层协议实现**：深入 Protobuf 网关、长连接、移动端验签、文档虚拟化 DOM 与 AST 级别；
- **专为 Agent 打造的安全双闸门**：物理环境变量隔离 + SHA256 草稿指纹确认 + 全局内存级敏感凭据动态脱敏，确保 Agent 每一个写操作绝对受控。

---

## 平台支持矩阵 (Platform Support Matrix)

AgentHands 涵盖即时通讯、内容社交、电商履约与音视频等多模态主流平台：

| 平台 | 接入机制 | 核心动作能力 (Action Surface) | 安全闸门标识 |
|---|---|---|---|
| **飞书 (Feishu / Lark)** | 内部 Protobuf + WebSocket + 虚拟 DOM | 会话消息收发、原生文件直发 (Type 3/13)、文档原地替换与增量块级外科编辑、卡片引用、日历日程管理、本地 AES-256-GCM 加密事件池 | `FEISHU_ALLOW_SEND`<br>`FEISHU_ALLOW_WRITE` |
| **哔哩哔哩 (Bilibili)** | Web Cookie + Wbi 动态签名 | 视频元数据查询、点赞/投币/收藏/一键三连、多层评论回复、图文动态发布与删除、私信收发、视频稿件分片投稿 | `BILIBILI_WRITE_ALLOWED` |
| **闲鱼 (Xianyu / Goofish)** | 阿里 MTOP 网关 + 本地 MD5 签名 | 商品发布、增量改价、详情编辑、上下架、卖家订单查询、运单号发货履约、退款取消、商品检索与地址簿管理 | `XIANYU_WRITE_ALLOWED` |
| **小红书 (Xiaohongshu)** | 创作者中心受控浏览器 + Shadow DOM | 图文笔记多图发布 (1-9张/卡片样式/话题)、视频笔记发布、笔记详情与互动数据提取、评论抓取、创作者看板 | `XIAOHONGSHU_WRITE_ALLOWED` |
| **QQ (OneBot 11)** | OneBot 11 HTTP 标准接口 | 好友私聊直发、群聊消息发送、消息撤回、群聊天历史检索、群成员禁言与踢出管理 | `QQ_WRITE_ALLOWED` |
| **X (Twitter)** | 内部 Web GraphQL 网关 | 推文发布、推文回复、推文删除、点赞与转推互动、个人书签导出、公开用户资料查询 | `X_WRITE_ALLOWED` |
| **Reddit** | 内部 Web REST 协议 | 板块帖子浏览 (hot/new/top)、讨论树抓取、文本/外链发帖、评论发表与回复、赞踩投票 | `REDDIT_WRITE_ALLOWED` |
| **Telegram** | 本地网关 / MTProto / Bot API | 用户私聊与频道广播、消息撤回、聊天历史时间线检索、群组元数据分析 | `TELEGRAM_WRITE_ALLOWED` |
| **YouTube** | InnerTube 内部接口 + Studio API | 视频详情检索、视频元数据批量修改 (标题/描述/隐私状态)、点赞/取消赞、评论回复、Community 社区动态发表 | `YOUTUBE_WRITE_ALLOWED` |

---

## 核心安全机制：Agent 受控双闸门

AgentHands 在通信底层强制实施严格的防幻觉安全规范：

1. **物理环境变量闸门**：写操作默认处于物理切断状态。未配置对应环境变量（如 `XIANYU_WRITE_ALLOWED=1`）时，CLI 拒绝向外部网络发出任何写请求；
2. **SHA256 内容指纹锁定**：任何处于写操作路径的命令默认输出一个包含结构化意图与 SHA256 校验指纹的草稿（Draft）；只有输入 `--execute --fingerprint <FP>` 且校验完全一致时才可解锁执行。正文或参数的任何细微改动均会导致指纹失效并立即熔断；
3. **全局内存级敏感脱敏注册表 (`register_secret`)**：所有提取的 Cookie、Token、密钥只要进入内存即刻登记脱敏，无论终端报错、堆栈打印或 Agent 反思，敏感凭据永不泄漏；
4. **不可信内容显式定界 (`untrusted`)**：从外部平台读取的所有正文均使用自适应定界栅栏包裹，确保第三方内容被大模型严格视为纯数据而非系统指令。

---

## 统一管理控制台 (AgentHands Console)

AgentHands 内置一套基于 FastAPI 与纯净前端构建的人类治理控制台：

- **多平台会话总览**：实时聚合各平台会话健康度、登录账号、UID 与令牌状态；
- **二维码扫码中心**：可视化生成登录二维码并自动监听扫码状态，凭据安全加密落盘；
- **人类安全治理中枢**：支持在界面中动态开启或切断各个平台的写闸门授权；
- **一键紧急熔断 (Emergency Freeze)**：在发生突发异常或策略偏差时，一键物理撤销全平台所有写操作权限。

```bash
# 启动控制台（默认监听 8990 端口）
python3 -m webui --host 0.0.0.0 --port 8990
```

---

## 快速上手与 CLI 规范

每个平台均为自包含的独立 CLI 工具，参数走标准 `argparse`，结构化数据走 `stdout`，进度与诊断走 `stderr`：

```bash
# 1. 账号探活与状态检查
skills/bilibili/scripts/bilibili.py whoami --json
skills/xianyu/scripts/xianyu.py whoami --json

# 2. 安全写操作：生成草稿指纹（默认拦截）
skills/bilibili/scripts/bilibili.py interact BV1xx411c7mD --triple

# 3. 经人类核验后执行写操作
skills/bilibili/scripts/bilibili.py interact BV1xx411c7mD --triple --execute --fingerprint <FINGERPRINT>

# 4. 在授权自动化管道中直接执行
skills/bilibili/scripts/bilibili.py comment BV1xx411c7mD "测试评论" --direct
```

---

## 自动化测试验证

本项目包含严谨的离线单元测试套件，在无网络、无浏览器、无真实 Cookie 的沙箱环境下验证全部协议编解码与状态机：

```bash
# 运行离线自动化测试套件
./test.sh
```

---

## 目录结构

```text
AgentHands/
├── assets/                  # 官方矢量设计资产与 Logo
├── skills/                  # 各平台核心接管实现
│   ├── feishu/              # 飞书 (文档/消息/日历/事件池)
│   ├── bilibili/            # 哔哩哔哩 (动态/投稿/评论/三连)
│   ├── xianyu/              # 闲鱼 (商品/订单/搜索/地址)
│   ├── xiaohongshu/         # 小红书 (图文/视频/创作者中心)
│   ├── qq/                  # QQ (私聊/群聊/群管/OneBot 11)
│   ├── x/                   # X (推文/回复/书签/GraphQL)
│   ├── reddit/              # Reddit (发帖/回复/投票/Subreddit)
│   ├── telegram/            # Telegram (消息/频道/历史)
│   └── youtube/             # YouTube (元数据/互动/社区)
├── webui/                   # 统一管理控制台与人类安全中枢
├── test.sh                  # 顶层离线自动化测试套件
├── README.md                # 简体中文使用说明
├── README_en.md             # English Documentation
└── LICENSE                  # MIT License
```

---

## License

This project is licensed under the [MIT License](LICENSE).  
Copyright (c) 2026 Tiggy-Chan. All Rights Reserved.
