// Chainlit 2.12 forwards server window messages to this page. Use its built-in
// New Chat path; the server preserves the Agent session separately.
(() => {
  // Keep a stable identity for this browser tab across Chainlit's New Chat
  // sessionId reset. The server reads only this named cookie, never all cookies.
  const storageKey = "taxharness_tab_id";
  let tabId = sessionStorage.getItem(storageKey);
  if (!tabId) {
    tabId = (globalThis.crypto && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    sessionStorage.setItem(storageKey, tabId);
  }
  document.cookie = `${storageKey}=${encodeURIComponent(tabId)}; Path=/; SameSite=Lax`;
  window.addEventListener("focus", () => {
    document.cookie = `${storageKey}=${encodeURIComponent(tabId)}; Path=/; SameSite=Lax`;
  });
})();

window.addEventListener("message", (event) => {
  const data = event && event.data;
  if (!data || !["taxharness_new_thread", "taxharness_resume_history"].includes(data.type)) return;
  const button = document.getElementById("new-chat-button");
  if (button) {
    button.click();
  } else {
    // The built-in route is the same clean-thread path used by Chainlit's UI.
    window.location.assign("/");
  }
});
