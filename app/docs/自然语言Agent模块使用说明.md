# 自然语言财税办理

连续聊天入口：

```powershell
& 'E:\AI-Chem\ChemIntent\.venv-cpython\Scripts\python.exe' 'D:\AI黑客松\TaxAgentHarness\scripts\chat.py'
```

启动后直接输入中文，`/退出` 结束。以下单轮命令适合脚本调用和指定会话操作。

当前 Agent 使用固定的程序用户身份，当前企业由会话状态维护。它可以调用已有查询、规则、
任务和发票保存工具；金额、权限、revision 和保存结果仍由程序服务决定。

## 启动一轮

配置入口是项目根 `config.toml`（可由 `config.example.toml` 复制后填写），也可用
`TAXHARNESS_CONFIG` 指定一个配置文件；模型固定为 `deepseek-flash`，预算上限为 US$2，
累计记录写入项目 `artifacts/agent/agent.sqlite3`。每次 `agent-chat` 执行一个回合，
使用同一个 `sessionId` 就能继续同一会话。

```powershell
$python = 'E:\AI-Chem\ChemIntent\.venv-cpython\Scripts\python.exe'
$env:PYTHONPATH = 'D:\AI黑客松\TaxAgentHarness\src'
$env:TAXHARNESS_LIVE_MODEL_ENABLED = 'true'
$sessionId = 'session-' + [guid]::NewGuid().ToString('N')

& $python -m taxharness.cli agent-chat `
  --database 'D:\AI黑客松\TaxAgentHarness\artifacts\runtime\taxharness.sqlite3' `
  --user-id U001 --company-id C001 --session-id $sessionId `
  --message '检查甲企业2026年9月缺少用途的发票，并告诉我还剩哪些。' `
  --budget-usd 2
```

`agent-chat` 会保存本轮输入、程序上下文、模型输出、工具调用和结果。预算不足或凭据不可用时，
会显示相应原因。

使用独立业务副本做实调时，显式传入 `--agent-store D:\AI黑客松\TaxAgentHarness\artifacts\agent\agent.sqlite3`，让费用继续进入现有共享账本；偏好库可用 `--preference-store` 指向本项目独立的 `preferences.sqlite3`。

## 继续、切换和查看过程

```powershell
# 继续原对话；企业沿用会话当前状态
& $python -m taxharness.cli agent-chat --database 'D:\AI黑客松\TaxAgentHarness\artifacts\runtime\taxharness.sqlite3' `
  --user-id U001 --session-id $sessionId --message '继续刚才的任务。' --budget-usd 2

# 查看该 session 的输入、输出和工具结果
& $python -m taxharness.cli agent-turns --user-id U001 --session-id $sessionId `
  --agent-store 'D:\AI黑客松\TaxAgentHarness\artifacts\agent\agent.sqlite3'

# 新要求优先，取消尚未执行的后续工具
& $python -m taxharness.cli agent-cancel --user-id U001 --session-id $sessionId `
  --agent-store 'D:\AI黑客松\TaxAgentHarness\artifacts\agent\agent.sqlite3'
```

临时查询乙企业时，在请求中明确企业；这不会改变当前办理企业。只有用户明确说“切到乙企业”
才调用切换工具。切换和同批其他工具会被分开，后续工具使用新企业状态。

## 保存和恢复原则

保存工具缺省字段不改；空字符串表示明确清空用途或备注。程序先读取当前 invoice revision，
将用户、企业、发票、原始 changes 和 revision 绑定到稳定操作记录。已提交操作只补回执，
不会重新生成另一个业务写入；当前值相同但没有原操作记录时只返回无需修改。用户取消后，
尚未执行的旧写入不会自动抢跑；已提交的业务保存不会被取消撤销。

## 长期表达偏好

只有用户明确提出“以后”“记住”“长期按这个方式”等稳定要求时，Agent 才会调用偏好工具保存；“这次请简短一点”只影响当前回答，不会写入长期偏好。偏好使用受控名称和值，不接受任意 prompt 或权限指令。

支持的表达项是：

| 名称 | 可选值 | 例子 |
|---|---|---|
| `response_language` | 中文、英文 | “以后用中文回答” |
| `response_detail` | 简短、标准、详细 | “以后回答简短些” |
| `answer_order` | 结果优先、依据优先 | “先说结论，再给必要依据” |
| `amount_order` | 发票总额/银行收入/银行支出的六种排列 | “以后先说发票总额，再说银行收入和支出” |
| `response_format` | 自动、文字、表格 | “以后用表格呈现” |

默认保存为用户通用偏好；明确说“在甲企业”时保存为该用户在 C001 的企业偏好。企业偏好覆盖同名的用户通用偏好，切换到另一家企业不会串用。偏好只影响表达方式，不改变权限、金额、规则、发票或任务。

也可使用无模型的结构化 CLI：

```powershell
# 用户通用偏好
$created = (& $python -m taxharness.cli preference-create --user-id U001 `
  --key response_detail --value 简短 | Out-String) | ConvertFrom-Json
$prefId = $created.preference.preference_id

# 只在甲企业生效的金额顺序；这也是“先发票总额、再银行收入和支出”的受控表达
& $python -m taxharness.cli preference-create --user-id U001 --scope company --company-id C001 `
  --key amount_order --value '发票总额→银行收入→银行支出'

# 查看全部可见偏好，或查看某企业的当前生效组合
& $python -m taxharness.cli preference-list --user-id U001
& $python -m taxharness.cli preference-list --user-id U001 --company-id C001

# preference-create 返回的 preference_id 用于更正、删除
& $python -m taxharness.cli preference-update --user-id U001 --preference-id $prefId --value 标准
& $python -m taxharness.cli preference-delete --user-id U001 --preference-id $prefId
```

偏好独立保存在 `artifacts/agent/preferences.sqlite3`。每次模型调用前会重新加载当前用户和当前企业的有效偏好；更正或删除后，旧会话不会继续把旧值当成当前偏好。普通回复默认隐藏偏好内部编号和版本号。
