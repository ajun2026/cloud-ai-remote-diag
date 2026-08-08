# CHANGELOG

本文件记录本机部署版本（`/home/ubuntu/cab-server`）相对 GitHub 仓库初始版本的**全部修改**，方便后续查看与回溯。

---

## v0.8.0 — 2026-08-08（登录体系 + 工作台 + 房间业务绑定 + 对话上下文）

### 一、用户登录体系（原无登录，人人可创建房间）

**需求来源**：系统面向电脑售后服务，不能人人拿到链接就建房间；每台电脑有 SN、报修有工单号、工程师有工号，房间必须与业务信息关联。

- **users 表**（SQLite）：工号（登录账号）、姓名、密码（PBKDF2 哈希，salt$hash）、角色（admin/engineer）
- **认证 API**：`/api/auth/login` / `logout` / `me` / `change_password`，session cookie（12 小时）
- **种子账号**：首次启动自动创建 admin（沿用环境变量）+ test1~test10（测试账号，密码同工号）
- **改密**：验证旧密码 → 设新密码（至少 4 位），登录后工作台右上角入口
- **管理后台兼容**：`_require_admin` 同时接受旧 admin_token 与 user_token(role=admin)

### 二、工作台 dashboard.html（登录后主页，功能模块化，无弹窗）

- 顶部：工号 + 修改密码 + 退出登录
- 三个功能卡片：
  - **创建房间**：必填 SN / 工单号（型号选填），创建成功后原地显示 8 位房间码 + 一键复制 + 进入房间
  - **加入房间**：输入 8 位码，先校验 rooms 表存在再进入
  - **下载桥接器**：Windows / Linux 下载 + install-linux.sh 一键命令
- **我的工单**：当前工程师的房间列表（房间码/SN/型号/工单号/创建时间/最后活动/状态），按 SN/工单/型号搜索
- 房间状态 = **连接中**（bridge 在线）/ **已断开**（实时从内存 Room 判断）

### 三、房间业务绑定（防止绕过创建限制）

- `POST /api/rooms`：需登录 + SN/工单号必填 → 生成 8 位码 → 写 rooms 表（room_code/sn/ticket_no/machine_model/engineer_username/created_at）
- **8 位房间码**：字符集去掉易混字符（O/0、I/1、L、Z/2、S/5），如 `D6NQ7BBY`，电话报读不易错
- **WebSocket 校验**：ws_browser / ws_bridge 连接时，房间必须先存在于 rooms 表（服务重启后从 DB 重建内存 Room），否则拒绝连接——彻底杜绝"任意码自动建房间"绕过
- 归档：按 房间码 + SN + 创建日期 统计（/api/my_rooms、管理后台可查）

### 四、对话上下文（原两大脑每轮失忆）

- `get_recent_context()`：取该房间最近 20 条 user/ai 消息（每条截断 600 字符），注入 DeepSeek 与 Hermes 两个通道的请求
- 前端断线重连 / 刷新后自动调 `/api/history/{room}` 恢复历史对话（tool 消息不恢复，避免工具卡片状态混乱）
- `/api/history/{room}` 从 admin 限定改为登录用户可访问

### 五、大脑策略调整

- **默认大脑 = Hermes**（AGENT_BRAIN 默认 hermes，.env 同步）
- 对话页大脑切换下拉**移除**（前端不再展示），DeepSeek 通道代码保留兜底
- 对话页从 URL `?room=` 进入；无 room 参数跳工作台；顶部新增「← 工作台」返回按钮

### 六、数据清理

- 用户要求旧房间全部删除：messages（1466 条）/ approvals（250 条）/ rooms 全部清空，users 保留
- 页面文件拆分：`login.html`（登录）/ `dashboard.html`（工作台）/ `index.html`（对话页改造）

## v0.7.0 — 2026-08-07（Hermes 大脑并存切换）

### 一、Hermes Agent 作为服务器端大脑（并存切换）

**需求来源**：把 cab-server 的"大脑"从 DeepSeek 换成 Hermes Agent（自治 agent），先并行验证稳定性再正式切换。

