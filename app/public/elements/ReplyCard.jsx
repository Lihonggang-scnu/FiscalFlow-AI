import { useState } from "react";
import { ChevronDown, ChevronUp, Copy } from "lucide-react";

export default function ReplyCard() {
  const [notice, setNotice] = useState("");
  const [fallback, setFallback] = useState("");

  const copyReply = async () => {
    const text = String(props.text || "");
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
      await navigator.clipboard.writeText(text);
      setFallback("");
      setNotice("已复制回复，保留原始 Markdown。 ");
    } catch (_error) {
      setFallback(text);
      setNotice("复制失败，请在下方文本框中手动选择并复制。 ");
    }
  };

  const toggleReply = async (toggle = "reply") => {
    setNotice("");
    try {
      const payload = props.history_session_id
        ? { session_id: props.history_session_id, turn_id: props.history_turn_id, toggle }
        : { message_id: props.message_id };
      await callAction({ name: "toggle_reply", payload });
    } catch (_error) {
      setNotice("展开状态未更新，请重试。 ");
    }
  };

  return (
    <div className="th-reply-actions">
      {props.turn_toggle ? <button className="th-quiet-button" type="button" onClick={() => toggleReply("turn")}>
        {props.turn_expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
        {props.turn_expanded ? `收起${props.turn_label || "本回合"}` : `展开${props.turn_label || "本回合"}`}
      </button> : null}
      {props.history_session_id && props.long && props.turn_expanded ? <button className="th-quiet-button" type="button" onClick={() => toggleReply("reply")}>
        {props.reply_expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
        {props.reply_expanded ? "收起长回复" : "展开完整回复"}
      </button> : null}
      {!props.history_session_id && props.long ? <button className="th-quiet-button" type="button" onClick={() => toggleReply("reply")}>
        {props.expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
        {props.expanded ? "收起长回复" : "展开完整回复"}
      </button> : null}
      <button className="th-quiet-button" type="button" onClick={copyReply}><Copy size={15} />{props.copy_label || "复制回复"}</button>
      {notice ? <span className="th-copy-status" role="status" aria-live="polite">{notice}</span> : null}
      {fallback ? <label className="th-copy-fallback">回复原文<textarea readOnly value={fallback} onFocus={(event) => event.target.select()} onClick={(event) => event.target.select()} /></label> : null}
    </div>
  );
}
