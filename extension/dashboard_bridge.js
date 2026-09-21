// Wake the extension as soon as a dashboard handoff button creates work.
// The bridge only observes the button click; it never reads dashboard data.
document.addEventListener("click", (event) => {
  const button = event.target.closest?.(".dianxiaomi-product, .dianxiaomi-store");
  if (!button) return;
  window.setTimeout(() => {
    chrome.runtime.sendMessage({ type: "dianxiaomi-handoff-created" }).catch(() => undefined);
  }, 600);
});
