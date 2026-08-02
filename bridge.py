"""
Cloud AI Remote Diagnostics Assistant — Windows Bridge
Runs on the user's Windows PC. Connects to cloud server, receives and executes diagnostic/control commands.

Integrated with 45 tools from winremote-mcp.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import locale
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import websockets

# ============================================================
# Optional imports — tools degrade gracefully when missing
# ============================================================
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

try:
    from PIL import ImageGrab, ImageDraw, ImageFont
    HAS_PIL = True
    # Enable DPI awareness
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
except ImportError:
    ImageGrab = None
    ImageDraw = None
    ImageFont = None
    HAS_PIL = False

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.05
    HAS_PYAUTOGUI = True
except ImportError:
    pyautogui = None
    HAS_PYAUTOGUI = False

try:
    from thefuzz import fuzz
    HAS_THEFUZZ = True
except ImportError:
    fuzz = None
    HAS_THEFUZZ = False

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    tabulate = None
    HAS_TABULATE = False

# Win32 imports
try:
    import win32api
    import win32clipboard
    import win32con
    import win32gui
    import win32process
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# Winreg (stdlib, but windows only)
try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

# ============================================================
# Logging config
# ============================================================
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "bridge.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bridge")

# ============================================================
# Config
# ============================================================
SERVER_URL = "ws://106.54.193.9:8000"


# ============================================================
# Helpers
# ============================================================
def _tobool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


def _ps_escape(s: str) -> str:
    return s.replace("'", "''")


def _run_ps(command: str, timeout: int = 30) -> str:
    """Run a PowerShell command synchronously, return output."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[STDERR] {result.stderr}"
        if result.returncode != 0:
            output += f"\n[Exit code: {result.returncode}]"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s"
    except Exception as e:
        return f"PowerShell error: {e}"


def _ps_to_json(command: str, timeout: int = 30) -> str:
    """Run PowerShell, pipe to ConvertTo-Json, return raw JSON string."""
    full = f"{command} | ConvertTo-Json -Depth 4 -Compress"
    return _run_ps(full, timeout)


def _tabulate(rows, headers, tablefmt="simple") -> str:
    if HAS_TABULATE and tabulate:
        return tabulate(rows, headers=headers, tablefmt=tablefmt)
    # fallback ASCII table
    lines = ["  ".join(str(h) for h in headers)]
    lines.append("-" * len(lines[0]))
    for row in rows:
        lines.append("  ".join(str(c) for c in row))
    return "\n".join(lines)


def _fuzzy_match(query: str, candidates: list[str], threshold: int = 60) -> str | None:
    """Return best fuzzy match or None."""
    if HAS_THEFUZZ and fuzz:
        best_score, best = 0, None
        for c in candidates:
            score = fuzz.partial_ratio(query.lower(), c.lower())
            if score > best_score:
                best_score, best = score, c
        return best if best_score >= threshold else None
    # fallback: substring match
    q = query.lower()
    for c in candidates:
        if q in c.lower():
            return c
    return None


