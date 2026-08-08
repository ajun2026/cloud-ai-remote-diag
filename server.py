"""
Cloud AI Remote Diagnostics Assistant — Server
FastAPI + WebSocket + Agent Core
Features: 49 tools, Tier 2/3 approval, SQLite chat history, admin dashboard
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ============================================================
# Logging config
# ============================================================
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

chat_logger = logging.getLogger("chat")
chat_logger.setLevel(logging.INFO)
chat_handler = logging.FileHandler(LOG_DIR / "chat.log", encoding="utf-8")
chat_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
chat_logger.addHandler(chat_handler)

run_logger = logging.getLogger("server")
run_logger.setLevel(logging.INFO)
run_handler = logging.FileHandler(LOG_DIR / "server.log", encoding="utf-8")
run_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
run_logger.addHandler(run_handler)
console = logging.StreamHandler()
console.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
console.setLevel(logging.INFO)
run_logger.addHandler(console)

# ============================================================
# Config
# ============================================================
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-your-api-key-here")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "deepseek-chat")

# ============================================================
# Hermes 通道配置（Hermes 为默认大脑；老 DeepSeek 通道保留兜底）
#   AGENT_BRAIN: hermes（默认）| deepseek（兜底，前端入口已隐藏）
#   前端 WebSocket 消息可带 "brain": "hermes" 按请求覆盖
# ============================================================
HERMES_BASE_URL = os.getenv("HERMES_BASE_URL", "http://127.0.0.1:8642/v1")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
HERMES_MODEL = os.getenv("HERMES_MODEL", "hermes-agent")
AGENT_BRAIN = os.getenv("AGENT_BRAIN", "hermes")  # hermes（默认）| deepseek（兜底）

# HTTP 桥接接口共享密钥（Hermes 通过 curl 调 /api/bridge/execute 时校验）
BRIDGE_HTTP_SECRET = os.getenv("BRIDGE_HTTP_SECRET", "")

SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

# ============================================================
# SQLite database for chat history
# ============================================================
DB_PATH = LOG_DIR / "chat.db"

def _db_connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = _db_connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT NOT NULL,
            role TEXT NOT NULL,       -- 'user', 'ai', 'tool', 'status', 'error'
            content TEXT NOT NULL,
            tool_name TEXT DEFAULT NULL,
            tier INTEGER DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_code, created_at)")
    # Approval log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args TEXT DEFAULT NULL,
            tier INTEGER NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,  -- 0=pending, 1=approved, -1=denied
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # 用户表（工号/密码/角色）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,       -- 工号（登录账号）
            name TEXT NOT NULL DEFAULT '',        -- 姓名
            password_hash TEXT NOT NULL,          -- 密码哈希（salt$hash）
            role TEXT NOT NULL DEFAULT 'engineer', -- admin | engineer
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # 房间业务信息表（房间码 + SN + 工单号 + 型号 + 工程师 + 创建时间）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT NOT NULL UNIQUE,
            sn TEXT NOT NULL,
            ticket_no TEXT NOT NULL,
            machine_model TEXT DEFAULT '',
            engineer_username TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rooms_engineer ON rooms(engineer_username, created_at)")
    conn.commit()
    conn.close()
    seed_users()


def hash_password(pw: str) -> str:
    """PBKDF2 密码哈希，格式 salt$hex。"""
    salt = secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return f"{salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
        return hmac.compare_digest(calc, h)
    except Exception:
        return False


def seed_users():
    """首次启动时创建初始账号：admin（来自环境变量）+ test1~test10。"""
    try:
        conn = _db_connect()
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count > 0:
            conn.close()
            return
        # 管理员：沿用环境变量 ADMIN_USERNAME / ADMIN_PASSWORD
        admin_user = os.getenv("ADMIN_USERNAME", "admin")
        admin_pw = os.getenv("ADMIN_PASSWORD", "admin")
        conn.execute(
            "INSERT INTO users (username, name, password_hash, role) VALUES (?, ?, ?, ?)",
            (admin_user, "管理员", hash_password(admin_pw), "admin"),
        )
        for i in range(1, 11):
            conn.execute(
                "INSERT INTO users (username, name, password_hash, role) VALUES (?, ?, ?, ?)",
                (f"test{i}", f"测试用户{i}", hash_password(f"test{i}"), "engineer"),
            )
        conn.commit()
        conn.close()
        run_logger.info("[seed] 初始账号已创建：admin + test1~test10")
    except Exception as e:
        run_logger.error(f"[seed] seed_users failed: {e}")


def get_user(username: str) -> Optional[dict]:
    try:
        conn = _db_connect()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None

def save_message(room_code: str, role: str, content: str, tool_name: str = None, tier: int = None):
    try:
        conn = _db_connect()
        conn.execute(
            "INSERT INTO messages (room_code, role, content, tool_name, tier) VALUES (?, ?, ?, ?, ?)",
            (room_code, role, content, tool_name, tier)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        run_logger.error(f"DB save error: {e}")

def save_approval(room_code: str, tool_name: str, args: dict, tier: int, approved: int):
    try:
        conn = _db_connect()
        conn.execute(
            "INSERT INTO approvals (room_code, tool_name, args, tier, approved) VALUES (?, ?, ?, ?, ?)",
            (room_code, tool_name, json.dumps(args, ensure_ascii=False), tier, approved)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        run_logger.error(f"DB approval save error: {e}")

def get_room_messages(room_code: str, limit: int = 200) -> list[dict]:
    try:
        conn = _db_connect()
        rows = conn.execute(
            "SELECT * FROM messages WHERE room_code = ? ORDER BY created_at ASC LIMIT ?",
            (room_code, limit)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_recent_context(room_code: str, current_user_msg: str, max_msgs: int = 20, max_chars: int = 600) -> list[dict]:
    """取该房间最近的 user/ai 对话作为模型上下文（排除刚保存的当前消息本身）。

    - 默认最多 20 条、每条截断 600 字符，防止 token 爆炸
    - 两个大脑（DeepSeek / Hermes）共用，保证"问了后面记得前面"
    """
    rows = get_room_messages(room_code, 200)
    pairs = [r for r in rows if r["role"] in ("user", "ai")]
    # 最后一条 user 消息刚被保存、还没回复——跳过，避免与本次 user_message 重复
    if pairs and pairs[-1]["role"] == "user" and pairs[-1]["content"] == current_user_msg:
        pairs = pairs[:-1]
    ctx = [{"role": r["role"], "content": (r["content"] or "")[:max_chars]} for r in pairs]
    return ctx[-max_msgs:]

def get_all_rooms() -> list[dict]:
    try:
        conn = _db_connect()
        rows = conn.execute("""
            SELECT room_code,
                   MIN(created_at) AS first_seen,
                   MAX(created_at) AS last_seen,
                   COUNT(*) AS msg_count
            FROM messages
            GROUP BY room_code
            ORDER BY last_seen DESC
            LIMIT 100
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

def get_server_stats() -> dict:
    try:
        conn = _db_connect()
        total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        total_rooms = conn.execute("SELECT COUNT(DISTINCT room_code) FROM messages").fetchone()[0]
        total_tools = conn.execute("SELECT COUNT(*) FROM messages WHERE role='tool'").fetchone()[0]
        approvals = conn.execute(
            "SELECT approved, COUNT(*) FROM approvals GROUP BY approved"
        ).fetchall()
        approval_stats = {"pending": 0, "approved": 0, "denied": 0}
        for row in approvals:
            if row[0] == 0: approval_stats["pending"] = row[1]
            elif row[0] == 1: approval_stats["approved"] = row[1]
            elif row[0] == -1: approval_stats["denied"] = row[1]
        conn.close()
        return {
            "total_messages": total_msgs,
            "total_rooms": total_rooms,
            "total_tool_calls": total_tools,
            "approval_stats": approval_stats,
        }
    except Exception as e:
        return {"error": str(e)}

# Initialize DB on startup
init_db()

# ============================================================
# Command risk classifier — for the generic RunCommand tool
# ============================================================
# Returns (tier, category, reason):
#   tier 1  → read-only, auto-execute
#   tier 3  → modifying, requires user approval popup
#   tier -1 → dangerous, hard-blocked (never executed)
_READONLY_RE = [
    r"^\s*(get|select|where|sort|group|measure|format-table|ft|fl|out-string|convertto-json|convertto-csv|compare-object|find)\b",
    r"^\s*(systeminfo|ipconfig|tasklist|dir|type|whoami|netstat|ping|tracert|nslookup|hostname|ver|vol|tree|findstr|reg\s+query|sc\s+query|schtasks\s+/query|driverquery|gpresult|msinfo32)\b",
    r"^\s*get-\w+",
    r"^\s*test-\w+",
    r"^\s*[a-z]:\\",  # path listing
    # Linux read-only
    r"^\s*(uname|lscpu|free|df|lsblk|blkid|lsusb|lspci|dmidecode|smartctl|journalctl|dmesg|ps|top|htop|ss|ip|ifconfig|netstat|route|uptime|whoami|hostname|cat(?!\s*>)|ls|find|du|mount|lsof|vmstat|iostat|id|groups|last|w|date|pwd|sysctl|stat|file|which|whereis|uname -a)\b",
    r"^\s*systemctl\s+(status|list-units|show|is-active|is-enabled)\b",
    r"^\s*fdisk\s+-l\b",
]
_MODIFY_RE = [
    r"^\s*(set|new|remove|copy|move|rename|start|stop|restart|restart-computer|stop-computer|install|uninstall|update|write|clear|flush|disable|enable|invoke|kill|taskkill|shutdown|format|del|rd|mkdir|rmdir|xcopy|robocopy|wmic|diskpart|takeown|icacls|cacls|attrib|reg\s+add|reg\s+delete|sc\s+(start|stop|config|delete)|net\s+(start|stop|user|localgroup|share))\b",
    r"^\s*set-\w+",
    r"^\s*new-\w+",
    r"^\s*remove-\w+",
    r"^\s*stop-\w+",
    r"^\s*start-\w+",
    r"^\s*restart-\w+",
    r"^\s*disable-\w+",
    r"^\s*enable-\w+",
    r"^\s*clear-\w+",
    r"^\s*invoke-\w+",
    r"^\s*add-\w+",
    # Linux modify
    r"^\s*(apt|apt-get|aptitude|dnf|yum|zypper|pacman|apk)\s+(install|remove|purge|autoremove|update|upgrade|dist-upgrade)\b",
    r"^\s*pip\d?\s+(install|uninstall)\b",
    r"^\s*(kill|pkill|killall)\b",
    r"^\s*(rm|mv|cp|mkdir|rmdir|touch|chmod|chown|ln|tar|unzip|zip|gzip|gunzip|curl\s+-o|wget\s+-O)\b",
    r"^\s*systemctl\s+(start|stop|restart|reload|enable|disable|mask|set-default|daemon-reload)\b",
    r"^\s*service\s+\S+\s+(start|stop|restart|reload)\b",
    r"^\s*(useradd|usermod|userdel|passwd|groupadd|groupdel|chsh)\b",
    r"^\s*(iptables|ufw|firewall-cmd|nft|sed\s+-i|awk\s+-i)\b",
    r"^\s*(mount|umount|reboot|poweroff|shutdown|halt|swapoff|swapon)\b",
    r"^\s*(echo|printf|tee)\s+.*[>]",  # 写文件重定向
    r"^\s*cat\s+>",
]
_DANGEROUS_RE = [
    r"\bformat\s+[a-z]:",
    r"\bdiskpart\b",
    r"\bformat-volume\b",
    r"\bclear-eventlog\b",
    r"\bremove-item\b.*\b(recurse|force)\b",
    r"\bdel\b.*\s/s\b",
    r"\brd\b.*\s/s\b",
    r"\breg\s+delete\b",
    r"\bsc\s+delete\b",
    r"\bwmic\b.*\bdelete\b",
    r"\btakeown\b",
    r"\bicacls\b.*\b/grant\b",
    r"\bnet\s+user\b",
    r"\bnet\s+localgroup\b",
    r"\bbcdedit\b.*\b/delete\b",
    r"\bremove-\w+\b.*\b-recurse\b",
    # destructive verbs hidden after a pipe: Get-Process | Stop-Process etc.
    r"\|\s*(stop|kill|remove|clear|set|new|invoke|disable|enable|format-volume)-\w+",
    # Linux destructive
    r"rm\s+-(rf|fr)\s*(\s|/|\*)+",        # rm -rf / 或 /* 等
    r"\bmkfs(\.\w+)?\s+",                  # 创建文件系统
    r"\bdd\s+.*\bof=/dev/",                # dd 写裸设备
    r"\bshred\s",                          # 粉碎
    r"\bwipefs\s",                         # 擦除文件系统
    r"\bparted\s+\S+\s+(mklabel|rm|mkpart)\b",
    r"\bfdisk\s+(?!-l\b)",                 # fdisk 非只读操作
    r"\bgdisk\s",
    r"\bmkswap\s+/dev/",
    r"\bchmod\s+-R\s+[0-7]{3,4}\s+/\b",
    r"\bchown\s+-R\s+\S+\s+/\b",
    r"echo\s+[^|]*>\s*/dev/sd",
    r":\(\)\s*\{[^}]*\|[^}]*&",            # fork bomb
]