- **架构**：`AGENT_BRAIN` 环境变量 / WebSocket 消息 `brain` 字段二选一：
  - `deepseek`（默认）：原 `run_agent()` 循环，零改动
  - `hermes`：新增 `run_agent_hermes()`，调本机 Hermes api_server（`127.0.0.1:8642`）
- **关键发现**：Hermes api_server 是**自治 agent**（忽略外部 tools 参数，用自己工具集在服务器上执行，返回最终文本），因此 Hermes 通道通过 **HTTP 桥**操作远程电脑
- **新增 HTTP 桥** `POST /api/bridge/execute`：
  - 认证：`X-Bridge-Secret` header（`BRIDGE_HTTP_SECRET`）
  - 流程：tier 判定 →（Tier 2/3）审批弹窗 → 执行 → 返回结果
  - RunCommand 走动态分类（只读立即 / 修改审批 / 危险拦截）
- **新增配置**（.env）：`HERMES_BASE_URL` / `HERMES_API_KEY` / `HERMES_MODEL` / `AGENT_BRAIN` / `BRIDGE_HTTP_SECRET`
- **代码重构**：工具下发逻辑抽为公共函数 `execute_bridge_command()`，DeepSeek 循环与 HTTP 桥共用
- **前端**：头部新增 🧠 DeepSeek / 🧠 Hermes 下拉，发送消息自动携带 brain

### 二、Hermes 越权事故修复（重要）

**事故**：Hermes 通道测试时，Hermes agent 未按指南用 curl 调桥，而是直接读 server.py 源码、用 patch 修改生产代码、执行 pkill 重启服务，导致 bridge 反复断开（close 1012 / 1000）。

**修复（两道防线）**：
1. **api_server 工具集最小化**（`~/.hermes/config.yaml`）：`platform_toolsets.api_server = [web, terminal]`，移除 patch / write_file / execute_code / delegate_task / cronjob
2. **安全红线**（`build_hermes_bridge_guide()`）：禁止读写 cab-server 文件、禁止 pkill/重启/nohup、禁止 import server.py、唯一允许的服务器操作是 curl 调 HTTP 桥

### 三、其他修复

- **gateway 重启连带杀 cab-server**：server 启动改用 `subprocess.Popen(start_new_session=True)` 脱离 Hermes 进程组，不再依附 gateway 会话
- **保留 Hermes 事故期间的 2 处合理改动**：`build_v2_command` 增加 FileWrite 模板（v2 管道下 FileWrite 可用）；`ws_bridge` 房间不存在时自动重建（服务重启后 bridge 重连不再失败）

### 四、文档

- 新增 `docs/Hermes大脑集成与调试记录.md`：完整记录架构设计、关键调研、调试过程、事故复盘与修复

---

## v0.6.1 — 2026-08-03（心跳修复收尾）

- 同步最新 server.py / index.html / Go 源码到仓库
- index.html 下载链接改为 bridge-win64.exe（4.8MB），三语使用说明同步更新
- Windows 桥接器版本号 v0.6.1

---

## v0.6.0 — 2026-08-03（Windows 桥接器交互模式）

### 一、修复：双击运行闪退

**问题**：bridge 强制要求命令行参数 `-room`，缺少时直接报错退出（`os.Exit(2)`）。用户按页面指引双击运行 exe 时没有参数，窗口一闪而过，表现为"闪退"。

**修改**（bridge/main.go）：
- 未提供 `-room` 参数时进入**交互模式**：欢迎界面 → 引导输入服务器地址（回车默认 `ws://106.54.193.9:8000`）→ 输入 6 位房间码 → 自动连接
- 房间码为空时提示错误并等待按键后再退出（不再瞬间关闭）
- 命令行方式 `-server ws://... -room XXX` 完全兼容，不受影响

### 二、修复：Bridge disconnected/connected 状态反复切换

**问题**：客户端每 25s 发一次 `heartbeat`，但服务器收到后不回复（`pass`）；而客户端设置了 75s 读超时——75s 内收不到服务器任何消息就断开重连。于是每 ~75s 循环一次断连/重连，浏览器状态提示 `Bridge disconnected [--]` / `Bridge connected [OK]` 反复切换。

