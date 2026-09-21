// Wake the extension as soon as a dashboard handoff button creates work.
// The bridge only observes the button click; it never reads dashboard data.
function dashboardApiBase() {
  const current = new URL(location.href);
  if (["127.0.0.1", "localhost"].includes(current.hostname)) {
    return `http://${current.hostname}:8000`;
  }
  if (current.hostname === "galoremw.github.io" || current.hostname.endsWith(".onrender.com")) {
    return "https://aliexpress-monitor-api.onrender.com";
  }
  return null;
}

function notifyDashboardContext() {
  const apiBaseUrl = dashboardApiBase();
  if (apiBaseUrl) {
    const current = new URL(location.href);
    chrome.runtime.sendMessage({
      type: "dashboard-context",
      api_base_url: apiBaseUrl,
      worker_mode: current.searchParams.get("dianxiaomi_worker") === "1",
    }).catch(() => undefined);
  }
}

notifyDashboardContext();

document.addEventListener("click", (event) => {
  const button = event.target.closest?.(".dianxiaomi-product, .dianxiaomi-store");
  if (!button) return;
  window.setTimeout(() => {
    chrome.runtime.sendMessage({
      type: "dianxiaomi-handoff-created",
      api_base_url: dashboardApiBase(),
    }).catch(() => undefined);
  }, 600);
});
