// Wake the extension as soon as a dashboard handoff button creates work.
// The bridge only observes the button click; it never reads dashboard data.
function dashboardApiBase() {
  const current = new URL(location.href);
  if (["127.0.0.1", "localhost"].includes(current.hostname)) {
    // The local frontend proxies /api to FastAPI. Keep the dashboard cookie
    // on the same origin so the isolated worker can mint its bearer session.
    return current.origin;
  }
  if (current.hostname === "galoremw.github.io" || current.hostname.endsWith(".onrender.com")) {
    return "https://aliexpress-monitor-api.onrender.com";
  }
  return null;
}

async function notifyDashboardContext() {
  const apiBaseUrl = dashboardApiBase();
  if (apiBaseUrl) {
    const current = new URL(location.href);
    let apiAccessToken = null;
    try {
      const response = await fetch(`${apiBaseUrl}/api/auth/extension-token`, {
        credentials: "include",
      });
      if (response.ok) apiAccessToken = (await response.json()).access_token || null;
    } catch {
      // The worker will retry on the next dashboard load or queue event.
    }
    chrome.runtime.sendMessage({
      type: "dashboard-context",
      api_base_url: apiBaseUrl,
      worker_mode: current.searchParams.get("dianxiaomi_worker") === "1",
      api_access_token: apiAccessToken,
    }).catch(() => undefined);
  }
}

void notifyDashboardContext();

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