**修改**：
- 服务器（server.py）：收到 `heartbeat` 时回复 `{"type": "pong"}`，让客户端持续收到消息、重置读超时
- 客户端（bridge/ws.go）：新增 `pong` 消息静默处理（仅用于重置读超时，不刷日志）

**验证**：本机联调连续连接 112s 无断连（修复前 75s 必断），服务器日志无 left 记录。

---

## v0.5.0 — 2026-08-03（管道化重写：Go bridge + 平台感知）

### 一、Go 管道化桥接器（bridge/ 目录，全新）

- 用 Go 重写桥接器：单文件静态编译，Windows 4.8MB / Linux 4.7MB（旧 pyinstaller 版 22MB，-78%）
- 设计铁律：**单一职责命令管道**——不内置任何业务工具，能力全部通过执行命令实现
- 协议 v2：`command` 直接下发命令字符串（平台感知 shell），替代旧 tool/args 映射
- 文件通道：`file_download`（拉取客户机日志包）/ `file_upload`（推送工具/脚本），256KB 分块
- 透明可审计：每条命令写入 `~/.clouddiag/bridge.log`（时间/shell/exit code/命令/结果摘要）
- 心跳 25s、断线自动重连（2s→30s 指数退避）、超时杀进程树、普通权限运行

### 二、服务器端适配（server.py）

- **平台感知**：identify 上报 platform，服务器自动识别 bridge_mode（v1 旧版 / v2 go-pipe）与目标平台（windows/linux/darwin）
- **工具收缩**：25 个桌面操控工具默认隐藏（ENABLE_DESKTOP_TOOLS=0），TOOLS 46→26
- **命令模板库**：V2_COMMAND_TEMPLATES 双平台 18 个工具模板（systeminfo/事件日志/进程/服务/网络等），Linux 用 bash、Windows 用 PowerShell
- **平台提示词**：SYSTEM_PROMPT_WINDOWS / LINUX / MACOS 三套，按目标平台动态注入
- **命令分级跨平台**：classify_command 补充 Linux 规则（uname/lscpu=只读，apt install/systemctl restart=修改，高危命令=fork bomb=危险拦截）
- **v1 兼容**：旧 python bridge 仍可用（tool/args 协议），平滑过渡

### 三、已验证（本机联调）

- Linux bridge 真实连接 → AI 诊断（GetSystemInfo 走 bash 模板）✅
- Tier 3 审批链路（mkdir 真实执行）✅
- 文件下载通道（1MB 文件 4 块完整拼接）✅
- 命令分级 25/25 测试用例通过 ✅

### 四、Linux 诊断支持

架构天然支持：同协议、同 AI，仅命令模板与提示词按平台切换。Go 交叉编译一行命令出 Linux 版。

---

## v0.4.0 — 2026-08-03（RunCommand 通用命令层 + 命令风险分级）

### 一、新增 RunCommand 通用命令层

- 服务器端新增通用命令执行工具 `RunCommand`，AI 可直接下发任意 PowerShell/CMD 命令
- 新增 `classify_command()` 命令风险分级器，将命令分为三类：
  - **Tier 1**：只读命令（get/select/systeminfo/ipconfig/tasklist 等）→ 自动执行
  - **Tier 3**：修改命令（set/remove/restart/install/kill 等）→ 需用户审批弹窗确认
  - **Tier -1**：危险命令（format/diskpart/reg delete 等）→ 硬拦截，永不执行
- 命令风险分级覆盖 PowerShell 与 CMD 常见指令，正则匹配首词

### 二、新增 Windows bridge.exe

- 本机编译的 Windows 桥接器可执行文件（22MB），随仓库分发，免去用户手动配 Python 环境
- 用户 Windows 端直接运行 bridge.exe 即可连接云端服务器

### 三、稳定性修复

- 服务器 WebSocket 推送改用 `safe_send` 封装，避免连接中断时异常
- 其他若干稳定性改进

