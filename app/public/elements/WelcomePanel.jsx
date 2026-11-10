import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  BookOpenText,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  FileSearch,
  Landmark,
  ListChecks,
  MessageSquareText,
  Plus,
  RefreshCw,
  ReceiptText,
  Save,
  Search,
  ShieldCheck,
  Undo2,
  XCircle,
  X,
} from "lucide-react";

const PAGE_LABELS = { A: "办件视图", B: "对话工作区", C: "票据核对台" };
const WORK_STATUS_LABELS = {
  waiting_input: "等待输入",
  open: "办理中",
  out_of_scope: "超出范围",
  completed: "已完成",
  cancelled: "已取消",
  superseded: "已替代",
};
const WORK_SCOPE_LABELS = { all_missing: "期间用途补全", selected: "指定发票办理" };
const RULE_KIND_LABELS = { amount_statistics: "金额统计", invoice_purpose: "发票用途", bank_requirements: "银行流水" };
const FRONTIER_STATUS_LABELS = {
  needs_input: "待补信息",
  ready_to_save: "待保存",
  awaiting_confirmation: "等待确认",
  waiting: "暂缓等待",
  completed: "已完成",
};
const FRONTIER_ACTION_LABELS = {
  save: "保存草稿",
  confirm: "确认草稿",
  ask: "补充信息",
  wait: "等待回复",
  finish: "办件完成",
  inspect_rule: "检查规则",
};

function frontierDiffLines(diff = {}) {
  const lines = [
    ...(diff.added_targets || []).map((invoiceId) => `新增目标 ${invoiceId}`),
    ...(diff.removed_targets || []).map((invoiceId) => `移出目标 ${invoiceId}`),
  ];
  for (const [key, change] of Object.entries(diff.changes || {})) {
    if (change?.added || change?.removed) {
      const added = change.added?.length ? `新增 ${change.added.join("、")}` : "";
      const removed = change.removed?.length ? `移出 ${change.removed.join("、")}` : "";
      lines.push(`${FRONTIER_STATUS_LABELS[key] || "办理状态"}${added && removed ? "：" : ""}${[added, removed].filter(Boolean).join("；")}`);
    } else if (key === "drafts") {
      lines.push(`草稿已更新：${(change.updated || []).join("、")}`);
    } else if (key === "waiting_reasons") {
      lines.push("暂缓说明已更新");
    } else if (key === "notes") {
      lines.push("办件事项已更新");
    } else if (key === "rule") {
      lines.push(`适用规则：${frontierRuleLabel(change.before)} → ${frontierRuleLabel(change.after)}`);
    } else if (key === "invoice_revisions") {
      for (const [invoiceId, revision] of Object.entries(change)) {
        lines.push(`${invoiceId} 资料版本由 ${revision.before} 更新为 ${revision.after}`);
      }
    }
  }
  if (diff.affected_targets?.length && diff.unchanged_targets?.length) {
    lines.push(`${diff.unchanged_targets.join("、")} 的办理状态保持不变`);
  }
  return lines;
}

function ruleStatusLabel(rule, purposeCheckStatus) {
  if (rule?.status === "active") {
    return ["unsupported", "not_applicable"].includes(rule.execution_status) || purposeCheckStatus === "cannot_determine"
      ? "有效 · 未判定"
      : "有效";
  }
  if (["rule_conflict", "conflict"].includes(rule?.status) || purposeCheckStatus === "rule_conflict") return "规则冲突";
  if (rule?.status === "inactive") return "已停用";
  if (["rule_not_found", "no_applicable_rule", "not_found"].includes(rule?.status) || purposeCheckStatus === "rule_not_found") return "无适用规则";
  if (purposeCheckStatus === "out_of_scope") return "检查范围外";
  return "未判定";
}

function ruleVersionLabel(rule) {
  return rule?.rule_id && rule?.version ? `${rule.rule_id} · ${rule.version}` : "发票用途检查";
}

function frontierRuleLabel(rule) {
  if (!rule) return "无适用规则";
  return [rule.name || rule.rule_id, rule.version].filter(Boolean).join(" · ") || rule.summary || rule.status || "规则信息已更新";
}

function invoicePurposeLabel(invoice) {
  if (!invoice) return "未读取";
  if (invoice.purpose_status === "filled") return "用途已填写";
  if (invoice.purpose_status === "required") return "待补用途";
  if (invoice.purpose_status === "optional") return "未填写（非必填）";
  return "未填写／未判定";
}

function taskPendingIds(task, frontier) {
  const pending = task?.pending_invoice_ids || [];
  const drafts = frontier?.task_id === task?.task_id ? Object.keys(frontier?.drafts || {}) : [];
  return [...new Set([...pending, ...drafts])];
}

function taskStatusLabel(task, frontier) {
  if (task?.status === "completed" && taskPendingIds(task, frontier).length) return "用途已补齐 · 待确认更正";
  return WORK_STATUS_LABELS[task?.status] || task?.status;
}

