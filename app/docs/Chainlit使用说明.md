# Chainlit 本机使用说明

本项目的 Chainlit 界面只监听本机 `127.0.0.1`，调用已有 `AgentRuntime`，不另建 Agent 编排、登录系统或公网服务。默认 UI 业务副本位于 `artifacts/chainlit/`，费用继续写入共享 `artifacts/agent/agent.sqlite3`。

## 首次准备和启动

使用现有 Python 环境，不安装新依赖：

```powershell
$python = $env:TAXHARNESS_PYTHON
if (-not $python) { $python = (Get-Command python).Source }
& powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_chainlit.ps1 `
  -PythonPath $python `
  -SourceXlsx data\02_示例数据.xlsx `
  -RulesDoc data\03_业务规则.docx.txt `
  -Port 8010
```

如果独立 UI 数据库已经存在，后续启动可以省略 `-SourceXlsx` 和 `-RulesDoc`。换机器时可将 `-PythonPath` 换为现有环境路径，或设置 `TAXHARNESS_PYTHON`；产品代码不依赖固定盘符。

启动脚本会记录 PID 到 `artifacts/chainlit/chainlit.pid`，日志写入同目录。浏览器打开：

```text
http://127.0.0.1:8010
```

停止本次脚本拥有的服务：

```powershell
& powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_chainlit.ps1
```

停止只作用于 PID 文件中的本次 Chainlit 进程，不会关闭其他 Python 进程。

## 页面操作

- 开场工作台展示云帐房财税工作台标识、当前用户和企业范围，并列出可处理的财税事项。它不会触发 Agent 请求；在下方输入框中自行输入后才会开始处理。
- 页面设置中选择用户和当前企业。企业列表按业务库 ACL 过滤，U002 不能选择 C002。这里的用户选择用于本机比赛模拟，不是企业登录或 SSO。
- 切换用户或企业会先停止旧 Agent session，再打开一个新的空白会话。旧对话保存在对应用户和业务库的历史列表中；页面 JS 用仅本标签的 `taxharness_tab_id` cookie/sessionStorage 透传选择。
- 点击“新对话”会创建新的 Agent session，模型上下文从空白开始。点击开场消息或助手回复下方的“对话档案”可打开侧栏；历史条目按最近更新时间排列，支持按当前页的问题、企业和状态筛选，并可分页浏览。选择“查看”会在侧栏回放已保存的用户消息与助手回复，不重放工具；选择“继续此对话”会回到同一个 Agent session，使用原有 checkpoint 上下文。关闭侧栏不会改变历史记录。
- 助手回复保持原有 Markdown、表格和代码格式。较长回复可在原消息位置展开或收起；“复制回复”复制完整原始 Markdown。历史侧栏中的每个回合都可独立展开或收起；较长回复展开回合后仍可单独收起，支持复制单条回复或整段对话。如果浏览器不允许直接写入剪贴板，页面会提供可手动选取的原文。
- 历史索引默认保存在当前业务库同目录的 `conversation-history.sqlite3`，索引只保存会话归属、业务库/Checkpoint 范围和实际涉及企业；消息仍读取 `artifacts/agent/agent.sqlite3`，Agent 状态仍读取配置的 checkpoint 数据库。旧会话通过 AgentStore 与当前 checkpoint 线程表匹配恢复。
- 打开或继续历史对话时，服务端按当前模拟用户、当前业务库和会话实际涉及的每家企业重新核对权限。若其中有企业当前不可访问，对话内容不会显示，也不会把旧数据交给模型。
- 长期偏好从独立偏好库重新读取，任务从业务库按当前规则重新核对。
- 直接输入中文即可查询金额、查看缺用途、继续任务、更正任务或明确保存用途/备注。
- 当前轮可以使用 Chainlit 停止按钮。系统会先发送取消请求，等待正在运行的 worker 安全返回后再关闭 runtime；不会在后台继续抢跑旧写入。
- 普通回复保持业务表达，不显示 task-id、revision、操作号或文件路径。
- 展开“本轮过程”可以查看本轮模型输入、有效偏好、任务/规则上下文、工具调用和工具结果；点击其中的“关闭本轮过程”可从页面移除这条诊断。诊断区域会遮蔽凭据字段；业务回复不会因此变成原始 JSON。

## 配置路径

启动脚本支持显式参数：

```powershell
& powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_chainlit.ps1 `
  -Database artifacts\chainlit\live-business.sqlite3 `
  -AgentStore artifacts\agent\agent.sqlite3 `
  -PreferenceStore artifacts\chainlit\preferences.sqlite3 `
  -CheckpointStore artifacts\chainlit\checkpoints.sqlite3
```

也可以通过 `TAXHARNESS_DATABASE`、`TAXHARNESS_AGENT_STORE`、`TAXHARNESS_PREFERENCE_STORE`、`TAXHARNESS_CHECKPOINT_STORE`、`TAXHARNESS_HISTORY_STORE` 和 `TAXHARNESS_UI_BUDGET_USD` 指定。密钥只从 `TAXHARNESS_API_KEY` 或外部 `credential_dir` 读取，不放进项目文件、数据库或页面诊断。