def classify_command(command: str) -> tuple[int, str, str]:
    """Classify a PowerShell command's risk. Returns (tier, category, reason)."""
    cmd = (command or "").strip()
    if not cmd:
        return 3, "empty", "空命令"
    low = cmd.lower()
    # 1) dangerous → hard block
    for pat in _DANGEROUS_RE:
        if re.search(pat, low):
            return -1, "dangerous", f"高危命令已被系统拦截: 匹配规则 {pat!r}"
    # 2) read-only → auto
    for pat in _READONLY_RE:
        if re.match(pat, low):
            return 1, "readonly", "只读命令，自动执行"
    # 3) modify → approval
    for pat in _MODIFY_RE:
        if re.match(pat, low):
            return 3, "modify", "修改类命令，需用户批准"
    # 4) unknown → conservative: require approval
    return 3, "unknown", "无法识别的命令，默认需用户批准"


# ============================================================
# Tool definitions — 3 tiers
# ============================================================
TOOL_TIERS = {
    "run_systeminfo": 1, "run_dxdiag": 1, "read_event_log": 1, "run_powershell": 1,
    "RunCommand": 3,  # dynamic tier — overridden by classify_command at runtime
    "GetSystemInfo": 1, "Snapshot": 1, "AnnotatedSnapshot": 1, "GetClipboard": 1,
    "ListProcesses": 1, "FileList": 1, "FileSearch": 1, "FileRead": 1,
    "FileDownload": 1, "RegRead": 1, "ServiceList": 1, "TaskList": 1,
    "EventLog": 1, "Ping": 1, "PortCheck": 1, "NetConnections": 1,
    "OCR": 1, "ScreenRecord": 1, "Notification": 1, "Wait": 1,
    "GetTaskStatus": 1, "GetRunningTasks": 1,
    # Tier 2 — Interactive
    "Click": 2, "Type": 2, "Move": 2, "Scroll": 2, "Shortcut": 2,
    "FocusWindow": 2, "MinimizeAll": 2, "Scrape": 2, "CancelTask": 2,
    "ReconnectSession": 2,
    # Tier 3 — Dangerous
    "Shell": 3, "App": 3, "PlaySound": 3, "FileWrite": 3, "FileUpload": 3,
    "KillProcess": 3, "RegWrite": 3, "ServiceStart": 3, "ServiceStop": 3,
    "TaskCreate": 3, "TaskDelete": 3, "SetClipboard": 3, "LockScreen": 3,
    "Shutdown": 3,
}

