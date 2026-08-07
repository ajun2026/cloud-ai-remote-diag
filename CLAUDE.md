# 云端 AI 远程运维助手 — 项目说明（供 AI Agent 阅读）

## 项目是什么

一个"云端 AI 远程诊断 Windows 电脑"的系统。用户在浏览器用自然语言描述电脑问题，云端 AI 通过 Windows 上的轻量桥接器（bridge.exe）远程执行诊断命令，分析后给出报告。

## 核心架构

```
浏览器 (Web UI)  ←→  云端 Server (server.py)  ←→  本地桥接器 (bridge.exe on Windows)
     ↑                        ↑                         ↑
  用户聊天界面            FastAPI + Agent核心          执行 systeminfo/dxdiag
                         大脑二选一（brain 参数）        事件日志/PowerShell
                         ├─ DeepSeek（默认，tool-calling）
                         └─ Hermes（自治 agent，经 HTTP 桥）
                         106.54.193.9:8000
```

## Hermes 大脑并存切换（v0.4.0）

- **切换方式**：WebSocket 消息带 `brain` 字段（前端 🧠 下拉）或环境变量 `AGENT_BRAIN=deepseek|hermes`
- **DeepSeek 通道**：原 `run_agent()` 循环（tool-calling，直连 DeepSeek API）
- **Hermes 通道**：`run_agent_hermes()` → 本机 Hermes api_server（`127.0.0.1:8642`，自治 agent）→ 用 curl 调 `POST /api/bridge/execute`（X-Bridge-Secret 认证）操作远程电脑
- **⚠️ 安全红线（重要）**：Hermes api_server 的 `platform_toolsets` 已最小化（仅 web+terminal）；**任何 Hermes agent 都禁止读取/修改本目录文件、禁止 pkill/重启服务、禁止 import server.py**——详见 `docs/Hermes大脑集成与调试记录.md` 事故复盘

## 文件说明

| 文件 | 角色 | 说明 |
|:---|:---|:---|
| `server.py` | 云端服务 | FastAPI + WebSocket + Agent 核心，大脑二选一（DeepSeek / Hermes 桥） |
| `bridge.py` | 桥接器源码 | Python，连接服务器，接收命令并在 Windows 上执行 |
| `dist/bridge.exe` | 桥接器成品 | PyInstaller 打包，8.8MB，单文件，客户双击即用 |
| `static/index.html` | Web UI | 聊天界面，连接弹窗，导出对话，大脑切换下拉，下载桥接器 |
| `static/bridge.exe` | 下载用 | 用户在网页可直接下载 |
| `.env` | 配置 | API Key、Base URL、模型名、Hermes 通道配置 |
| `docs/Hermes大脑集成与调试记录.md` | 文档 | Hermes 大脑集成架构、调试过程、事故复盘 |
| `requirements.txt` | 依赖 | fastapi, uvicorn, websockets, httpx, python-dotenv |
| `winremote-mcp-master.zip` | 待集成 | 含 45 个 Windows 工具的 MCP 项目，计划集成到桥接器 |

## 服务器部署

- **地址：** http://106.54.193.9:8000 （服务名 `server.py`）
- **进程管理：** 通过 SSH 操作
- **项目路径：** `/home/ubuntu/cab-server/`
- **Python 环境：** venv (`/home/ubuntu/cab-server/venv/`)
- **重启命令：** `pkill -f server.py; cd /home/ubuntu/cab-server && python3 -c "import subprocess; subprocess.Popen(['./venv/bin/python','server.py'], start_new_session=True, stdout=open('server.log','a'), stderr=subprocess.STDOUT)"`（**必须脱离 Hermes 进程组**，否则 gateway 重启会连带杀掉；nohup 会被 Hermes 拦截）
- **日志：** `logs/server.log`（运行日志） + `logs/chat.log`（对话记录）

## 当前 AI Agent 能力（49 个工具）

| 等级 | 数量 | 类型 | 说明 |
|:---|:---:|:---|:---|
| Tier 1 | 26 | 只读诊断 | 系统信息、截图、进程列表、文件查看、网络测试、事件日志、OCR |
| Tier 2 | 10 | 交互操作 | 鼠标点击、键盘输入、滚动、窗口操控、快捷键 |
| Tier 3 | 13 | 修改操作 | 执行命令、杀进程、启停服务、注册表写入、文件写入 |

## 9 个问题进度

| # | 问题 | 状态 |
|:---:|:---|:---:|
| 1 | AI Agent 在哪 | ✅ 已实现 |
| 2 | 能做什么 | ✅ 49 个工具（Tier 1/2/3） |
| 3 | 软件问题修复 | ✅ 已实现（Tier 3 审批弹窗） |
| 4 | 日志记录 | ✅ 已完成（server + bridge 双端日志 + SQLite 持久化） |
| 5 | 多端查看历史 | ✅ 已实现（SQLite + API + /admin） |
| 6 | 管理后台 | ✅ 已实现（/admin 页面，房间管理、日志查看、机器识别） |
| 7 | 页面下载桥接器 | ✅ 已完成（弹窗内有下载+说明） |
| 8 | 导出对话 | ✅ 已完成（前端一键下载.txt） |
| 9 | 集成 WinRemote MCP | ✅ 已完成（45 个工具内嵌到 bridge.py） |

## 关键约定

- 所有 Python 文件、HTML 文件**避免使用 emoji**（Windows GBK 编码会崩溃）
- bridge.py 用 `logging` 模块，不用 `print`
- 服务器端文件编码 UTF-8
- 桥接器默认连接 `ws://106.54.193.9:8000`
- DeepSeek API Key 在 `.env` 中

## 本地开发

- Python 3.12.8 安装在 `C:\Python312`
- 打包桥接器：`pyinstaller --onefile --name bridge --console --clean bridge.py`
- 输出在 `dist/bridge.exe`
- 当前电脑可直接跑 `python bridge.py` 测试（已安装所有依赖）
