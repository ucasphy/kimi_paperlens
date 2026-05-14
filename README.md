# Paper-Lens for Kimi Code

在 Kimi Code 上复现的 [paper-lens](https://github.com/nekoneko0831/paper-lens) 项目，适配了 Kimi Code CLI 作为后端驱动，保留了完整的 Web UI 界面。

## 主要改动

- **后端适配器**：将 `ClaudeCLIAdapter` 替换为 `KimiCLIAdapter`，使用 `kimi --print --output-format stream-json` 模式驱动会话
- **MCP 服务器**：保留 MCP 架构处理 `AskUserQuestion`，通过 `--mcp-config` 注册 `paper_lens` MCP 服务器
- **Session Resume**：利用 Kimi CLI 的 `--resume` 参数保持多轮对话状态
- **CORS & 平台兼容**：放宽 CORS 限制，修复 Linux 下的文件打开逻辑

## 项目结构

```
.
├── .claude/skills/paper-lens/    # CLI Skill（论文阅读指令）
├── paper-lens-backend/           # FastAPI 后端
│   ├── adapters/
│   │   ├── kimi_cli.py           # Kimi CLI 适配器（新增）
│   │   └── claude_cli.py         # 原始 Claude 适配器（保留）
│   ├── server.py                 # FastAPI 主服务
│   ├── mcp_server.py             # MCP  ask_user 服务器
│   └── .venv/                    # uv 虚拟环境
├── paper-lens-web/               # Next.js 前端
│   ├── src/
│   └── scripts/dev-with-backend.mjs
├── paper-notes/                  # 论文笔记输出目录
└── start.sh                      # 一键启动脚本
```

## 环境要求

- Python 3.11+
- Node.js 18+ & npm
- [uv](https://docs.astral.sh/uv/)（Python 包管理）
- Kimi Code CLI（已登录）

## 快速开始

### 1. 后端环境（已配置）

```bash
cd paper-lens-backend
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 2. 前端依赖（已配置）

```bash
cd paper-lens-web
npm install
```

### 3. 启动服务

```bash
# 一键启动（前端 + 后端）
./start.sh

# 或手动启动
export KIMI_CLI_PATH=$(which kimi)
cd paper-lens-web
npm run dev
```

打开浏览器访问 **http://localhost:3000**

## 使用说明

1. **上传论文**：拖拽 PDF 到 Web UI，或粘贴 arXiv 链接下载
2. **选择模式**：Speed Read（速览）/ Deep Learn（深度学习）/ Present（展示）/ Chat（自由对话）
3. **交互问答**：当 Kimi 需要向你提问时，Web UI 会弹出选择框
4. **查看笔记**：所有输出保存在 `paper-notes/<论文名>/` 目录下

## 端口配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `PORT` | 3000 | Next.js 前端端口 |
| `PAPER_LENS_BACKEND_PORT` | 8765 | FastAPI 后端端口 |
| `KIMI_CLI_PATH` | `kimi` | Kimi CLI 可执行文件路径 |

## 已知限制

- Kimi CLI 的 `--print` 模式每轮对话会 spawn 新进程（通过 `--resume` 保持状态），因此相比 Claude 的长连接模式，每轮会有约 1-2 秒的启动延迟
- 流式输出为每轮一次性返回（非逐字流式），前端打字机效果取决于每轮消息长度
- 工具调用结果中的图片/文件内容在 JSON 序列化时可能较大
