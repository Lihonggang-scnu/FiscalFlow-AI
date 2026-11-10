import { useState } from "react";
import { ArrowLeft, Copy, MessageSquareText } from "lucide-react";

export default function ConversationHistory() {
  const [notice, setNotice] = useState("");
  const [fallback, setFallback] = useState("");
  const turns = Array.isArray(props.turns) ? props.turns : [];

  const runAction = async (name, payload = {}) => {
    setNotice("");
    try {
      await callAction({ name, payload });
    } catch (_error) {
      setNotice("操作未完成，请重试。 ");
    }
  };

  const copyTranscript = async () => {
    const text = turns.map((turn, index) =>
      `第 ${index + 1} 回合\n用户\n${turn.request_text || ""}\n\n助手\n${turn.output_text || ""}`
    ).join("\n\n────────────────────\n\n");
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
      await navigator.clipboard.writeText(text);
      setFallback("");
      setNotice("整段对话已复制，保留原始 Markdown。 ");
    } catch (_error) {
      setFallback(text);
      setNotice("复制失败，请在下方文本框中手动选择并复制。 ");
    }
  };

  return (
    <section className="th-transcript-panel" aria-label="历史对话记录">
      <header className="th-transcript-heading">
        <button className="th-quiet-button" type="button" onClick={() => runAction("show_context")}><ArrowLeft size={15} />返回当前资料</button>
        <h2>{props.title || "历史对话"}</h2>
        <p>{props.turn_count || turns.length} 个已保存回合 · 查看记录不会重放工具</p>
        {props.continued ? <p className="th-inline-status">下一条消息会接入同一个 Agent 会话。</p> : null}
        <div className="th-transcript-actions">
          <button className="th-secondary-button" type="button" onClick={copyTranscript} disabled={!turns.length}><Copy size={15} />复制整段对话</button>
          <button className="th-primary-button" type="button" onClick={() => runAction("conversation_history_continue", { session_id: props.session_id })}><MessageSquareText size={15} />继续此对话</button>
        </div>
      </header>
      {notice ? <p className="th-copy-status" role="status" aria-live="polite">{notice}</p> : null}
      {fallback ? <label className="th-copy-fallback">对话原文<textarea readOnly value={fallback} onFocus={(event) => event.target.select()} onClick={(event) => event.target.select()} /></label> : null}
    </section>
  );
}
