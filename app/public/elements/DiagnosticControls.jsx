import { X } from "lucide-react";

export default function DiagnosticControls() {
  const close = async () => {
    try {
      await callAction({ name: "dismiss_diagnostic", payload: { step_id: props.step_id } });
    } catch (_error) {
      // The diagnostic remains visible if Chainlit cannot complete the close action.
    }
  };

  return <button className="th-quiet-button th-diagnostic-close" type="button" onClick={close}><X size={15} />关闭本轮过程</button>;
}
