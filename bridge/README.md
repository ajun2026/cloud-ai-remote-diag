# bridge/ — 透明管道化桥接器（Go）

v2 管道化设计：单一职责的**命令管道 + 文件通道**，把服务器的命令送进本地 shell，把结果送回来。

## 设计原则

1. **单一职责**：不内置任何业务工具，所有能力都通过执行命令实现
2. **平台无关**：同一份代码交叉编译 Windows / Linux / macOS
3. **透明可审计**：每条执行过的命令写入 `~/.clouddiag/bridge.log`
4. **行为面最小**：无桌面操控、不请求管理员权限、不自启动、无静默后台

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
- `identify` — 上报 hostname/os/platform/arch/local_ip/username/version/bridge="go-pipe"
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
