# Cloud AI Remote Diagnostics

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tools](https://img.shields.io/badge/Tools-49-orange.svg)](#tool-tiers)

A cloud-based AI remote diagnostics system for Windows computers. Users describe PC problems in natural language through a browser, and the cloud AI remotely executes diagnostic and repair commands via a lightweight Windows bridge program, then provides analysis reports.

> **Live Demo:** http://106.54.193.9:8000  
> **Admin Dashboard:** http://106.54.193.9:8000/admin （默认账号：admin / admin）
> **Update Log:** 详见 [CHANGELOG.md](CHANGELOG.md)
> **Project Evolution:** 项目演进史（Python → Go 重写原因、架构变化）详见 [HISTORY.md](HISTORY.md)

---

## Architecture

```
Browser (Web UI)  <-->  Cloud Server (server.py)  <-->  Windows Bridge (bridge.py / bridge.exe)
      |                        |                              |
  Chat interface         FastAPI + AI Agent          Executes systeminfo/dxdiag/
  3-language support     DeepSeek V4 Flash            PowerShell/process mgmt/
  Approval dialog        106.54.193.9:8000            49 diagnostic tools
```

## Features

### AI-Powered Diagnostics
- **Natural Language Interface** -- Describe your PC problem in plain language, AI plans and executes diagnostics
- **49 Windows Tools** -- System info, screenshots, process monitoring, network tests, registry access, event logs, OCR, desktop control, and more
- **Smart Analysis** -- AI collects data, analyzes results, and produces Chinese-language diagnostic reports with markdown formatting

### Safety and Approval System
- **3-Tier Tool Classification** -- Tier 1 (read-only, safe), Tier 2 (interactive, awareness needed), Tier 3 (destructive, approval required)
- **Visual Approval Popup** -- When AI wants to use Tier 2/3 tools, a red popup appears with countdown timer
- **Auto-Approve Toggle** -- Optionally skip Tier 2 approval for faster workflows
- **Tab Title Flash** -- Browser tab flashes when approval is pending (can't miss it!)

### Admin Dashboard
- **Real-time Room Monitoring** -- See all active rooms with browser/bridge connection status
- **Machine Identification** -- Auto-detects hostname, OS, IP address, username on bridge connect
- **Chat History** -- SQLite persistence with full conversation search and export
- **Live Log Viewer** -- View server.log, chat.log in real-time from the dashboard
- **Approval Audit Trail** -- Track every approval request (approved/denied/pending)

### Multi-Language UI
- Simplified Chinese (default)
- Traditional Chinese
- English
- Language preference saved across sessions

### Data Management
- **SQLite Chat Database** -- All conversations persisted with room-level query API
- **Export Conversations** -- One-click .txt download from the chat interface
- **Admin Export** -- Download full chat history per room from the dashboard

## Tool Tiers

| Tier | Count | Type | Examples |
|:---:|:---:|:---|:---|
| **Tier 1** | 26 | Read-Only Diagnostics | `GetSystemInfo`, `Snapshot`, `Ping`, `ListProcesses`, `EventLog`, `OCR`, `NetConnections`, `FileRead` |
| **Tier 2** | 10 | Interactive Control | `Click`, `Type`, `Shortcut`, `FocusWindow`, `Scroll`, `Scrape` |
| **Tier 3** | 13 | System Modification | `Shell`, `KillProcess`, `RegWrite`, `ServiceStart/Stop`, `FileWrite`, `LockScreen` |

### Full Tool List

<details>
<summary>Click to expand all 49 tools</summary>

| Tool | Tier | Description |
|:---|:---:|:---|
| `GetSystemInfo` | T1 | CPU, memory, disk, network, uptime summary |
| `run_systeminfo` | T1 | Raw Windows systeminfo command output |
| `run_dxdiag` | T1 | DirectX diagnostic report (GPU, audio, drivers) |
| `Snapshot` | T1 | Desktop screenshot + window list + UI elements |
| `AnnotatedSnapshot` | T1 | Screenshot with numbered red rectangles on clickable elements |
| `GetClipboard` | T1 | Read Windows clipboard text |
| `ListProcesses` | T1 | List running processes with CPU/memory usage |
| `ServiceList` | T1 | List Windows services and their status |
| `TaskList` | T1 | List Windows scheduled tasks |
| `FileList` | T1 | List directory contents with size and date |
| `FileSearch` | T1 | Search files by glob pattern |
| `FileRead` | T1 | Read file content (text or binary/base64) |
| `FileDownload` | T1 | Download file as base64-encoded content |
| `RegRead` | T1 | Read Windows registry values |
| `Ping` | T1 | Ping a host |
| `PortCheck` | T1 | Check if TCP port is open |
| `NetConnections` | T1 | List active network connections |
| `read_event_log` | T1 | Read Windows System event log |
| `EventLog` | T1 | Read any Windows Event Log with level filter |
| `OCR` | T1 | Extract text from screen via OCR |
| `ScreenRecord` | T1 | Record screen as animated GIF |
| `Notification` | T1 | Show Windows toast notification |
| `Wait` | T1 | Pause execution for N seconds |
| `GetTaskStatus` | T1 | Get status of a specific task |
| `GetRunningTasks` | T1 | List all running/pending tasks |
| `run_powershell` | T1 | Execute read-only PowerShell command |
| `Click` | T2 | Mouse click at screen coordinates |
| `Type` | T2 | Type text (optionally at coordinates) |
| `Move` | T2 | Move mouse or drag |
| `Scroll` | T2 | Scroll at position |
| `Shortcut` | T2 | Execute keyboard shortcut (e.g. ctrl+c) |
| `FocusWindow` | T2 | Bring window to foreground |
| `MinimizeAll` | T2 | Minimize all windows (Win+D) |
| `ReconnectSession` | T2 | Reconnect RDP session to console |
| `Scrape` | T2 | Fetch URL content as markdown |
| `CancelTask` | T2 | Cancel a running task |
| `Shell` | T3 | Execute arbitrary PowerShell command |
| `App` | T3 | Launch/switch/resize applications |
| `KillProcess` | T3 | Kill a process by PID or name |
| `FileWrite` | T3 | Write content to a file |
| `FileUpload` | T3 | Upload file from base64 data |
| `RegWrite` | T3 | Write Windows registry values |
| `ServiceStart` | T3 | Start a Windows service |
| `ServiceStop` | T3 | Stop a Windows service |
| `TaskCreate` | T3 | Create a scheduled task |
| `TaskDelete` | T3 | Delete a scheduled task |
| `SetClipboard` | T3 | Set clipboard text |
| `LockScreen` | T3 | Lock Windows workstation |
| `PlaySound` | T3 | Play audio file on the host |

</details>

## Quick Start

### Prerequisites

- Python 3.10+
- DeepSeek API key (or any OpenAI-compatible API)
- Windows PC (for the bridge)

### Server Setup

```bash
# Clone the repository
git clone https://github.com/ajun2026/cloud-ai-remote-diag.git
cd cloud-ai-remote-diag

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Configure API key
cp .env.example .env
# Edit .env with your DeepSeek API key

# Start the server
python server.py
# Server runs at http://localhost:8000
```

### Bridge Setup (Windows)

```bash
# Install bridge dependencies
pip install psutil Pillow pyautogui pywin32 tabulate thefuzz

# Run the bridge
python bridge.py
# Enter the 6-digit room code from the web UI
```

### Build bridge.exe

```bash
pyinstaller --onefile --name bridge --console --clean bridge.py \
  --hidden-import psutil --hidden-import PIL --hidden-import PIL.ImageGrab \
  --hidden-import pyautogui --hidden-import win32api --hidden-import tabulate \
  --collect-all pyautogui
# Output: dist/bridge.exe (~22MB)
```

## Configuration

Create a `.env` file in the project root:

```env
# API Configuration
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_MODEL=deepseek-chat

# Server Configuration
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
```

## Project Structure

```
cloud-ai-remote-diag/
  server.py              FastAPI + WebSocket + AI Agent core
  bridge.py              Windows bridge (49 tools)
  static/
    index.html           Web UI (zh-CN/zh-TW/EN)
  requirements.txt       Python dependencies
  CLAUDE.md              Project documentation (Chinese)
  logs/                  Runtime logs + SQLite DB (gitignored)
    server.log
    chat.log
    chat.db
  dist/                  Built bridge.exe (gitignored)
```

## API Endpoints

| Endpoint | Method | Description |
|:---|:---:|:---|
| `/` | GET | Web UI |
| `/admin` | GET | Admin dashboard |
| `/api/health` | GET | Server health check |
| `/api/rooms` | POST | Create a new room (returns 6-digit code) |
| `/api/history/{room}` | GET | Get chat history for a room |
| `/api/rooms/list` | GET | List all rooms with message counts |
| `/api/admin/stats` | GET | Server statistics (rooms, messages, approvals) |
| `/api/admin/logs/{name}` | GET | View server/chat/bridge logs |
| `/ws/browser/{room}` | WebSocket | Browser-side connection |
| `/ws/bridge/{room}` | WebSocket | Bridge-side connection |

## WebSocket Protocol

### Browser to Server
```json
{"type": "chat", "content": "My PC is running slow"}
{"type": "approval_response", "id": "approve_xxx", "approved": true}
{"type": "auto_approve_toggle", "enabled": true}
```

### Server to Browser
```json
{"type": "tool_start", "tool": "ListProcesses", "args": {...}, "tier": 1}
{"type": "tool_result", "tool": "ListProcesses", "content": "..."}
{"type": "approval_required", "id": "approve_xxx", "tool": "KillProcess", "tier": 3}
{"type": "ai_message", "content": "# Diagnostic Report..."}
{"type": "status", "content": "Bridge connected [OK]"}
```

### Server to Bridge
```json
{"type": "command", "id": "cmd_1_xxx", "tool": "GetSystemInfo", "args": {}}
```

### Bridge to Server
```json
{"type": "command_result", "id": "cmd_1_xxx", "output": "System: Windows 11..."}
{"type": "identify", "info": {"hostname": "DESKTOP-PC", "os": "Windows 11"}}
```

## Tech Stack

| Component | Technology |
|:---|:---|
| **Server Framework** | FastAPI + Uvicorn |
| **Real-time Communication** | WebSockets |
| **AI Engine** | DeepSeek V4 Flash (OpenAI-compatible API) |
| **Windows Bridge** | Python asyncio + subprocess + ctypes |
| **Desktop Automation** | PyAutoGUI + PyWin32 + Pillow |
| **System Monitoring** | psutil |
| **Database** | SQLite (chat history + approvals) |
| **Frontend** | Vanilla HTML/CSS/JS (zero dependencies) |
| **Packaging** | PyInstaller (single .exe) |

## License

MIT License

## Credits

- **WinRemote MCP** by [dddabtc](https://github.com/dddabtc) -- The 45 Windows tools integrated into the bridge
- **DeepSeek** -- AI model powering the diagnostic agent
- Built with FastAPI, PyAutoGUI, PyWin32, and many other open-source projects

---

**Status:** Production | **Version:** 0.3.0 | **Last Updated:** 2026-08-02