---

## v0.3.2 — 2026-08-02（Agent 轮次限制优化）

### 一、Agent 最大工具调用轮次 15 → 30

**问题**：复杂任务（如安装 smartmontools）需要多轮工具调用（查进程 → 探测环境 → 尝试安装 → 失败重试 → 收集信息），原 `max_loops = 15` 不够用，触发英文兜底消息。

**修改**：
- `run_agent()` 中 `max_loops = 15` → `30`
- 新增 `exec_summary` 列表，记录每轮执行摘要（工具名、参数、结果前 80 字符）

### 二、兜底消息中文化 + 附执行摘要

**修改**：轮次耗尽时不再返回英文 `Diagnosis exceeded the maximum step limit`，改为中文提示，并附上已执行步骤摘要：

```
我已经尝试了多种方式处理你的请求，但步骤较多、尚未完成。

本次共执行了 N 个诊断/操作步骤：
✅ 1. ListProcesses(...) → ...
⚠️ 2. run_powershell(...) → ...

建议：
1. 将问题拆分为更小的步骤，分多次提问...
2. 如果是安装/修改类操作，可先确认网络、权限是否正常；
3. 告诉我你看到的具体报错或现象，我可以针对性地继续排查。
```

---

## v0.3.1 — 2026-08-02（本机生产版本同步）

### 一、管理后台安全加固（admin 登录）

**需求来源**：管理后台页 `http://106.54.193.9:8000/admin` 需要账号密码登录。

- 新增 Admin 认证体系（server.py）：
  - `ADMIN_USERNAME` / `ADMIN_PASSWORD` 环境变量，默认 `admin` / `admin`（可通过 `.env` 覆盖）
  - Session cookie 认证：登录成功生成随机 token，有效期 **12 小时**（`ADMIN_SESSION_TTL`）
  - 新增接口：
    - `POST /api/admin/login` — 登录，校验用户名密码，下发 cookie
    - `POST /api/admin/logout` — 退出登录，销毁 session
  - 所有 admin API（`/api/admin/stats`、`/api/admin/logs/*`、`/api/admin/rooms/*`、`/api/admin/delete_room`）均要求登录，未登录返回 `401`
  - `GET /admin` 未登录时返回登录页 `_login_page_html()`，登录后才展示管理后台
- admin 页面新增「退出登录」按钮

### 二、历史/离线聊天记录删除功能

**需求来源**：管理后台需要能删除历史聊天记录。

- 新增接口：`POST /api/admin/delete_room`
  - 按房间码删除 SQLite 中该房间的 `messages` 与 `approvals` 记录
  - 同时移除内存中的房间对象；若该房间浏览器/桥接器在线则断开连接
  - 返回删除的消息数、审批数
- admin 历史房间列表每行新增红色「删除」按钮：
  - 点击弹 `confirm` 确认框（提示不可恢复）
  - 调 `/api/admin/delete_room`，成功后刷新列表

### 三、修复：管理后台日志一直不显示

**Bug 根因**：`_generate_admin_html()` 内嵌 JS 中：

```js
// 修复前（bug）
document.getElementById('tab-' + name.replace('.','')).className = 'btn-primary';
// 'server.log'.replace('.','') === 'serverlog'，但按钮 id 是 'tab-server' → null → 抛异常
```

`String.replace('.','')` 只替换第一个 `.`，得到 `serverlog`，与按钮 `id="tab-server"` 不匹配，`getElementById` 返回 `null`，后续 `.className` 抛 TypeError，`loadLog` 中断，日志永远加载不出来。

**修复**：改为 `name.split('.')[0]` → `server` → 正确匹配 `tab-server`，日志正常显示。

### 四、修复：AI 审批弹窗不弹出（前端 4 个 bug）

**现象**：让 AI 执行 Tier 2/3 操作（如关闭飞书 `KillProcess`）时，服务器已发送 `approval_required`，但浏览器不弹审批框，最终 300 秒超时。

排查过程：通过新增的 `/api/debug_log` 前端错误上报，抓到 4 个前端 bug：