TOOLS = [
    {"type":"function","function":{"name":"run_systeminfo","description":"Run systeminfo to get OS version, hardware, memory, and network info.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"run_dxdiag","description":"Generate DirectX diagnostic report (dxdiag) with GPU, audio, and driver info.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"read_event_log","description":"Read Windows System event log for errors and warnings.","parameters":{"type":"object","properties":{"max_events":{"type":"integer","description":"Max entries (default 50)","default":50},"level":{"type":"string","enum":["Critical","Error","Warning","Information"],"description":"Level filter"}},"required":[]}}},
    {"type":"function","function":{"name":"run_powershell","description":"Execute a read-only PowerShell diagnostic command. Do NOT use for write/delete/modify.","parameters":{"type":"object","properties":{"command":{"type":"string","description":"PowerShell command (read-only only)"}},"required":["command"]}}},
    {"type":"function","function":{"name":"GetSystemInfo","description":"Get system info: CPU, memory, disk, network, uptime. Quick summary.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"Snapshot","description":"Capture desktop screenshot + window list + interactive UI elements. Returns base64 JPEG + text summary.","parameters":{"type":"object","properties":{"use_vision":{"type":"boolean","description":"Include screenshot (default true)","default":True},"quality":{"type":"integer","description":"JPEG quality 1-100","default":75},"max_width":{"type":"integer","description":"Max image width, 0=native","default":0},"monitor":{"type":"integer","description":"Monitor 0=all, 1/2/3=specific","default":0}},"required":[]}}},
    {"type":"function","function":{"name":"AnnotatedSnapshot","description":"Screenshot with numbered red rectangles on clickable UI elements.","parameters":{"type":"object","properties":{"max_elements":{"type":"integer","description":"Max elements (default 30)","default":30},"quality":{"type":"integer","description":"JPEG quality 1-100","default":75},"max_width":{"type":"integer","description":"Max image width","default":0}},"required":[]}}},
    {"type":"function","function":{"name":"GetClipboard","description":"Read the Windows clipboard text content.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"SetClipboard","description":"Set the Windows clipboard text. [Tier 3 — write]","parameters":{"type":"object","properties":{"text":{"type":"string","description":"Text to place on clipboard"}},"required":["text"]}}},
    {"type":"function","function":{"name":"Click","description":"Mouse click at screen coordinates. [Tier 2 — interactive]","parameters":{"type":"object","properties":{"x":{"type":"integer","description":"X coordinate"},"y":{"type":"integer","description":"Y coordinate"},"button":{"type":"string","enum":["left","right","middle"],"default":"left"},"action":{"type":"string","enum":["click","double","hover"],"default":"click"}},"required":["x","y"]}}},
    {"type":"function","function":{"name":"Type","description":"Type text, optionally at coordinates. [Tier 2]","parameters":{"type":"object","properties":{"text":{"type":"string","description":"Text to type"},"x":{"type":"integer","default":0},"y":{"type":"integer","default":0},"clear":{"type":"boolean","default":False},"press_enter":{"type":"boolean","default":False}},"required":["text"]}}},
    {"type":"function","function":{"name":"Move","description":"Move mouse or drag. [Tier 2]","parameters":{"type":"object","properties":{"x":{"type":"integer"},"y":{"type":"integer"},"drag":{"type":"boolean","default":False},"start_x":{"type":"integer","default":0},"start_y":{"type":"integer","default":0},"duration":{"type":"number","default":0.3}},"required":["x","y"]}}},
    {"type":"function","function":{"name":"Scroll","description":"Scroll at position. [Tier 2]","parameters":{"type":"object","properties":{"amount":{"type":"integer"},"x":{"type":"integer","default":0},"y":{"type":"integer","default":0},"horizontal":{"type":"boolean","default":False}},"required":["amount"]}}},
    {"type":"function","function":{"name":"Shortcut","description":"Execute keyboard shortcut e.g. 'ctrl+c', 'alt+tab'. [Tier 2]","parameters":{"type":"object","properties":{"keys":{"type":"string","description":"Shortcut string"}},"required":["keys"]}}},
    {"type":"function","function":{"name":"Wait","description":"Pause execution for N seconds.","parameters":{"type":"object","properties":{"seconds":{"type":"number","default":1.0}},"required":[]}}},
    {"type":"function","function":{"name":"FocusWindow","description":"Bring window to foreground. [Tier 2]","parameters":{"type":"object","properties":{"title":{"type":"string","default":""},"handle":{"type":"integer","default":0}},"required":[]}}},
    {"type":"function","function":{"name":"MinimizeAll","description":"Minimize all windows (Win+D). [Tier 2]","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"App","description":"Launch/switch/resize an application. [Tier 3]","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["launch","switch","resize"],"default":"launch"},"name":{"type":"string","default":""},"args":{"type":"string","default":""},"handle":{"type":"integer","default":0},"width":{"type":"integer","default":0},"height":{"type":"integer","default":0}},"required":[]}}},
    {"type":"function","function":{"name":"ReconnectSession","description":"Reconnect disconnected RDP session to console. [Tier 2]","parameters":{"type":"object","properties":{"force":{"type":"boolean","default":False}},"required":[]}}},
    {"type":"function","function":{"name":"Notification","description":"Show a Windows toast notification.","parameters":{"type":"object","properties":{"title":{"type":"string","default":"Bridge Alert"},"message":{"type":"string","default":""}},"required":[]}}},
    {"type":"function","function":{"name":"PlaySound","description":"Play audio file (.wav/.mp3/.ogg). [Tier 3]","parameters":{"type":"object","properties":{"path":{"type":"string","default":""},"url":{"type":"string","default":""}},"required":[]}}},
    {"type":"function","function":{"name":"LockScreen","description":"Lock the Windows workstation. [Tier 3]","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"Shutdown","description":"Shut down the Windows PC immediately. [Tier 3 — system power off]","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"RunCommand","description":"Execute an arbitrary PowerShell command on the remote PC. Use this for ANY request not covered by the other tools — e.g. querying driver details, temperature, startup items, BIOS info, power plan, disk health, or performing a system change the user asked for. The system auto-classifies the command: read-only commands run immediately; modifying commands show an approval popup; dangerous commands (format/diskpart/registry delete etc.) are blocked.","parameters":{"type":"object","properties":{"command":{"type":"string","description":"PowerShell command to execute (e.g. 'Get-Temperature', 'shutdown /s /t 0', 'Get-Service | Where-Object Status -eq Running')"}},"required":["command"]}}},
    {"type":"function","function":{"name":"ListProcesses","description":"List running processes with CPU/memory usage.","parameters":{"type":"object","properties":{"filter":{"type":"string","default":""},"sort_by":{"type":"string","enum":["cpu","memory","name"],"default":"memory"},"limit":{"type":"integer","default":30}},"required":[]}}},
    {"type":"function","function":{"name":"KillProcess","description":"Kill a process by PID or name. [Tier 3 — destructive]","parameters":{"type":"object","properties":{"pid":{"type":"integer","default":0},"name":{"type":"string","default":""}},"required":[]}}},
    {"type":"function","function":{"name":"FileRead","description":"Read file content. Returns base64 for binary.","parameters":{"type":"object","properties":{"path":{"type":"string"},"encoding":{"type":"string","default":"utf-8"}},"required":["path"]}}},
    {"type":"function","function":{"name":"FileWrite","description":"Write content to a file. [Tier 3]","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"encoding":{"type":"string","default":"utf-8"},"append":{"type":"boolean","default":False}},"required":["path","content"]}}},
    {"type":"function","function":{"name":"FileList","description":"List directory contents with size and date.","parameters":{"type":"object","properties":{"path":{"type":"string","default":"."},"show_hidden":{"type":"boolean","default":False}},"required":[]}}},
    {"type":"function","function":{"name":"FileSearch","description":"Search files by name pattern (glob).","parameters":{"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string","default":"."},"recursive":{"type":"boolean","default":True},"limit":{"type":"integer","default":50}},"required":["pattern"]}}},
    {"type":"function","function":{"name":"FileDownload","description":"Download a file as base64-encoded content.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
    {"type":"function","function":{"name":"FileUpload","description":"Upload a file from base64-encoded content. [Tier 3]","parameters":{"type":"object","properties":{"path":{"type":"string"},"data_base64":{"type":"string"}},"required":["path","data_base64"]}}},
    {"type":"function","function":{"name":"RegRead","description":"Read a Windows registry value.","parameters":{"type":"object","properties":{"key":{"type":"string"},"value_name":{"type":"string"}},"required":["key","value_name"]}}},
    {"type":"function","function":{"name":"RegWrite","description":"Write a Windows registry value. [Tier 3 — dangerous]","parameters":{"type":"object","properties":{"key":{"type":"string"},"value_name":{"type":"string"},"data":{"type":"string"},"reg_type":{"type":"string","enum":["REG_SZ","REG_EXPAND_SZ","REG_DWORD","REG_QWORD","REG_BINARY","REG_MULTI_SZ"],"default":"REG_SZ"}},"required":["key","value_name","data"]}}},
    {"type":"function","function":{"name":"ServiceList","description":"List Windows services.","parameters":{"type":"object","properties":{"filter":{"type":"string","default":""}},"required":[]}}},
    {"type":"function","function":{"name":"ServiceStart","description":"Start a Windows service. [Tier 3]","parameters":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}},
    {"type":"function","function":{"name":"ServiceStop","description":"Stop a Windows service. [Tier 3]","parameters":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}},
    {"type":"function","function":{"name":"TaskList","description":"List Windows scheduled tasks.","parameters":{"type":"object","properties":{"filter":{"type":"string","default":""}},"required":[]}}},
    {"type":"function","function":{"name":"TaskCreate","description":"Create a scheduled task. [Tier 3 — persistence]","parameters":{"type":"object","properties":{"name":{"type":"string"},"command":{"type":"string"},"schedule":{"type":"string"}},"required":["name","command","schedule"]}}},
    {"type":"function","function":{"name":"TaskDelete","description":"Delete a scheduled task. [Tier 3]","parameters":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}},
    {"type":"function","function":{"name":"EventLog","description":"Read Windows Event Log (System/Application/Security).","parameters":{"type":"object","properties":{"log_name":{"type":"string","default":"System"},"count":{"type":"integer","default":20},"level":{"type":"string","enum":["critical","error","warning","information","verbose"],"default":""}},"required":[]}}},
    {"type":"function","function":{"name":"Ping","description":"Ping a host.","parameters":{"type":"object","properties":{"host":{"type":"string"},"count":{"type":"integer","default":4}},"required":["host"]}}},
    {"type":"function","function":{"name":"PortCheck","description":"Check if a TCP port is open.","parameters":{"type":"object","properties":{"host":{"type":"string"},"port":{"type":"integer"},"timeout":{"type":"number","default":5.0}},"required":["host","port"]}}},
    {"type":"function","function":{"name":"NetConnections","description":"List active network connections.","parameters":{"type":"object","properties":{"filter":{"type":"string","default":""},"limit":{"type":"integer","default":50}},"required":[]}}},
    {"type":"function","function":{"name":"OCR","description":"Extract text from screen using OCR.","parameters":{"type":"object","properties":{"left":{"type":"integer","default":0},"top":{"type":"integer","default":0},"right":{"type":"integer","default":0},"bottom":{"type":"integer","default":0},"lang":{"type":"string","default":"eng"}},"required":[]}}},
    {"type":"function","function":{"name":"ScreenRecord","description":"Record screen as animated GIF.","parameters":{"type":"object","properties":{"duration":{"type":"number","default":3.0},"fps":{"type":"integer","default":5},"left":{"type":"integer","default":0},"top":{"type":"integer","default":0},"right":{"type":"integer","default":0},"bottom":{"type":"integer","default":0},"max_width":{"type":"integer","default":800}},"required":[]}}},
    {"type":"function","function":{"name":"Shell","description":"Execute a PowerShell command. [Tier 3 — full system access]","parameters":{"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"integer","default":30},"cwd":{"type":"string","default":""}},"required":["command"]}}},
    {"type":"function","function":{"name":"Scrape","description":"Fetch URL content as markdown. [Tier 2]","parameters":{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}}},
    {"type":"function","function":{"name":"CancelTask","description":"Cancel a running task. [Tier 2]","parameters":{"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]}}},
    {"type":"function","function":{"name":"GetTaskStatus","description":"Get status of a task or list recent tasks.","parameters":{"type":"object","properties":{"task_id":{"type":"string","default":""}},"required":[]}}},
    {"type":"function","function":{"name":"GetRunningTasks","description":"List all running and pending tasks.","parameters":{"type":"object","properties":{},"required":[]}}},
]

# ============================================================
# v2 管道化配置（go-pipe bridge）
# ============================================================
# 桌面操控类工具：管道化设计下默认隐藏（行为面收敛，杀软友好）。
# 如需恢复，设 ENABLE_DESKTOP_TOOLS=1 重新加载即可（旧 python bridge 仍需要它们）。
ENABLE_DESKTOP_TOOLS = os.getenv("ENABLE_DESKTOP_TOOLS", "0") == "1"
DESKTOP_TOOLS = {
    "Snapshot", "AnnotatedSnapshot", "GetClipboard", "SetClipboard",
    "Click", "Type", "Move", "Scroll", "Shortcut", "Wait",
    "FocusWindow", "MinimizeAll", "App", "ReconnectSession",
    "Notification", "PlaySound", "LockScreen", "OCR", "ScreenRecord", "Scrape",
    "TaskCreate", "TaskDelete", "CancelTask", "GetTaskStatus", "GetRunningTasks",
}
if not ENABLE_DESKTOP_TOOLS:
    TOOLS = [t for t in TOOLS if t["function"]["name"] not in DESKTOP_TOOLS]

# ============================================================
# 平台感知：Linux / macOS 平台剔除 Windows 专用工具
# 防止 AI 在 Linux 上调用 run_dxdiag / run_powershell / RegRead 等
# Windows 命令，导致 bash 里跑 PowerShell 语法报错。
# ============================================================
WINDOWS_ONLY_TOOLS = {
    "run_dxdiag",   # DirectX 诊断，仅 Windows
    "run_powershell",  # PowerShell 语法，仅 Windows（Linux 用 bash）
    "RegRead",      # 注册表，仅 Windows
    "RegWrite",     # 注册表，仅 Windows
    "GetClipboard", "SetClipboard",  # 剪贴板（桌面工具已过滤，双保险）
}

def get_tools_for_platform(platform: str) -> list:
    """按目标平台过滤 TOOLS。Windows 返回全量；Linux/macOS 剔除 Windows 专用工具。"""
    if platform == "windows":
        return TOOLS
    return [t for t in TOOLS if t["function"]["name"] not in WINDOWS_ONLY_TOOLS]

# v2 工具 → 命令模板（Windows: PowerShell / CMD；Linux: bash）
# 模板中的 {arg} 占位符取自工具参数；timeout 为默认秒数。
V2_COMMAND_TEMPLATES = {
    "windows": {
        "run_systeminfo":   ("systeminfo", 90),
        "run_dxdiag":       ("dxdiag /whql:off /t \"%TEMP%\\dxdiag.txt\" 2>nul & type \"%TEMP%\\dxdiag.txt\"", 150),
        "read_event_log":   ("Get-WinEvent -LogName System -MaxEvents {max_events} -ErrorAction SilentlyContinue | Select-Object -First {max_events} TimeCreated,Id,LevelDisplayName,ProviderName,Message | Format-Table -AutoSize -Wrap", 90),
        "run_powershell":   ("{command}", 60),
        "RunCommand":       ("{command}", 60),
        "Shell":            ("{command}", 60),
        "Shutdown":         ("shutdown /s /t 0", 30),
        "GetSystemInfo":    ("systeminfo", 90),
        "ListProcesses":    ("Get-Process | Sort-Object WS -Descending | Select-Object -First {limit} Id,ProcessName,@{n='CPU(s)';e={$_.CPU}},@{n='Mem(MB)';e={[math]::Round($_.WS/1MB,1)}} | Format-Table -AutoSize", 60),
        "KillProcess":      ("Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue; if ('{name}' -ne '') {{ Stop-Process -Name '{name}' -Force -ErrorAction SilentlyContinue }}", 30),
        "FileRead":         ("Get-Content -Raw -Path '{path}' -ErrorAction SilentlyContinue", 60),
        "FileList":         ("Get-ChildItem -Force -Path '{path}' -ErrorAction SilentlyContinue | Select-Object Mode,LastWriteTime,Length,Name | Format-Table -AutoSize", 60),
        "FileSearch":       ("Get-ChildItem -Path '{path}' -Recurse -Filter '{pattern}' -ErrorAction SilentlyContinue | Select-Object -First {limit} FullName,Length | Format-Table -AutoSize", 120),
        "RegRead":          ("Get-ItemProperty -Path '{key}' -ErrorAction SilentlyContinue | Select-Object '{value_name}' | Format-List", 30),
        "ServiceList":      ("Get-Service | Where-Object {{ $_.Name -like '*{filter}*' }} | Select-Object Status,Name,DisplayName | Format-Table -AutoSize", 60),
        "ServiceStart":     ("Start-Service -Name '{name}' -ErrorAction SilentlyContinue; Get-Service -Name '{name}' | Select-Object Status,Name | Format-Table -AutoSize", 60),
        "ServiceStop":      ("Stop-Service -Name '{name}' -Force -ErrorAction SilentlyContinue; Get-Service -Name '{name}' | Select-Object Status,Name | Format-Table -AutoSize", 60),
        "EventLog":         ("Get-WinEvent -LogName {log_name} -MaxEvents {count} -ErrorAction SilentlyContinue | Select-Object TimeCreated,Id,LevelDisplayName,ProviderName,Message | Format-Table -AutoSize -Wrap", 90),
        "Ping":             ("ping -n {count} {host}", 60),
        "PortCheck":        ("Test-NetConnection -ComputerName {host} -Port {port} -WarningAction SilentlyContinue | Select-Object ComputerName,RemotePort,TcpTestSucceeded | Format-List", 60),
        "NetConnections":   ("Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue | Select-Object -First {limit} LocalAddress,LocalPort,RemoteAddress,RemotePort,OwningProcess | Format-Table -AutoSize", 60),
    },
    "linux": {
        "run_systeminfo":   ("uname -a; echo '---'; lscpu; echo '---'; free -h; echo '---'; df -h; echo '---'; ip addr 2>/dev/null || ifconfig", 90),
        "read_event_log":   ("journalctl -p err..emerg -n {max_events} --no-pager 2>/dev/null || dmesg | tail -{max_events}", 60),
        "run_powershell":   ("{command}", 60),
        "RunCommand":       ("{command}", 60),
        "Shell":            ("{command}", 60),
        "Shutdown":         ("shutdown -h now", 30),
        "GetSystemInfo":    ("uname -a; echo '---'; lscpu; echo '---'; free -h; echo '---'; df -h; echo '---'; uptime", 90),
        "ListProcesses":    ("ps aux --sort=-%mem | head -{limit}", 60),
        "KillProcess":      ("kill -9 {pid} 2>/dev/null; pkill -f '{name}' 2>/dev/null", 30),
        "FileRead":         ("cat '{path}' 2>/dev/null", 60),
        "FileList":         ("ls -lah '{path}' 2>/dev/null", 60),
        "FileSearch":       ("find '{path}' -name '{pattern}' -type f 2>/dev/null | head -{limit}", 120),
        "ServiceList":      ("systemctl list-units --type=service --no-pager 2>/dev/null | grep -i '{filter}' | head -30", 60),
        "ServiceStart":     ("systemctl start {name} 2>&1; systemctl status {name} --no-pager 2>&1 | head -10", 60),
        "ServiceStop":      ("systemctl stop {name} 2>&1; systemctl status {name} --no-pager 2>&1 | head -10", 60),
        "EventLog":         ("journalctl -u {log_name} -n {count} --no-pager 2>/dev/null || journalctl -n {count} --no-pager", 90),
        "Ping":             ("ping -c {count} {host}", 60),
        "PortCheck":        ("timeout 10 bash -c 'echo > /dev/tcp/{host}/{port}' && echo '端口 {port} 开放' || echo '端口 {port} 关闭/不可达'", 60),
        "NetConnections":   ("ss -tnp state established 2>/dev/null | head -{limit}", 60),
    },
}


def build_system_prompt(room: "Room") -> str:
    """根据目标平台动态组装系统提示词（v2 管道化：平台感知）"""
    platform = getattr(room, "platform", "windows")
    if platform == "linux":
        return SYSTEM_PROMPT_LINUX
    if platform == "darwin":
        return SYSTEM_PROMPT_MACOS
    return SYSTEM_PROMPT_WINDOWS


def build_v2_command(tool_name: str, args: dict, platform: str) -> tuple[str, int]:
    """把工具调用映射为 v2 命令管道可执行的命令字符串 (command, timeout)。

    未在模板中的工具回退为 RunCommand 语义：直接透传 args 里的 command。
    """
    templates = V2_COMMAND_TEMPLATES.get(platform, V2_COMMAND_TEMPLATES["windows"])
    # FileWrite 不在模板表中（参数是 path/content 而非 command），需特判：
    # 内容转 base64 写入，避免引号/换行破坏 PowerShell 语法
    if tool_name == "FileWrite":
        path = (args.get("path") or "").replace("'", "''")
        content = (args.get("content") or "").encode("utf-8")
        b64 = base64.b64encode(content).decode("ascii")
        if args.get("append"):
            method = "[IO.File]::AppendAllBytes"
        else:
            method = "[IO.File]::WriteAllBytes"
        return ("$b='{b64}';{method}('{path}',[Convert]::FromBase64String($b))"
                .format(b64=b64, method=method, path=path)), 30
    if tool_name in templates:
        tmpl, timeout = templates[tool_name]
        try:
            cmd = tmpl.format(**{k: (v if v is not None else "") for k, v in args.items()})
        except (KeyError, ValueError):
            cmd = tmpl
        # 清理未替换的 {xxx} 占位符
        cmd = re.sub(r"\{[a-z_]+\}", "", cmd)
        return cmd, timeout
    # 回退：工具带 command 参数（RunCommand/Shell/run_powershell）
    if "command" in args:
        return args["command"], int(args.get("timeout", 60))
    return "", 60


def platform_shell(platform: str) -> str:
    return "powershell" if platform == "windows" else "bash"

SYSTEM_PROMPT_WINDOWS = """You are a professional Windows remote diagnostics assistant. You remotely execute diagnostic commands via a bridge program installed on the user's PC to help troubleshoot computer issues.

## Your Capabilities

You have access to 49 tools across 3 tiers:

### Tier 1 — Read-only Diagnostics (always available, no approval needed)
- **System Info**: GetSystemInfo, run_systeminfo, run_dxdiag, ListProcesses
- **Desktop View**: Snapshot, AnnotatedSnapshot, GetClipboard, OCR, ScreenRecord
- **Files**: FileList, FileSearch, FileRead, FileDownload
- **Registry**: RegRead (read only)
- **Services & Tasks**: ServiceList, TaskList (view only)
- **Network**: Ping, PortCheck, NetConnections
- **Events**: read_event_log, EventLog
- **Other**: Notification, Wait, GetTaskStatus, GetRunningTasks, run_powershell

### Tier 2 — Interactive Desktop Control (requires user approval)
- **Mouse**: Click, Move, Scroll
- **Keyboard**: Type, Shortcut
- **Window**: FocusWindow, MinimizeAll
- **Other**: Scrape, CancelTask, ReconnectSession

### Tier 3 — System Modification (requires explicit user approval)
- **Shell**: Shell (arbitrary PowerShell), App (launch apps)
- **System**: Shutdown (shut down the PC), LockScreen (lock workstation)
- **Files**: FileWrite, FileUpload
- **Processes**: KillProcess
- **Registry**: RegWrite
- **Services**: ServiceStart, ServiceStop
- **Tasks**: TaskCreate, TaskDelete
- **Other**: SetClipboard, PlaySound

## Scope (IMPORTANT)
This service is dedicated to **computer problem diagnosis and repair**. Only handle requests related to troubleshooting computer issues (system info, performance problems, blue screens, software faults, network issues, hardware status, drivers, startup items, etc.).
If the user asks for something **unrelated to diagnostics** (e.g. "play my favorite song", "watch a video", other entertainment/life requests): DO NOT attempt to execute it and DO NOT keep trying — politely reply in Chinese that this remote diagnosis assistant focuses on computer problem diagnosis and cannot do such things, then ask what computer problem they need help with.

## IMPORTANT Rules for Actions
When the user asks you to do something (close a program, delete a file, etc.):
1. First use a Tier 1 tool to understand the situation (e.g. ListProcesses to find PIDs)
2. Then IMMEDIATELY call the action tool — do NOT write paragraphs asking for confirmation
3. The system shows an approval popup — that IS the confirmation
4. If the tool returns [approval_denied], tell the user "The operation was denied"

## RunCommand — the general-purpose tool (use for anything not covered)
The **RunCommand** tool executes an arbitrary PowerShell command on the user's PC. Use it whenever:
- The user asks for something not covered by the dedicated tools (temperature, BIOS version, startup items, power plan, driver details, disk health, Windows update status, etc.)
- You need to run a diagnostic that has no dedicated tool

The system automatically classifies each command:
- **Read-only** (Get-*, Select-*, systeminfo, ipconfig, tasklist, dir, reg query, etc.) → executes immediately, no popup
- **Modifying** (shutdown, Set-*, New-*, Remove-Item, Start-Service, reg add, etc.) → approval popup appears
- **Dangerous** (format, diskpart, reg delete, Remove-Item -Recurse, net user, etc.) → **blocked, will never execute**

Examples:
- "帮我查一下 CPU 温度" → RunCommand(command="Get-Temperature") — auto-runs
- "看启动项" → RunCommand(command="Get-CimInstance Win32_StartupCommand") — auto-runs
- "查看电源计划" → RunCommand(command="powercfg /getactivescheme") — auto-runs
- "修改电源计划为高性能" → RunCommand(command="powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c") — approval popup
- "清理临时文件" → RunCommand(command="Remove-Item $env:TEMP\\* -Recurse -Force") — approval popup (or blocked if -Recurse+Force matches)

Do NOT call RunCommand for things the dedicated tools already handle (file read/write, registry read, event log, process list, etc.) — use the dedicated tool instead.

Example flow:
User: "Close Feishu"
You: call ListProcesses(filter="Feishu") first → see 9 processes
You: "Feishu has 9 processes, closing them now." → call KillProcess(name="Feishu")
  → Approval popup appears → user clicks Approve → done!

User: "帮我关机" / "shut down the PC"
You: call Shutdown() immediately → approval popup appears → user approves → PC shuts down
  Do NOT just reply with text saying you will shut down — actually call the Shutdown tool.

NOT this:
User: "Close Feishu"
You: "Are you sure? This is dangerous. Please confirm..." → never calls the tool!

## How to Work
1. Understand the problem, plan what to collect
2. Tier 1 tools first to diagnose
3. For actions: EXPLAIN BRIEFLY (1-2 sentences) then CALL THE TOOL
4. Report in Chinese with markdown
5. If information is insufficient, ask for more details"""

SYSTEM_PROMPT_LINUX = """You are a professional Linux remote diagnostics assistant. You remotely execute diagnostic commands via a bridge program installed on the user's Linux machine to help troubleshoot computer issues.

## Your Capabilities
- Run read-only diagnostic commands immediately (uname, lscpu, free, df, lsblk, smartctl, journalctl, dmesg, ps, ss, ip, systemctl status...)
- Modifying commands (service restart, package install, process kill, file change) require an approval popup on the user's browser — the user must click Approve.
- Dangerous commands (format, disk wipe, fdisk destructive ops, `rm -rf /`) are hard-blocked and never executed.

## Key Platform Facts
- Shell is bash (/bin/bash). Use bash syntax, not PowerShell.
- Services: systemctl (list: `systemctl list-units --type=service`; status: `systemctl status <name>`)
- Logs: journalctl (system log: `journalctl -b -p err` ; dmesg for kernel messages)
- Processes: ps aux; network: ss -tnp; disk: df -h, lsblk, smartctl -a /dev/sdX (if installed)
- Packages: apt/dnf/yum depending on distro
- Most diagnostics are Tier 1 read-only and run instantly.

## How to Work
1. Understand the problem, plan what to collect
2. Tier 1 read-only commands first to diagnose
3. For actions: EXPLAIN BRIEFLY (1-2 sentences) then CALL THE TOOL
4. Report in Chinese with markdown
5. If information is insufficient, ask for more details"""

SYSTEM_PROMPT_MACOS = """You are a professional macOS remote diagnostics assistant. You remotely execute diagnostic commands via a bridge program installed on the user's Mac to help troubleshoot computer issues.

## Your Capabilities
- Run read-only diagnostic commands immediately (system_profiler, sw_vers, top, df, ioreg, log show, ps, netstat...)
- Modifying commands require an approval popup on the user's browser — the user must click Approve.
- Dangerous commands are hard-blocked and never executed.

## Key Platform Facts
- Shell is bash/zsh (/bin/bash). Use POSIX/bash syntax.
- System info: sw_vers, system_profiler SPHardwareDataType; logs: `log show --last 1h --predicate 'messageType == error'`; memory: vm_stat, top -l 1
- Processes: ps aux; network: netstat -an, lsof -i; disk: df -h, diskutil list
- Package manager: brew (if installed)

## How to Work
1. Understand the problem, plan what to collect
2. Tier 1 read-only commands first to diagnose
3. For actions: EXPLAIN BRIEFLY (1-2 sentences) then CALL THE TOOL
4. Report in Chinese with markdown
5. If information is insufficient, ask for more details"""

# 兼容别名（旧代码引用）
SYSTEM_PROMPT = SYSTEM_PROMPT_WINDOWS




# ============================================================
# Room management
# ============================================================
class Room:
    def __init__(self, code: str):
        self.code = code
        self.browser_ws: Optional[WebSocket] = None
        self.bridge_ws: Optional[WebSocket] = None
        self.created_at = datetime.now(timezone.utc)
        self.pending_commands: dict[str, asyncio.Future] = {}
        self.pending_files: dict[str, dict] = {}  # 文件通道分块缓存
        # Approval: cmd_id -> Future that resolves when user approves/denies
        self.pending_approvals: dict[str, asyncio.Future] = {}
        # Auto-approve setting: if True, skip approval prompts for Tier 2
        self.auto_approve_tier2: bool = False
        # Machine identity — populated when bridge connects and sends identify
        self.machine: dict = {}
        self.remote_ip: str = ""
        # --- 诊断追踪字段（/api/diag 用）---
        self.last_heartbeat: Optional[datetime] = None   # 最近一次 bridge 心跳
        self.bridge_connect_count: int = 0               # bridge 连接次数（重连统计）
        self.bridge_disconnect_count: int = 0            # bridge 断开次数
        self.last_disconnect_reason: str = ""            # 最近一次断开原因
        self.last_disconnect_at: Optional[datetime] = None
        self.cmd_history: list[dict] = []                # 最近命令执行记录（环形，最多 20 条）
        self._cmd_history_max = 20
        # Bridge 协议版本: "v1" = 旧 python bridge (tool/args), "v2" = go-pipe (command)
        self.bridge_mode: str = "v1"
        # 目标平台: windows | linux | darwin（默认 windows，兼容旧 bridge）
        self.platform: str = "windows"

    def record_command(self, tool: str, command: str, timeout: int, platform: str, sent: bool):
        """记录一次命令下发（用于诊断追溯）。"""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "command": command[:200],
            "timeout": timeout,
            "platform": platform,
            "sent": sent,  # True=已发送给 bridge, False=bridge 不在未发送
        }
        self.cmd_history.append(entry)
        if len(self.cmd_history) > self._cmd_history_max:
            self.cmd_history = self.cmd_history[-self._cmd_history_max:]

    def is_ready(self) -> bool:
        return self.browser_ws is not None and self.bridge_ws is not None


rooms: dict[str, Room] = {}


def generate_room_code() -> str:
    """生成 8 位房间码：大写字母+数字，去掉易混字符（O/0、I/1、L、Z/2、S/5），电话报读不易错。"""
    chars = "ABCDEFGHJKMNPQRTUVWXY346789"
    return "".join(secrets.choice(chars) for _ in range(8))


# ============================================================
# FastAPI app
# ============================================================
app = FastAPI(title="Cloud AI Remote Diagnostics", version="0.8.0")

# ============================================================
# Admin authentication — simple session cookie
# ============================================================
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_SESSIONS: dict[str, float] = {}   # token -> expiry ts
ADMIN_SESSION_TTL = 12 * 3600            # 12 hours
ADMIN_SECRET = os.getenv("ADMIN_SECRET", secrets.token_hex(16))


def _admin_token_valid(token: str) -> bool:
    exp = ADMIN_SESSIONS.get(token, 0)
    return exp > time.time()


def _require_admin(request: Request):
    """Dependency: check admin session cookie (or user session with role=admin)."""
    token = request.cookies.get("admin_token", "")
    if token and _admin_token_valid(token):
        return True
    user = _require_user(request)
    if user and user.get("role") == "admin":
        return True
    return False


@app.post("/api/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    if body.get("username") != ADMIN_USERNAME or body.get("password") != ADMIN_PASSWORD:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    token = secrets.token_hex(24)
    ADMIN_SESSIONS[token] = time.time() + ADMIN_SESSION_TTL
    resp = JSONResponse({"ok": True, "token": token})
    resp.set_cookie("admin_token", token, max_age=ADMIN_SESSION_TTL, httponly=True, samesite="lax")
    return resp


@app.post("/api/admin/logout")
async def admin_logout(request: Request):
    token = request.cookies.get("admin_token", "")
    ADMIN_SESSIONS.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("admin_token")
    return resp


# ============================================================
# 用户认证（工程师账号）— users 表 + session cookie
# ============================================================
USER_SESSIONS: dict[str, dict] = {}   # token -> {username, role, exp}
USER_SESSION_TTL = 12 * 3600            # 12 hours


def _require_user(request: Request) -> Optional[dict]:
    """校验 user_token cookie，返回 {username, role} 或 None。"""
    token = request.cookies.get("user_token", "")
    sess = USER_SESSIONS.get(token)
    if not sess or sess.get("exp", 0) <= time.time():
        return None
    return {"username": sess["username"], "role": sess["role"]}


def _set_user_cookie(resp, username: str, role: str):
    token = secrets.token_hex(24)
    USER_SESSIONS[token] = {"username": username, "role": role, "exp": time.time() + USER_SESSION_TTL}
    resp.set_cookie("user_token", token, max_age=USER_SESSION_TTL, httponly=True, samesite="lax")


@app.post("/api/auth/login")
async def user_login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = get_user(username)
    if not user or not verify_password(password, user["password_hash"]):
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    resp = JSONResponse({"ok": True, "username": user["username"], "name": user["name"], "role": user["role"]})
    _set_user_cookie(resp, user["username"], user["role"])
    run_logger.info(f"[auth] {username} 登录成功")
    return resp


@app.post("/api/auth/logout")
async def user_logout(request: Request):
    token = request.cookies.get("user_token", "")
    USER_SESSIONS.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("user_token")
    return resp


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = _require_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    return {"username": user["username"], "role": user["role"]}


@app.post("/api/auth/change_password")
async def change_password(request: Request):
    user = _require_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    body = await request.json()
    old_pw = body.get("old_password") or ""
    new_pw = body.get("new_password") or ""
    if len(new_pw) < 4:
        return JSONResponse({"error": "新密码至少 4 位"}, status_code=400)
    db_user = get_user(user["username"])
    if not db_user or not verify_password(old_pw, db_user["password_hash"]):
        return JSONResponse({"error": "旧密码不正确"}, status_code=400)
    conn = _db_connect()
    conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hash_password(new_pw), user["username"]))
    conn.commit()
    conn.close()
    run_logger.info(f"[auth] {user['username']} 修改了密码")
    return {"ok": True}


@app.post("/api/admin/delete_room")
async def admin_delete_room(request: Request):
    """Delete a room's chat history (and approvals) from SQLite."""
    if not _require_admin(request):
        return JSONResponse({"error": "未授权"}, status_code=401)
    body = await request.json()
    room_code = (body.get("room_code") or "").strip().upper()
    if not room_code:
        return JSONResponse({"error": "缺少房间码"}, status_code=400)
    try:
        conn = _db_connect()
        cur = conn.execute("DELETE FROM messages WHERE room_code = ?", (room_code,))
        m_del = cur.rowcount
        cur2 = conn.execute("DELETE FROM approvals WHERE room_code = ?", (room_code,))
        a_del = cur2.rowcount
        conn.commit()
        conn.close()
        # Also drop any in-memory room (disconnect if active)
        if room_code in rooms:
            r = rooms.pop(room_code, None)
            if r:
                for ws in (getattr(r, "browser_ws", None), getattr(r, "bridge_ws", None)):
                    try:
                        if ws:
                            asyncio.create_task(ws.close())
                    except Exception:
                        pass
        run_logger.info(f"[admin] deleted room {room_code}: {m_del} messages, {a_del} approvals")
        return {"ok": True, "deleted_messages": m_del, "deleted_approvals": a_del}
    except Exception as e:
        run_logger.error(f"[admin] delete room failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)



static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root(request: Request):
    """入口：未登录跳登录页，已登录跳工作台。"""
    if not _require_user(request):
        return RedirectResponse(url="/login")
    return RedirectResponse(url="/dashboard")


def _html_file(name: str, no_cache: bool = True) -> HTMLResponse:
    """读取 static 下的页面文件。"""
    html_path = static_dir / name
    if html_path.exists():
        headers = {"Cache-Control": "no-cache, no-store, must-revalidate"} if no_cache else None
        return HTMLResponse(html_path.read_text(encoding="utf-8"), headers=headers)
    return HTMLResponse(f"<h1>Missing static/{name}</h1>")


@app.get("/login")
async def login_page():
    return _html_file("login.html")


@app.get("/dashboard")
async def dashboard_page(request: Request):
    if not _require_user(request):
        return RedirectResponse(url="/login")
    return _html_file("dashboard.html")


@app.get("/chat")
async def chat_page(request: Request):
    if not _require_user(request):
        return RedirectResponse(url="/login")
    return _html_file("index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "rooms": len(rooms), "tools": len(TOOLS), "version": "0.8.0"}


@app.post("/api/debug_log")
async def debug_log(request: Request):
    """前端 JS 错误上报端点（调试用）"""
    try:
        body = await request.json()
        run_logger.error(f"[UI-ERROR] {json.dumps(body, ensure_ascii=False)[:600]}")
    except Exception as e:
        run_logger.error(f"[UI-ERROR] parse failed: {e}")
    return {"ok": True}


@app.post("/api/rooms")
async def create_room(request: Request):
    """创建房间：需登录，必填 SN + 工单号（型号选填），生成 8 位房间码并绑定业务信息。"""
    user = _require_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    body = await request.json()
    sn = (body.get("sn") or "").strip()
    ticket_no = (body.get("ticket_no") or "").strip()
    machine_model = (body.get("machine_model") or "").strip()
    if not sn:
        return JSONResponse({"error": "SN 序列号必填"}, status_code=400)
    if not ticket_no:
        return JSONResponse({"error": "工单号必填"}, status_code=400)
    # 生成唯一 8 位房间码（内存 + 数据库双向查重）
    code = None
    for _ in range(50):
        candidate = generate_room_code()
        if candidate not in rooms and not room_record_exists(candidate):
            code = candidate
            break
    if not code:
        return JSONResponse({"error": "房间码生成失败，请重试"}, status_code=500)
    rooms[code] = Room(code)
    try:
        conn = _db_connect()
        conn.execute(
            "INSERT INTO rooms (room_code, sn, ticket_no, machine_model, engineer_username) VALUES (?, ?, ?, ?, ?)",
            (code, sn, ticket_no, machine_model, user["username"]),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        rooms.pop(code, None)
        run_logger.error(f"Room create DB error: {e}")
        return JSONResponse({"error": f"创建失败: {e}"}, status_code=500)
    run_logger.info(f"Room created: {code} by {user['username']} (SN={sn}, ticket={ticket_no})")
    return {"room_code": code, "sn": sn, "ticket_no": ticket_no, "machine_model": machine_model}


def room_record_exists(room_code: str) -> bool:
    try:
        conn = _db_connect()
        row = conn.execute("SELECT 1 FROM rooms WHERE room_code = ?", (room_code,)).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


@app.get("/api/my_rooms")
async def my_rooms(request: Request):
    """当前登录工程师的房间列表（含连接状态）。"""
    user = _require_user(request)
    if not user:
        return JSONResponse({"error": "未登录"}, status_code=401)
    try:
        conn = _db_connect()
        rows = conn.execute(
            "SELECT * FROM rooms WHERE engineer_username = ? ORDER BY created_at DESC LIMIT 100",
            (user["username"],),
        ).fetchall()
        conn.close()
    except Exception as e:
        run_logger.error(f"my_rooms query error: {e}")
        rows = []
    result = []
    for r in rows:
        d = dict(r)
        room = rooms.get(d["room_code"])
        bridge_online = bool(room and room.bridge_ws is not None)
        browser_online = bool(room and room.browser_ws is not None)
        last_seen = d["created_at"]
        msgs = get_room_messages(d["room_code"], 1)
        if msgs:
            last_seen = msgs[-1]["created_at"]
        result.append({
            "room_code": d["room_code"],
            "sn": d["sn"],
            "ticket_no": d["ticket_no"],
            "machine_model": d["machine_model"],
            "engineer_username": d["engineer_username"],
            "created_at": d["created_at"],
            "last_seen": last_seen,
            "bridge_online": bridge_online,
            "browser_online": browser_online,
            "status": "连接中" if bridge_online else "已断开",
        })
    return {"rooms": result}


@app.get("/api/rooms/check/{room_code}")
async def check_room(request: Request, room_code: str):
    """校验房间码是否存在（工作台「加入房间」用）。"""
    if not _require_user(request):
        return JSONResponse({"error": "未登录"}, status_code=401)
    code = room_code.strip().upper()
    exists = room_record_exists(code)
    return {"room_code": code, "exists": exists}


# ============================================================
# Chat history API
# ============================================================
@app.get("/api/history/{room_code}")
async def get_history(request: Request, room_code: str, limit: int = 200):
    """Get chat history for a room from SQLite.（登录用户可访问）"""
    if not _require_user(request):
        return JSONResponse({"error": "未授权"}, status_code=401)
    messages = get_room_messages(room_code, limit)
    return {"room_code": room_code, "messages": messages}


@app.get("/api/rooms/list")
async def list_rooms(request: Request):
    """List all rooms with message history."""
    if not _require_admin(request):
        return JSONResponse({"error": "未授权"}, status_code=401)
    return {"rooms": get_all_rooms()}


# ============================================================
# Admin API
# ============================================================
@app.get("/api/diag/{room_code}")
async def diag_room(room_code: str):
    """诊断接口：返回房间的完整健康状态，方便排查 bridge 断链等问题。

    无需登录（只含房间级诊断信息，不含聊天内容）。
    """
    room = rooms.get(room_code.upper())
    if not room:
        return JSONResponse({"room_code": room_code.upper(), "exists": False,
                             "error": "房间不存在或已过期（服务器重启后房间内存清空，需重新创建）"}, status_code=404)

    now = datetime.now(timezone.utc)
    hb_age = None
    if room.last_heartbeat:
        hb_age = round((now - room.last_heartbeat).total_seconds(), 1)

    return {
        "room_code": room.code,
        "exists": True,
        "created_at": room.created_at.isoformat(),
        "bridge_connected": room.bridge_ws is not None,
        "browser_connected": room.browser_ws is not None,
        "platform": room.platform,
        "bridge_mode": room.bridge_mode,
        "machine": room.machine,
        "remote_ip": room.remote_ip,
        "last_heartbeat": room.last_heartbeat.isoformat() if room.last_heartbeat else None,
        "heartbeat_age_s": hb_age,
        "connect_count": room.bridge_connect_count,
        "disconnect_count": room.bridge_disconnect_count,
        "last_disconnect_reason": room.last_disconnect_reason,
        "last_disconnect_at": room.last_disconnect_at.isoformat() if room.last_disconnect_at else None,
        "pending_commands": len(room.pending_commands),
        "pending_approvals": len(room.pending_approvals),
        "cmd_history": room.cmd_history[-10:],  # 最近 10 条命令
    }


@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    """Return server stats for admin dashboard."""
    if not _require_admin(request):
        return JSONResponse({"error": "未授权"}, status_code=401)
    active_rooms = []
    for code, room in rooms.items():
        active_rooms.append({
            "room_code": code,
            "browser_connected": room.browser_ws is not None,
            "bridge_connected": room.bridge_ws is not None,
            "created_at": room.created_at.isoformat(),
            "auto_approve_tier2": room.auto_approve_tier2,
            "machine": room.machine,
            "remote_ip": room.remote_ip,
        })

    db_stats = get_server_stats()
    return {
        "active_rooms": active_rooms,
        "active_count": len(active_rooms),
        **db_stats,
        "tool_count": len(TOOLS),
        "version": "0.8.0",
    }


@app.get("/api/admin/logs/{log_name}")
async def admin_logs(request: Request, log_name: str, lines: int = 200):
    """Read server log files (server.log, chat.log, bridge.log)."""
    if not _require_admin(request):
        return JSONResponse({"error": "未授权"}, status_code=401)
    allowed = {"server.log", "chat.log", "bridge.log"}
    if log_name not in allowed:
        return JSONResponse({"error": f"Log not allowed. Choose: {', '.join(sorted(allowed))}"}, status_code=400)
    log_path = LOG_DIR / log_name
    if not log_path.exists():
        return {"log": log_name, "content": "(log file not found)", "lines": 0}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content_lines = f.readlines()
        tail = content_lines[-lines:] if len(content_lines) > lines else content_lines
        return {"log": log_name, "content": "".join(tail), "total_lines": len(content_lines), "shown": len(tail)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/admin")
async def admin_page(request: Request):
    if not _require_admin(request):
        return HTMLResponse(_login_page_html())
    html_path = static_dir / "admin.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    # Generate inline admin page
    return _generate_admin_html()


def _login_page_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>管理后台登录 — 云端 AI 远程运维助手</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Microsoft YaHei", sans-serif;
    background: #1a1a2e; color: #eaeaea; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }
  .login-box {
    background: #16213e; border: 1px solid #2a2a4a; border-radius: 12px;
    padding: 40px 44px; width: 360px; box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  }
  .login-box h1 { font-size: 20px; color: #60a5fa; margin-bottom: 6px; text-align:center; }
  .login-box .sub { font-size: 13px; color: #a0a0b0; text-align:center; margin-bottom: 28px; }
  .login-box label { display:block; font-size: 13px; color: #a0a0b0; margin: 14px 0 6px; }
  .login-box input {
    width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #2a2a4a;
    background: #0f3460; color: #eaeaea; font-size: 14px; outline: none;
  }
  .login-box input:focus { border-color: #60a5fa; }
  .login-box button {
    width: 100%; margin-top: 24px; padding: 11px; border: none; border-radius: 8px;
    background: #60a5fa; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer;
  }
  .login-box button:hover { background: #3b82f6; }
  .login-box .err { color: #f87171; font-size: 13px; text-align:center; margin-top: 12px; min-height: 18px; }
  .login-box .back { display:block; text-align:center; margin-top: 16px; font-size: 12px; color:#a0a0b0; text-decoration:none; }
  .login-box .back:hover { color:#60a5fa; }
</style>
</head>
<body>
<div class="login-box">
  <h1>🔐 管理后台</h1>
  <div class="sub">云端 AI 远程运维助手 · 请登录</div>
  <form id="login-form">
    <label for="username">账号</label>
    <input type="text" id="username" name="username" placeholder="请输入账号" autocomplete="username" required>
    <label for="password">密码</label>
    <input type="password" id="password" name="password" placeholder="请输入密码" autocomplete="current-password" required>
    <button type="submit" id="login-btn">登 录</button>
    <div class="err" id="login-err"></div>
  </form>
  <a class="back" href="/">← 返回聊天页面</a>
</div>
<script>
document.getElementById('login-form').addEventListener('submit', async function(e) {
  e.preventDefault();
  const btn = document.getElementById('login-btn');
  const err = document.getElementById('login-err');
  btn.disabled = true; btn.textContent = '登录中...'; err.textContent = '';
  try {
    const resp = await fetch('/api/admin/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('username').value.trim(),
        password: document.getElementById('password').value
      })
    });
    const data = await resp.json();
    if (resp.ok && data.ok) {
      window.location.href = '/admin';
    } else {
      err.textContent = data.error || '登录失败';
      btn.disabled = false; btn.textContent = '登 录';
    }
  } catch(ex) {
    err.textContent = '网络错误: ' + ex.message;
    btn.disabled = false; btn.textContent = '登 录';
  }
});
</script>
</body>
</html>
"""


def _generate_admin_html():
    return HTMLResponse(r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>管理后台 — 云端 AI 远程运维助手</title>
<style>
  :root {
    --bg: #1a1a2e; --surface: #16213e; --surface2: #0f3460;
    --accent: #e94560; --text: #eaeaea; --text2: #a0a0b0;
    --border: #2a2a4a; --success: #4ade80; --warn: #fbbf24;
    --error: #f87171; --info: #60a5fa;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--text); padding: 20px;
  }
  h1 { margin-bottom: 6px; color: var(--info); }
  h1 .subtitle { font-size: 14px; color: var(--text2); font-weight: 400; margin-left: 12px; }
  h2 { margin: 24px 0 10px; font-size: 18px; color: var(--warn); }
  .stats { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:20px; }
  .stat-card {
    background: var(--surface); border:1px solid var(--border);
    border-radius:10px; padding:16px 24px; min-width:140px; text-align:center;
  }
  .stat-card .num { font-size: 32px; font-weight: 700; color: var(--info); }
  .stat-card .label { font-size: 12px; color: var(--text2); margin-top:4px; }
  table {
    width:100%; border-collapse:collapse; margin-bottom:16px;
    background: var(--surface); border-radius:8px; overflow:hidden;
  }
  th, td {
    padding:8px 12px; text-align:left; font-size:13px;
    border-bottom:1px solid var(--border);
  }
  th { background: var(--surface2); color: var(--text2); }
  .badge {
    display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600;
  }
  .badge.on { background:rgba(74,222,128,0.2); color:var(--success); }
  .badge.off { background:rgba(248,113,113,0.2); color:var(--error); }
  .badge.yes { background:rgba(251,191,36,0.15); color:var(--warn); }
  .badge.no { background:rgba(96,165,250,0.15); color:var(--info); }
  pre {
    background:var(--surface); border:1px solid var(--border); border-radius:8px;
    padding:12px; font-size:11px; max-height:400px; overflow:auto; white-space:pre-wrap;
  }
  button {
    padding:8px 16px; border:none; border-radius:6px; cursor:pointer;
    font-size:13px; font-weight:600; margin-right:8px;
  }
  .btn-primary { background:var(--info); color:#fff; }
  .btn-refresh { background:var(--surface2); color:var(--text); border:1px solid var(--border); }
  .log-tabs { display:flex; gap:8px; margin-bottom:12px; }
  .log-tabs button.active { background:var(--info); }
  #log-container { margin-top:16px; }
</style>
</head>
<body>
<h1>管理后台 <span class="subtitle">云端 AI 远程运维助手 v0.8.0</span></h1>

<div class="stats" id="stats-cards">
  <div class="stat-card"><div class="num" id="stat-rooms">-</div><div class="label">当前活跃房间</div></div>
  <div class="stat-card"><div class="num" id="stat-total-rooms">-</div><div class="label">历史房间总数</div></div>
  <div class="stat-card"><div class="num" id="stat-msgs">-</div><div class="label">消息总数</div></div>
  <div class="stat-card"><div class="num" id="stat-tools">-</div><div class="label">工具调用次数</div></div>
  <div class="stat-card"><div class="num" id="stat-approved">-</div><div class="label">已同意 / 已拒绝</div></div>
</div>

<button class="btn-refresh" onclick="refresh()" style="margin-bottom:16px;">刷新数据</button>
<button class="btn-refresh" onclick="logout()" style="margin-bottom:16px;">退出登录</button>
<a href="/" style="color:var(--info);margin-left:12px;font-size:13px;">返回聊天页面</a>

<h2>当前活跃房间</h2>
<table id="rooms-table">
  <thead><tr><th>房间码</th><th>主机名</th><th>系统</th><th>IP</th><th>用户</th><th>浏览器</th><th>桥接器</th><th>T2 自动</th><th>创建时间</th></tr></thead>
  <tbody></tbody>
</table>

<h2>历史聊天记录</h2>
<table id="history-rooms">
  <thead><tr><th>房间码</th><th>消息数</th><th>首次记录</th><th>最近记录</th><th>操作</th></tr></thead>
  <tbody></tbody>
</table>

<h2>服务器日志</h2>
<div class="log-tabs">
  <button class="btn-primary" id="tab-server" onclick="loadLog('server.log')">server.log</button>
  <button class="btn-refresh" id="tab-chat" onclick="loadLog('chat.log')">chat.log</button>
  <button class="btn-refresh" id="tab-bridge" onclick="loadLog('bridge.log')">bridge.log</button>
</div>
<pre id="log-container">点击上方标签加载日志...</pre>

<script>
async function refresh() {
  const resp = await fetch('/api/admin/stats');
  const data = await resp.json();

  document.getElementById('stat-rooms').textContent = data.active_count;
  document.getElementById('stat-total-rooms').textContent = data.total_rooms;
  document.getElementById('stat-msgs').textContent = data.total_messages;
  document.getElementById('stat-tools').textContent = data.total_tool_calls;
  const as = data.approval_stats || {};
  document.getElementById('stat-approved').textContent = (as.approved||0) + ' / ' + (as.denied||0);

  // 活跃房间表格
  const tbody = document.querySelector('#rooms-table tbody');
  tbody.innerHTML = '';
  for (const r of data.active_rooms || []) {
    const m = r.machine || {};
    tbody.innerHTML += '<tr>'
      + '<td><strong>' + r.room_code + '</strong></td>'
      + '<td>' + (m.hostname || '-') + '</td>'
      + '<td title="' + (m.os||'') + '">' + (m.os ? m.os.split(' ')[0] + ' ' + (m.os.split(' ')[1]||'') : '-') + '</td>'
      + '<td><code style="font-size:11px;color:var(--info)">' + (m.local_ip || '-') + '</code></td>'
      + '<td>' + (m.username || '-') + '</td>'
      + '<td><span class="badge ' + (r.browser_connected ? 'on' : 'off') + '">' + (r.browser_connected ? '在线' : '离线') + '</span></td>'
      + '<td><span class="badge ' + (r.bridge_connected ? 'on' : 'off') + '">' + (r.bridge_connected ? '在线' : '离线') + '</span></td>'
      + '<td>' + (r.auto_approve_tier2 ? '<span class="badge yes">是</span>' : '<span class="badge no">否</span>') + '</td>'
      + '<td>' + new Date(r.created_at).toLocaleString('zh-CN') + '</td>'
      + '</tr>';
  }

  // 历史房间表格
  const resp2 = await fetch('/api/rooms/list');
  const roomsData = await resp2.json();
  const htbody = document.querySelector('#history-rooms tbody');
  htbody.innerHTML = '';
  for (const r of roomsData.rooms || []) {
    htbody.innerHTML += '<tr>'
      + '<td><strong>' + r.room_code + '</strong></td>'
      + '<td>' + r.msg_count + '</td>'
      + '<td>' + r.first_seen + '</td>'
      + '<td>' + r.last_seen + '</td>'
      + '<td><a href="#" onclick="viewRoom(\'' + r.room_code + '\')" style="color:var(--info)">查看</a>'
      + '&nbsp;|&nbsp;<a href="#" onclick="deleteRoom(\'' + r.room_code + '\')" style="color:var(--error)">删除</a></td>'
      + '</tr>';
  }
}

async function viewRoom(code) {
  const resp = await fetch('/api/history/' + code);
  const data = await resp.json();
  const msgs = data.messages || [];
  let text = '房间 ' + code + ' 的聊天记录' + '\\n' + '='.repeat(60) + '\\n\\n';
  const roleMap = { user: '用户', ai: 'AI分析', tool: '工具调用', status: '系统状态', error: '错误' };
  for (const m of msgs) {
    const badge = m.tier ? ' [T' + m.tier + ']' : '';
    const roleLabel = roleMap[m.role] || m.role;
    text += '[' + roleLabel + badge + '] ' + m.created_at + '\\n' + m.content.substr(0, 2000) + '\\n\\n';
  }
  const blob = new Blob([text], {type:'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = '聊天记录_' + code + '.txt'; a.click();
  URL.revokeObjectURL(url);
}

async function deleteRoom(code) {
  if (!confirm('确定删除房间 ' + code + ' 的所有聊天记录？此操作不可恢复！')) return;
  try {
    const resp = await fetch('/api/admin/delete_room', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ room_code: code })
    });
    const data = await resp.json();
    if (resp.ok && data.ok) {
      alert('已删除房间 ' + code + '：' + (data.deleted_messages||0) + ' 条消息，' + (data.deleted_approvals||0) + ' 条审批');
      refresh();
    } else {
      alert(data.error || '删除失败');
    }
  } catch(e) {
    alert('网络错误: ' + e.message);
  }
}

async function logout() {
  try {
    await fetch('/api/admin/logout', { method: 'POST' });
  } catch(e) {}
  window.location.href = '/admin';
}

function setActiveLog(name) {
  document.querySelectorAll('.log-tabs button').forEach(b => b.className = 'btn-refresh');
  const tabId = 'tab-' + name.split('.')[0];
  const btn = document.getElementById(tabId);
  if (btn) btn.className = 'btn-primary';
}

async function loadLog(name) {
  setActiveLog(name);
  try {
    const resp = await fetch('/api/admin/logs/' + name + '?lines=300');
    const data = await resp.json();
    document.getElementById('log-container').textContent = data.content || data.error || '(empty)';
  } catch(e) {
    document.getElementById('log-container').textContent = 'Failed to load: ' + e.message;
  }
}

refresh();
setInterval(refresh, 15000);  // 每15秒自动刷新
</script>
</body>
</html>
""")


# ============================================================
# Agent core — OpenAI-compatible tool calling loop with approval
# ============================================================
async def request_approval(room: Room, fn_name: str, fn_args: dict, tier: int,
                            browser_ws: WebSocket, timeout: float = 300.0) -> tuple[bool, str]:
    """Request user approval for a Tier 2/3 tool. Returns (approved, reason).
       Default timeout = 5 minutes to give user plenty of time to see and respond."""
    approval_id = f"approve_{fn_name}_{int(time.time())}"
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    room.pending_approvals[approval_id] = future

    try:
        await browser_ws.send_json({
            "type": "approval_required",
            "id": approval_id,
            "tool": fn_name,
            "args": fn_args,
            "tier": tier,
            "timeout": timeout,
        })
        run_logger.info(f"[{room.code}] Sent approval_required for {fn_name} (tier {tier}), id={approval_id}")

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return False, "approval_timeout"

        return result.get("approved", False), result.get("reason", "")
    finally:
        room.pending_approvals.pop(approval_id, None)


async def execute_bridge_command(room: Room, fn_name: str, fn_args: dict, cmd_id: str, tier: int = 1) -> str:
    """把工具调用发送到 bridge 并等待结果（供 agent 循环和 HTTP 桥共用）。

    返回结果字符串。不处理审批（审批由调用方负责）。
    """
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    room.pending_commands[cmd_id] = future

    # Map tool names bridge doesn't know to bridge's Shell command
    bridge_tool = fn_name
    bridge_args = fn_args
    if fn_name == "Shutdown":
        bridge_tool = "Shell"
        bridge_args = {"command": "shutdown /s /t 0", "timeout": 30, "cwd": ""}
    elif fn_name == "RunCommand":
        bridge_tool = "Shell"
        bridge_args = {
            "command": fn_args.get("command", ""),
            "timeout": int(fn_args.get("timeout", 60)),
            "cwd": fn_args.get("cwd", ""),
        }

    if room.bridge_ws:
        if room.bridge_mode == "v2":
            # v2 文件通道：FileDownload → 拉取 bridge 端文件；FileUpload → 推送
            if fn_name == "FileDownload":
                path = fn_args.get("path", "")
                run_logger.info(f"[{room.code}] v2 file_download {path}")
                await room.bridge_ws.send_json({"type": "file_download", "id": cmd_id, "path": path})
            elif fn_name == "FileUpload":
                path = fn_args.get("path", "")
                data_b64 = fn_args.get("data_base64", "")
                name = os.path.basename(path) if path else "upload.bin"
                run_logger.info(f"[{room.code}] v2 file_upload {path} ({len(data_b64) // 1024} KB base64)")
                # 分块推送（单块 ≤256KB）
                raw = base64.b64decode(data_b64) if data_b64 else b""
                total = max(1, (len(raw) + 256 * 1024 - 1) // (256 * 1024))
                for i in range(total):
                    chunk = raw[i * 256 * 1024:(i + 1) * 256 * 1024]
                    await room.bridge_ws.send_json({
                        "type": "file_upload", "id": cmd_id, "path": path,
                        "name": name, "data": base64.b64encode(chunk).decode(),
                        "chunk": i, "total": total,
                    })
            else:
                # v2 管道化：所有工具统一映射为命令字符串下发（平台感知）
                command, cmd_timeout = build_v2_command(fn_name, fn_args, room.platform)
                run_logger.info(f"[{room.code}] v2 dispatch {fn_name} → command ({cmd_timeout}s, {room.platform})")
                room.record_command(fn_name, command, cmd_timeout, room.platform, sent=True)
                await room.bridge_ws.send_json({
                    "type": "command",
                    "id": cmd_id,
                    "command": command,
                    "timeout": cmd_timeout,
                    "cwd": "",
                    "shell": platform_shell(room.platform),
                    "tier": tier,
                })
        else:
            await room.bridge_ws.send_json({
                "type": "command",
                "id": cmd_id,
                "tool": bridge_tool,
                "args": bridge_args,
                "tier": tier,
            })
    else:
        room.record_command(fn_name, "", 0, room.platform, sent=False)
        future.set_result("[error] Bridge not connected")

    try:
        result = await asyncio.wait_for(future, timeout=120.0)
    except asyncio.TimeoutError:
        result = "[timeout] Command exceeded 120s"

    room.pending_commands.pop(cmd_id, None)
    return result


async def run_agent(
    user_message: str,
    room: Room,
    http_client: httpx.AsyncClient,
    browser_ws: WebSocket,
) -> str:
    messages = [
        {"role": "system", "content": build_system_prompt(room)},
        *get_recent_context(room.code, user_message),
        {"role": "user", "content": user_message},
    ]

    loop_count = 0
    max_loops = 30
    exec_summary = []  # 记录已执行的工具轮次，兜底时反馈给用户

    while loop_count < max_loops:
        loop_count += 1

        payload = {
            "model": OPENAI_MODEL,
            "messages": messages,
            "tools": get_tools_for_platform(getattr(room, "platform", "windows")),
            "tool_choice": "auto",
        }

        # Call model API with retry on transient network errors
        data = None
        for attempt in range(3):
            try:
                resp = await http_client.post(
                    f"{OPENAI_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
                run_logger.warning(f"[{room.code}] API call attempt {attempt+1} failed: {e}")
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))

        if data is None:
            raise RuntimeError("Model API returned no response after retries")

        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            content = msg.get("content", "")
            save_message(room.code, "ai", content)
            return content

        messages.append(msg)

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = json.loads(tc["function"]["arguments"] or "{}")
            tc_id = tc["id"]
            tier = TOOL_TIERS.get(fn_name, 1)

            # RunCommand: dynamic tier based on command classification
            cmd_class_reason = ""
            if fn_name == "RunCommand":
                tier, cmd_cat, cmd_class_reason = classify_command(fn_args.get("command", ""))
                run_logger.info(f"[{room.code}] RunCommand classified as tier={tier} ({cmd_cat}): {cmd_class_reason}")

            await browser_ws.send_json({
                "type": "tool_start",
                "tool": fn_name,
                "args": fn_args,
                "tier": tier,
            })

            # === DANGEROUS: hard block, never executed ===
            if tier < 0:
                result = f"[blocked] {cmd_class_reason}: {fn_args.get('command', '')[:200]}"
                save_approval(room.code, fn_name, fn_args, 3, -1)
                save_message(room.code, "tool", result, fn_name, 3)
                run_logger.warning(f"[{room.code}] Blocked dangerous RunCommand: {fn_args.get('command', '')[:120]}")

                await browser_ws.send_json({
                    "type": "tool_result",
                    "tool": fn_name,
                    "content": result[:3000],
                    "tier": 3,
                    "denied": True,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result,
                })
                continue

            # === APPROVAL CHECK for Tier 2/3 ===
            if tier >= 2:
                # For Tier 2 with auto_approve, skip the prompt
                if tier == 2 and room.auto_approve_tier2:
                    save_approval(room.code, fn_name, fn_args, tier, 1)
                    run_logger.info(f"[{room.code}] Auto-approved Tier 2: {fn_name}")
                else:
                    # Send tool_start to frontend FIRST so user sees what's coming
                    await browser_ws.send_json({
                        "type": "tool_waiting_approval",
                        "tool": fn_name,
                        "args": fn_args,
                        "tier": tier,
                    })

                    approved, reason = await request_approval(
                        room, fn_name, fn_args, tier, browser_ws
                    )

                    if not approved:
                        result = f"[approval_denied] Tier {tier} tool '{fn_name}' was denied by user."
                        if reason and reason != "approval_timeout":
                            result += f" Reason: {reason}"
                        elif reason == "approval_timeout":
                            result += " (approval timed out)"

                        save_approval(room.code, fn_name, fn_args, tier, -1)
                        save_message(room.code, "tool", result, fn_name, tier)

                        await browser_ws.send_json({
                            "type": "tool_result",
                            "tool": fn_name,
                            "content": result[:3000],
                            "tier": tier,
                            "denied": True,
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": result,
                        })
                        continue

                    save_approval(room.code, fn_name, fn_args, tier, 1)
                    run_logger.info(f"[{room.code}] User approved Tier {tier}: {fn_name}")

            # Execute the tool
            cmd_id = f"cmd_{loop_count}_{tc_id}"
            result = await execute_bridge_command(room, fn_name, fn_args, cmd_id, tier)

            save_message(room.code, "tool", result[:2000], fn_name, tier)

            # 记录执行摘要（兜底时反馈给用户）
            status = "✅" if "error" not in result.lower()[:50] and "timeout" not in result.lower()[:50] and "[错误]" not in result[:30] else "⚠️"
            args_brief = json.dumps(fn_args, ensure_ascii=False)[:60] if fn_args else ""
            exec_summary.append(f"{status} {loop_count}. {fn_name}({args_brief}) → {result[:80].strip()}")

            await browser_ws.send_json({
                "type": "tool_result",
                "tool": fn_name,
                "content": result[:3000],
                "tier": tier,
            })

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result[:8000],
            })

    # 已到达最大工具调用轮次，返回中文兜底消息并附执行摘要
    summary_txt = "\n".join(exec_summary[-20:]) if exec_summary else "（本轮未执行任何工具）"
    fallback = (
        "我已经尝试了多种方式处理你的请求，但步骤较多、尚未完成。\n\n"
        f"本次共执行了 {len(exec_summary)} 个诊断/操作步骤：\n{summary_txt}\n\n"
        "建议：\n"
        "1. 将问题拆分为更小的步骤，分多次提问，例如先「查看硬件信息」再「查看某进程」；\n"
        "2. 如果是安装/修改类操作，可先确认网络、权限是否正常；\n"
        "3. 告诉我你看到的具体报错或现象，我可以针对性地继续排查。"
    )
    return fallback


# ============================================================
# Hermes 通道（并存切换：brain=hermes）
#   Hermes api_server 是自治 agent：它用自己的工具集在服务器上
#   工作，并通过 HTTP 桥接接口（/api/bridge/execute）操作远程电脑。
# ============================================================
def build_hermes_bridge_guide(room: Room) -> str:
    """构造给 Hermes 的桥接操作指南（注入 system prompt）。"""
    platform = getattr(room, "platform", "windows")
    secret = BRIDGE_HTTP_SECRET or "（未配置 BRIDGE_HTTP_SECRET）"
    return f"""
## 远程桥接操作指南（重要）

你是"云端 AI 远程运维助手"的智能大脑。用户在浏览器里与你对话，目标电脑通过 bridge 程序连接本服务器。
当前目标：房间码 {room.code}，平台 {platform}。桥接接口地址是 http://127.0.0.1:8000（本服务器本机）。

### 🚫 安全红线（必须严格遵守，违反 = 越权）
1. **禁止读取/修改本服务器上 /home/ubuntu/cab-server 目录的任何文件**（server.py、.env、static/ 等）——不要用 read_file/terminal 查看或改动它们。
2. **禁止执行任何影响本服务器进程的命令**：pkill、kill、systemctl、service、nohup、python server.py、重启/停止/启动 cab-server 或 Hermes gateway。
3. **禁止运行 python 导入 cab-server 的 server.py** 或直接调用其内部函数（如 build_v2_command、execute_bridge_command、rooms 等）。
4. **唯一允许的服务器操作**：用 curl 调用下面的 HTTP 桥接接口来操作**目标电脑**（通过 bridge 转发）。任何诊断、查询、操作目标电脑的行为都必须走这个接口。
5. 你在服务器上的一切 terminal 命令，只允许两类：① curl 调 HTTP 桥；② 用于理解问题的最小只读检查（如 curl http://127.0.0.1:8000/api/health）。

### 如何操作目标电脑
必须通过本服务器的 HTTP 桥接接口，用 curl 调用：

POST http://127.0.0.1:8000/api/bridge/execute
Header: X-Bridge-Secret: {secret}
Header: Content-Type: application/json
Body: {{"room_code": "{room.code}", "tool": "<工具名>", "args": {{...}}}}

响应示例：
{{"status": "ok", "tool": "GetSystemInfo", "tier": 1, "result": "..."}}
{{"status": "denied", "tier": 3, "reason": "..."}}
{{"status": "blocked", "reason": "..."}}

### 常用工具（tier 1 只读立即执行；tier 2/3 自动弹审批窗给浏览器用户，接口会阻塞等待用户批准后返回）
- Tier 1 只读：GetSystemInfo, run_systeminfo, run_dxdiag, ListProcesses, FileList, FileSearch, FileRead, FileDownload, RegRead, ServiceList, TaskList, EventLog, Ping, PortCheck, NetConnections, read_event_log, run_powershell
- Tier 2 交互（需审批）：Click, Type, Move, Scroll, Shortcut, FocusWindow, MinimizeAll, Scrape
- Tier 3 修改（需审批）：Shell, App, KillProcess, FileWrite, FileUpload, RegWrite, ServiceStart, ServiceStop, TaskCreate, TaskDelete, SetClipboard, LockScreen, Shutdown, PlaySound

### RunCommand（最常用）
传任意 PowerShell 命令，系统自动分类：
- 只读命令（Get-*, Select-*, systeminfo, ipconfig, tasklist, reg query 等）→ 立即执行
- 修改命令（shutdown, Set-*, New-*, Remove-Item, Start-Service, reg add 等）→ 弹审批窗
- 危险命令（format, diskpart, reg delete, Remove-Item -Recurse, net user 等）→ 直接拦截

例子：查 CPU 温度 → RunCommand(command="Get-Temperature")；看启动项 → RunCommand(command="Get-CimInstance Win32_StartupCommand")

### 业务范围（重要，防止无效硬做）
本系统是「电脑问题远程诊断」专用工具，只处理与**电脑故障排查、诊断、维修**相关的事务（系统信息、性能/卡顿、蓝屏、软件故障、网络问题、硬件状态、驱动、启动项等）。
- 遇到与电脑诊断**无关**的请求（如播放音乐/视频、游戏、娱乐、购物、闲聊等）：**不要执行、不要反复尝试找办法**，直接礼貌回复：「当前远程诊断助手专注于电脑问题诊断，无法执行这类操作。如果您有电脑故障需要排查，请告诉我具体问题。」
- 判断标准：该请求是否服务于电脑问题诊断/维修目的。不是 → 按上一条处理，不要消耗时间硬做。

### 工作流程
1. 先诊断：用 Tier 1 工具或 RunCommand 只读命令收集信息（必要时多次调用，逐步深入）
2. 用户要求操作时：简要说明后用 curl 调用对应工具，等待接口返回（期间浏览器会弹审批窗给用户）
3. 若工具返回 [approval_denied]，如实告知用户操作被拒绝
4. 最终用中文 + markdown 给出结论和后续建议
5. 信息不足时主动询问用户补充细节
"""


async def run_agent_hermes(
    user_message: str,
    room: Room,
    http_client: httpx.AsyncClient,
    browser_ws: WebSocket,
) -> str:
    """Hermes 通道：把用户消息交给本机 Hermes api_server（自治 agent）处理。

    Hermes 用自己的工具集（terminal/web/file 等）工作，并通过
    /api/bridge/execute HTTP 桥操作远程电脑。返回最终文本。
    """
    system_prompt = build_system_prompt(room) + "\n\n" + build_hermes_bridge_guide(room)
    payload = {
        "model": HERMES_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            *get_recent_context(room.code, user_message),
            {"role": "user", "content": user_message},
        ],
    }
    headers = {
        "Authorization": f"Bearer {HERMES_API_KEY}",
        "Content-Type": "application/json",
    }

    run_logger.info(f"[{room.code}] Hermes channel start (model={HERMES_MODEL})")

    data = None
    for attempt in range(3):
        try:
            resp = await http_client.post(
                f"{HERMES_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=330.0,  # 覆盖客户端默认 120s：自治 agent + 审批等待可能较长
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException) as e:
            run_logger.warning(f"[{room.code}] Hermes API attempt {attempt+1} failed: {e}")
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))

    if data is None:
        raise RuntimeError("Hermes API returned no response after retries")

    content = data["choices"][0]["message"].get("content", "")
    run_logger.info(f"[{room.code}] Hermes channel done ({len(content)} chars)")
    save_message(room.code, "ai", content)
    return content


# ============================================================
# HTTP 桥接接口 — 供 Hermes（或外部 agent）通过 HTTP 操作 bridge
#   认证：X-Bridge-Secret header 必须等于 BRIDGE_HTTP_SECRET
#   流程：校验 → tier 判定 → （tier≥2）审批弹窗 → 执行 → 返回结果
# ============================================================
@app.post("/api/bridge/execute")
async def api_bridge_execute(request: Request):
    secret = request.headers.get("X-Bridge-Secret", "")
    if not BRIDGE_HTTP_SECRET or secret != BRIDGE_HTTP_SECRET:
        return JSONResponse({"status": "error", "error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid_json"}, status_code=400)

    room_code = str(body.get("room_code", "")).upper()
    fn_name = body.get("tool")
    fn_args = body.get("args") or {}

    room = rooms.get(room_code)
    if not room:
        return JSONResponse({"status": "error", "error": "room_not_found"}, status_code=404)
    if not room.bridge_ws:
        return JSONResponse({"status": "error", "error": "bridge_not_connected"}, status_code=409)
    if fn_name not in TOOL_TIERS:
        return JSONResponse({"status": "error", "error": f"unknown_tool: {fn_name}"}, status_code=400)

    tier = TOOL_TIERS.get(fn_name, 1)

    # RunCommand：动态 tier（命令分类）
    if fn_name == "RunCommand":
        tier, cmd_cat, reason = classify_command(fn_args.get("command", ""))
        run_logger.info(f"[{room.code}] HTTP bridge RunCommand classified as tier={tier} ({cmd_cat})")
        if tier < 0:
            return JSONResponse({
                "status": "blocked", "tier": -1, "reason": reason,
                "result": f"[blocked] {reason}: {fn_args.get('command', '')[:200]}",
            })

    # 审批（Tier 2/3）
    if tier >= 2:
        if not room.browser_ws:
            return JSONResponse({"status": "error", "error": "no_browser_for_approval"}, status_code=409)
        if tier == 2 and room.auto_approve_tier2:
            save_approval(room.code, fn_name, fn_args, tier, 1)
            run_logger.info(f"[{room.code}] HTTP bridge auto-approved Tier 2: {fn_name}")
        else:
            approved, reason = await request_approval(room, fn_name, fn_args, tier, room.browser_ws)
            if not approved:
                save_approval(room.code, fn_name, fn_args, tier, -1)
                run_logger.info(f"[{room.code}] HTTP bridge approval denied for {fn_name}")
                return JSONResponse({"status": "denied", "tier": tier, "reason": reason})
            save_approval(room.code, fn_name, fn_args, tier, 1)
            run_logger.info(f"[{room.code}] HTTP bridge approval granted for {fn_name}")

    # 执行
    cmd_id = f"http_{int(time.time())}_{secrets.token_hex(3)}"
    result = await execute_bridge_command(room, fn_name, fn_args, cmd_id, tier)
    save_message(room.code, "tool", result[:2000], fn_name, tier)

    run_logger.info(f"[{room.code}] HTTP bridge {fn_name} done (tier={tier}, {len(result)} chars)")
    return JSONResponse({"status": "ok", "tool": fn_name, "tier": tier, "result": result})


# ============================================================
# WebSocket endpoints
# ============================================================
@app.websocket("/ws/browser/{room_code}")
async def ws_browser(websocket: WebSocket, room_code: str):
    await websocket.accept()

    room = rooms.get(room_code)
    if not room:
        # 房间必须先在数据库中存在（工作台创建时绑定 SN/工单号）——防止绕过创建限制
        if not room_record_exists(room_code):
            await websocket.send_json({"type": "error", "content": "房间不存在。请先在工作台创建房间（需绑定 SN 与工单号），再让桥接器连接。"})
            await websocket.close()
            return
        room = Room(room_code)
        rooms[room_code] = room
        run_logger.info(f"Room re-created from DB (browser): {room_code}")

    room.browser_ws = websocket
    run_logger.info(f"[browser] joined room {room_code}")

    ready_msg = "Bridge connected [OK]" if room.bridge_ws else "Waiting for bridge connection [--]"
    await websocket.send_json({
        "type": "status",
        "content": f"Joined room {room_code}  {ready_msg}",
        "bridge_connected": room.bridge_ws is not None,
    })

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0), limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))
    agent_task = None

    try:
        while True:
            raw = await websocket.receive_text()
            msg_data = json.loads(raw)

            if msg_data.get("type") == "chat":
                user_message = msg_data["content"]
                # brain: deepseek（默认）| hermes —— 消息级覆盖环境变量 AGENT_BRAIN
                brain = msg_data.get("brain") or AGENT_BRAIN
                save_message(room_code, "user", user_message)
                chat_logger.info(f"[{room_code}] USER: {user_message[:500]}")

                if not room.bridge_ws:
                    await websocket.send_json({
                        "type": "error",
                        "content": "Bridge not connected. Please start the bridge on the Windows PC and enter the room code.",
                    })
                    continue

                # Send "analyzing" and log it
                run_logger.info(f"[{room_code}] Starting agent (brain={brain}) for message: {user_message[:100]}")
                await websocket.send_json({
                    "type": "status",
                    "content": "Analyzing your request...",
                })

                # 若上一个 agent 还在运行，拒绝新消息（保持串行）
                if agent_task and not agent_task.done():
                    run_logger.info(f"[{room_code}] Busy, rejecting message: {user_message[:50]}")
                    await websocket.send_json({
                        "type": "error",
                        "content": "上一条请求还在处理中，请稍候再试。",
                    })
                    continue

                # 后台任务运行 agent，主循环保持活跃以便接收 approval_response
                async def agent_runner(user_message, room, http_client, websocket, brain):
                    async def safe_send(payload: dict):
                        try:
                            await websocket.send_json(payload)
                        except Exception as e:
                            run_logger.warning(f"[{room_code}] browser send failed (client gone?): {e}")

                    try:
                        if brain == "hermes":
                            answer = await asyncio.wait_for(
                                run_agent_hermes(user_message, room, http_client, websocket),
                                timeout=330.0  # Hermes 自治 agent 需要更长预算
                            )
                        else:
                            answer = await asyncio.wait_for(
                                run_agent(user_message, room, http_client, websocket),
                                timeout=300.0  # 5 minute total timeout for agent
                            )
                        chat_logger.info(f"[{room_code}] AI ({brain}): {answer[:500]}")
                        await safe_send({
                            "type": "ai_message",
                            "content": answer,
                        })
                        await safe_send({"type": "ai_done"})
                    except asyncio.TimeoutError:
                        run_logger.error(f"[{room_code}] Agent timed out after 5 minutes")
                        await safe_send({
                            "type": "error",
                            "content": "Request timed out. Please try again with a simpler question.",
                        })
                    except httpx.HTTPStatusError as e:
                        await safe_send({
                            "type": "error",
                            "content": f"AI API call failed ({e.response.status_code}). Please check API configuration.",
                        })
                    except Exception as e:
                        run_logger.exception(f"Agent error in room {room_code}")
                        await safe_send({
                            "type": "error",
                            "content": f"Agent error: {str(e)}",
                        })

                agent_task = asyncio.create_task(
                    agent_runner(user_message, room, http_client, websocket, brain)
                )

            elif msg_data.get("type") == "approval_response":
                # User responded to an approval prompt
                approval_id = msg_data["id"]
                approved = msg_data.get("approved", False)
                reason = msg_data.get("reason", "")
                future = room.pending_approvals.get(approval_id)
                if future and not future.done():
                    future.set_result({"approved": approved, "reason": reason})
                    run_logger.info(f"[{room_code}] Approval {approval_id}: {'approved' if approved else 'denied'}")

            elif msg_data.get("type") == "auto_approve_toggle":
                # Toggle auto-approve for Tier 2
                room.auto_approve_tier2 = msg_data.get("enabled", False)
                await websocket.send_json({
                    "type": "status",
                    "content": f"Tier 2 auto-approval: {'ON' if room.auto_approve_tier2 else 'OFF'}",
                })
                run_logger.info(f"[{room_code}] auto_approve_tier2 = {room.auto_approve_tier2}")

            elif msg_data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        run_logger.info(f"[browser] left room {room_code}")
    finally:
        room.browser_ws = None
        await http_client.aclose()


@app.websocket("/ws/bridge/{room_code}")
async def ws_bridge(websocket: WebSocket, room_code: str):
    await websocket.accept()

    room = rooms.get(room_code)
    if not room:
        # 房间必须先在数据库中存在（工作台创建时绑定 SN/工单号）——防止绕过创建限制
        if not room_record_exists(room_code):
            await websocket.send_json({"type": "error", "content": "Room not found. Create it from the dashboard first (SN + ticket required)."})
            await websocket.close()
            return
        room = Room(room_code)
        rooms[room_code] = room
        run_logger.info(f"Room re-created from DB (bridge): {room_code}")

    reason = "unknown"  # 断开原因（finally 中记录，需在 try 前初始化）

    room.bridge_ws = websocket
    room.remote_ip = websocket.client.host if hasattr(websocket.client, 'host') else ""
    room.bridge_connect_count += 1
    room.last_heartbeat = datetime.now(timezone.utc)
    run_logger.info(f"[bridge] joined room {room_code} (ip={room.remote_ip}, connect#{room.bridge_connect_count})")

    await websocket.send_json({
        "type": "status",
        "content": f"Connected to room {room_code}",
    })

    if room.browser_ws:
        await room.browser_ws.send_json({
            "type": "status",
            "content": "Bridge connected [OK] — ready for diagnostics",
            "bridge_connected": True,
        })

    # Ask bridge for machine identity (bridge may have sent it already,
    # but this is a fallback in case the auto-send was missed)
    await websocket.send_json({"type": "identify_request"})

    try:
        while True:
            raw = await websocket.receive_text()
            msg_data = json.loads(raw)

            if msg_data.get("type") == "command_result":
                cmd_id = msg_data["id"]
                output = msg_data.get("output", "")
                future = room.pending_commands.get(cmd_id)
                if future and not future.done():
                    future.set_result(output)

            elif msg_data.get("type") == "file_download_result":
                # bridge 分块上传文件 → 服务器拼接
                fid = msg_data["id"]
                run_logger.info(f"[{room_code}] file chunk fid={fid} {msg_data.get('chunk')}/{msg_data.get('total')}")
                buf = room.pending_files.get(fid, {"chunks": [], "total": msg_data.get("total", 1), "size": msg_data.get("size", 0), "name": msg_data.get("name", "")})
                buf["chunks"].append(msg_data.get("data", ""))
                room.pending_files[fid] = buf
                if len(buf["chunks"]) >= buf["total"]:
                    try:
                        raw = b"".join(base64.b64decode(c) for c in buf["chunks"])
                        fut = room.pending_commands.get(fid)
                        run_logger.info(f"[{room_code}] file complete fid={fid} chunks={len(buf['chunks'])} rawlen={len(raw)}")
                        if fut and not fut.done():
                            fut.set_result(f"[file_received] name={buf['name']} size={len(raw)} bytes")
                        else:
                            run_logger.warning(f"[{room_code}] file_download_result for unknown id {fid}")
                    except Exception as e:
                        run_logger.error(f"[{room_code}] 文件拼接失败: {e} buflen={len(buf['chunks'])}")
                    room.pending_files.pop(fid, None)

            elif msg_data.get("type") == "file_download_error":
                fid = msg_data["id"]
                fut = room.pending_commands.get(fid)
                if fut and not fut.done():
                    fut.set_result(f"[file_error] {msg_data.get('error', '')}")
                room.pending_files.pop(fid, None)

            elif msg_data.get("type") == "file_upload_result":
                fid = msg_data["id"]
                fut = room.pending_commands.get(fid)
                if fut and not fut.done():
                    fut.set_result(f"[file_uploaded] path={msg_data.get('path', '')}")

            elif msg_data.get("type") == "identify":
                # Bridge sent machine identity info
                info = msg_data.get("info", {})
                room.machine = info
                # v2 go-pipe bridge 上报 platform；旧 python bridge 无此字段
                if info.get("bridge") == "go-pipe" or info.get("platform"):
                    room.bridge_mode = "v2"
                    room.platform = info.get("platform", "windows")
                else:
                    room.bridge_mode = "v1"
                    room.platform = "windows"
                run_logger.info(f"[bridge] {room_code} identify: hostname={info.get('hostname')}, "
                                f"os={info.get('os')}, platform={room.platform}, mode={room.bridge_mode}, "
                                f"ip={info.get('local_ip')}, user={info.get('username')}")

            elif msg_data.get("type") == "heartbeat":
                # 必须回复，否则客户端 75s 读超时会断开重连（导致状态反复切换）
                room.last_heartbeat = datetime.now(timezone.utc)
                await websocket.send_json({"type": "pong"})

            elif msg_data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect as e:
        reason = f"websocket_disconnect code={e.code or 'unknown'}"
        run_logger.info(f"[bridge] left room {room_code} ({reason})")
    except Exception as e:
        reason = f"error: {type(e).__name__}: {e}"
        run_logger.warning(f"[bridge] ws_bridge exception in {room_code}: {reason}")
    finally:
        room.bridge_ws = None
        room.bridge_disconnect_count += 1
        room.last_disconnect_reason = reason
        room.last_disconnect_at = datetime.now(timezone.utc)
        if room.browser_ws:
            try:
                await room.browser_ws.send_json({
                    "type": "status",
                    "content": "Bridge disconnected [--]",
                    "bridge_connected": False,
                })
            except Exception:
                pass


# ============================================================
# Entry point
# ============================================================
if __name__ == "__main__":
    import uvicorn
    run_logger.info(f"Starting server v0.8.0 on {SERVER_HOST}:{SERVER_PORT}, model={OPENAI_MODEL}, tools={len(TOOLS)}")
    run_logger.info(f"DB: {DB_PATH}, approval: enabled for Tier 2/3")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")