def _truncate(text: str, max_len: int = 8000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n\n... (truncated)"


# ============================================================
# Original tools (kept for backward compatibility)
# ============================================================
async def run_systeminfo() -> str:
    proc = await asyncio.create_subprocess_exec(
        "systeminfo",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    if proc.returncode != 0:
        return f"[error] systeminfo failed:\n{stderr.decode('gbk', errors='replace')}"
    return stdout.decode("gbk", errors="replace") or stdout.decode("utf-8", errors="replace")


async def run_dxdiag() -> str:
    tmpdir = tempfile.gettempdir()
    tmpfile = os.path.join(tmpdir, f"dxdiag_output_{os.getpid()}.txt")
    if os.path.exists(tmpfile):
        os.remove(tmpfile)
    proc = await asyncio.create_subprocess_exec(
        "dxdiag", "/t", tmpfile,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=60.0)
    await asyncio.sleep(1)
    if not os.path.exists(tmpfile):
        return "[error] dxdiag report file not generated"
    try:
        with open(tmpfile, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"[error] failed to read dxdiag report: {e}"
    finally:
        try:
            os.remove(tmpfile)
        except Exception:
            pass
    if not content.strip():
        return "[warning] dxdiag report is empty"
    return _extract_dxdiag_key_sections(content)


def _extract_dxdiag_key_sections(content: str) -> str:
    key_headers = [
        "System Information", "Display Devices", "Sound Devices",
        "Disk & DVD/CD-ROM Drives", "System Devices",
    ]
    sections = []
    lines = content.split("\n")
    in_key = False
    current = []
    for line in lines:
        is_header = any(
            line.strip().startswith("--") and h in line for h in key_headers
        ) or line.strip().startswith("---------------")
        if is_header:
            if current:
                sections.append("\n".join(current))
                current = []
            in_key = any(h in line for h in key_headers)
            if in_key:
                sections.append(line)
            continue
        if in_key:
            if line.strip() == "":
                in_key = False
                if current:
                    sections.append("\n".join(current))
                    current = []
                continue
            current.append(line)
    if current:
        sections.append("\n".join(current))
    result = "\n".join(sections)
    return result[:6000] + ("\n\n... (truncated)" if len(result) > 6000 else "")


async def read_event_log(max_events: int = 50, level: str = "Error,Warning") -> str:
    ps = f"""
$maxEvents = {max_events}
$levels = @({",".join(f'"{l}"' for l in level.split(","))})
Get-WinEvent -LogName System -MaxEvents $maxEvents -ErrorAction SilentlyContinue |
    Where-Object {{ $_.LevelDisplayName -in $levels }} |
    Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message |
    ForEach-Object {{
        $msg = $_.Message -replace '\\r?\\n', ' '
        if ($msg.Length -gt 300) {{ $msg = $msg.Substring(0, 300) + "..." }}
        [PSCustomObject]@{{
            Time  = $_.TimeCreated.ToString("yyyy-MM-dd HH:mm:ss")
            Level = $_.LevelDisplayName
            ID    = $_.Id
            Source = $_.ProviderName
            Message = $msg
        }}
    }} | ConvertTo-Json -Depth 3
"""
    return await _run_powershell_script(ps)


async def run_powershell_command(command: str) -> str:
    return await _run_powershell_script(command)


async def _run_powershell_script(script: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        return "[timeout] PowerShell command exceeded 30s"
    out = stdout.decode("utf-8", errors="replace") or stdout.decode("gbk", errors="replace")
    err = stderr.decode("utf-8", errors="replace") or stderr.decode("gbk", errors="replace")
    parts = []
    if out.strip():
        parts.append(out.strip())
    if err.strip():
        parts.append(f"[stderr]\n{err.strip()}")
    return "\n".join(parts) if parts else "[done] command executed, no output"


# ============================================================
# Desktop tools (screenshot, clipboard, window mgmt)
# ============================================================
def _run_sync_in_executor(func, *args, **kwargs):
    """Helper for the dispatch: get or create an event loop and run a sync func."""
    # We're in async context; but these tool functions are sync.
    # We'll call them directly — the dispatcher wraps them in run_in_executor.
    return func(*args, **kwargs)


def tool_GetSystemInfo() -> str:
    if not HAS_PSUTIL:
        return "[error] psutil not installed"
    import platform as _platform
    cpu_pct = psutil.cpu_percent(interval=0.5)
    cpu_count = psutil.cpu_count()
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")
    boot = datetime.fromtimestamp(psutil.boot_time())
    uptime = datetime.now() - boot
    net = psutil.net_io_counters()
    lines = [
        f"System: {_platform.system()} {_platform.release()} ({_platform.machine()})",
        f"CPU: {cpu_pct}% ({cpu_count} cores)",
        f"Memory: {mem.percent}% — {mem.used // 1048576}MB / {mem.total // 1048576}MB",
        f"Disk (C:): {disk.percent}% — {disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB",
        f"Network: Sent {net.bytes_sent // 1048576}MB / Recv {net.bytes_recv // 1048576}MB",
        f"Uptime: {str(uptime).split('.')[0]} (boot: {boot.strftime('%Y-%m-%d %H:%M')})",
    ]
    return "\n".join(lines)


def tool_Snapshot(use_vision: str | bool = True, quality: int = 75,
                   max_width: int = 0, monitor: int = 0) -> str:
    if not HAS_PIL:
        return "[error] Pillow not installed"
    use_vision = _tobool(use_vision)
    lines = []
    # Screenshot as base64 JPEG
    if use_vision:
        try:
            if monitor <= 0:
                img = ImageGrab.grab(all_screens=True)
            else:
                monitors = win32api.EnumDisplayMonitors() if HAS_WIN32 else []
                if monitor <= len(monitors):
                    _hmon, _hdc, rect = monitors[monitor - 1]
                    img = ImageGrab.grab(bbox=(rect[0], rect[1], rect[2], rect[3]))
                else:
                    img = ImageGrab.grab(all_screens=True)
            if max_width > 0 and img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, int(img.height * ratio)), resample=3)
            if img.mode in ("RGBA", "LA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode()
            lines.append(f"[IMAGE:base64_jpeg:{b64}]")
        except Exception as e:
            lines.append(f"[screenshot error: {e}]")
    # Window list
    if HAS_WIN32:
        try:
            from dataclasses import dataclass
            windows = []
            def _cb(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                if not title:
                    return True
                rect = win32gui.GetWindowRect(hwnd)
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                except Exception:
                    pid = 0
                windows.append((hwnd, title, rect, pid))
                return True
            win32gui.EnumWindows(_cb, None)
            lines.append(f"\nWindows ({len(windows)}):")
            for hwnd, title, rect, pid in windows[:30]:
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                lines.append(f"  [{hwnd}] {title} ({w}x{h} at {rect[0]},{rect[1]})")
        except Exception as e:
            lines.append(f"[window list error: {e}]")
    # Interactive elements from foreground
    if HAS_WIN32:
        try:
            fg = win32gui.GetForegroundWindow()
            if fg:
                elements = []
                def _cb2(hwnd, _):
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    cls = win32gui.GetClassName(hwnd)
                    text = win32gui.GetWindowText(hwnd)
                    try:
                        rect = win32gui.GetWindowRect(hwnd)
                    except Exception:
                        return True
                    elements.append({
                        "index": len(elements) + 1,
                        "class": cls, "text": text,
                        "rect": {"left": rect[0], "top": rect[1],
                                  "right": rect[2], "bottom": rect[3]},
                    })
                    return True
                win32gui.EnumChildWindows(fg, _cb2, None)
                if elements:
                    lines.append(f"\nInteractive Elements ({len(elements)}):")
                    for el in elements[:30]:
                        r = el["rect"]
                        cx = (r["left"] + r["right"]) // 2
                        cy = (r["top"] + r["bottom"]) // 2
                        label = el["text"] or el["class"]
                        lines.append(f"  [{el['index']}] {label} — center ({cx},{cy})")
        except Exception:
            pass
    return "\n".join(lines)


def tool_AnnotatedSnapshot(max_elements: int = 30, quality: int = 75,
                             max_width: int = 0) -> str:
    if not HAS_PIL:
        return "[error] Pillow not installed"
    if not HAS_WIN32:
        return "[error] pywin32 not installed"
    try:
        img = ImageGrab.grab()
        native_w = img.width
        if max_width > 0 and img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)))
        # Enumerate elements
        fg = win32gui.GetForegroundWindow()
        elements = []
        if fg:
            def _cb(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                cls = win32gui.GetClassName(hwnd)
                text = win32gui.GetWindowText(hwnd)
                try:
                    rect = win32gui.GetWindowRect(hwnd)
                except Exception:
                    return True
                elements.append({
                    "index": len(elements) + 1,
                    "class": cls, "text": text,
                    "rect": {"left": rect[0], "top": rect[1],
                              "right": rect[2], "bottom": rect[3]},
                })
                return True
            win32gui.EnumChildWindows(fg, _cb, None)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("arial.ttf", 14)
        except Exception:
            font = ImageFont.load_default()
        scale = img.width / native_w if img.width != native_w else 1.0
        element_lines = []
        for el in elements[:max_elements]:
            idx = el["index"]
            r = el["rect"]
            x1, y1 = int(r["left"] * scale), int(r["top"] * scale)
            x2, y2 = int(r["right"] * scale), int(r["bottom"] * scale)
            draw.rectangle([x1, y1, x2, y2], outline="red", width=2)
            label = str(idx)
            bbox = font.getbbox(label)
            lw, lh = bbox[2] - bbox[0] + 6, bbox[3] - bbox[1] + 4
            draw.rectangle([x1, y1 - lh - 2, x1 + lw, y1 - 2], fill="red")
            draw.text((x1 + 3, y1 - lh - 1), label, fill="white", font=font)
            cx = (r["left"] + r["right"]) // 2
            cy = (r["top"] + r["bottom"]) // 2
            name = el["text"] or el["class"]
            element_lines.append(f"  [{idx}] {name} — center ({cx},{cy})")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode()
        text = f"[IMAGE:base64_jpeg:{b64}]\nAnnotated {len(element_lines)} elements:\n" + "\n".join(element_lines)
        return text
    except Exception as e:
        return f"AnnotatedSnapshot error: {e}"


def tool_GetClipboard() -> str:
    if not HAS_WIN32:
        return "[error] pywin32 not installed"
    try:
        win32clipboard.OpenClipboard()
        data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        return data
    except Exception as e:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
        return f"GetClipboard error: {e}"


def tool_SetClipboard(text: str) -> str:
    if not HAS_WIN32:
        return "[error] pywin32 not installed"
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        return "Clipboard set"
    except Exception as e:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
        return f"SetClipboard error: {e}"


def tool_Click(x: int, y: int, button: str = "left", action: str = "click") -> str:
    if not HAS_PYAUTOGUI:
        return "[error] pyautogui not installed"
    try:
        if action == "hover":
            pyautogui.moveTo(x, y)
            return f"Hovered at ({x},{y})"
        elif action == "double":
            pyautogui.doubleClick(x, y, button=button)
            return f"Double-clicked {button} at ({x},{y})"
        else:
            pyautogui.click(x, y, button=button)
            return f"Clicked {button} at ({x},{y})"
    except Exception as e:
        return f"Click error: {e}"


def tool_Type(text: str, x: int = 0, y: int = 0,
               clear: str | bool = False, press_enter: str | bool = False) -> str:
    if not HAS_PYAUTOGUI:
        return "[error] pyautogui not installed"
    try:
        if x or y:
            pyautogui.click(x, y)
            time.sleep(0.1)
        if _tobool(clear):
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("delete")
            time.sleep(0.05)
        pyautogui.typewrite(text, interval=0.02) if text.isascii() else pyautogui.write(text)
        if _tobool(press_enter):
            pyautogui.press("enter")
        return f"Typed {len(text)} chars"
    except Exception as e:
        return f"Type error: {e}"


def tool_Scroll(amount: int, x: int = 0, y: int = 0,
                 horizontal: str | bool = False) -> str:
    if not HAS_PYAUTOGUI:
        return "[error] pyautogui not installed"
    try:
        if x or y:
            pyautogui.moveTo(x, y)
        if _tobool(horizontal):
            pyautogui.hscroll(amount)
        else:
            pyautogui.scroll(amount)
        direction = "horizontally" if _tobool(horizontal) else "vertically"
        return f"Scrolled {amount} {direction}"
    except Exception as e:
        return f"Scroll error: {e}"


def tool_Move(x: int, y: int, drag: str | bool = False,
               start_x: int = 0, start_y: int = 0, duration: float = 0.3) -> str:
    if not HAS_PYAUTOGUI:
        return "[error] pyautogui not installed"
    try:
        if _tobool(drag):
            if start_x or start_y:
                pyautogui.moveTo(start_x, start_y)
            pyautogui.drag(x - pyautogui.position()[0], y - pyautogui.position()[1],
                           duration=duration)
            return f"Dragged to ({x},{y})"
        else:
            pyautogui.moveTo(x, y, duration=duration)
            return f"Moved to ({x},{y})"
    except Exception as e:
        return f"Move error: {e}"


def tool_Shortcut(keys: str) -> str:
    if not HAS_PYAUTOGUI:
        return "[error] pyautogui not installed"
    try:
        parts = [k.strip() for k in keys.lower().split("+")]
        pyautogui.hotkey(*parts)
        return f"Executed shortcut: {keys}"
    except Exception as e:
        return f"Shortcut error: {e}"


def tool_Wait(seconds: float = 1.0) -> str:
    time.sleep(seconds)
    return f"Waited {seconds}s"


def tool_FocusWindow(title: str = "", handle: int = 0) -> str:
    if not HAS_WIN32:
        return "[error] pywin32 not installed"
    hwnd = None
    if handle:
        hwnd = handle
    elif title:
        best_score, best = 0, None
        def _cb(h, _):
            nonlocal best_score, best
            if not win32gui.IsWindowVisible(h):
                return True
            t = win32gui.GetWindowText(h)
            if not t:
                return True
            s = fuzz.partial_ratio(title.lower(), t.lower()) if HAS_THEFUZZ else (
                100 if title.lower() in t.lower() else 0)
            if s > best_score:
                best_score, best = s, h
            return True
        win32gui.EnumWindows(_cb, None)
        if best_score < 50:
            return f"No window matching '{title}' (best score {best_score})"
        hwnd = best
    if not hwnd:
        return "No window found"
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return f"Focused window handle={hwnd} title='{win32gui.GetWindowText(hwnd)}'"
    except Exception as e:
        return f"FocusWindow error: {e}"


def tool_MinimizeAll() -> str:
    if HAS_PYAUTOGUI:
        try:
            pyautogui.hotkey("win", "d")
            return "Minimized all windows"
        except Exception as e:
            return f"MinimizeAll error: {e}"
    else:
        try:
            ctypes.windll.user32.keybd_event(0x5B, 0, 0, 0)  # LWin down
            ctypes.windll.user32.keybd_event(0x44, 0, 0, 0)  # D down
            ctypes.windll.user32.keybd_event(0x44, 0, 2, 0)  # D up
            ctypes.windll.user32.keybd_event(0x5B, 0, 2, 0)  # LWin up
            return "Minimized all windows"
        except Exception as e:
            return f"MinimizeAll error: {e}"


def tool_App(action: str = "launch", name: str = "", args: str = "",
              handle: int = 0, width: int = 0, height: int = 0) -> str:
    try:
        if action == "launch":
            safe = name.replace("'", "''")
            cmd = f"Start-Process '{safe}'"
            if args:
                safe_a = args.replace("'", "''")
                cmd += f" -ArgumentList '{safe_a}'"
            subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           timeout=10, capture_output=True)
            return f"Launched {name}"
        elif action == "switch":
            return tool_FocusWindow(title=name, handle=handle)
        elif action == "resize":
            if not handle:
                return "resize requires a window handle"
            if not HAS_WIN32:
                return "[error] pywin32 not installed"
            rect = win32gui.GetWindowRect(handle)
            win32gui.MoveWindow(handle, rect[0], rect[1], width, height, True)
            return f"Resized {handle} to {width}x{height}"
        return f"Unknown action: {action}"
    except Exception as e:
        return f"App error: {e}"


def tool_Notification(title: str = "Bridge Alert", message: str = "") -> str:
    from xml.sax.saxutils import escape as xml_escape
    safe_title = xml_escape(title)
    safe_msg = xml_escape(message)
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = @"
<toast>
  <visual><binding template="ToastGeneric">
    <text>{safe_title}</text>
    <text>{safe_msg}</text>
  </binding></visual>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("BridgeApp").Show($toast)
"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       timeout=10, capture_output=True)
        return "Notification shown"
    except Exception as e:
        return f"Notification error: {e}"


def tool_PlaySound(path: str = "", url: str = "") -> str:
    tmp_path = None
    try:
        if not path and not url:
            return "[error] provide either 'path' (local file) or 'url' (remote file)"
        if url and not path:
            import urllib.request
            suffix = ".wav"
            if ".mp3" in url:
                suffix = ".mp3"
            elif ".ogg" in url:
                suffix = ".ogg"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            urllib.request.urlretrieve(url, tmp_path)
            path = tmp_path
        ext = os.path.splitext(path)[1].lower()
        safe = path.replace("'", "''")
        if ext in (".mp3", ".ogg", ".wma", ".m4a"):
            ps = (
                "Add-Type -AssemblyName presentationCore; "
                f"$p = New-Object System.Windows.Media.MediaPlayer; "
                f"$p.Open([uri]'{safe}'); $p.Play(); "
                "Start-Sleep -Milliseconds 500; "
                "while ($p.NaturalDuration.HasTimeSpan -and "
                "$p.Position -lt $p.NaturalDuration.TimeSpan) "
                "{ Start-Sleep -Milliseconds 200 }; $p.Close()"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           timeout=120, capture_output=True)
        else:
            ps = f"(New-Object System.Media.SoundPlayer '{safe}').PlaySync()"
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           timeout=30, capture_output=True)
        return f"Played: {path}"
    except Exception as e:
        return f"PlaySound error: {e}"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def tool_LockScreen() -> str:
    try:
        ctypes.windll.user32.LockWorkStation()
        return "Screen locked"
    except Exception as e:
        return f"LockScreen error: {e}"


