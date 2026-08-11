"""
云端 AI 远程运维助手 — Windows 桥接器
运行在用户 Windows 电脑上，连接云端服务器，接收并执行诊断命令
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime

import websockets

# ============================================================
# 配置
# ============================================================
# 服务器地址（启动时可通过命令行参数覆盖）
SERVER_URL = "wss://clouddiag.online"


# ============================================================
# 命令执行
# ============================================================
async def run_systeminfo() -> str:
    """执行 systeminfo 命令"""
    proc = await asyncio.create_subprocess_exec(
        "systeminfo",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)

    if proc.returncode != 0:
        return f"[错误] systeminfo 执行失败:\n{stderr.decode('gbk', errors='replace')}"

    # systeminfo 输出中文在 GBK 编码下
    return stdout.decode("gbk", errors="replace") or stdout.decode("utf-8", errors="replace")


async def run_dxdiag() -> str:
    """执行 dxdiag 诊断"""
    tmpdir = tempfile.gettempdir()
    tmpfile = os.path.join(tmpdir, f"dxdiag_output_{os.getpid()}.txt")

    # 先清理可能残留的旧文件
    if os.path.exists(tmpfile):
        os.remove(tmpfile)

    proc = await asyncio.create_subprocess_exec(
        "dxdiag", "/t", tmpfile,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=60.0)

    # 等待文件写入完成
    await asyncio.sleep(1)

    if not os.path.exists(tmpfile):
        return "[错误] dxdiag 报告文件未生成"

    try:
        with open(tmpfile, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"[错误] 读取 dxdiag 报告失败: {e}"
    finally:
        try:
            os.remove(tmpfile)
        except Exception:
            pass

    if not content.strip():
        return "[警告] dxdiag 报告为空"

    # dxdiag 报告通常很长，只提取关键部分
    return _extract_dxdiag_key_sections(content)


def _extract_dxdiag_key_sections(content: str) -> str:
    """提取 dxdiag 报告中的关键诊断章节"""
    sections = []
    key_headers = [
        "System Information",
        "Display Devices",
        "Sound Devices",
        "Disk & DVD/CD-ROM Drives",
        "System Devices",
    ]

    lines = content.split("\n")
    in_key_section = False
    current_section = []

    for line in lines:
        # 检测节标题
        is_header = any(
            line.strip().startswith("--") and h in line for h in key_headers
        ) or line.strip().startswith("---------------")

        if is_header:
            if current_section:
                sections.append("\n".join(current_section))
                current_section = []
            in_key_section = any(h in line for h in key_headers)
            if in_key_section:
                sections.append(line)
            continue

        if in_key_section:
            stripped = line.strip()
            if stripped == "":
                in_key_section = False
                if current_section:
                    sections.append("\n".join(current_section))
                    current_section = []
                continue
            current_section.append(line)

    if current_section:
        sections.append("\n".join(current_section))

    result = "\n".join(sections)
    # 限制长度
    if len(result) > 6000:
        result = result[:6000] + "\n\n... (报告已截断)"
    return result


async def read_event_log(max_events: int = 50, level: str = "Error,Warning") -> str:
    """通过 PowerShell 读取 Windows 系统事件日志"""
    ps_script = f"""
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
    }} |
    ConvertTo-Json -Depth 3
"""
    return await _run_powershell_script(ps_script)


async def run_powershell_command(command: str) -> str:
    """执行任意 PowerShell 命令（只读诊断用）"""
    ps_script = command
    return await _run_powershell_script(ps_script)


async def _run_powershell_script(script: str) -> str:
    """底层 PowerShell 执行"""
    proc = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        return "[超时] PowerShell 命令执行超过 30 秒"

    out = stdout.decode("utf-8", errors="replace") or stdout.decode("gbk", errors="replace")
    err = stderr.decode("utf-8", errors="replace") or stderr.decode("gbk", errors="replace")

    result_parts = []
    if out.strip():
        result_parts.append(out.strip())
    if err.strip():
        result_parts.append(f"[stderr]\n{err.strip()}")

    return "\n".join(result_parts) if result_parts else "[完成] 命令已执行，无输出"


# ============================================================
# WebSocket 客户端
# ============================================================
async def connect_and_serve(server_url: str, room_code: str):
    """连接服务器并循环处理命令"""
    ws_url = f"{server_url}/ws/bridge/{room_code}"

    print(f"正在连接: {ws_url}")

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        print(f"✅ 已连接到房间 {room_code}")

        # 心跳任务
        async def heartbeat():
            while True:
                try:
                    await ws.send(json.dumps({"type": "heartbeat", "time": datetime.now().isoformat()}))
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

                    print(f"\n▶ [{tool_name}] 开始执行...")

                    # 执行对应的工具
                    start = asyncio.get_event_loop().time()
                    try:
                        if tool_name == "run_systeminfo":
                            output = await run_systeminfo()
                        elif tool_name == "run_dxdiag":
                            output = await run_dxdiag()
                        elif tool_name == "read_event_log":
                            output = await read_event_log(
                                max_events=args.get("max_events", 50),
                                level=args.get("level", "Error,Warning"),
                            )
                        elif tool_name == "run_powershell":
                            output = await run_powershell_command(args.get("command", ""))
                        else:
                            output = f"[错误] 未知工具: {tool_name}"
                    except Exception as e:
                        output = f"[执行异常] {type(e).__name__}: {e}"

                    elapsed = asyncio.get_event_loop().time() - start
                    print(f"◀ [{tool_name}] 完成 ({elapsed:.1f}s, {len(output)} 字符)")

                    # 回传结果
                    await ws.send(json.dumps({
                        "type": "command_result",
                        "id": cmd_id,
                        "tool": tool_name,
                        "output": output,
                    }))

                elif msg.get("type") == "status":
                    print(f"[服务器] {msg['content']}")

                elif msg.get("type") == "error":
                    print(f"[服务器错误] {msg['content']}")

        except websockets.exceptions.ConnectionClosed:
            print("\n⚠️  与服务器的连接已断开")
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass


# ============================================================
# 启动入口
# ============================================================
def main():
    global SERVER_URL

    print("=" * 50)
    print("  云端 AI 远程运维助手 — Windows 桥接器")
    print("=" * 50)

    # 获取服务器地址
    if len(sys.argv) > 1:
        SERVER_URL = sys.argv[1]

    server_input = input(f"服务器地址 [{SERVER_URL}]: ").strip()
    if server_input:
        SERVER_URL = server_input

    # 获取房间码
    while True:
        room = input("请输入 6 位房间码: ").strip().upper()
        if len(room) == 6:
            break
        print("❌ 房间码应为 6 位（字母+数字）")

    # 运行
    try:
        asyncio.run(connect_and_serve(SERVER_URL, room))
    except KeyboardInterrupt:
        print("\n\n桥接器已停止")
    except ConnectionRefusedError:
        print(f"\n❌ 无法连接到 {SERVER_URL}，请确保服务器已启动")
    except Exception as e:
        print(f"\n❌ 连接失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
