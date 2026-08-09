# bridge/ — 透明管道化桥接器（Go）

v2 管道化设计：单一职责的**命令管道 + 文件通道**，把服务器的命令送进本地 shell，把结果送回来。

## 设计原则

1. **单一职责**：不内置任何业务工具，所有能力都通过执行命令实现
2. **平台无关**：同一份代码交叉编译 Windows / Linux / macOS
3. **透明可审计**：每条执行过的命令写入 `~/.clouddiag/bridge.log`
4. **行为面最小**：无桌面操控、不自启动、无静默后台；**默认不请求管理员权限**，仅在用户确认/显式请求时才提权（v0.6.2+）

## 管理员提权（v0.6.2+）

读取完整 BIOS 设置（`Lenovo_BiosSetting`）等操作需要管理员权限。bridge 支持两种提权方式：

```bash
# 方式 1：命令行显式请求（UAC 弹窗确认）
bridge -server wss://your-server:8000 -room 房间码 --elevate

# 方式 2：双击运行时按提示确认（交互模式默认询问 [Y/n]）
```

提权原理：`ShellExecuteW + "runas"` 触发 UAC → 用户确认 → 以管理员启动新进程（带 `--elevated` 防递归）。提权成功后本进程退出，新进程自动重连同一房间。

- 提权后 bridge 上报 `is_admin=true`，服务器端 BIOS 工具直接全量读取
- 非管理员运行时上报 `is_admin=false`，服务器端工具预判后给出提权指引（不空跑命令）
- Linux/macOS 不支持自动提权，请手动 `sudo` 运行

## 构建

```bash
# Linux
GOOS=linux GOARCH=amd64 go build -ldflags "-s -w" -o bridge-linux64 .

# Windows（客户机）
GOOS=windows GOARCH=amd64 go build -ldflags "-s -w" -o bridge-win64.exe .

# macOS
GOOS=darwin GOARCH=amd64 go build -ldflags "-s -w" -o bridge-macos .
```

产物对比：旧 pyinstaller 版 22MB → Go 版 **~4.8MB**（-78%）。

## 运行

```bash
bridge -server wss://your-server:8000 -room 房间码
# 本地调试
bridge -server ws://127.0.0.1:8000 -room ABC123
```

## 协议（v2）

**bridge → server**
- `identify` — 上报 hostname/os/platform/arch/local_ip/username/version/bridge="go-pipe"/**is_admin**（是否管理员权限，v0.6.2+）
- `command_result` — `{id, output, exit_code, error, duration_ms}`
- `heartbeat` — 每 25s
- `file_download_result` — 分块上传本机文件（256KB/块，独立 base64）
- `file_download_error` / `file_upload_result` / `pong`

**server → bridge**
- `identify_request`
- `command` — `{id, command, timeout, cwd, shell}`（shell: powershell/cmd/bash/sh/auto）
- `file_download` — `{id, path}` 拉取文件
- `file_upload` — `{id, path, name, data, chunk, total}` 推送文件
- `ping` / `close`

## 安全

- 不内置桌面操控工具（点击/截图/剪贴板等）→ 杀软行为面收敛
- 所有命令经服务器端 `classify_command` 分级：Tier1 自动执行 / Tier3 审批 / 危险命令硬拦截
- 本地审计日志透明可查
- 生产环境使用 `wss://`（TLS）
