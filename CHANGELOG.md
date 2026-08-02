# CHANGELOG

本文件记录本机部署版本（`/home/ubuntu/cab-server`）相对 GitHub 仓库初始版本的**全部修改**，方便后续查看与回溯。

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