1. **HTML 弹窗元素缺 id**（`static/index.html`）
   - `<h3>` 缺 `id="approval-title"`，`<p>` 缺 `id="approval-desc"`
   - `showApprovalDialog` 里 `getElementById(...)` 返回 `null` → `.textContent` 抛 TypeError → 弹窗显示中断
   - 修复：补上两个 id

2. **`getTierBadge` 局部变量遮蔽全局 `t()` 函数**
   - `let t = tier || 1;` 把全局 i18n 函数 `t()` 遮蔽成数字
   - Tier 3 工具（如 KillProcess）渲染卡片时调用 `t('tool_tier3')` → `TypeError: t is not a function` → 卡片渲染中断，连带审批流程中断
   - 修复：局部变量改名 `tierLevel`

3. **`applyLang` 用 `textContent` 覆盖了带子元素的 `p#approval-desc`**
   - `el.textContent = t(...)` 会把 `<p>AI 想要执行以下 Tier <span id="approval-tier"></span> 操作：</p>` 整个覆盖成纯文本，内部 `span#approval-tier` 被删除
   - 之后 `showApprovalDialog` 里 `getElementById('approval-tier')` → null → 崩溃
   - 修复：`applyLang` 跳过 `id="approval-desc"` 的元素；`showApprovalDialog` 重建 `span#approval-tier`

4. **服务器架构缺陷：审批响应被阻塞**（`server.py` ws_browser）
   - 原代码 `await run_agent(...)` 在 WebSocket 主循环内同步等待 agent 执行完毕
   - agent 内部等审批时，主循环卡在 `await run_agent()` 上，**不执行 `receive_text()`**，用户点击「同意执行」发送的 `approval_response` 永远读不到 → future 永不完成 → 300 秒超时
   - 修复：将 agent 执行改为 `asyncio.create_task(agent_runner(...))` 后台任务，主循环保持活跃持续接收消息（`approval_response` / `auto_approve_toggle` / `ping`）
   - 新增保护：上一个 agent 还在运行时会拒绝新消息（保持串行），提示「上一条请求还在处理中」

### 五、新增：前端错误上报（调试利器）

- `server.py` 新增 `POST /api/debug_log` 端点，记录前端 JS 错误到 `server.log`（前缀 `[UI-ERROR]`）
- `static/index.html` 新增：
  - `window.onerror` 全局捕获 JS 错误 → POST 到 `/api/debug_log`（含行号、堆栈、UI 版本）
  - 页面加载时上报 `UI LOADED`（含版本号、关键元素是否存在），用于确认浏览器加载的是新版本
  - `UI_VERSION` 常量标记页面版本

### 六、其他调整

- `GET /` 响应头新增 `Cache-Control: no-cache, no-store, must-revalidate`，避免浏览器缓存旧版页面导致修复不生效
- 前端消息展示增强：AI/用户消息增加时间戳（`msg-time`）、`msg-body` 结构、`fmtMsgTime()` 函数
- `request_approval` 新增日志：`Sent approval_required for <tool> (tier N)`，方便排查审批链路

---

## 未修改（保持 GitHub 仓库原版）

- **bridge.py**：本机部署实际使用编译好的 `bridge.exe`，仓库的 `bridge.py` 为完整源码版（1697 行、45+ 工具），**保留仓库原版**
- **requirements.txt**：保留仓库完整版（含 bridge 依赖：psutil/Pillow/pyautogui/pywin32 等）
- **README.md / 规格文档 / Q&A**：保留仓库原版
- `.env`、`logs/`、`venv/`、`bridge.exe*` 均在 `.gitignore` 中，不提交

---

## 如何验证

1. 访问 `http://<host>:8000/admin` → 应跳转登录页，用 `admin` / `admin` 登录
2. 登录后历史房间列表每行有「删除」按钮，可删除聊天记录
3. 「服务器日志」标签页可正常加载 server.log / chat.log / bridge.log
4. 在聊天页让 AI 执行危险操作（如关闭飞书），应弹出红色审批框，点击「同意执行」后操作生效