function WorkItemsPage({ data = {}, month = null, selectedTaskId = null, selectedInvoiceId = null, recentTurns = [], notice = "", onOpenInvoice, onSelectInvoice, onContinueChat }) {
  const [busy, setBusy] = useState(false);
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const invoices = Array.isArray(data.invoices) ? data.invoices : [];
  const selectedTask = tasks.find((task) => task.task_id === selectedTaskId) || tasks[0] || null;
  const frontier = data.frontier?.task_id === selectedTask?.task_id ? data.frontier : null;
  const frontierDiff = frontierDiffLines(frontier ? data.diff : {});
  const recentChanges = Array.isArray(data.recent_changes) ? data.recent_changes : [];
  const nextActionText = (frontier?.next_actions || []).map((action) => (
    `${FRONTIER_ACTION_LABELS[action.action] || action.action}${action.invoice_ids?.length ? ` ${action.invoice_ids.join("、")}` : ""}`
  )).join("；");
  const targetIds = Array.isArray(selectedTask?.target_invoice_ids) ? selectedTask.target_invoice_ids : [];
  const pendingIds = taskPendingIds(selectedTask, frontier);
  const invoiceById = new Map(invoices.map((invoice) => [invoice.invoice_id, invoice]));
  const preferredInvoiceId = selectedInvoiceId && targetIds.includes(selectedInvoiceId)
    ? selectedInvoiceId
    : pendingIds.find((invoiceId) => invoiceById.has(invoiceId)) || targetIds.find((invoiceId) => invoiceById.has(invoiceId)) || null;
  const selectedInvoice = preferredInvoiceId ? invoiceById.get(preferredInvoiceId) : null;
  const purposeRule = (data.rules || []).find((rule) => rule.kind === "invoice_purpose") || null;
  const completionKnown = !["waiting_input", "out_of_scope"].includes(selectedTask?.status);
  const fixedTargetProgress = selectedTask?.scope === "selected" && completionKnown;
  const completedCount = fixedTargetProgress ? Math.max(0, targetIds.length - pendingIds.length) : 0;
  const progress = fixedTargetProgress && targetIds.length ? Math.max(0, Math.min(100, Math.round(completedCount * 100 / targetIds.length))) : 0;
  const canCreate = Boolean(data.can_modify) && month === "2026-09";

  const run = async (name, payload = {}) => {
    setBusy(true);
    try { await callAction({ name, payload }); } catch (_error) { /* Server notice carries the result. */ }
    finally { setBusy(false); }
  };

  return (
    <main className="th-page-view th-page-work-items" aria-label="办件视图">
      <header className="th-page-titlebar">
        <div>
          <h1>{selectedTask ? (WORK_SCOPE_LABELS[selectedTask.scope] || selectedTask.scope) : "办件视图"}</h1>
          <p className="th-page-description">查看目标发票和仍待处理项，核对详情可进入票据核对台。</p>
        </div>
        <div className="th-page-actions">
          <button type="button" className="th-button th-button-secondary" onClick={onContinueChat}><MessageSquareText size={15} />继续当前对话</button>
          <button type="button" className="th-button th-button-secondary" disabled={!month || busy} onClick={() => run("workspace_refresh")}><RefreshCw size={15} />刷新办件</button>
          {canCreate && !tasks.some((task) => ["waiting_input", "open", "out_of_scope"].includes(task.status)) ? <button type="button" className="th-button th-button-primary" disabled={busy} onClick={() => run("workspace_create_task")}><Plus size={15} />创建用途办件</button> : null}
        </div>
      </header>
      {notice ? <p className="th-workspace-notice" role="status" aria-live="polite">{notice}</p> : null}
      {!month ? <section className="th-page-empty"><CircleCheck size={21} /><h2>先选择办理期间</h2><p>选择月份后显示当前用户和企业的办件及目标发票。</p></section> : !selectedTask ? (
        <section className="th-page-empty th-page-empty-inline"><CircleCheck size={21} /><h2>该期间暂无办件</h2><p>{month === "2026-09" && data.can_modify ? "可创建用途办件，系统会根据适用规则读取实际缺项。" : "可进入票据核对台查看该期间的发票资料。"}</p>{canCreate ? <button type="button" className="th-button th-button-primary" disabled={busy} onClick={() => run("workspace_create_task")}><Plus size={15} />创建用途办件</button> : null}</section>
      ) : (
        <div className="th-work-board">
          <div className="th-work-main-column">
          <section className="th-data-panel th-work-ticket" aria-label="当前目标发票详情">
            <div className="th-work-ticket-heading"><div><span className={`th-status-pill th-status-${selectedTask.status}`}>{taskStatusLabel(selectedTask, frontier)}</span><h2>{WORK_SCOPE_LABELS[selectedTask.scope] || selectedTask.scope}</h2></div>{selectedTask.status === "completed" && !pendingIds.length ? <CircleCheck size={20} /> : <Check size={20} />}</div>
            <p className="th-work-message">{selectedTask.status === "completed" && pendingIds.length ? "发票用途已补齐，另有未保存的更正草稿。" : selectedTask.message || "暂无办理说明。"}</p>
            {selectedInvoice ? <>
              <div className="th-work-invoice-title"><div><span>当前目标发票</span><strong>{selectedInvoice.invoice_id}</strong></div><span className={`th-purpose-state th-purpose-${selectedInvoice.purpose_status || "undetermined"}`}>{invoicePurposeLabel(selectedInvoice)}</span></div>
              <dl className="th-work-invoice-facts">
                <div><dt>开票日期</dt><dd>{selectedInvoice.issue_date}</dd></div><div><dt>销售方</dt><dd>{selectedInvoice.seller_name || "—"}</dd></div><div><dt>金额</dt><dd>{selectedInvoice.amount} 元</dd></div><div><dt>项目</dt><dd>{selectedInvoice.project_name || "—"}</dd></div>
                <div className="th-work-fact-wide"><dt>用途</dt><dd>{String(selectedInvoice.purpose || "").trim() || "未填写"}</dd></div><div className="th-work-fact-wide"><dt>备注</dt><dd>{String(selectedInvoice.remarks || "").trim() || "—"}</dd></div>
              </dl>
              <div className="th-work-ticket-actions"><button type="button" className="th-button th-button-primary" onClick={() => onOpenInvoice?.(selectedTask.task_id, selectedInvoice.invoice_id)}><ReceiptText size={15} />进入票据核对台<ArrowRight size={15} /></button></div>
            </> : <section className="th-page-empty th-work-ticket-empty"><h3>{targetIds.length ? "目标发票未在本期资料中读取" : "当前办件没有目标发票"}</h3><p>{targetIds.length ? "请刷新办件和票据资料后再核对。" : "目标范围以办件记录为准。"}</p></section>}
            <p className="th-work-next">下一步：{nextActionText || selectedTask.next_step || "—"}</p>
          </section>
          <section className="th-data-panel th-work-conversation" aria-label="办件沟通">
            <div className="th-panel-heading"><div><h2>办件沟通</h2><span>当前会话最近记录</span></div><button type="button" className="th-inline-link" onClick={onContinueChat}>继续对话 <ArrowRight size={14} /></button></div>
            {recentTurns.length ? <div className="th-work-dialogue-list">{recentTurns.map((turn, index) => <article className="th-work-turn" key={`${index}-${turn.request_text.slice(0, 18)}`}>
              {turn.request_text ? <div className="th-work-turn-user"><strong>您</strong><p>{turn.request_text}</p></div> : null}
              {turn.output_text ? <div className="th-work-turn-assistant"><strong>财税助手</strong><p>{turn.output_text}</p></div> : null}
            </article>)}</div> : <p className="th-panel-empty">当前会话还没有已保存的交流内容。</p>}
          </section>
          </div>
          <aside className="th-data-panel th-work-progress" aria-label="办件进度">
            <div className="th-panel-heading"><div><h2>当前办件进度</h2><span>{month}</span></div><span className="th-work-progress-count">{fixedTargetProgress ? `${completedCount} / ${targetIds.length}` : completionKnown ? `待处理 ${pendingIds.length} 张` : "未判定"}</span></div>
            {fixedTargetProgress ? <div className="th-progress-track" role="progressbar" aria-label="办件目标进度" aria-valuemin={0} aria-valuemax={targetIds.length} aria-valuenow={completedCount}><span style={{ width: `${progress}%` }} /></div> : null}
            <div className="th-progress-summary">{fixedTargetProgress ? <><span>目标 {targetIds.length} 张</span><span>待处理 {pendingIds.length} 张</span></> : selectedTask.scope === "all_missing" && completionKnown ? <span>待处理项按当前规则结果展示</span> : <span>当前任务无法判定完成进度</span>}</div>
            <ul className="th-progress-list">{targetIds.map((invoiceId) => {
              const invoice = invoiceById.get(invoiceId);
              const pending = pendingIds.includes(invoiceId);
              const uncertainTask = ["waiting_input", "out_of_scope"].includes(selectedTask.status);
              const label = frontier?.waiting?.includes(invoiceId) ? "暂缓等待" : frontier?.drafts?.[invoiceId] ? "待确认草稿" : pending ? "待处理" : selectedTask.status === "completed" ? "办件已完成" : uncertainTask ? "未判定" : invoicePurposeLabel(invoice);
              return <li key={invoiceId} className={preferredInvoiceId === invoiceId ? "is-selected" : ""}><button type="button" onClick={() => onSelectInvoice?.(selectedTask.task_id, invoiceId)}><span className="th-progress-invoice-main"><strong>{invoiceId}</strong><small>{invoice ? `${invoice.project_name || "—"} · ${invoice.amount} 元` : "未读取该期间发票"}</small></span><span className={`th-purpose-state th-purpose-${pending ? "required" : invoice?.purpose_status || "undetermined"}`}>{label}</span></button></li>;
            })}{!targetIds.length ? <li className="th-progress-empty">该办件暂无目标发票。</li> : null}</ul>
            {frontier ? <section className="th-work-frontier" aria-label="办理状态与最近变化">
              <div className="th-frontier-heading"><h3>办理状态</h3><span>{nextActionText ? `下一步：${nextActionText}` : "当前状态已更新"}</span></div>
              <ul className="th-frontier-list">
                {Object.entries(FRONTIER_STATUS_LABELS).flatMap(([key, label]) => (frontier[key] || []).map((invoiceId) => (
                  <li key={`${key}-${invoiceId}`}><strong>{label}</strong><span>{invoiceId}</span>{key === "waiting" && frontier.waiting_reasons?.[invoiceId] ? <small>{frontier.waiting_reasons[invoiceId]}</small> : null}</li>
                )))}
                {Object.entries(frontier.drafts || {}).flatMap(([invoiceId, fields]) => Object.entries(fields).map(([field, draftItem]) => (
                  <li className="th-frontier-draft" key={`draft-${invoiceId}-${field}`}><strong>{frontier.waiting?.includes(invoiceId) ? "暂缓草稿" : draftItem.save_requested ? "待保存草稿" : "待确认草稿"}</strong><span>{invoiceId} · {field === "purpose" ? "用途" : "备注"}</span><small>{draftItem.value}</small></li>
                )))}
              </ul>
              <div className="th-frontier-changes"><strong>最近变化</strong>
                {frontierDiff.length || recentChanges.length ? <ul>{frontierDiff.map((line) => <li key={line}>{line}</li>)}{recentChanges.map((change, index) => <li key={`${change.kind}-${change.occurred_at}-${index}`}>{change.summary}</li>)}</ul> : <p>{data.diff?.initial ? "已读取当前办理状态。" : "没有新的办理变化。"}</p>}
              </div>
            </section> : null}
            <section className="th-work-rule-summary" aria-label="发票用途规则">
              <div className="th-work-rule-heading"><strong>发票用途规则</strong><span>{ruleVersionLabel(purposeRule)}</span></div>
              <span className={`th-rule-validity ${purposeRule?.status === "active" && purposeRule.execution_status === "supported" ? "is-valid" : ""}`}>{ruleStatusLabel(purposeRule, data.purpose_check_status)}</span>
              <p>{purposeRule?.execution_summary || data.purpose_check_summary || "当前期间没有可判定的发票用途规则。"}</p>
            </section>
            {data.can_modify && ["waiting_input", "open", "out_of_scope"].includes(selectedTask.status) ? <button type="button" className="th-button th-button-danger" disabled={busy} onClick={() => { if (window.confirm("确认取消这个办件？已保存的发票用途不会撤销。")) run("workspace_cancel_task", { task_id: selectedTask.task_id }); }}><XCircle size={15} />取消办件</button> : null}
          </aside>
        </div>
      )}
    </main>
  );
}

