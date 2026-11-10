import { useEffect, useState } from "react";
import { BookOpenText, CalendarDays, ListChecks, ReceiptText, RefreshCw } from "lucide-react";

const RULE_LABELS = {
  amount_statistics: "金额统计",
  invoice_purpose: "发票用途",
  bank_requirements: "银行流水",
};

const TASK_STATUS = {
  waiting_input: "等待补充",
  open: "进行中",
  out_of_scope: "超出范围",
  completed: "已完成",
  cancelled: "已取消",
  superseded: "已替代",
};

export default function ContextPanel() {
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState("");
  const [invoiceId, setInvoiceId] = useState(props.invoice_id || "");
  const [section, setSection] = useState("invoices");
  const month = props.month || "";
  const invoices = Array.isArray(props.invoices) ? props.invoices : [];
  const rules = Array.isArray(props.rules) ? props.rules : [];
  const tasks = Array.isArray(props.tasks) ? props.tasks : [];
  const invoice = invoices.find((item) => item.invoice_id === invoiceId);

  useEffect(() => {
    setLoading(false);
    setNotice(props.error || "");
    setInvoiceId(props.invoice_id || "");
    setSection("invoices");
  }, [props.company_id, props.month, props.invoice_id, props.error]);

  const selectMonth = async (value) => {
    setNotice("");
    setLoading(true);
    if (value !== month) setInvoiceId("");
    setSection("invoices");
    try {
      await callAction({ name: "set_context_month", payload: { month: value } });
      setLoading(false);
    } catch (_error) {
      setLoading(false);
      setNotice("资料未能加载，请重试。 ");
    }
  };

  const selectInvoice = async (value) => {
    setInvoiceId(value);
    setNotice("");
    try {
      await callAction({ name: "select_context_invoice", payload: { invoice_id: value } });
    } catch (_error) {
      setInvoiceId("");
      setNotice("发票选择未保存，请重试。 ");
    }
  };

  const submitMonth = async (event) => {
    event.preventDefault();
    const value = event.currentTarget.elements.namedItem("month").value || "";
    await selectMonth(value);
  };

  const showSection = (nextSection) => {
    setSection(nextSection);
  };

  return (
    <section className="th-context-panel" aria-label="当前企业资料">
      <header className="th-context-heading">
        <div>
          <h2>当前资料</h2>
          <p>{props.company_label || props.company_id || "未选择企业"}</p>
        </div>
        <ReceiptText size={18} aria-hidden="true" />
      </header>

      <form className="th-context-month-form" onSubmit={submitMonth}>
        <label className="th-context-month-label" htmlFor="th-context-month"><CalendarDays size={15} />查看期间</label>
        <input
          key={`${props.company_id || "company"}-${month || "empty"}`}
          id="th-context-month"
          name="month"
          className="th-context-month"
          type="month"
          defaultValue={month}
          aria-describedby="th-context-month-note"
        />
        <div className="th-context-month-actions">
          <button className="th-context-load-button" type="submit">查看资料</button>
          <button className="th-context-clear-button" type="button" onClick={() => selectMonth("")} disabled={!month}>清除</button>
        </div>
      </form>
      <p className="th-context-note" id="th-context-month-note">{month ? `当前资料期间：${month}。` : "资料期间尚未选择。"}仅筛选右侧资料，不会改变对话中已确认的月份。</p>

      {loading ? <p className="th-context-state" role="status"><RefreshCw size={14} />正在读取所选期间…</p> : null}
      {notice ? <p className="th-context-error" role="alert">{notice}</p> : null}
      {!month && !loading ? <div className="th-context-empty"><strong>先选择一个月份</strong><span>选择后显示当前企业的发票、适用规则和任务。</span></div> : null}

      {month && !loading && !notice ? (
        <>
          <div className="th-context-tabs" role="group" aria-label="资料类型">
            <button type="button" aria-pressed={section === "invoices"} onClick={() => showSection("invoices")}><ReceiptText size={14} />发票<span>{invoices.length}</span></button>
            <button type="button" aria-pressed={section === "rules"} onClick={() => showSection("rules")}><BookOpenText size={14} />规则<span>{rules.length}</span></button>
            <button type="button" aria-pressed={section === "tasks"} onClick={() => showSection("tasks")}><ListChecks size={14} />任务<span>{tasks.length}</span></button>
          </div>

          {section === "invoices" ? (
            <div className="th-context-section" role="region" aria-label="发票资料">
              {invoices.length ? (
                <>
                  <label className="th-context-month-label" htmlFor="th-invoice-select">选择发票</label>
                  <select id="th-invoice-select" className="th-invoice-select" value={invoiceId} onChange={(event) => selectInvoice(event.target.value)}>
                    <option value="">选择票号、销售方和金额</option>
                    {invoices.map((item) => <option key={item.invoice_id} value={item.invoice_id}>{item.invoice_id} · {item.seller_name} · {item.amount} 元</option>)}
                  </select>
                  {invoice ? (
                    <article className="th-selected-invoice">
                      <div className="th-invoice-topline"><strong>{invoice.invoice_id}</strong><time>{invoice.issue_date}</time><span className="th-invoice-amount">{invoice.amount} 元</span></div>
                      <dl>
                        <div><dt>销售方</dt><dd>{invoice.seller_name || "—"}</dd></div>
                        <div><dt>项目</dt><dd>{invoice.project_name || "—"}</dd></div>
                        <div><dt>用途</dt><dd>{invoice.purpose || <span className="th-missing-value">未填写</span>}</dd></div>
                        <div><dt>备注</dt><dd>{invoice.remarks || "无"}</dd></div>
                      </dl>
                    </article>
                  ) : <p className="th-context-empty">选择一张发票，查看当前记录中的用途、备注和票面信息。</p>}
                </>
              ) : <p className="th-context-empty">所选月份没有发票记录。</p>}
            </div>
          ) : null}

          {section === "rules" ? (
            <div className="th-context-section" role="region" aria-label="适用规则">
              {rules.length ? rules.map((rule, index) => (
                <article className="th-rule-row" key={`${rule.rule_id || rule.kind}-${rule.version || index}`}>
                  <div className="th-rule-title">
                    <strong>{RULE_LABELS[rule.kind] || rule.kind || "规则"}</strong>
                    <span>{rule.rule_id} · {rule.version}</span>
                  </div>
                  <p>{rule.name || "未命名规则"} · 自 {rule.effective_from || "—"} 生效</p>
                  <p className="th-rule-status">{rule.status === "active" ? "有效" : rule.status || "状态未知"} · {rule.execution_summary || "当前期间适用"}</p>
                  {rule.content ? <details className="th-rule-content"><summary>查看规则原文</summary><pre>{rule.content}</pre></details> : null}
                </article>
              )) : <p className="th-context-empty">所选月份没有适用规则。</p>}
            </div>
          ) : null}

          {section === "tasks" ? (
            <div className="th-context-section" role="region" aria-label="任务">
              {tasks.length ? tasks.map((task) => (
                <article className="th-task-row" key={task.task_id}>
                  <div className="th-rule-title"><strong>{task.scope === "selected" ? "指定发票" : "用途补充"}</strong><span>{TASK_STATUS[task.status] || task.status}</span></div>
                  <p>目标：{task.target_invoice_ids.length ? task.target_invoice_ids.join("、") : "未指定"}</p>
                  <p>仍待处理：{task.pending_invoice_ids.length ? task.pending_invoice_ids.join("、") : "无"}</p>
                  {task.message ? <p>{task.message}</p> : null}
                  {task.next_step ? <p className="th-task-next">下一步：{task.next_step}</p> : null}
                </article>
              )) : <p className="th-context-empty">当前用户在此企业和月份没有任务。</p>}
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
