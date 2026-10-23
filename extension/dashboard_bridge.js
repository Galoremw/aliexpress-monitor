// Wake the extension as soon as a dashboard handoff button creates work.
// The bridge only observes the button click; it never reads dashboard data.
function dashboardApiBase() {
  const current = new URL(location.href);
  if (isLocalCollectorDashboard(current)) {
    // The local frontend proxies /api to FastAPI. Keep the dashboard cookie
    // on the same origin so the isolated worker can mint its bearer session.
    return current.origin;
  }
  if (current.hostname === "galoremw.github.io" || current.hostname.endsWith(".onrender.com")) {
    return "https://aliexpress-monitor-api.onrender.com";
  }
  return null;
}

function isLocalDashboard() {
  return ["127.0.0.1", "localhost"].includes(location.hostname)
    && ["3000", "8000"].includes(location.port);
}

function isLocalCollectorDashboard(url) {
  const localHost = ["127.0.0.1", "localhost"].includes(url.hostname);
  const lanHost = url.hostname === "192.168.66.115";
  return url.protocol === "http:"
    && (localHost && ["3000", "8000"].includes(url.port)
      || lanHost && url.port === "3000");
}

function isLoopbackDashboard(url) {
  return url.protocol === "http:"
    && ["127.0.0.1", "localhost"].includes(url.hostname)
    && ["3000", "8000"].includes(url.port);
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
      dianxiaomi_local_dashboard: isLocalDashboard(),
      worker_mode: current.searchParams.get("dianxiaomi_worker") === "1"
        && current.searchParams.get("dianxiaomi_local") === "1"
        && ["127.0.0.1", "localhost"].includes(current.hostname),
      dianxiaomi_mode_explicit: current.searchParams.has("dianxiaomi_worker"),
      browser_collection_mode: current.searchParams.get("browser_collection") === "1"
        && (!isLoopbackDashboard(current)
          || current.searchParams.get("browser_collection_profile") === "isolated"),
      browser_collection_local_mode: current.searchParams.get("browser_collection") === "1"
        && isLocalCollectorDashboard(current)
        && current.searchParams.get("browser_collection_profile") === "isolated",
      browser_collection_mode_explicit: isLoopbackDashboard(current)
        || current.searchParams.has("browser_collection"),
      api_access_token: apiAccessToken,
    }).catch(() => undefined);
  }
}

void notifyDashboardContext();

document.addEventListener("click", (event) => {
  const reviewButton = event.target.closest?.(".collect-reviews");
  if (reviewButton) {
    window.setTimeout(() => {
      chrome.runtime.sendMessage({
        type: "review-collection-created",
        api_base_url: dashboardApiBase(),
      }).catch(() => undefined);
    }, 300);
    return;
  }
});

window.addEventListener("aliexpress-monitor:browser-run-requested", () => {
  if (!isLocalCollectorDashboard(new URL(location.href))) return;
  chrome.runtime.sendMessage({
    type: "browser-collection-run-requested",
    api_base_url: dashboardApiBase(),
  }).catch(() => undefined);
});

window.addEventListener("aliexpress-monitor:dianxiaomi-queue-requested", () => {
  if (!dashboardApiBase()) return;
  chrome.runtime.sendMessage({
    type: "dianxiaomi-handoff-created",
    api_base_url: dashboardApiBase(),
  }).catch(() => undefined);
});