function makeOperationSuffix() {
  return globalThis.crypto?.randomUUID?.().replaceAll("-", "") || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function InvoiceCheckPage({ data = {}, month = null, selectedInvoiceId = null, draft = {}, notice = "", userId = "", companyId = "", onDraftChange, onPersistDraft, onSelectInvoice }) {
  const [busy, setBusy] = useState(false);
  const [undoingField, setUndoingField] = useState("");
  const [localError, setLocalError] = useState("");
  const currentSaveKey = useRef("");
  const invoices = Array.isArray(data.invoices) ? data.invoices : [];
  const rules = Array.isArray(data.rules) ? data.rules : [];
  const purposeRules = rules.filter((rule) => rule.kind === "invoice_purpose");
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const invoice = invoices.find((item) => item.invoice_id === selectedInvoiceId) || null;
  const activeDraft = draft?.invoice_id === selectedInvoiceId ? draft : null;
  const purposeValue = activeDraft?.purpose ?? invoice?.purpose ?? "";
  const remarksValue = activeDraft?.remarks ?? invoice?.remarks ?? "";
  const canModify = Boolean(data.can_modify);
  const undoFields = Array.isArray(invoice?.undo_fields) ? invoice.undo_fields : [];
  const dirty = Boolean(invoice && (purposeValue !== (invoice.purpose || "") || remarksValue !== (invoice.remarks || "")));
  const relatedTasks = invoice ? tasks.filter((task) => (task.target_invoice_ids || []).includes(invoice.invoice_id)) : [];

  const changeDraft = (field, value) => {
    if (!invoice) return;
    setLocalError("");
    currentSaveKey.current = "";
    onDraftChange?.({
      invoice_id: invoice.invoice_id,
      expected_revision: activeDraft?.expected_revision ?? invoice.revision,
      purpose: field === "purpose" ? value : purposeValue,
      remarks: field === "remarks" ? value : remarksValue,
      draft_event_ids: activeDraft?.draft_event_ids || {},
      modified_fields: [...new Set([...(activeDraft?.modified_fields || []), field])],
      idempotency_key: "",
    });
  };

  const save = async () => {
    if (!invoice || !dirty || busy || !canModify) return;
    setBusy(true);
    if (!(await onPersistDraft?.())) { setBusy(false); return; }
    const revision = activeDraft?.expected_revision ?? invoice.revision;
    const changes = { purpose: purposeValue, remarks: remarksValue };
    const fingerprint = `${revision}\u0000${changes.purpose}\u0000${changes.remarks}`;
    const savedKey = activeDraft?.idempotency_key;
    const operationKey = savedKey && currentSaveKey.current === fingerprint
      ? savedKey
      : `${userId}:${companyId}:invoice:${invoice.invoice_id}:${makeOperationSuffix()}`;
    currentSaveKey.current = fingerprint;
    onDraftChange?.({ invoice_id: invoice.invoice_id, expected_revision: revision, ...changes,
      draft_event_ids: activeDraft?.draft_event_ids || {}, modified_fields: [], idempotency_key: operationKey });
    setLocalError("");
    try {
      await callAction({ name: "workspace_save_invoice", payload: { invoice_id: invoice.invoice_id, expected_revision: revision, idempotency_key: operationKey, changes } });
    } catch (_error) {
      setLocalError("保存请求未返回；可再次点击保存继续核对。 ");
    } finally {
      setBusy(false);
    }
  };

  const undoField = async (field) => {
    if (!invoice || !canModify || dirty || busy || !undoFields.includes(field)) return;
    setBusy(true);
    setUndoingField(field);
    setLocalError("");
    try {
      await callAction({ name: "workspace_undo_invoice", payload: { invoice_id: invoice.invoice_id, field } });
    } catch (_error) {
      setLocalError("撤销请求未返回；请刷新票据资料后确认当前内容。");
    } finally {
      setUndoingField("");
      setBusy(false);
    }
  };

  return (
    <main className="th-page-view th-page-invoices" aria-label="票据核对台">
      <header className="th-page-titlebar">
        <div><h1>票据核对台</h1><p className="th-page-description">查看当前企业在所选期间的发票字段、用途状态及适用规则。</p></div>
        <div className="th-page-actions"><span className="th-row-count">{invoices.length} 张发票</span><button type="button" className="th-button th-button-secondary" disabled={!month || busy} onClick={() => callAction({ name: "workspace_refresh" })}><RefreshCw size={15} />刷新资料</button></div>
      </header>
      {notice || localError ? <p className="th-workspace-notice" role="status" aria-live="polite">{localError || notice}</p> : null}
      {month && data.purpose_check_status && data.purpose_check_status !== "ok" ? <p className="th-purpose-check-note">用途完整性检查未判定{data.purpose_check_summary ? `：${data.purpose_check_summary}` : "。"}</p> : null}
      {!month ? <section className="th-page-empty"><FileSearch size={22} /><h2>先选择核对期间</h2><p>选择月份后显示业务库中属于当前企业的发票与规则。</p></section> : invoices.length ? (
        <div className="th-invoice-layout">
          <section className="th-data-panel th-invoice-list-panel" aria-label="发票列表">
            <div className="th-panel-heading"><div><h2>期间发票</h2><span>按开票日期排列 · {invoices.length} 条记录</span></div></div>
            <div className="th-invoice-table-wrap"><table className="th-invoice-table"><thead><tr><th>发票编号</th><th>开票日期</th><th>销售方</th><th>项目</th><th>金额</th><th>用途状态</th></tr></thead><tbody>
              {invoices.map((item) => <tr key={item.invoice_id} className={selectedInvoiceId === item.invoice_id ? "is-selected" : ""}>
                <td><button type="button" className="th-row-link" onClick={() => onSelectInvoice(item.invoice_id)}><strong>{item.invoice_id}</strong></button></td><td>{item.issue_date}</td><td>{item.seller_name || "—"}</td><td>{item.project_name || "—"}</td><td className="th-money-cell">{item.amount} 元</td><td><span className={`th-purpose-state th-purpose-${item.purpose_status || "undetermined"}`}>{invoicePurposeLabel(item)}</span></td>
              </tr>)}
            </tbody></table></div>
          </section>
          <aside className="th-invoice-inspector" aria-label="发票与规则详情">
            {invoice ? <>
              <section className="th-data-panel th-invoice-detail-panel">
                <div className="th-panel-heading"><div><p className="th-eyebrow">当前发票</p><h2>{invoice.invoice_id}</h2></div><ShieldCheck size={20} /></div>
                <dl className="th-invoice-facts"><div><dt>开票日期</dt><dd>{invoice.issue_date}</dd></div><div><dt>销售方</dt><dd>{invoice.seller_name || "—"}</dd></div><div><dt>项目</dt><dd>{invoice.project_name || "—"}</dd></div><div><dt>金额</dt><dd>{invoice.amount} 元</dd></div><div><dt>用途状态</dt><dd>{invoicePurposeLabel(invoice)}</dd></div></dl>
                <div className="th-invoice-edit-fields">
                  <label>发票用途<textarea rows={3} value={purposeValue} readOnly={!canModify} onChange={(event) => changeDraft("purpose", event.target.value)} placeholder={canModify ? "由经办人根据实际业务信息填写" : "当前账号仅可查看"} /></label>
                  <label>备注<textarea rows={2} value={remarksValue} readOnly={!canModify} onChange={(event) => changeDraft("remarks", event.target.value)} placeholder="—" /></label>
                  {canModify ? <><button type="button" className="th-button th-button-primary th-save-invoice" disabled={!dirty || busy} onClick={save}>{busy ? <RefreshCw size={15} /> : <Save size={15} />}{busy ? "保存中…" : "明确保存用途与备注"}</button>{dirty ? <button type="button" className="th-button th-button-secondary" disabled={busy} onClick={onPersistDraft}>暂存草稿</button> : null}</> : <p className="th-readonly-note"><ShieldCheck size={14} />当前账号仅可查看；用途、备注由有修改权限的用户保存。</p>}
                  {canModify && undoFields.length ? <div className="th-undo-saved-fields" aria-label="撤销最近已保存的修改"><span>最近已保存</span>{undoFields.map((field) => <button type="button" key={field} className="th-button th-button-secondary" disabled={busy || dirty} title={dirty ? "请先保存或清除未保存的编辑。" : undefined} onClick={() => undoField(field)}>{undoingField === field ? <RefreshCw size={14} /> : <Undo2 size={14} />}{undoingField === field ? "撤销中…" : `撤销${field === "purpose" ? "用途" : "备注"}`}</button>)}</div> : null}
                  {dirty ? <p className="th-draft-note">{data.frontier?.target_invoice_ids?.includes(invoice.invoice_id) ? "草稿暂存到当前办件后，对话和新会话都能继续使用。" : "这张票不在当前办件中，草稿保留在当前对话会话中。"}</p> : null}
                </div>
              </section>
              <section className="th-data-panel th-invoice-rules-panel"><div className="th-panel-heading"><div><h2>发票用途规则</h2><span>{month}</span></div><BookOpenText size={18} /></div>
                {purposeRules.length ? purposeRules.map((rule) => <details className="th-invoice-rule" key={`${rule.rule_id || rule.kind}-${rule.version || ""}`}><summary><span>{ruleVersionLabel(rule)} · {rule.name || RULE_KIND_LABELS[rule.kind] || "发票用途规则"}</span><span className="th-rule-validity">{ruleStatusLabel(rule, data.purpose_check_status)}</span></summary><p>{rule.execution_summary || data.purpose_check_summary || "当前期间规则信息。"}</p>{rule.content ? <details className="th-rule-source"><summary>查看规则原文</summary><pre>{rule.content}</pre></details> : null}</details>) : <div className="th-purpose-rule-empty"><strong>{ruleStatusLabel(null, data.purpose_check_status)}</strong><p>{data.purpose_check_summary || "当前期间没有可判定的发票用途规则。"}</p></div>}
              </section>
              {relatedTasks.length ? <section className="th-data-panel th-related-tasks-panel"><div className="th-panel-heading"><div><h2>关联办件</h2><span>{relatedTasks.length} 条</span></div></div>{relatedTasks.map((task) => <div className="th-related-task" key={task.task_id}><strong>{task.scope === "selected" ? "指定发票办理" : "期间用途补全"}</strong><span>{task.month || "未设期间"} · {taskStatusLabel(task, data.frontier)}</span><small>目标 {task.target_invoice_ids?.length ?? 0} 张 · 待处理 {taskPendingIds(task, data.frontier).length} 张</small></div>)}</section> : null}
            </> : <section className="th-page-empty th-invoice-unselected"><FileSearch size={21} /><h2>选择一张发票</h2><p>查看业务库中的实际字段、用途状态、适用规则与关联办件。</p></section>}
          </aside>
        </div>
      ) : <section className="th-page-empty"><FileSearch size={22} /><h2>该期间暂无发票记录</h2><p>没有符合当前企业与期间范围的发票。</p></section>}
    </main>
  );
}

function initialInvoiceDraft(remote, selectedId, invoices) {
  if (remote?.invoice_id === selectedId) return remote;
  const invoice = invoices.find((item) => item.invoice_id === selectedId);
  return invoice ? {
    invoice_id: invoice.invoice_id,
    expected_revision: invoice.revision,
    purpose: invoice.purpose || "",
    remarks: invoice.remarks || "",
    draft_event_ids: {},
    modified_fields: [],
    idempotency_key: "",
  } : {};
}

export default function WelcomePanel() {
  const [query, setQuery] = useState("");
  const [notice, setNotice] = useState("");
  const [narrow, setNarrow] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [activePage, setActivePage] = useState(props.active_page || "B");
  const [invoiceDraft, setInvoiceDraft] = useState(() => initialInvoiceDraft(
    props.workspace_draft || {},
    props.workspace_invoice_id,
    props.workspace_data?.invoices || [],
  ));
  const historyTrigger = useRef(null);
  const wasHistoryOpen = useRef(false);
  const lastWorkspaceDraft = useRef("");
  const userLabel = String(props.user_label || props.user_id || "—");
  const companyLabel = String(props.company_label || props.company_id || "未选择企业");
  const scopeLabel = `当前用户：${userLabel}；当前企业：${companyLabel}`;
  const entries = Array.isArray(props.history_entries) ? props.history_entries : [];
  const workspace = props.workspace_data || { tasks: [], invoices: [], rules: [], can_modify: false };
  const tasks = Array.isArray(workspace.tasks) ? workspace.tasks : [];
  const invoices = Array.isArray(workspace.invoices) ? workspace.invoices : [];
  const offset = Number.isInteger(props.history_offset) ? props.history_offset : 0;
  const needle = query.trim().toLocaleLowerCase();
  const visibleEntries = useMemo(() => entries.filter((entry) => (
    `${entry.title || ""} ${entry.preview || ""} ${entry.company_id || ""} ${entry.status || ""}`
  ).toLocaleLowerCase().includes(needle)), [entries, needle]);
  const selectedTask = tasks.find((task) => task.task_id === props.workspace_task_id) || tasks[0] || null;
  const visibleInvoice = invoices.find((invoice) => invoice.invoice_id === props.workspace_invoice_id) || null;
  const workspaceDraftToken = JSON.stringify([
    props.workspace_invoice_id,
    visibleInvoice?.revision,
    props.workspace_draft || {},
  ]);
  const selectedInvoiceUnsaved = Boolean(visibleInvoice && invoiceDraft?.invoice_id === visibleInvoice.invoice_id && (
    invoiceDraft.purpose !== (visibleInvoice.purpose || "") || invoiceDraft.remarks !== (visibleInvoice.remarks || "")
  ));

  useEffect(() => {
    const media = window.matchMedia("(max-width: 1119px)");
    const updateLayout = async () => {
      const isNarrow = media.matches;
      setNarrow(isNarrow);
      if (!isNarrow) setHistoryOpen(false);
      try {
        await callAction({ name: "initialize_workspace", payload: { is_narrow: isNarrow } });
      } catch (_error) {
        setNotice("工作区布局未更新；可刷新页面重试。");
      }
    };
    updateLayout();
    media.addEventListener("change", updateLayout);
    return () => media.removeEventListener("change", updateLayout);
  }, []);

  useEffect(() => {
    setActivePage(props.active_page || "B");
  }, [props.active_page]);

  useEffect(() => {
    if (lastWorkspaceDraft.current === workspaceDraftToken) return;
    lastWorkspaceDraft.current = workspaceDraftToken;
    setInvoiceDraft(initialInvoiceDraft(
      props.workspace_draft || {},
      props.workspace_invoice_id,
      invoices,
    ));
  }, [workspaceDraftToken]);

  useEffect(() => {
    if (!historyOpen) return undefined;
    const closeOnEscape = (event) => {
      if (event.key === "Escape") setHistoryOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [historyOpen]);

  useEffect(() => {
    if (wasHistoryOpen.current && !historyOpen && narrow) historyTrigger.current?.focus();
    wasHistoryOpen.current = historyOpen;
  }, [historyOpen, narrow]);

  useEffect(() => {
    setQuery("");
    setHistoryOpen(false);
  }, [props.user_id]);

  const runAction = async (name, payload = {}) => {
    setNotice("");
    try {
      await callAction({ name, payload });
      return true;
    } catch (_error) {
      setNotice("操作未完成，请重试。");
      return false;
    }
  };

  const persistCurrentInvoiceDraft = async () => {
    if (!invoiceDraft?.invoice_id || !invoiceDraft.modified_fields?.length) return true;
    try {
      await callAction({
        name: "workspace_set_draft",
        payload: {
          invoice_id: invoiceDraft.invoice_id,
          expected_revision: invoiceDraft.expected_revision,
          changes: { purpose: invoiceDraft.purpose || "", remarks: invoiceDraft.remarks || "" },
          modified_fields: invoiceDraft.modified_fields,
          draft_event_ids: invoiceDraft.draft_event_ids || {},
          persist: true,
          idempotency_key: invoiceDraft.idempotency_key || "",
        },
      });
      setInvoiceDraft((previous) => ({ ...previous, modified_fields: [] }));
      return true;
    } catch (_error) {
      setNotice("未保存草稿，页面未切换；请稍后重试。");
      return false;
    }
  };

  const setPage = async (page) => {
    if (page === activePage) {
      if (narrow) setHistoryOpen(false);
      return;
    }
    if (!(await persistCurrentInvoiceDraft())) return;
    setActivePage(page);
    setHistoryOpen(false);
    await runAction("set_workspace_page", { page });
  };

  const updateInvoiceDraft = (nextDraft) => {
    setInvoiceDraft(nextDraft);
  };

  const viewConversation = async (sessionId) => {
    if (narrow) setHistoryOpen(false);
    await runAction("conversation_history_view", { session_id: sessionId });
  };

  const continueConversation = async (sessionId) => {
    if (narrow) setHistoryOpen(false);
    await runAction("conversation_history_continue", { session_id: sessionId });
  };

  const startConversation = async () => {
    if (!(await persistCurrentInvoiceDraft())) return;
    const shared = workspace.frontier?.target_invoice_ids?.includes(invoiceDraft?.invoice_id);
    if (selectedInvoiceUnsaved && !shared && !window.confirm("当前会话中的票据草稿未关联办件，开始新对话后将清除。是否继续？")) return;
    if (narrow) setHistoryOpen(false);
    await runAction("new_conversation");
  };

  const loadMonth = async (event) => {
    event.preventDefault();
    const month = event.currentTarget.elements.namedItem("month")?.value || "";
    await runAction("set_context_month", { month });
  };

  const switchIdentity = async () => {
    if (!(await persistCurrentInvoiceDraft())) return;
    const shared = workspace.frontier?.target_invoice_ids?.includes(invoiceDraft?.invoice_id);
    if (selectedInvoiceUnsaved && !shared && !window.confirm("当前会话中的票据草稿未关联办件，切换后将清除。是否继续？")) return;
    const settingsButton = document.getElementById("chat-settings-open-modal");
    if (settingsButton) settingsButton.click();
    else setNotice("当前设置入口不可用；请返回对话工作区后重试。");
  };

  const chooseTask = async (task) => {
    const invoiceId = task.pending_invoice_ids?.[0] || task.target_invoice_ids?.[0] || "";
    setHistoryOpen(false);
    await runAction("workspace_select_task", { task_id: task.task_id, invoice_id: invoiceId });
  };

  const chooseInvoice = async (invoiceId) => {
    if (!(await persistCurrentInvoiceDraft())) return;
    setHistoryOpen(false);
    await runAction("workspace_select_invoice", { invoice_id: invoiceId });
  };

  const openTaskInvoice = async (taskId, invoiceId) => {
    await runAction("workspace_open_invoice", { task_id: taskId, invoice_id: invoiceId });
  };

  const bodyClass = `th-workspace-app th-page-${activePage.toLowerCase()}`;

  return (
    <>
      <div className="th-workspace-mobile-tools" aria-label="工作区面板">
        <button ref={historyTrigger} type="button" className="th-mobile-tool" aria-expanded={historyOpen} onClick={() => setHistoryOpen(true)}>
          <ListChecks size={16} />列表
        </button>
        <span className="th-mobile-scope" title={scopeLabel} aria-label={scopeLabel}>
          <span>{companyLabel}</span><span aria-hidden="true">/</span><span>{props.workspace_month || "未选期间"}</span>
        </span>
      </div>

      {narrow && historyOpen ? <button type="button" className="th-workspace-backdrop" aria-label="关闭列表抽屉" onClick={() => setHistoryOpen(false)} /> : null}
      <div className={bodyClass} data-active-page={activePage}>
        <nav className="th-primary-nav" aria-label="主导航">
          <div className="th-primary-brand" aria-label="财税工作台"><Landmark size={18} /></div>
          <div className="th-primary-links">
            {(["A", "B", "C"]).map((page) => (
              <button key={page} type="button" className={`th-primary-link${activePage === page ? " is-active" : ""}`} aria-current={activePage === page ? "page" : undefined} onClick={() => setPage(page)}>
                {page === "A" ? <ListChecks size={17} /> : page === "B" ? <MessageSquareText size={17} /> : <ReceiptText size={17} />}
                <span className="th-primary-code">{page}</span>
                <span className="th-primary-label">{PAGE_LABELS[page]}</span>
              </button>
            ))}
          </div>
          <div className="th-primary-footer"><Landmark size={15} /><span>财税工作台</span></div>
        </nav>

        <aside className={`th-workspace-shell${narrow ? " th-workspace-drawer" : ""}${historyOpen ? " is-open" : ""}`} aria-label={`${PAGE_LABELS[activePage]}列表`} aria-hidden={narrow && !historyOpen}>
          <header className="th-workspace-header">
            <div className="th-workspace-brand">
              <span className="th-workspace-mark"><Landmark size={16} strokeWidth={1.8} /></span>
              <div><strong>财税工作台</strong><span>{PAGE_LABELS[activePage]}</span></div>
            </div>
            {narrow ? <button type="button" className="th-icon-button" aria-label="关闭列表" onClick={() => setHistoryOpen(false)}><X size={17} /></button> : null}
            {activePage === "B" ? <button type="button" className="th-new-conversation" onClick={startConversation}><Plus size={15} />新对话</button> : null}
          </header>

          {activePage === "B" ? (
            <>
              <div className="th-workspace-history-heading"><h1>历史对话</h1><span>{entries.length} 条</span></div>
              <label className="th-search-box">
                <Search size={15} aria-hidden="true" />
                <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索当前页对话" aria-label="搜索当前页的历史对话" />
                {query ? <button type="button" className="th-clear-search" aria-label="清除搜索" onClick={() => setQuery("")}><X size={14} /></button> : null}
              </label>
              <p className="th-page-note">第 {Math.floor(offset / 50) + 1} 页 · 搜索当前页</p>
              <div className="th-history-list">
                {visibleEntries.map((entry) => (
                  <article className={`th-history-item${entry.session_id === props.session_id ? " is-current" : ""}`} key={entry.session_id}>
                    <div className="th-history-item-top"><h2 title={entry.title}>{entry.title || "未命名对话"}</h2><time className="th-history-date">{entry.updated_at}</time></div>
                    <p className="th-history-preview">{entry.preview || "暂无文字摘要"}</p>
                    <div className="th-history-meta"><span>{entry.company_id}</span><span>{entry.turn_count} 回合</span><span>{entry.status}</span></div>
                    <div className="th-history-item-actions">
                      <button type="button" className="th-history-view" onClick={() => viewConversation(entry.session_id)}>查看</button>
                      <button type="button" className="th-history-continue" onClick={() => continueConversation(entry.session_id)}>继续</button>
                    </div>
                  </article>
                ))}
                {!visibleEntries.length ? <div className="th-empty-state">{entries.length ? "当前页没有匹配的对话。" : "还没有已保存的对话。开始提问后，记录会留在这里。"}</div> : null}
              </div>
              <footer className="th-workspace-footer">
                <div className="th-page-actions">
                  <button type="button" className="th-icon-button" aria-label="较新对话" disabled={offset <= 0} onClick={() => runAction("show_more_history", { offset: Math.max(0, offset - 50) })}><ChevronLeft size={16} /></button>
                  <span>第 {Math.floor(offset / 50) + 1} 页</span>
                  <button type="button" className="th-icon-button" aria-label="更早对话" disabled={!props.history_has_more} onClick={() => runAction("show_more_history", { offset: offset + 50 })}><ChevronRight size={16} /></button>
                </div>
              </footer>
            </>
          ) : activePage === "A" ? (
            <>
              <div className="th-workspace-history-heading"><h1>办件列表</h1><span>{tasks.length} 条</span></div>
              <div className="th-secondary-list">
                {tasks.map((task) => (
                  <button type="button" key={task.task_id} className={`th-secondary-item${selectedTask?.task_id === task.task_id ? " is-current" : ""}`} onClick={() => chooseTask(task)}>
                    <span className="th-secondary-item-top"><strong>{task.month || "未设期间"}</strong><small>{taskStatusLabel(task, workspace.frontier)}</small></span>
                    <span className="th-secondary-item-title">{task.scope === "selected" ? "指定发票" : "用途补全"}</span>
                    <span className="th-secondary-item-meta">目标 {task.target_invoice_ids?.length ?? 0} 张 · 待处理 {taskPendingIds(task, workspace.frontier).length} 张</span>
                  </button>
                ))}
                {!tasks.length ? <div className="th-empty-state">{props.workspace_month ? "该期间暂无办件。" : "先选择期间查看办件。"}</div> : null}
              </div>
            </>
          ) : (
            <>
              <div className="th-workspace-history-heading"><h1>期间发票</h1><span>{invoices.length} 张</span></div>
              <div className="th-secondary-list">
                {invoices.map((invoice) => (
                  <button type="button" key={invoice.invoice_id} className={`th-secondary-item${props.workspace_invoice_id === invoice.invoice_id ? " is-current" : ""}`} onClick={() => chooseInvoice(invoice.invoice_id)}>
                    <span className="th-secondary-item-top"><strong>{invoice.invoice_id}</strong><small>{invoice.issue_date}</small></span>
                    <span className="th-secondary-item-title">{invoice.seller_name || "—"}</span>
                    <span className="th-secondary-item-meta">{invoice.project_name || "—"} · {invoice.amount} 元</span>
                  </button>
                ))}
                {!invoices.length ? <div className="th-empty-state">{props.workspace_month ? "该期间暂无发票。" : "先选择期间查看发票。"}</div> : null}
              </div>
            </>
          )}

          <footer className="th-workspace-identity"><span>{props.user_label || props.user_id || "—"}</span><strong title={companyLabel}>{companyLabel}</strong></footer>
          {notice || props.workspace_notice ? <p className="th-copy-status" role="status" aria-live="polite">{notice || props.workspace_notice}</p> : null}
        </aside>

        <header className="th-workspace-topbar">
          <div className="th-topbar-identity"><span>当前企业</span><strong title={companyLabel}>{companyLabel}</strong></div>
          <span className="th-topbar-divider" aria-hidden="true" />
          <form className="th-topbar-period-form" onSubmit={loadMonth}>
            <label className="th-topbar-period"><span>当前期间</span><input key={props.workspace_month || "empty-month"} type="month" name="month" defaultValue={props.workspace_month || ""} aria-label="当前工作期间" /></label>
            <button type="submit" className="th-topbar-load" aria-label="查看期间"><ArrowRight size={14} /><span>查看期间</span></button>
          </form>
          <span className="th-topbar-spacer" />
          <span className="th-topbar-user">{userLabel}</span>
          <button type="button" className="th-topbar-switch" onClick={switchIdentity}>切换用户/企业</button>
          {activePage !== "B" ? <button type="button" className="th-topbar-new-chat" onClick={() => setPage("B")}><MessageSquareText size={15} />当前对话</button> : null}
        </header>

        <div className="th-workspace-page-layer" aria-hidden={activePage === "B"}>
          <div className="th-page-mount" hidden={activePage !== "A"}>
            <WorkItemsPage
              data={workspace}
              month={props.workspace_month || null}
              selectedTaskId={props.workspace_task_id || null}
              selectedInvoiceId={props.workspace_invoice_id || null}
              recentTurns={Array.isArray(props.workspace_recent_turns) ? props.workspace_recent_turns : []}
              notice={props.workspace_notice || notice}
              onOpenInvoice={openTaskInvoice}
              onContinueChat={() => setPage("B")}
            />
          </div>
          <div className="th-page-mount" hidden={activePage !== "C"}>
            <InvoiceCheckPage
              data={workspace}
              month={props.workspace_month || null}
              selectedInvoiceId={props.workspace_invoice_id || null}
              draft={invoiceDraft}
              notice={props.workspace_notice || notice}
              userId={props.user_id || ""}
              companyId={props.company_id || ""}
              onDraftChange={updateInvoiceDraft}
              onPersistDraft={persistCurrentInvoiceDraft}
              onSelectInvoice={chooseInvoice}
            />
          </div>
        </div>

        {narrow ? (
          <nav className="th-mobile-page-nav" aria-label="页面导航">
            {(["A", "B", "C"]).map((page) => (
              <button key={page} type="button" className={activePage === page ? "is-active" : ""} aria-current={activePage === page ? "page" : undefined} onClick={() => setPage(page)}>
                {page === "A" ? <ListChecks size={17} /> : page === "B" ? <MessageSquareText size={17} /> : <ReceiptText size={17} />}
                <span>{PAGE_LABELS[page]}</span>
              </button>
            ))}
          </nav>
        ) : null}
      </div>
    </>
  );
}
