# AgentHands 🖐️

> **Personal Account Action Framework for AI Agents without Developer APIs**  
> 让 AI Agent 真正拥有你的“手和脚”：基于个人会话接管主流平台个人账号，告别繁琐的企业应用审批与权限壁垒。

---

## 🌟 为什么需要 AgentHands？

当今大模型（LLM）具备了强大的大脑，但在接入真实互联网服务时，开发者往往面临尴尬的死局：
1. **企业门槛高**：官方 API 往往仅向企业认证开发者开放，要求申请 App ID / App Secret，并经企业管理员严格审批，个人开发者根本拿不到权限。
2. **能力严重残缺**：现有的各类开放平台机器人无法以“本人身份”读写私聊、无法编辑个人云文档、无法查阅个人日程。
3. **安全性与失控风险**：直接把账号操作交给 Agent 极易因大模型幻觉引发误操作或敏感信息泄露。

**AgentHands** 专为打破这些壁垒而生：
- **零 API 门槛**：直接基于个人 Web 会话 Cookie 接管，能力等同本人在浏览器中操作。
- **工业级协议逆向**：深入 Protobuf 网关、长连接 WebSocket、文档虚拟化 DOM 与 AST 级别。
- **专为 Agent 打造的安全双闸门**：环境变量物理隔离 + SHA256 草稿指纹确认 + 全局敏感凭证动态脱敏，确保 Agent 行动绝对受控。

---

## 🚀 平台支持矩阵 (Platform Matrix)

| 平台 | 状态 | 核心能力 |
|---|---|---|
| **飞书 (Feishu / Lark)** | 🟢 已生产就绪 (`v0.1.0`) | 会话收发、原生文件直发 (Type 3/13)、文档无痕替换与增量块编辑、卡片引用、日历管理、WS 实时监听守护、本地 AES-256-GCM 加密事件池 |
| **哔哩哔哩 (Bilibili)** | 🟡 规划中 | 动态发布、视频稿件管理、私信与评论互动、粉丝数据监控 |
| **小红书 (Xiaohongshu)** | 🟡 规划中 | 图文笔记发布、评论互动、私信触达 |
| **抖音 (Douyin)** | 🟡 规划中 | 视频管理、私信与通知监听 |

---

## 🛡️ 安全核心：Agent 受控双闸门机制

AgentHands 在通信层强制执行最严格的防幻觉保护：

1. **物理环境变量闸门**：写操作默认被物理切断，只有显式注入授权环境变量时方可执行发送。
2. **SHA256 内容指纹锁定**：Agent 构造的写操作必须生成内容指纹交由人类口头或交互确认；正文有任何细微改动，指纹立即失效并熔断。
3. **全局脱敏注册表 (`register_secret`)**：所有提取的 Cookie、Token、密钥只要进入内存即刻登记脱敏，无论终端报错、堆栈打印或 Agent 反思，敏感凭据永不泄漏。

---

## 🧪 自动化测试验证

本项目包含严谨的离线单元测试套件，在无网络、无浏览器、无真实 Cookie 的沙箱环境下验证全部协议编解码与状态机：

```bash
# 运行离线全量单测与 CLI 契约检查
./test.sh
```

---

## 📂 目录结构

```text
AgentHands/
├── skills/
│   └── feishu/              # 飞书平台全功能接管实现
│       ├── scripts/
│       │   ├── _feishu/     # 核心业务实现（文档、消息、日历、加密事件池、守护）
│       │   ├── vendor/      # lark_bridge 逆向协议底座（含 16 项工业级补丁）
│       │   └── feishu.py    # 统一 CLI 与 Agent Skill 路由入口
│       ├── tests/           # 172 项自动化离线单元测试
│       ├── references/      # 协议抓包分析与 Docx AST 规范
│       └── SKILL.md         # Claude Code / Codex / Hermes 接入定义
├── test.sh                  # 顶层离线自动化测试套件
└── README.md
```

---

## 📄 License

Copyright (c) 2026 Tiggy-Chan. All Rights Reserved.