def tool_ReconnectSession(force: bool = False) -> str:
    try:
        result = subprocess.run(["query", "session"], capture_output=True,
                                text=True, timeout=10)
        if result.returncode != 0:
            return f"Failed to query sessions: {result.stderr}"
        lines = result.stdout.strip().split("\n")
        user_sid, is_disc = None, False
        for line in lines[1:]:
            line = line.lstrip(">").strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            name = parts[0].lower()
            if name in ("services", "rdp-tcp"):
                continue
            for i, p in enumerate(parts[1:], 1):
                if p.isdigit():
                    sid = int(p)
                    if i + 1 < len(parts):
                        state = parts[i + 1].lower()
                        has_user = i > 1 and not parts[i - 1].isdigit()
                        if has_user or name == "console":
                            user_sid = sid
                            is_disc = state in ("disc", "disconnected")
                    break
        if user_sid is None:
            return "No user session found"
        if not is_disc and not force:
            return "Session already connected"
        subprocess.run(["tscon", str(user_sid), "/dest:console"],
                       capture_output=True, text=True, timeout=10)
        time.sleep(1)
        return "Session reconnected to console"
    except Exception as e:
        return f"ReconnectSession error: {e}"


# ============================================================
# Process tools
# ============================================================
def tool_ListProcesses(filter: str = "", sort_by: str = "memory",
                        limit: int = 30) -> str:
    if not HAS_PSUTIL:
        return "[error] psutil not installed"
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status"]):
        try:
            info = p.info
            name = info["name"] or ""
            if filter:
                if HAS_THEFUZZ and fuzz:
                    if fuzz.partial_ratio(filter.lower(), name.lower()) < 60:
                        continue
                elif filter.lower() not in name.lower():
                    continue
            mem_mb = (info["memory_info"].rss / 1048576) if info["memory_info"] else 0
            procs.append({
                "PID": info["pid"], "Name": name,
                "CPU%": info["cpu_percent"] or 0,
                "Mem(MB)": round(mem_mb, 1),
                "Status": info["status"],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    key_map = {"cpu": "CPU%", "memory": "Mem(MB)", "name": "Name"}
    sk = key_map.get(sort_by, "Mem(MB)")
    procs.sort(key=lambda x: x[sk], reverse=(sk != "Name"))
    procs = procs[:limit]
    if not procs:
        return "No processes found."
    return _tabulate([list(p.values()) for p in procs], list(procs[0].keys()))


def tool_KillProcess(pid: int = 0, name: str = "") -> str:
    if not HAS_PSUTIL:
        return "[error] psutil not installed"
    if pid:
        try:
            p = psutil.Process(pid)
            p.kill()
            return f"Killed PID {pid} ({p.name()})"
        except psutil.NoSuchProcess:
            return f"PID {pid} not found"
        except psutil.AccessDenied:
            return f"Access denied for PID {pid}"
        except Exception as e:
            return f"KillProcess error: {e}"
    if name:
        killed = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                pname = p.info["name"] or ""
                match = False
                if HAS_THEFUZZ and fuzz:
                    match = fuzz.ratio(name.lower(), pname.lower()) > 80
                else:
                    match = name.lower() == pname.lower()
                if match:
                    p.kill()
                    killed.append(f"{pname} (PID {p.info['pid']})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed:
            return f"Killed: {', '.join(killed)}"
        return f"No process matching '{name}'"
    return "Provide pid or name."


# ============================================================
# Network tools
# ============================================================
def tool_Ping(host: str, count: int = 4) -> str:
    try:
        result = subprocess.run(
            ["ping", "-n", str(count), host],
            capture_output=True, text=True,
            timeout=count * 5 + 10,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Ping timed out after {count * 5 + 10}s"
    except Exception as e:
        return f"Ping error: {e}"


def tool_PortCheck(host: str, port: int, timeout: float = 5.0) -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        if result == 0:
            return f"Port {port} on {host} is OPEN"
        else:
            return f"Port {port} on {host} is CLOSED (code {result})"
    except socket.timeout:
        return f"Port {port} on {host} — connection timed out ({timeout}s)"
    except Exception as e:
        return f"PortCheck error: {e}"


def tool_NetConnections(filter: str = "", limit: int = 50) -> str:
    if not HAS_PSUTIL:
        return "[error] psutil not installed"
    try:
        conns = psutil.net_connections(kind="inet")
        rows = []
        for c in conns:
            local = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""
            remote = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""
            status, pid = c.status, c.pid or ""
            if filter:
                searchable = f"{local} {remote} {status} {pid}"
                if filter.lower() not in searchable.lower():
                    continue
            rows.append([local, remote, status, pid])
        if not rows:
            return "No connections found."
        return _tabulate(rows[:limit], ["Local", "Remote", "Status", "PID"])
    except Exception as e:
        return f"NetConnections error: {e}"


# ============================================================
# File tools
# ============================================================
def tool_FileRead(path: str, encoding: str = "utf-8") -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"
        if encoding == "binary":
            data = p.read_bytes()
            return base64.b64encode(data).decode()
        else:
            return _truncate(p.read_text(encoding=encoding, errors="replace"), 100000)
    except Exception as e:
        return f"FileRead error: {e}"


def tool_FileWrite(path: str, content: str, encoding: str = "utf-8",
                    append: str | bool = False) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if _tobool(append) else "w"
        with open(p, mode, encoding=encoding) as f:
            f.write(content)
        return f"Written {len(content)} chars to {path}"
    except Exception as e:
        return f"FileWrite error: {e}"


def tool_FileList(path: str = ".", show_hidden: str | bool = False) -> str:
    try:
        p = Path(path)
        if not p.is_dir():
            return f"Not a directory: {path}"
        rows = []
        for item in sorted(p.iterdir()):
            name = item.name
            if not _tobool(show_hidden) and name.startswith("."):
                continue
            try:
                stat = item.stat()
                size = stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                kind = "DIR" if item.is_dir() else "FILE"
                if item.is_dir():
                    size_str = "<DIR>"
                elif size < 1024:
                    size_str = f"{size}B"
                elif size < 1048576:
                    size_str = f"{size // 1024}KB"
                else:
                    size_str = f"{size // 1048576}MB"
                rows.append([kind, name, size_str, mtime])
            except Exception:
                rows.append(["?", name, "?", "?"])
        if not rows:
            return "Directory is empty."
        return _tabulate(rows, ["Type", "Name", "Size", "Modified"])
    except Exception as e:
        return f"FileList error: {e}"


def tool_FileSearch(pattern: str, path: str = ".",
                     recursive: str | bool = True, limit: int = 50) -> str:
    try:
        p = Path(path)
        matches = list(p.rglob(pattern)) if _tobool(recursive) else list(p.glob(pattern))
        if not matches:
            return f"No files matching '{pattern}' in {path}"
        lines = []
        for m in matches[:limit]:
            try:
                lines.append(f"  {m} ({m.stat().st_size} bytes)")
            except Exception:
                lines.append(f"  {m}")
        result = f"Found {len(matches)} files"
        if len(matches) > limit:
            result += f" (showing first {limit})"
        return result + ":\n" + "\n".join(lines)
    except Exception as e:
        return f"FileSearch error: {e}"


def tool_FileDownload(path: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"File not found: {path}"
        data = p.read_bytes()
        b64 = base64.b64encode(data).decode()
        return f"base64:{len(data)}bytes:{b64}"
    except Exception as e:
        return f"FileDownload error: {e}"


def tool_FileUpload(path: str, data_base64: str) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = base64.b64decode(data_base64, validate=True)
        p.write_bytes(data)
        return f"Written {len(data)} bytes to {path}"
    except Exception as e:
        return f"FileUpload error: {e}"


# ============================================================
# Registry tools
# ============================================================
_ROOT_KEYS = {
    "hkcr": winreg.HKEY_CLASSES_ROOT if HAS_WINREG else None,
    "hkey_classes_root": winreg.HKEY_CLASSES_ROOT if HAS_WINREG else None,
    "hkcu": winreg.HKEY_CURRENT_USER if HAS_WINREG else None,
    "hkey_current_user": winreg.HKEY_CURRENT_USER if HAS_WINREG else None,
    "hklm": winreg.HKEY_LOCAL_MACHINE if HAS_WINREG else None,
    "hkey_local_machine": winreg.HKEY_LOCAL_MACHINE if HAS_WINREG else None,
    "hku": winreg.HKEY_USERS if HAS_WINREG else None,
    "hkey_users": winreg.HKEY_USERS if HAS_WINREG else None,
    "hkcc": winreg.HKEY_CURRENT_CONFIG if HAS_WINREG else None,
    "hkey_current_config": winreg.HKEY_CURRENT_CONFIG if HAS_WINREG else None,
}

_REG_TYPES = {
    "reg_sz": winreg.REG_SZ if HAS_WINREG else None,
    "reg_expand_sz": winreg.REG_EXPAND_SZ if HAS_WINREG else None,
    "reg_dword": winreg.REG_DWORD if HAS_WINREG else None,
    "reg_qword": winreg.REG_QWORD if HAS_WINREG else None,
    "reg_binary": winreg.REG_BINARY if HAS_WINREG else None,
    "reg_multi_sz": winreg.REG_MULTI_SZ if HAS_WINREG else None,
}


def _parse_key(key: str):
    parts = key.split("\\", 1)
    root_name = parts[0].lower()
    subkey = parts[1] if len(parts) > 1 else ""
    root = _ROOT_KEYS.get(root_name)
    if root is None:
        raise ValueError(f"Unknown root key: {root_name}. Use HKCR, HKCU, HKLM, HKU, or HKCC.")
    return root, subkey


def tool_RegRead(key: str, value_name: str) -> str:
    if not HAS_WINREG:
        return "[error] Registry only available on Windows"
    try:
        root, subkey = _parse_key(key)
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as k:
            data, reg_type = winreg.QueryValueEx(k, value_name)
            return f"Value: {data!r} (type: {reg_type})"
    except FileNotFoundError:
        return f"[error] Key or value not found: {key}\\{value_name}"
    except Exception as e:
        return f"RegRead error: {e}"


def tool_RegWrite(key: str, value_name: str, data: str, reg_type: str = "REG_SZ") -> str:
    if not HAS_WINREG:
        return "[error] Registry only available on Windows"
    try:
        root, subkey = _parse_key(key)
        rtype = _REG_TYPES.get(reg_type.lower())
        if rtype is None:
            return f"[error] Unknown type '{reg_type}'. Use: {', '.join(_REG_TYPES.keys())}"
        final_data = data
        if reg_type.upper() in ("REG_DWORD", "REG_QWORD"):
            final_data = int(data)
        elif reg_type.upper() == "REG_MULTI_SZ":
            final_data = data.split("|")
        with winreg.CreateKey(root, subkey) as k:
            winreg.SetValueEx(k, value_name, 0, rtype, final_data)
            return f"Written {value_name} = {final_data!r} to {key}"
    except Exception as e:
        return f"RegWrite error: {e}"


# ============================================================
# Service tools
# ============================================================
def tool_ServiceList(filter: str = "") -> str:
    try:
        cmd = "Get-Service"
        if filter:
            safe = _ps_escape(filter)
            cmd = (f"$f = '{safe}'; Get-Service"
                   ' | Where-Object { $_.DisplayName -like "*$f*" -or $_.Name -like "*$f*" }')
        cmd += " | Format-Table Name, DisplayName, Status, StartType -AutoSize"
        return _run_ps(cmd)
    except Exception as e:
        return f"ServiceList error: {e}"


def tool_ServiceStart(name: str) -> str:
    try:
        safe = _ps_escape(name)
        return _run_ps(f"Start-Service -Name '{safe}' -PassThru | Format-Table Name, Status -AutoSize")
    except Exception as e:
        return f"ServiceStart error: {e}"


def tool_ServiceStop(name: str) -> str:
    try:
        safe = _ps_escape(name)
        return _run_ps(f"Stop-Service -Name '{safe}' -Force -PassThru | Format-Table Name, Status -AutoSize")
    except Exception as e:
        return f"ServiceStop error: {e}"


# ============================================================
# Scheduled task tools
# ============================================================
def tool_TaskList(filter: str = "") -> str:
    try:
        cmd = "Get-ScheduledTask"
        if filter:
            safe = _ps_escape(filter)
            cmd = f"$f = '{safe}'; Get-ScheduledTask | Where-Object {{ $_.TaskName -like \"*$f*\" }}"
        cmd += " | Format-Table TaskName, State, TaskPath -AutoSize"
        return _run_ps(cmd)
    except Exception as e:
        return f"TaskList error: {e}"


def tool_TaskCreate(name: str, command: str, schedule: str) -> str:
    try:
        safe_name = _ps_escape(name)
        safe_cmd = _ps_escape(command)
        safe_sched = _ps_escape(schedule)
        return _run_ps(f"schtasks /Create /TN '{safe_name}' /TR '{safe_cmd}' /SC '{safe_sched}' /F")
    except Exception as e:
        return f"TaskCreate error: {e}"


def tool_TaskDelete(name: str) -> str:
    try:
        safe = _ps_escape(name)
        return _run_ps(f"schtasks /Delete /TN '{safe}' /F")
    except Exception as e:
        return f"TaskDelete error: {e}"


# ============================================================
# EventLog (enhanced — multiple log types + level filter)
# ============================================================
def tool_EventLog(log_name: str = "System", count: int = 20, level: str = "") -> str:
    try:
        count = min(max(count, 1), 1000)
        safe_log = _ps_escape(log_name)
        cmd = f"Get-WinEvent -LogName '{safe_log}' -MaxEvents {count}"
        if level:
            level_map = {
                "critical": 1, "error": 2, "warning": 3,
                "information": 4, "verbose": 5,
            }
            lvl_num = level_map.get(level.lower())
            if lvl_num:
                cmd = (f"Get-WinEvent -FilterHashtable @{{LogName='{safe_log}';Level={lvl_num}}}"
                       f" -MaxEvents {count}")
        cmd += " | Format-Table TimeCreated, Id, LevelDisplayName, Message -AutoSize -Wrap"
        return _run_ps(cmd, timeout=30)
    except Exception as e:
        return f"EventLog error: {e}"


# ============================================================
# OCR
# ============================================================
def tool_OCR(left: int = 0, top: int = 0, right: int = 0,
              bottom: int = 0, lang: str = "eng") -> str:
    if not HAS_PIL:
        return "[error] Pillow not installed"
    # Try pytesseract first
    try:
        import pytesseract
        if left or top or right or bottom:
            img = ImageGrab.grab(bbox=(left, top, right, bottom))
        else:
            img = ImageGrab.grab()
        return pytesseract.image_to_string(img, lang=lang).strip() or "(no text detected)"
    except ImportError:
        pass
    except Exception as e:
        log.warning(f"pytesseract OCR failed: {e}")
    # Fallback: Windows built-in OCR
    try:
        png_bytes = io.BytesIO()
        if left or top or right or bottom:
            img = ImageGrab.grab(bbox=(left, top, right, bottom))
        else:
            img = ImageGrab.grab()
        img.save(png_bytes, format="PNG")
        png_data = png_bytes.getvalue()
    except Exception as e:
        return f"OCR screenshot error: {e}"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(png_data)
        tmp_path = tmp.name
    try:
        ps = f"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
Add-Type -TypeDefinition @'
using System;
using System.Threading.Tasks;
using System.Runtime.CompilerServices;
public static class AsyncHelper {{
    public static T Await<T>(Windows.Foundation.IAsyncOperation<T> op) {{
        return Task.Run(() => {{
            while (op.Status == Windows.Foundation.AsyncStatus.Started) {{
                System.Threading.Thread.Sleep(10);
            }}
            return op.GetResults();
        }}).Result;
    }}
}}
'@ -ReferencedAssemblies "C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\System.Runtime.WindowsRuntime.dll"
$path = '{tmp_path.replace(chr(39), chr(39) + chr(39))}'
$stream = [System.IO.File]::OpenRead($path)
$ras = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream($stream)
$decoder = [AsyncHelper]::Await([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($ras))
$bitmap = [AsyncHelper]::Await($decoder.GetSoftwareBitmapAsync())
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$result = [AsyncHelper]::Await($engine.RecognizeAsync($bitmap))
$stream.Close()
Write-Output $result.Text
"""
        result = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                capture_output=True, text=True, timeout=30)
        text = result.stdout.strip()
        return text if text else "(no text detected)"
    except Exception as e:
        return f"OCR error: {e}"
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


# ============================================================
# Screen Recording
# ============================================================
def tool_ScreenRecord(duration: float = 3.0, fps: int = 5,
                       left: int = 0, top: int = 0, right: int = 0,
                       bottom: int = 0, max_width: int = 800) -> str:
    if not HAS_PIL:
        return "[error] Pillow not installed"
    duration = min(max(duration, 0.5), 10.0)
    fps = min(max(fps, 1), 10)
    interval = 1.0 / fps
    total_frames = int(duration * fps)
    bbox = (left, top, right, bottom) if (left or top or right or bottom) else None
    frames = []
    start = time.monotonic()
    for i in range(total_frames):
        target_time = start + i * interval
        now = time.monotonic()
        if now < target_time:
            time.sleep(target_time - now)
        img = ImageGrab.grab(bbox=bbox)
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)))
        frames.append(img)
    if not frames:
        return "[error] No frames captured"
    buf = io.BytesIO()
    frame_dur = int(1000 / fps)
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                    duration=frame_dur, loop=0, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    size_kb = (len(b64) * 3 // 4) // 1024
    return f"[IMAGE:base64_gif:{b64}]\nRecorded {duration}s at {fps}fps ({size_kb}KB GIF)"


# ============================================================
# Shell
# ============================================================
def tool_Shell(command: str, timeout: int = 30, cwd: str = "") -> str:
    """Execute a PowerShell command."""
    try:
        if cwd:
            safe_cwd = cwd.replace("'", "''")
            command = f"Set-Location -LiteralPath '{safe_cwd}'; {command}"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[STDERR] {result.stderr}"
        if result.returncode != 0:
            output += f"\n[Exit code: {result.returncode}]"
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s"
    except Exception as e:
        return f"Shell error: {e}"


def tool_Scrape(url: str) -> str:
    """Fetch URL content as markdown."""
    try:
        import urllib.request
        import urllib.error
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return "Scrape error: only http/https allowed"
        if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
            return "Scrape error: localhost not allowed"
        req = urllib.request.Request(url, headers={"User-Agent": "Bridge/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read(1024 * 1024 + 1)
            if len(html) > 1024 * 1024:
                return "Scrape error: response exceeds 1 MB limit"
            html = html.decode("utf-8", errors="replace")
        # Try markdownify
        try:
            from markdownify import markdownify
            md = markdownify(html, heading_style="ATX", strip=["script", "style"])
        except ImportError:
            # Simple HTML-to-text fallback
            import re
            md = re.sub(r'<[^>]+>', '', html)
            md = re.sub(r'\n\s*\n', '\n\n', md)
        return _truncate(md.strip(), 50000)
    except Exception as e:
        return f"Scrape error: {e}"


# ============================================================
# Task management stubs (simplified — no async task manager needed)
# ============================================================
def tool_CancelTask(task_id: str) -> str:
    return f"Task cancellation not supported in bridge mode (task_id={task_id})"


def tool_GetTaskStatus(task_id: str = "") -> str:
    return "Task tracking not supported in bridge mode"


def tool_GetRunningTasks() -> str:
    return "Task tracking not supported in bridge mode"


# ============================================================
# Machine identification (collected on connect)
# ============================================================
def _collect_identify_info() -> dict:
    """Collect machine identity info: hostname, OS, IP, etc."""
    import platform as _platform
    info = {
        "hostname": "",
        "os": "",
        "arch": "",
        "python_version": "",
        "local_ip": "",
        "mac_address": "",
        "username": "",
        "domain": "",
        "connected_at": datetime.now().isoformat(),
    }
    try:
        info["hostname"] = _platform.node() or os.environ.get("COMPUTERNAME", "")
    except Exception:
        pass
    try:
        info["os"] = f"{_platform.system()} {_platform.release()} ({_platform.version()})"
    except Exception:
        pass
    try:
        info["arch"] = _platform.machine()
    except Exception:
        pass
    try:
        info["python_version"] = _platform.python_version()
    except Exception:
        pass
    try:
        info["username"] = os.environ.get("USERNAME", "") or os.environ.get("USER", "")
    except Exception:
        pass
    try:
        info["domain"] = os.environ.get("USERDOMAIN", "")
    except Exception:
        pass
    # Local IP — find first non-loopback
    try:
        hostname = info["hostname"]
        info["local_ip"] = socket.gethostbyname(hostname)
    except Exception:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            info["local_ip"] = s.getsockname()[0]
            s.close()
        except Exception:
            pass
    # MAC address
    try:
        import uuid
        mac = uuid.getnode()
        info["mac_address"] = ':'.join(f"{(mac >> (i*8)) & 0xff:02x}" for i in range(5, -1, -1))
    except Exception:
        pass
    return info


# ============================================================
# Tool dispatch table
# ============================================================
TOOL_TABLE = {
    # Original tools
    "run_systeminfo":      (run_systeminfo, True),
    "run_dxdiag":          (run_dxdiag, True),
    "read_event_log":      (read_event_log, True),
    "run_powershell":      (run_powershell_command, True),

    # Tier 1 — Read-only diagnostic
    "GetSystemInfo":       (tool_GetSystemInfo, False),
    "Snapshot":            (tool_Snapshot, False),
    "AnnotatedSnapshot":   (tool_AnnotatedSnapshot, False),
    "GetClipboard":        (tool_GetClipboard, False),
    "ListProcesses":       (tool_ListProcesses, False),
    "FileList":            (tool_FileList, False),
    "FileSearch":          (tool_FileSearch, False),
    "FileRead":            (tool_FileRead, False),
    "FileDownload":        (tool_FileDownload, False),
    "RegRead":             (tool_RegRead, False),
    "ServiceList":         (tool_ServiceList, False),
    "TaskList":            (tool_TaskList, False),
    "EventLog":            (tool_EventLog, False),
    "Ping":                (tool_Ping, False),
    "PortCheck":           (tool_PortCheck, False),
    "NetConnections":      (tool_NetConnections, False),
    "OCR":                 (tool_OCR, False),
    "ScreenRecord":        (tool_ScreenRecord, False),
    "Notification":        (tool_Notification, False),
    "Wait":                (tool_Wait, False),
    "GetTaskStatus":       (tool_GetTaskStatus, False),
    "GetRunningTasks":     (tool_GetRunningTasks, False),

    # Tier 2 — Interactive (desktop control)
    "Click":               (tool_Click, False),
    "Type":                (tool_Type, False),
    "Move":                (tool_Move, False),
    "Scroll":              (tool_Scroll, False),
    "Shortcut":            (tool_Shortcut, False),
    "FocusWindow":         (tool_FocusWindow, False),
    "MinimizeAll":         (tool_MinimizeAll, False),
    "Scrape":              (tool_Scrape, False),
    "CancelTask":          (tool_CancelTask, False),
    "ReconnectSession":    (tool_ReconnectSession, False),

    # Tier 3 — Dangerous (write/destructive)
    "Shell":               (tool_Shell, False),
    "App":                 (tool_App, False),
    "PlaySound":           (tool_PlaySound, False),
    "FileWrite":           (tool_FileWrite, False),
    "FileUpload":          (tool_FileUpload, False),
    "KillProcess":         (tool_KillProcess, False),
    "RegWrite":            (tool_RegWrite, False),
    "ServiceStart":        (tool_ServiceStart, False),
    "ServiceStop":         (tool_ServiceStop, False),
    "TaskCreate":          (tool_TaskCreate, False),
    "TaskDelete":          (tool_TaskDelete, False),
    "SetClipboard":        (tool_SetClipboard, False),
    "LockScreen":          (tool_LockScreen, False),
}


# ============================================================
# WebSocket client
# ============================================================
async def connect_and_serve(server_url: str, room_code: str):
    ws_url = f"{server_url}/ws/bridge/{room_code}"
    log.info(f"connecting: {ws_url}")

    async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
        log.info(f"connected to room {room_code}")

        # ---- 连接成功后立即发送机器身份信息 ----
        try:
            identify_info = _collect_identify_info()
            await ws.send(json.dumps({"type": "identify", "info": identify_info}))
            log.info(f"identify sent: hostname={identify_info.get('hostname')}, os={identify_info.get('os')}")
        except Exception as e:
            log.warning(f"identify failed: {e}")

        async def heartbeat():
            while True:
                try:
                    await ws.send(json.dumps({
                        "type": "heartbeat",
                        "time": datetime.now().isoformat(),
                    }))
                    await asyncio.sleep(10)
                except Exception:
                    break

        heartbeat_task = asyncio.create_task(heartbeat())

        try:
            async for raw in ws:
                msg = json.loads(raw)

                if msg.get("type") == "command":
                    cmd_id = msg["id"]
                    tool_name = msg["tool"]
                    args = msg.get("args", {})

                    log.info(f"tool_start: {tool_name} args={args}")

                    loop = asyncio.get_event_loop()
                    start = loop.time()

                    try:
                        handler_entry = TOOL_TABLE.get(tool_name)
                        if handler_entry is None:
                            output = f"[error] unknown tool: {tool_name}"
                        else:
                            handler, is_async = handler_entry
                            if is_async:
                                # async handler
                                output = await handler(**args)
                            else:
                                # sync handler — run in thread pool
                                output = await loop.run_in_executor(
                                    None, lambda: handler(**args)
                                )
                    except Exception as e:
                        output = f"[exception] {type(e).__name__}: {e}"
                        log.error(f"tool error: {tool_name}\n{traceback.format_exc()}")

                    elapsed = loop.time() - start
                    log.info(f"tool_done: {tool_name} elapsed={elapsed:.1f}s result_len={len(str(output))}")

                    await ws.send(json.dumps({
                        "type": "command_result",
                        "id": cmd_id,
                        "tool": tool_name,
                        "output": str(output),
                    }))

                elif msg.get("type") == "identify_request":
                    # Server asked for identity info
                    info = _collect_identify_info()
                    await ws.send(json.dumps({"type": "identify", "info": info}))
                    log.info("identify response sent")

                elif msg.get("type") == "status":
                    log.info(f"server_status: {msg['content']}")

                elif msg.get("type") == "error":
                    log.error(f"server_error: {msg['content']}")

        except websockets.exceptions.ConnectionClosed:
            log.warning("connection closed")
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass


# ============================================================
# Entry point
# ============================================================
def main():
    global SERVER_URL

    # Hide the startup details behind --verbose flag
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    if verbose:
        log.info("=== bridge starting ===")
        log.info(f"server: {SERVER_URL}")
        log.info(f"platform: {sys.platform}")
        log.info(f"tools available: {len(TOOL_TABLE)}")
        log.info(f"optional deps: psutil={HAS_PSUTIL}, PIL={HAS_PIL}, "
                 f"pyautogui={HAS_PYAUTOGUI}, pywin32={HAS_WIN32}, "
                 f"tabulate={HAS_TABULATE}, thefuzz={HAS_THEFUZZ}")
    else:
        log.info(f"bridge starting, server={SERVER_URL}, platform={sys.platform}")

    # If server URL passed as argument, use it silently
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        SERVER_URL = sys.argv[1]
    if len(sys.argv) > 2 and not sys.argv[2].startswith("-"):
        room = sys.argv[2].strip().upper()
        if len(room) == 6:
            log.info(f"room from arg: {room}")
        else:
            print(f"[X] Invalid room code: {room}")
            print("Press Enter to exit...")
            input()
            return
    else:
        print("=" * 52)
        print("  Cloud AI Remote Diagnostics - Windows Bridge")
        print("=" * 52)
        print()
        print(f"  Server: {SERVER_URL}")
        print()
        print("  Please open http://106.54.193.9:8000 in your browser,")
        print("  create a room, then enter the 6-digit room code below.")
        print()

        while True:
            room = input("  Room code > ").strip().upper()
            if len(room) == 6:
                break
            print("  [X] Room code must be 6 characters (letters + digits)")
            print()

    print()
    print(f"  Connecting to room {room}...")
    print()

    try:
        asyncio.run(connect_and_serve(SERVER_URL, room))
    except KeyboardInterrupt:
        log.info("bridge stopped by user")
        print("\n  Bridge stopped.")
    except ConnectionRefusedError:
        log.error(f"connection refused: {SERVER_URL}")
        print(f"\n  [X] Cannot connect to server: {SERVER_URL}")
        print("  Please check that the server is running.")
    except Exception as e:
        log.exception(f"connection failed: {type(e).__name__}: {e}")
        print(f"\n  [X] Connection error: {e}")

    print()
    print("  Press Enter to exit...")
    input()


if __name__ == "__main__":
    main()
