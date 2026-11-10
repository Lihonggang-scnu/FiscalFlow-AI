# TaskLedger 财税工作台

**让票据资料核实从一次问答，变成一件可以持续办完的事。**

面对多家企业、多张发票和反复补充的业务说明，会计不需要每次重新交代前情。TaskLedger 将对话中的信息整理成逐票待办：哪些资料还缺、哪些已经补充、哪些等待确认，随办理过程同步更新。

## 办事方式

**一段话，拆成多张票的办理事项。** 经办人可以说“F003 是寄送客户验收文件，F009 的用途还要问采购”，系统分别整理并保留两张票的进度；资料未说清的部分继续等待补充。

**更正一张，其他事项接着办。** 发现发票号或用途说错时，可以直接在对话中更正，也可以在票据页面调整。工作台展示受影响的事项，保留其他已确认的进度；换一个对话也能接续。

**规则调整前，先看影响。** 例如，企业将用途必填门槛从 1000 元调整到 500 元，可先预览哪些发票会新增待补事项，再决定如何处理。

**图片、PDF 与说明一起理解。** 上传票据并补充一句用途或办理要求，助手结合票面内容和文字处理；看不清的字段留待核实。已保存的对话可再次查看附件。

**按角色协同办理。** 企业工作台支持经办人补充、会计核实和复核人员审核，逐票跟进用途与备注。财税工作台还提供办件、对话和票据核对三个可切换的页面。

## 快速开始

运行需要 Python 3.12 或 3.13。以下示例适用于 Windows PowerShell，在仓库目录中执行：

```powershell
cd app
python -m pip install -r requirements.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
$env:TAXHARNESS_DATABASE = Join-Path (Get-Location) 'artifacts/runtime/taxharness.sqlite3'
python -m taxharness.cli init-db
python -m taxharness.cli query --user-id U001 --company-id C001 --month 2026-09
```

上面的命令使用随仓库提供的示例账套，初始化并查询本地业务库。

### 打开财税工作台

对话和票据识别使用兼容接口的模型。配置自己的 DeepSeek 凭据目录，并设置本地预算账本后启动：

```powershell
$env:TAXHARNESS_CREDENTIAL_DIR = '填写自己的凭据目录'
$env:TAXHARNESS_LIVE_MODEL_ENABLED = 'true'
$env:TAXHARNESS_BUDGET_STORE = Join-Path (Get-Location) 'artifacts/agent/budget.sqlite3'
$env:TAXHARNESS_UI_BUDGET_USD = '0.30'
python -m chainlit run scripts/chainlit_app.py --headless --host 127.0.0.1 --port 8010
```

凭据目录中放置一个包含 API 密钥的 TXT 文件。`TAXHARNESS_UI_BUDGET_USD` 是预算账本限额，上例仅作配置示范，请结合所用模型价格与预算调整。浏览器访问 <http://127.0.0.1:8010/>。识别 PDF 页面还需要本机能找到 `pdfinfo`、`pdftotext` 和 `pdftoppm`。

可以从这些问题开始：

- “检查甲企业 2026 年 9 月缺用途的发票，先建立任务。”
- “F003 用于寄送客户验收文件，F009 还要问采购；先记下来，不要保存。”
- “如果乙企业用途必填门槛改为 500 元，会新增哪些待补发票？”

### 打开企业工作台

```powershell
& .\scripts\start_enterprise.ps1
```

浏览器访问 <http://127.0.0.1:8011/>，按页面引导创建管理员，添加企业和成员，再导入票据开展办理。模型对话的配置见 [企业工作台说明](app/docs/企业工作台运行说明_2026-10-27.md)。

## 仓库内容

- `app/`：工作台、任务管理、模型接入、数据导入和测试代码。
- `datasets/`：电子凭证、发票及多币种测试样本。
- `test-results/`：测试汇总和实际运行记录。
- `research/`：业务资料、公开来源及技术研究。

测试运行方法和当前结果见 [测试结果汇总](test-results/测试结果汇总.md)。
