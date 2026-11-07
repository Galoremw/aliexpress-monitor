importScripts("config.js");

const API_BASE = globalThis.ALIEXPRESS_MONITOR_API_BASE || "http://127.0.0.1:8000";
const POLL_ALARM = "aliexpress-monitor-browser-collection";
const NOTIFICATION_ID = "aliexpress-monitor-verification";
const MAX_OPEN_COLLECTION_PAGES = 2;
const AUTH_REQUIRED_NOTIFICATION_ID = "aliexpress-monitor-auth-required";
const FIRST_PRODUCT_NOTIFICATION_ID = "aliexpress-monitor-first-product";
const DIANXIAOMI_NOTIFICATION_ID = "aliexpress-monitor-dianxiaomi-confirmation";
const DIANXIAOMI_PRODUCT_OPERATION_KEY = "dianxiaomiProductOperation";
const COLLECTION_RESPONSE_TIMEOUT_MS = 90 * 1000;
const STORE_COLLECTION_RESPONSE_TIMEOUT_MS = 18 * 60 * 1000;
const STORE_PAGE_RESPONSE_TIMEOUT_MS = 10 * 60 * 1000;
const STORE_CANDIDATE_SCAN_RESPONSE_TIMEOUT_MS = 15 * 60 * 1000;
const STORE_PAGE_NAVIGATION_TIMEOUT_MS = 90 * 1000;
const STORE_COLLECTION_WORK_MS = 15 * 60 * 1000;
const STORE_PROGRESS_STORAGE_PREFIX = "browserCollectionStoreProgress:";
const POOL_RESET_CAPABILITY = "store_pool_reset_v1";
const tabProductIds = new Map();
let automationBusy = false;
let automationGeneration = 0;
let dianxiaomiBusy = false;
let browserCollectionModeReady = false;
let dianxiaomiWorkerModeReady = false;
let workerModeInitialization = null;
let workerIdentityInitialization = null;

function productIdFromUrl(url) {
  try {
    return new URL(url).pathname.match(/\/item\/(\d+)/i)?.[1] || null;
  } catch {
    return null;
  }
}

function dianxiaomiProductPageMismatch(targetUrl, actualUrl) {
  const expectedProductId = productIdFromUrl(targetUrl);
  const actualProductId = productIdFromUrl(actualUrl);
  if (!expectedProductId || !actualProductId || expectedProductId === actualProductId) return null;
  return {
    expected_product_id: expectedProductId,
    actual_product_id: actualProductId,
    actual_url: actualUrl,
  };
}

function monitoredProductIdFromUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.searchParams.get("monitor_product_id")
      || parsed.hash.match(/monitor_product_id=(\d+)/i)?.[1]
      || null;
  } catch {
    return null;
  }
}

function rememberTabProduct(tabId, url) {
  const monitoredId = monitoredProductIdFromUrl(url);
  const productId = productIdFromUrl(url);
  if (monitoredId) tabProductIds.set(tabId, monitoredId);
  else if (productId && !tabProductIds.has(tabId)) tabProductIds.set(tabId, productId);
}

function isAliExpressPage(url) {
  try {
    const parsed = new URL(url);
    return /(?:^|\.)aliexpress\.com$/i.test(parsed.hostname)
      && (/\/item\/\d+/i.test(parsed.pathname) || /\/store\/\d+/i.test(parsed.pathname));
  } catch {
    return false;
  }
}

function isAliExpressProductPage(url) {
  return isAliExpressPage(url) && Boolean(productIdFromUrl(url));
}

function isAliExpressChallengePage(url) {
  try {
    const parsed = new URL(url);
    return /(?:^|\.)aliexpress\.com$/i.test(parsed.hostname)
      && /(?:^|\/)(?:punish|captcha|challenge|verification|verify|security-check)(?:\/|$)/i.test(parsed.pathname);
  } catch {
    return false;
  }
}

function isLocalMonitorDashboard(url) {
  try {
    const parsed = new URL(url);
    const isLanCollectorHost = parsed.hostname === "192.168.66.115" && parsed.port === "3000";
    return parsed.protocol === "http:"
      && (isLanCollectorHost
        || ["127.0.0.1", "localhost"].includes(parsed.hostname) && ["3000", "8000"].includes(parsed.port));
  } catch {
    return false;
  }
}

function isLoopbackMonitorDashboard(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:"
      && ["127.0.0.1", "localhost"].includes(parsed.hostname)
      && ["3000", "8000"].includes(parsed.port);
  } catch {
    return false;
  }
}

function isIsolatedBrowserCollectorUrl(url) {
  try {
    const parsed = new URL(url);
    return isLocalMonitorDashboard(parsed.href)
      && parsed.searchParams.get("browser_collection") === "1"
      && parsed.searchParams.get("browser_collection_profile") === "isolated";
  } catch {
    return false;
  }
}

async function api(path, options = {}, requestedBaseUrl = null) {
  let stored = await chrome.storage.local.get(["apiAccessToken", "apiAccessTokens", "activeApiBase"]);
  const baseUrl = requestedBaseUrl || stored.activeApiBase || API_BASE;
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const send = (token) => fetch(`${baseUrl}${path}`, {
    ...options,
    credentials: "include",
    headers: { ...headers, ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  let token = stored.apiAccessTokens?.[baseUrl] || stored.apiAccessToken;
  let response = await send(token);
  if (response.status === 401) {
    if (await refreshApiTokenFromDashboard(baseUrl, true)) {
      stored = await chrome.storage.local.get(["apiAccessToken", "apiAccessTokens"]);
      token = stored.apiAccessTokens?.[baseUrl] || stored.apiAccessToken;
      response = await send(token);
    }
    if (response.status === 401) await notifyAuthRequired();
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || `本地监控台请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

async function notifyAuthRequired() {
  const now = Date.now();
  const { authRequiredNotifiedAt = 0 } = await chrome.storage.local.get("authRequiredNotifiedAt");
  if (now - authRequiredNotifiedAt < 5 * 60 * 1000) return;
  await chrome.storage.local.set({ authRequiredNotifiedAt: now });
  await chrome.notifications.create(AUTH_REQUIRED_NOTIFICATION_ID, {
    type: "basic",
    iconUrl: "icon.svg",
    title: "监控台需要重新登录",
    message: "扩展授权已失效。请登录本地监控台后，扩展会自动重试队列。",
    priority: 2,
  }).catch(() => undefined);
}

async function refreshApiTokenFromDashboard(baseUrl, force = false) {
  const stored = await chrome.storage.local.get(["apiAccessToken", "apiAccessTokens"]);
  if (!force && (stored.apiAccessTokens?.[baseUrl] || stored.apiAccessToken)) return true;
  const tabs = await chrome.tabs.query({
    url: [
      "http://127.0.0.1:3000/*",
      "http://localhost:3000/*",
      "http://192.168.66.115:3000/*",
      "http://127.0.0.1:8000/*",
      "http://localhost:8000/*",
      "https://galoremw.github.io/*",
      "https://*.onrender.com/*",
    ],
  });
  const apiHostname = new URL(baseUrl).hostname;
  const dashboardCandidates = tabs
    .filter((tab) => Number.isInteger(tab.id))
    .sort((left, right) => {
      const hostPriority = Number(new URL(right.url).hostname === apiHostname)
        - Number(new URL(left.url).hostname === apiHostname);
      if (hostPriority) return hostPriority;
      const priority = (tab) => tab.url?.includes("browser_collection=1") ? 0
        : tab.url?.includes("dianxiaomi_worker=1") ? 1 : 2;
      return priority(left) - priority(right);
    });
  for (const dashboard of dashboardCandidates) {
    try {
      const [injected] = await chrome.scripting.executeScript({
        target: { tabId: dashboard.id },
        world: "MAIN",
        args: [baseUrl],
        func: async (apiBase) => {
          try {
            const response = await fetch(`${apiBase}/api/auth/extension-token`, {
              credentials: "include",
            });
            if (!response.ok) return null;
            return await response.json();
          } catch {
            return null;
          }
        },
      });
      const token = injected?.result?.access_token;
      if (!token) continue;
      await chrome.storage.local.set({
        apiAccessToken: token,
        apiAccessTokens: {
          ...(stored.apiAccessTokens || {}),
          [baseUrl]: token,
        },
      });
      return true;
    } catch {
      continue;
    }
  }
  return false;
}

async function injectCollector(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!isAliExpressPage(tab.url)) return;
    rememberTabProduct(tabId, tab.url);
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["page_collector.js", "content.js"],
    });
  } catch {
    // Navigation may still be replacing the document; the completion event retries it.
  }
}

async function injectDianxiaomiProductBridge(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!isAliExpressProductPage(tab.url)) return;
    await chrome.scripting.executeScript({ target: { tabId }, files: ["dianxiaomi_product_bridge.js"] });
  } catch {
    // Navigation may still be replacing the document; the next poll retries.
  }
}

async function injectOpenAliExpressTabs() {
  const tabs = await chrome.tabs.query({
    url: ["https://aliexpress.com/*", "https://*.aliexpress.com/*"],
  });
  await Promise.all(tabs.map((tab) => Promise.all([
    injectCollector(tab.id),
    isAliExpressProductPage(tab.url) ? injectDianxiaomiProductBridge(tab.id) : Promise.resolve(),
  ])));
}

async function automationState() {
  return chrome.storage.local.get([
    "automationWindowId",
    "automationWindowOwned",
    "automationWindowBackground",
    "automationWindowFocusGuardUntil",
    "foregroundSingleTestItemId",
    "foregroundSingleTestReturnWindowId",
    "automationDashboardTabId",
    "automationTabId",
    "automationItem",
    "automationClaimToken",
    "browserCollectionIsolatedProfileAuthorized",
    "browserCollectionRemoteProfileEnabled",
    "automationItemPrefetched",
    "automationTaskKind",
    "prefetchedTabId",
    "prefetchedItem",
    "prefetchedClaimToken",
    "prefetchedTaskKind",
    "prefetchedReviewKnownKeys",
    "idleAutomationTabId",
    "reviewKnownKeys",
    "reviewProbeResult",
    "challengePaused",
    "identityReviewPaused",
    "challengeProvider",
    "firstProductPresentedRunId",
    "browserCollectionWorkerMode",
  ]);
}

async function browserCollectionWorkerIdentity() {
  let state = await chrome.storage.local.get([
    "browserCollectionWorkerId",
    "browserCollectionLocalProfileEnabled",
    "browserCollectionRemoteProfileEnabled",
    "browserCollectionWorkerLabel",
  ]);
  if (!state.browserCollectionWorkerId) {
    if (!workerIdentityInitialization) {
      workerIdentityInitialization = (async () => {
        const latest = await chrome.storage.local.get("browserCollectionWorkerId");
        if (latest.browserCollectionWorkerId) return latest.browserCollectionWorkerId;
        const workerId = crypto.randomUUID();
        await chrome.storage.local.set({ browserCollectionWorkerId: workerId });
        return workerId;
      })().finally(() => {
        workerIdentityInitialization = null;
      });
    }
    state.browserCollectionWorkerId = await workerIdentityInitialization;
  }
  const customLabel = typeof state.browserCollectionWorkerLabel === "string"
    ? state.browserCollectionWorkerLabel.trim()
    : "";
  const label = customLabel || (state.browserCollectionLocalProfileEnabled === true
    ? "Galore主机"
    : state.browserCollectionRemoteProfileEnabled === true ? "第二台电脑" : "Chrome采集器");
  return { worker_id: state.browserCollectionWorkerId, worker_label: label };
}

function browserCollectionWorkerLabelFromUrl(url) {
  try {
    return (new URL(url).searchParams.get("browser_collection_name") || "")
      .replace(/[\u0000-\u001f\u007f]/g, "")
      .trim()
      .slice(0, 128);
  } catch {
    return "";
  }
}

async function ensureIdleAutomationTab(workerWindow, state) {
  if (!workerWindow?.id) return null;
  const dashboardTab = state.automationDashboardTabId
    ? await chrome.tabs.get(state.automationDashboardTabId).catch(() => null)
    : null;
  const sharedDashboardWindow = state.browserCollectionRemoteProfileEnabled === true
    && dashboardTab?.windowId === workerWindow.id;
  const tabs = await chrome.tabs.query({ windowId: workerWindow.id }).catch(() => []);
  const protectedTabIds = new Set([
    state.automationTabId,
    state.prefetchedTabId,
    state.automationDashboardTabId,
  ].filter(Number.isInteger));
  const trackedIdleTab = state.idleAutomationTabId
    ? await chrome.tabs.get(state.idleAutomationTabId).catch(() => null)
    : null;
  const idleTab = (trackedIdleTab?.windowId === workerWindow.id
    && !protectedTabIds.has(trackedIdleTab.id)
    ? trackedIdleTab
    : null)
    || tabs.find((tab) => tab.url === "about:blank" && !protectedTabIds.has(tab.id))
    || await chrome.tabs.create({ windowId: workerWindow.id, url: "about:blank", active: false }).catch(() => null);
  await chrome.storage.local.set({
    automationWindowId: workerWindow.id,
    automationWindowOwned: !sharedDashboardWindow,
    automationWindowBackground: true,
    ...(idleTab ? { idleAutomationTabId: idleTab.id } : {}),
  });
  if (trackedIdleTab?.windowId === workerWindow.id
    && !protectedTabIds.has(trackedIdleTab.id)
    && trackedIdleTab.id !== idleTab?.id) {
    await chrome.tabs.remove(trackedIdleTab.id).catch(() => undefined);
  }
  return idleTab;
}

async function groupAutomationTabs(tabIds, windowId) {
  const eligibleTabs = (await Promise.all([...new Set(tabIds)].map((tabId) => chrome.tabs.get(tabId).catch(() => null))))
    .filter((tab) => tab && tab.windowId === windowId);
  if (!eligibleTabs.length) return null;
  const saved = await chrome.storage.local.get(["automationTabGroupId", "automationTabGroupWindowId"]);
  let groupId = saved.automationTabGroupWindowId === windowId ? saved.automationTabGroupId : null;
  try {
    if (!groupId || !(await chrome.tabGroups.get(groupId).catch(() => null))) {
      groupId = await chrome.tabs.group({ tabIds: eligibleTabs.map((tab) => tab.id) });
    } else {
      await chrome.tabs.group({ tabIds: eligibleTabs.map((tab) => tab.id), groupId });
    }
    await chrome.storage.local.set({ automationTabGroupId: groupId, automationTabGroupWindowId: windowId });
    await chrome.tabGroups.update(groupId, { title: "AliExpress 采集任务", color: "green", collapsed: true });
    return groupId;
  } catch {
    return null;
  }
}

async function ensureAutomationWindow({ ensureIdle = true } = {}) {
  const state = await automationState();
  const windowState = state.browserCollectionWorkerMode === true
    || state.browserCollectionIsolatedProfileAuthorized === true ? "normal" : "minimized";
  const dashboardTab = state.automationDashboardTabId
    ? await chrome.tabs.get(state.automationDashboardTabId).catch(() => null)
    : null;
  const reuseDashboardWindow = state.browserCollectionRemoteProfileEnabled === true;
  if (reuseDashboardWindow && !dashboardTab?.windowId) return null;
  let workerWindow = reuseDashboardWindow
    ? await chrome.windows.get(dashboardTab.windowId).catch(() => null)
    : state.automationWindowId && state.automationWindowBackground
      ? await chrome.windows.get(state.automationWindowId).catch(() => null)
      : null;
  if (!reuseDashboardWindow && workerWindow?.id && workerWindow.id === dashboardTab?.windowId) workerWindow = null;
  if (workerWindow?.id) {
    if (!reuseDashboardWindow) {
      await chrome.windows.update(workerWindow.id, { focused: false, state: windowState }).catch(() => undefined);
    }
  } else if (!reuseDashboardWindow) {
    workerWindow = await chrome.windows.create({
      focused: false,
      state: windowState,
      type: "normal",
      url: "about:blank",
    }).catch(() => null);
  }
  if (!workerWindow?.id) return null;

  await chrome.storage.local.set({
    automationWindowId: workerWindow.id,
    automationWindowOwned: !reuseDashboardWindow,
    automationWindowBackground: true,
    automationWindowFocusGuardUntil: Date.now() + 5000,
  });
  if (ensureIdle && !reuseDashboardWindow) await ensureIdleAutomationTab(workerWindow, state);
  return workerWindow;
}

async function migrateTrackedAutomationTabs(state) {
  const dashboardTab = state.automationDashboardTabId
    ? await chrome.tabs.get(state.automationDashboardTabId).catch(() => null)
    : null;
  if (!dashboardTab) return state;
  const trackedTabIds = [state.automationTabId, state.prefetchedTabId, state.idleAutomationTabId]
    .filter((tabId) => Number.isInteger(tabId) && tabId !== dashboardTab.id);
  const trackedTabs = await Promise.all(trackedTabIds.map((tabId) => chrome.tabs.get(tabId).catch(() => null)));
  const reuseDashboardWindow = state.browserCollectionRemoteProfileEnabled === true;
  if (reuseDashboardWindow && !trackedTabs.some(Boolean)) {
    await chrome.storage.local.set({
      automationWindowId: dashboardTab.windowId,
      automationWindowOwned: false,
      automationWindowBackground: true,
    });
    return automationState();
  }
  const tabsToMove = trackedTabs.filter((tab) => tab
    && (reuseDashboardWindow
      ? tab.windowId !== dashboardTab.windowId
      : tab.windowId === dashboardTab.windowId));
  if (!tabsToMove.length && !reuseDashboardWindow) return state;

  const workerWindow = await ensureAutomationWindow({ ensureIdle: false });
  if (!workerWindow) return state;
  for (const tab of tabsToMove) {
    await chrome.tabs.move(tab.id, { windowId: workerWindow.id, index: -1 });
    const movedTab = await chrome.tabs.get(tab.id).catch(() => null);
    if (movedTab?.windowId !== workerWindow.id) {
      throw new Error(reuseDashboardWindow
        ? "采集页未能迁移到监控台 Chrome 窗口，暂不继续处理在途任务"
        : "采集页未能迁移到独立采集窗口，暂不继续处理在途任务");
    }
  }

  const taskTabIds = [state.automationTabId, state.prefetchedTabId]
    .filter((tabId) => Number.isInteger(tabId));
  await groupAutomationTabs(taskTabIds, workerWindow.id);
  if (Number.isInteger(state.automationTabId)) {
    await chrome.tabs.update(state.automationTabId, { active: true }).catch(() => undefined);
  }
  const migratedState = await automationState();
  if (!reuseDashboardWindow) await ensureIdleAutomationTab(workerWindow, migratedState);
  return automationState();
}

async function guardAutomationWindowFocus(windowId) {
  const state = await automationState();
  if (windowId !== state.automationWindowId
    || !state.automationWindowBackground
    || state.challengePaused
    || Number.isInteger(state.foregroundSingleTestItemId)
    || !Number.isFinite(Number(state.automationWindowFocusGuardUntil))
    || Number(state.automationWindowFocusGuardUntil) < Date.now()) return;
  if (state.browserCollectionWorkerMode === true) {
    await chrome.storage.local.remove("automationWindowFocusGuardUntil");
    return;
  }
  await chrome.windows.update(windowId, {
    focused: false,
    state: state.browserCollectionIsolatedProfileAuthorized === true
      || state.automationItem?.target_type === "STORE" ? "normal" : "minimized",
  }).catch(() => undefined);
  await chrome.storage.local.remove("automationWindowFocusGuardUntil");
}

async function browserCollectionWorkerEnabled() {
  if (!browserCollectionModeReady) return false;
  const state = await chrome.storage.local.get("browserCollectionWorkerMode");
  return state.browserCollectionWorkerMode === true;
}

async function syncDedicatedWorkerMode() {
  browserCollectionModeReady = false;
  dianxiaomiWorkerModeReady = false;
  const tabs = await chrome.tabs.query({
    url: [
      "http://127.0.0.1:3000/*",
      "http://localhost:3000/*",
      "http://192.168.66.115:3000/*",
      "http://127.0.0.1:8000/*",
      "http://localhost:8000/*",
      "https://galoremw.github.io/*",
      "https://*.onrender.com/*",
    ],
  });
  const isolatedLocalTab = tabs.find((tab) => isIsolatedBrowserCollectorUrl(tab.url));
  const remoteWorkerTab = tabs.find((tab) => {
    try {
      const url = new URL(tab.url);
      return url.searchParams.get("browser_collection") === "1"
        && !["127.0.0.1", "localhost"].includes(url.hostname)
        && !isIsolatedBrowserCollectorUrl(url.href);
    } catch { return false; }
  });
  const localDianxiaomiDashboard = tabs.find((tab) => {
    try {
      const url = new URL(tab.url);
      return ["127.0.0.1", "localhost"].includes(url.hostname)
        && url.searchParams.get("dianxiaomi_worker") === "1"
        && url.searchParams.get("dianxiaomi_local") === "1";
    } catch { return false; }
  });
  const dianxiaomiDashboard = isolatedLocalTab || remoteWorkerTab || localDianxiaomiDashboard;
  const isolatedLocalEnabled = Boolean(isolatedLocalTab);
  const remoteWorkerEnabled = Boolean(remoteWorkerTab);
  const remoteWorkerLabel = browserCollectionWorkerLabelFromUrl(remoteWorkerTab?.url);
  await chrome.storage.local.set({
    browserCollectionWorkerMode: isolatedLocalEnabled || remoteWorkerEnabled,
    browserCollectionLocalProfileEnabled: isolatedLocalEnabled,
    browserCollectionIsolatedProfileAuthorized: isolatedLocalEnabled,
    browserCollectionRemoteProfileEnabled: remoteWorkerEnabled,
    browserCollectionWorkerLabel: remoteWorkerLabel,
    ...((isolatedLocalTab || remoteWorkerTab)
      ? { automationDashboardTabId: (isolatedLocalTab || remoteWorkerTab).id }
      : {}),
    dianxiaomiWorkerMode: Boolean(dianxiaomiDashboard),
    dianxiaomiLocalProfileEnabled: Boolean(localDianxiaomiDashboard),
    ...(dianxiaomiDashboard ? {
      dianxiaomiDashboardTabId: dianxiaomiDashboard.id,
      dianxiaomiDashboardWindowId: dianxiaomiDashboard.windowId,
    } : {}),
  });
  if (!dianxiaomiDashboard) {
    await chrome.storage.local.remove(["dianxiaomiDashboardTabId", "dianxiaomiDashboardWindowId"]);
  }
  browserCollectionModeReady = true;
  dianxiaomiWorkerModeReady = true;
  return {
    browserCollectionEnabled: isolatedLocalEnabled || remoteWorkerEnabled,
    localDianxiaomiWorker: Boolean(localDianxiaomiDashboard),
  };
}

async function ensureWorkerModesInitialized() {
  if (browserCollectionModeReady && dianxiaomiWorkerModeReady) return;
  if (!workerModeInitialization) {
    workerModeInitialization = syncDedicatedWorkerMode().finally(() => {
      workerModeInitialization = null;
    });
  }
  await workerModeInitialization;
}

async function clearAutomationItem() {
  await chrome.storage.local.remove([
    "automationItem", "automationClaimToken", "automationItemPrefetched", "automationTaskKind", "reviewKnownKeys", "reviewProbeResult",
    "challengePaused", "identityReviewPaused", "challengeProvider",
  ]);
}

async function clearPrefetchedItem() {
  await chrome.storage.local.remove(["prefetchedTabId", "prefetchedItem", "prefetchedClaimToken", "prefetchedTaskKind"]);
}

async function releaseAutomationTab(tabId) {
  const state = await automationState();
  const existingIdleTab = state.idleAutomationTabId && state.idleAutomationTabId !== tabId
    ? await chrome.tabs.get(state.idleAutomationTabId).catch(() => null)
    : null;
  const existingIdleIsReusable = Boolean(existingIdleTab
    && existingIdleTab.windowId === state.automationWindowId
    && existingIdleTab.id !== state.automationDashboardTabId);
  if (state.automationTabId === tabId) {
    if (state.prefetchedItem && state.prefetchedTabId) {
      if (existingIdleIsReusable) await chrome.tabs.remove(existingIdleTab.id).catch(() => undefined);
      await chrome.storage.local.set({
        automationTabId: state.prefetchedTabId,
        automationItem: state.prefetchedItem,
        ...(state.prefetchedClaimToken ? { automationClaimToken: state.prefetchedClaimToken } : {}),
        automationItemPrefetched: true,
        automationTaskKind: state.prefetchedTaskKind || "PRODUCT",
        ...(state.prefetchedTaskKind === "REVIEW" ? { reviewKnownKeys: state.prefetchedReviewKnownKeys || [] } : {}),
      });
      await chrome.storage.local.remove("idleAutomationTabId");
      await chrome.storage.local.remove([
        "prefetchedTabId", "prefetchedItem", "prefetchedClaimToken", "prefetchedTaskKind", "prefetchedReviewKnownKeys",
      ]);
      await chrome.tabs.update(state.prefetchedTabId, { active: true }).catch(() => undefined);
      return;
    }
    if (existingIdleIsReusable) await chrome.tabs.remove(existingIdleTab.id).catch(() => undefined);
    await clearAutomationItem();
    await chrome.storage.local.remove("automationTabId");
    await chrome.storage.local.set({ idleAutomationTabId: tabId });
    return;
  }
  if (state.prefetchedTabId === tabId) {
    await clearPrefetchedItem();
    await chrome.storage.local.set({ idleAutomationTabId: existingIdleIsReusable ? existingIdleTab.id : tabId });
  }
}

async function holdPrefetchedItem(reason) {
  const state = await automationState();
  if (!state.prefetchedItem) return;
  await api(`/api/browser-collection/items/${state.prefetchedItem.id}/failure`, {
    method: "POST",
    body: JSON.stringify({
      ...(await browserCollectionWorkerIdentity()),
      claim_token: state.prefetchedClaimToken,
      error_type: "queue_paused",
      error_message: reason,
    }),
  }).catch(() => undefined);
  if (state.prefetchedTabId) {
    const prefetchedTabId = state.prefetchedTabId;
    await clearPrefetchedItem();
    await chrome.tabs.remove(prefetchedTabId).catch(() => undefined);
  } else {
    await clearPrefetchedItem();
  }
}

async function resetStaleAutomationState(run) {
  const state = await automationState();
  const serverItems = new Map((run?.items || []).map((item) => [Number(item.id), item]));
  if (state.automationItem && state.automationTaskKind !== "REVIEW") {
    const serverItem = serverItems.get(Number(state.automationItem.id));
    if (!serverItem || !["RUNNING", "NEEDS_VERIFICATION"].includes(serverItem.status)) {
      if (state.automationTabId) await releaseAutomationTab(state.automationTabId);
      else await clearAutomationItem();
    }
  }
  const refreshed = await automationState();
  if (refreshed.prefetchedItem) {
    const serverItem = serverItems.get(Number(refreshed.prefetchedItem.id));
    if (!serverItem || serverItem.status !== "RUNNING") {
      if (refreshed.prefetchedTabId) await releaseAutomationTab(refreshed.prefetchedTabId);
      else await clearPrefetchedItem();
    }
  }
  return automationState();
}

async function ensureAlarm() {
  const alarm = await chrome.alarms.get(POLL_ALARM);
  if (!alarm) chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
}

async function sendCollectMessage(
  tabId,
  targetType,
  targetDate,
  monitoredProductIds = undefined,
  storeProgressContext = null,
) {
  const message = {
    type: "collect-visible-page",
    target_type: targetType,
    target_date: targetDate,
    history_target_date: targetDate,
    ...(monitoredProductIds ? { monitored_product_ids: monitoredProductIds } : {}),
    ...(storeProgressContext ? {
      item_id: storeProgressContext.itemId,
      claim_token: storeProgressContext.claimToken,
      monitored_products: storeProgressContext.monitoredProducts || [],
      expected_store_id: storeProgressContext.expectedStoreId,
      expected_store_name: storeProgressContext.expectedStoreName || null,
      identity_monitored_product_ids: storeProgressContext.identityMonitoredProductIds || [],
      verified_store_identity: storeProgressContext.verifiedStoreIdentity || null,
      approved_store_url_identities: storeProgressContext.approvedStoreUrlIdentities || [],
    } : {}),
  };
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch {
    await injectCollector(tabId);
    return chrome.tabs.sendMessage(tabId, message);
  }
}

async function sendStoreCandidateScanMessage(tabId, item, storeId, pageUrl, storeIdentity, priorCandidates) {
  const message = {
    type: "STORE_CANDIDATE_SCAN",
    targetDate: item.target_date,
    item_id: item.id,
    claim_token: (await automationState()).automationClaimToken,
    expectedStoreId: storeId,
    expectedStoreName: item.title,
    monitoredProductIds: item.monitored_product_ids || [],
    expectedPageUrl: pageUrl,
    verifiedStoreIdentity: storeIdentity || null,
    approvedStoreUrlIdentities: item.approved_store_url_identities || [],
    priorCandidates: priorCandidates || [],
  };
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch {
    await injectCollector(tabId);
    return chrome.tabs.sendMessage(tabId, message);
  }
}

function safeStoreOrdersUrl(url, storeId, routeAliasId = null) {
  try {
    const parsed = new URL(url);
    const allowedIds = new Set([String(storeId), ...(routeAliasId ? [String(routeAliasId)] : [])]);
    const routeId = parsed.pathname.match(/^\/store\/(\d+)\/pages\/all-items\.html$/)?.[1];
    return /(?:^|\.)aliexpress\.com$/i.test(parsed.hostname)
      && parsed.protocol === "https:"
      && allowedIds.has(routeId)
      && parsed.searchParams.get("shop_sortType") === "orders_desc";
  } catch {
    return false;
  }
}

function storeOrdersPageIdentity(url) {
  try {
    const parsed = new URL(url);
    if (!/(?:^|\.)aliexpress\.com$/i.test(parsed.hostname)
      || parsed.protocol !== "https:"
      || parsed.searchParams.get("shop_sortType") !== "orders_desc") return null;
    const storeId = parsed.pathname.match(/^\/store\/(\d+)\/pages\/all-items\.html$/)?.[1];
    if (!storeId) return null;
    const pageParam = [...parsed.searchParams.entries()]
      .find(([key]) => ["page", "pagenum", "pageno"].includes(key.toLowerCase()));
    return { storeId, page: pageParam?.[1] || "1" };
  } catch {
    return null;
  }
}

function storeOrdersUrlIdentity(url) {
  try {
    const parsed = new URL(url);
    if (!/(?:^|\.)aliexpress\.com$/i.test(parsed.hostname)
      || parsed.protocol !== "https:"
      || parsed.username || parsed.password
      || (parsed.port && parsed.port !== "443")) return null;
    const routeStoreId = parsed.pathname.match(/^\/store\/(\d+)(?:\/pages\/all-items\.html)?$/)?.[1];
    if (!routeStoreId) return null;
    return {
      route_store_id: routeStoreId,
      normalized_page_identity: `${parsed.hostname.toLowerCase()}/store/${routeStoreId}/pages/all-items.html`,
    };
  } catch {
    return null;
  }
}

function normalizedStoreOrdersPagePath(value) {
  try {
    const raw = String(value || "");
    const parsed = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : `https://${raw}`);
    if (!/(?:^|\.)aliexpress\.com$/i.test(parsed.hostname)
      || parsed.username || parsed.password || (parsed.port && parsed.port !== "443")) return null;
    const routeStoreId = parsed.pathname.match(/^\/store\/(\d+)\/pages\/all-items\.html$/)?.[1];
    return routeStoreId ? `/store/${routeStoreId}/pages/all-items.html` : null;
  } catch {
    return null;
  }
}

function sameStoreOrdersPageIdentity(left, right) {
  const leftPath = normalizedStoreOrdersPagePath(left);
  return Boolean(leftPath && leftPath === normalizedStoreOrdersPagePath(right));
}

function storeIdentityReviewTarget(url) {
  try {
    const parsed = new URL(url);
    const routeStoreId = parsed.pathname.match(/^\/store\/(\d+)(?:\/|$)/)?.[1];
    if (parsed.protocol !== "https:"
      || !/(?:^|\.)aliexpress\.com$/i.test(parsed.hostname)
      || parsed.username || parsed.password || (parsed.port && parsed.port !== "443")
      || !routeStoreId) return null;
    const isOrdersPage = /^\/store\/\d+\/pages\/all-items\.html$/.test(parsed.pathname);
    const pageUrl = isOrdersPage
      ? parsed.href
      : new URL(`/store/${routeStoreId}/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc`, parsed.origin).href;
    return {
      route_store_id: routeStoreId,
      page_url: pageUrl,
      normalized_page_identity: `${parsed.hostname.toLowerCase()}/store/${routeStoreId}/pages/all-items.html`,
    };
  } catch {
    return null;
  }
}

function approvedStoreUrlIdentity(url, storeId, approvedIdentities = []) {
  const current = storeOrdersUrlIdentity(url);
  if (!current || !Array.isArray(approvedIdentities)) return null;
  return approvedIdentities.find((identity) =>
    String(identity?.route_store_id || "") === current.route_store_id
    && sameStoreOrdersPageIdentity(identity?.normalized_page_identity, current.normalized_page_identity)
  ) || null;
}

function storeOrdersListingUrlFromRoot(url, storeId, approvedIdentities = []) {
  const identity = storeOrdersUrlIdentity(url);
  if (!identity || !/^\/store\/\d+\/?$/.test(new URL(url).pathname)
    || (identity.route_store_id !== String(storeId)
      && !approvedStoreUrlIdentity(url, storeId, approvedIdentities))) return null;
  const parsed = new URL(url);
  parsed.pathname = `/store/${identity.route_store_id}/pages/all-items.html`;
  parsed.searchParams.set("sortType", "bestmatch_sort");
  parsed.searchParams.set("shop_sortType", "orders_desc");
  return parsed.href;
}

function normalizedStoreName(value) {
  return typeof value === "string"
    ? value.normalize("NFKC").trim().replace(/\s+/g, " ").toLocaleLowerCase()
    : "";
}

function storeIdentityMatchedProductIds(proof, monitoredProductIds = []) {
  const configuredIds = Array.isArray(monitoredProductIds) && monitoredProductIds.length
    ? monitoredProductIds : proof?.matched_product_ids || [];
  const targetIds = new Set(configuredIds.map(String));
  const visibleIds = new Set((Array.isArray(proof?.visible_product_ids) ? proof.visible_product_ids : []).map(String));
  return new Set((Array.isArray(proof?.matched_product_ids) ? proof.matched_product_ids : [])
    .map(String)
    .filter((productId) => targetIds.has(productId) && visibleIds.has(productId)));
}

function storeIdentityFingerprintIsVerified(
  proof,
  monitoredProductIds = [],
  expectedStoreName = null,
  approvedIdentities = [],
) {
  const matchedIds = storeIdentityMatchedProductIds(proof, monitoredProductIds);
  if (matchedIds.size >= 2) return true;
  if (matchedIds.size !== 1) return false;
  const referenceNames = [expectedStoreName || proof?.expected_store_name,
    ...(Array.isArray(approvedIdentities)
      ? approvedIdentities.map((identity) => identity?.evidence?.ixspy_store_name)
      : [])]
    .map(normalizedStoreName)
    .filter(Boolean);
  const observedNames = [proof?.ixspy_store_name, proof?.matched_store_name,
    ...(Array.isArray(proof?.observed_store_names) ? proof.observed_store_names : [])]
    .map(normalizedStoreName)
    .filter(Boolean);
  return observedNames.some((name) => referenceNames.includes(name));
}

function storeIdentityProofHasConflict(
  proof,
  storeId,
  monitoredProductIds = [],
  expectedStoreName = null,
  approvedIdentities = [],
) {
  if (storeIdentityFingerprintIsVerified(
    proof, monitoredProductIds, expectedStoreName, approvedIdentities,
  )) return false;
  const displayedIds = [proof?.heading_store_id, ...(proof?.labeled_store_ids || [])]
    .filter((value) => value != null).map(String);
  return displayedIds.some((id) => id !== String(storeId));
}

function storeRouteAliasIsVerified(
  url,
  storeId,
  approvedIdentities,
  proof,
  monitoredProductIds = [],
  expectedStoreName = null,
) {
  const page = storeOrdersPageIdentity(url);
  const routeStoreId = page?.storeId || storeOrdersUrlIdentity(url)?.route_store_id;
  if (!routeStoreId || routeStoreId === String(storeId) || !proof
    || proof.target_store_id !== String(storeId)
    || String(proof.route_store_id) !== routeStoreId
    || storeIdentityProofHasConflict(
      proof, storeId, monitoredProductIds, expectedStoreName, approvedIdentities,
    )) return false;
  const proofPage = storeOrdersUrlIdentity(proof.page_url);
  const currentPage = storeOrdersUrlIdentity(url);
  const sameRoutePage = proofPage?.route_store_id === currentPage?.route_store_id
    && sameStoreOrdersPageIdentity(proofPage?.normalized_page_identity, currentPage?.normalized_page_identity);
  const fingerprintVerified = storeIdentityFingerprintIsVerified(
    proof, monitoredProductIds, expectedStoreName, approvedIdentities,
  );
  if (fingerprintVerified && sameRoutePage) return true;
  const matchingDisplayedId = String(proof.heading_store_id || "") === String(storeId)
    || (Array.isArray(proof.labeled_store_ids)
      && proof.labeled_store_ids.map(String).includes(String(storeId)));
  if (matchingDisplayedId) return true;
  if (!sameRoutePage) return false;

  const approvedAlias = approvedStoreUrlIdentity(url, storeId, approvedIdentities);
  const savedName = approvedAlias?.evidence?.ixspy_store_name;
  const currentName = proof.ixspy_store_name;
  const namesConflict = Boolean(savedName && currentName
    && normalizedStoreName(savedName) !== normalizedStoreName(currentName));
  return Boolean(approvedAlias
    && proof.identity_method === "backend_approved_url_alias"
    && proofPage?.route_store_id === currentPage?.route_store_id
    && sameStoreOrdersPageIdentity(proofPage?.normalized_page_identity, currentPage?.normalized_page_identity)
    && !namesConflict);
}

function storeOrdersUrlAllowedForTask(
  url,
  storeId,
  approvedIdentities = [],
  identityProof = null,
  monitoredProductIds = [],
  expectedStoreName = null,
) {
  const page = storeOrdersPageIdentity(url);
  if (page && page.storeId === String(storeId)) return true;
  if (page && approvedStoreUrlIdentity(url, storeId, approvedIdentities)) return true;
  const currentIdentity = storeOrdersUrlIdentity(url);
  const approvedIdentity = approvedStoreUrlIdentity(url, storeId, approvedIdentities);
  const proofPage = storeOrdersUrlIdentity(identityProof?.page_url);
  const expectedIdentityMethod = currentIdentity?.route_store_id === String(storeId)
    ? "backend_approved_url_identity"
    : "backend_approved_url_alias";
  const savedName = approvedIdentity?.evidence?.ixspy_store_name;
  const currentName = identityProof?.ixspy_store_name;
  const namesConflict = Boolean(savedName && currentName
    && normalizedStoreName(savedName) !== normalizedStoreName(currentName));
  const isApprovedRootAlias = Boolean(page === null && currentIdentity && approvedIdentity && identityProof
    && identityProof.target_store_id === String(storeId)
    && String(identityProof.route_store_id) === currentIdentity.route_store_id
    && identityProof.page_url === url
    && identityProof.identity_method === expectedIdentityMethod
    && proofPage?.route_store_id === currentIdentity.route_store_id
    && sameStoreOrdersPageIdentity(proofPage?.normalized_page_identity, currentIdentity.normalized_page_identity)
    && sameStoreOrdersPageIdentity(approvedIdentity.normalized_page_identity, currentIdentity.normalized_page_identity)
    && !storeIdentityProofHasConflict(
      identityProof, storeId, monitoredProductIds, expectedStoreName, approvedIdentities,
    )
    && !namesConflict);
  if (isApprovedRootAlias) return true;

  const explicitIds = [identityProof?.heading_store_id, ...(identityProof?.labeled_store_ids || [])]
    .filter((value) => value != null).map(String);
  return Boolean(page && currentIdentity && identityProof
    && identityProof.target_store_id === String(storeId)
    && String(identityProof.route_store_id) === currentIdentity.route_store_id
    && identityProof.page_url === url
    && proofPage?.route_store_id === currentIdentity.route_store_id
    && sameStoreOrdersPageIdentity(proofPage?.normalized_page_identity, currentIdentity.normalized_page_identity)
    && ((explicitIds.includes(String(storeId))
      && !storeIdentityProofHasConflict(
        identityProof, storeId, monitoredProductIds, expectedStoreName, approvedIdentities,
      ))
      || storeIdentityFingerprintIsVerified(
        identityProof, monitoredProductIds, expectedStoreName, approvedIdentities,
      )));
}

const STORE_COLLECTION_START_URL_OVERRIDES = Object.freeze({
  // Temporary workaround: AliExpress does not consistently initialize Orders sort on the generated URL.
  "1105272224": "https://www.aliexpress.com/store/1105272224/pages/all-items.html?spm=a2g0o.store_pc_home.pcShopHead_2014623462415.1&shop_sortType=orders_desc",
});

function storeCollectionStartUrl(item) {
  const targetPage = storeOrdersPageIdentity(item?.target_url);
  const candidate = item?.collection_start_url;
  if (targetPage && candidate
    && storeOrdersUrlAllowedForTask(
      candidate, targetPage.storeId, item.approved_store_url_identities || [],
    )) return candidate;

  const override = targetPage
    ? STORE_COLLECTION_START_URL_OVERRIDES[targetPage.storeId]
    : null;
  if (override && safeStoreOrdersUrl(override, targetPage.storeId)) return override;
  return item?.target_url || null;
}

function inferredNextStoreOrdersUrl(url, storeId, routeAliasId = null) {
  const identity = storeOrdersPageIdentity(url);
  const allowedIds = new Set([String(storeId), ...(routeAliasId ? [String(routeAliasId)] : [])]);
  if (!identity || !allowedIds.has(identity.storeId)) return null;
  const parsed = new URL(url);
  const pageParam = [...parsed.searchParams.keys()]
    .find((key) => ["page", "pagenum", "pageno"].includes(key.toLowerCase())) || "page";
  const currentPage = parsed.searchParams.get(pageParam) || "1";
  if (!/^\d+$/.test(currentPage)) return null;
  parsed.searchParams.set(pageParam, String(Number(currentPage) + 1));
  return parsed.href;
}

function isPendingStorePage(tab, expectedUrl) {
  if (!tab?.pendingUrl) return false;
  const pendingPage = storeOrdersPageIdentity(tab.pendingUrl);
  const expectedPage = storeOrdersPageIdentity(expectedUrl);
  return Boolean(pendingPage && expectedPage
    && pendingPage.storeId === expectedPage.storeId
    && pendingPage.page === expectedPage.page);
}

function storeProgressKey(itemId) {
  return `${STORE_PROGRESS_STORAGE_PREFIX}${itemId}`;
}

function storeProgressForItem(progress, item, storeId, targets) {
  return progress
    && String(progress.run_id) === String(item.run_id)
    && String(progress.item_id) === String(item.id)
    && String(progress.store_id) === String(storeId)
    && progress.target_date === item.target_date
    && JSON.stringify(progress.target_ids) === JSON.stringify(targets)
    && progress.products && Array.isArray(progress.visited_page_urls);
}

function newStoreProgress(item, storeId, targets) {
  return {
    run_id: item.run_id,
    item_id: item.id,
    store_id: storeId,
    target_date: item.target_date,
    target_ids: targets,
    products: {},
    visited_page_urls: [],
    page_scans: [],
    chart_reads: [],
  };
}

async function loadStoreProgress(item, storeId, targets) {
  const key = storeProgressKey(item.id);
  const stored = await chrome.storage.local.get(key);
  const progress = storeProgressForItem(stored[key], item, storeId, targets)
    ? stored[key]
    : newStoreProgress(item, storeId, targets);
  if (!Array.isArray(progress.page_scans)) progress.page_scans = [];
  if (!Array.isArray(progress.chart_reads)) progress.chart_reads = [];
  const products = new Map(Object.entries(progress.products)
    .filter(([productId]) => targets.includes(productId)));
  return { key, progress, products };
}

function mergeStoreProduct(products, product) {
  const productId = String(product.platform_product_id);
  const previous = products.get(productId);
  if (!previous || (product.historical_sales || []).length > (previous.historical_sales || []).length) {
    products.set(productId, product);
  }
}

function addVisitedStorePage(progress, pageUrl) {
  if (pageUrl && !progress.visited_page_urls.includes(pageUrl)) {
    progress.visited_page_urls.push(pageUrl);
  }
}

function expectedStoreCollectionDates(targetDate) {
  const target = Date.parse(`${targetDate}T00:00:00Z`);
  return Number.isFinite(target)
    ? Array.from({ length: 7 }, (_, index) => new Date(target - (7 - index) * 86400000).toISOString().slice(0, 10))
    : [];
}

function candidateHistoryCheckpointFailures(product, expectedDates) {
  const history = product?.historical_sales;
  const dates = product?.history_expected_dates;
  const missingDates = product?.history_missing_dates;
  const diagnostic = product?.history_read_diagnostic;
  const failures = new Set();
  if (!Array.isArray(expectedDates) || expectedDates.length !== 7
    || !Array.isArray(dates) || dates.length !== 7
    || dates.some((date, index) => date !== expectedDates[index])) {
    failures.add("expected_dates");
  }
  if (!Array.isArray(history)) failures.add("historical_sales");
  if (!Array.isArray(missingDates)) failures.add("missing_dates");
  if (diagnostic?.product_id !== String(product?.platform_product_id || "")) {
    failures.add("diagnostic_product_id");
  }
  if (failures.size) return [...failures];

  const observedDates = new Set();
  for (const point of history) {
    if (!point || point.value_type !== "daily_increment"
      || !expectedDates.includes(point.date) || observedDates.has(point.date)
      || typeof point.value !== "number" || !Number.isFinite(point.value)
      || point.value < 0) {
      failures.add("daily_increment_points");
      continue;
    }
    observedDates.add(point.date);
  }
  const expectedMissingDates = expectedDates.filter((date) => !observedDates.has(date));
  if (missingDates.length !== expectedMissingDates.length
    || missingDates.some((date, index) => date !== expectedMissingDates[index])
    || diagnostic.point_count !== history.length
    || !Array.isArray(diagnostic.missing_dates)
    || diagnostic.missing_dates.length !== expectedMissingDates.length
    || diagnostic.missing_dates.some((date, index) => date !== expectedMissingDates[index])) {
    failures.add("history_diagnostic_counts");
  }

  if (diagnostic.outcome === "complete") {
    if (!(product.history_daily_capture_complete === true
      && history.length === 7
      && diagnostic.source === "ixspy_visible_daily_increment")) {
      failures.add("complete_history");
    }
  }
  else if (diagnostic.outcome === "partial") {
    if (!(product.history_daily_capture_complete === false
      && history.length > 0 && history.length < 7
      && diagnostic.source === "ixspy_visible_daily_increment")) {
      failures.add("partial_history");
    }
  }
  else if (diagnostic.outcome === "unavailable") {
    const allowedUnavailableSource = /^ixspy_store_(?:card_|product_chart_unavailable|candidate_chart_read_(?:error|timeout)|product_chart_read_timeout)/.test(diagnostic.source || "")
      || ["ixspy_data_unavailable", "visible_ixspy_daily_increment_unavailable"].includes(diagnostic.source);
    if (history.length !== 0 || product.history_daily_capture_complete !== false
      || !allowedUnavailableSource
      || (diagnostic.error && (typeof diagnostic.error !== "string" || diagnostic.error.length > 300))) {
      failures.add("unavailable_history");
    }
  } else {
    failures.add("history_outcome");
  }
  return [...failures];
}

function retainCompleteStoreProgress(progress, targetDate) {
  const expectedDates = expectedStoreCollectionDates(targetDate);
  progress.products = Object.fromEntries(
    Object.entries(progress.products || {}).filter(([, product]) =>
      product?.history_read_diagnostic?.outcome === "complete"
      && candidateHistoryCheckpointFailures(product, expectedDates).length === 0),
  );
  return progress;
}

function mergeStoreDiagnosticRecord(records, diagnostic, key, limit) {
  if (!diagnostic || !diagnostic[key]) return Array.isArray(records) ? records : [];
  const existing = Array.isArray(records) ? records : [];
  const previous = existing.find((record) => record?.[key] === diagnostic[key]);
  let merged = diagnostic;
  if (previous && key === "url") {
    merged = { ...previous, ...diagnostic };
    for (const field of ["visible_product_ids", "matched_product_ids"]) {
      if (Array.isArray(previous[field]) || Array.isArray(diagnostic[field])) {
        merged[field] = [...new Set([
          ...(previous[field] || []),
          ...(diagnostic[field] || []),
        ].map(String))];
      }
    }
    if (Array.isArray(previous.identity_matches) || Array.isArray(diagnostic.identity_matches)) {
      const matches = new Map();
      for (const match of [...(previous.identity_matches || []), ...(diagnostic.identity_matches || [])]) {
        if (!match || typeof match !== "object") continue;
        const identity = JSON.stringify([
          match.monitored_product_id,
          match.observed_product_id,
          match.match_method,
          match.page_url,
        ]);
        matches.set(identity, match);
      }
      merged.identity_matches = [...matches.values()];
    }
  }
  return [...existing.filter((record) => record?.[key] !== diagnostic[key]), merged].slice(-limit);
}

function mergeStoreDiagnostics(progress, diagnostics) {
  if (diagnostics?.page_scan) {
    progress.page_scans = mergeStoreDiagnosticRecord(progress.page_scans, diagnostics.page_scan, "url", 10);
  }
  for (const chartRead of diagnostics?.chart_reads || []) {
    progress.chart_reads = mergeStoreDiagnosticRecord(progress.chart_reads, chartRead, "product_id", 20);
  }
}

async function saveStoreProductProgress(message, sender, sendResponse) {
  try {
    const state = await automationState();
    const item = state.automationItem;
    const storeId = item?.target_url
      ? new URL(item.target_url).pathname.match(/^\/store\/(\d+)\/pages\/all-items\.html$/)?.[1]
      : null;
    const product = message.product;
    const productId = String(product?.platform_product_id || "");
    const targets = [...new Set((item?.monitored_product_ids || []).map(String))].sort();
    const pageUrl = sender?.tab?.url;
    const routeId = storeOrdersPageIdentity(pageUrl)?.storeId;
    const identity = product?.store_identity;
    const routeAliasIsVerified = routeId !== storeId
      && storeRouteAliasIsVerified(
        pageUrl, storeId, item?.approved_store_url_identities, identity, targets, item?.title,
      );
    if (!item || item.target_type !== "STORE"
      || Number(item.id) !== Number(message.item_id)
      || state.automationTabId !== sender?.tab?.id
      || state.automationClaimToken !== message.claim_token
      || !storeId || !targets.includes(productId)
      || !safeStoreOrdersUrl(pageUrl, storeId, routeAliasIsVerified ? routeId : null)
      || productIdFromUrl(product?.url) !== productId) {
      sendResponse({ ok: false });
      return;
    }

    const { key, progress, products } = await loadStoreProgress(item, storeId, targets);
    mergeStoreProduct(products, product);
    progress.products = Object.fromEntries(products);
    if (product.history_read_diagnostic) {
      progress.chart_reads = mergeStoreDiagnosticRecord(
        progress.chart_reads, product.history_read_diagnostic, "product_id", 20,
      );
    }
    addVisitedStorePage(progress, pageUrl);
    await chrome.storage.local.set({ [key]: progress });
    sendResponse({ ok: true });
  } catch {
    sendResponse({ ok: false });
  }
}

async function saveStoreCandidateProgress(message, sender, sendResponse) {
  try {
    const state = await automationState();
    const item = state.automationItem;
    const storeId = item?.target_url
      ? new URL(item.target_url).pathname.match(/^\/store\/(\d+)\/pages\/all-items\.html$/)?.[1]
      : null;
    const product = message.product;
    const productId = String(product?.platform_product_id || "");
    const targets = [...new Set((item?.monitored_product_ids || []).map(String))].sort();
    const pageUrl = sender?.tab?.url;
    const routePage = storeOrdersPageIdentity(pageUrl);
    const expectedPage = storeOrdersPageIdentity(message.expected_page_url);
    const taskPage = storeOrdersPageIdentity(item?.discovery_page_url || item?.target_url);
    const identity = product?.store_identity;
    const routeAliasIsVerified = routePage?.storeId !== storeId
      && storeRouteAliasIsVerified(
        pageUrl, storeId, item?.approved_store_url_identities, identity, targets, item?.title,
      );
    const pageMatchesTask = Boolean(routePage && expectedPage && taskPage
      && routePage.page === expectedPage.page
      && routePage.page === taskPage.page
      && message.expected_page_url === pageUrl);
    const requiredDates = expectedStoreCollectionDates(item?.target_date);
    const contextValid = Boolean(item && item.target_type === "STORE"
      && Number(item.id) === Number(message.item_id)
      && state.automationTabId === sender?.tab?.id
      && state.automationClaimToken === message.claim_token
      && message.expected_store_id === storeId
      && pageMatchesTask
      && safeStoreOrdersUrl(pageUrl, storeId, routeAliasIsVerified ? routePage.storeId : null));
    if (!contextValid) {
      sendResponse({
        ok: false,
        reason: "candidate_context_invalid",
        message: "候选商品进度与当前店铺任务页面不匹配",
      });
      return;
    }
    const identityValid = Boolean(identity
      && identity.target_store_id === storeId
      && String(identity.route_store_id) === routePage?.storeId
      && identity.page_url === pageUrl
      && (routePage.storeId === storeId || routeAliasIsVerified));
    if (!identityValid) {
      sendResponse({
        ok: false,
        reason: "candidate_identity_invalid",
        message: "候选商品店铺身份校验失败",
      });
      return;
    }
    if (productIdFromUrl(product?.url) !== productId) {
      sendResponse({
        ok: false,
        reason: "candidate_identity_invalid",
        message: "候选商品链接 ID 与商品 ID 不一致",
      });
      return;
    }
    const historyFailures = candidateHistoryCheckpointFailures(product, requiredDates);
    if (historyFailures.length) {
      sendResponse({
        ok: false,
        reason: "candidate_history_invalid",
        failed_checks: historyFailures,
        message: `候选商品七日日增量检查点无效：${historyFailures.join(", ")}`,
      });
      return;
    }

    const { key, progress } = await loadStoreProgress(item, storeId, targets);
    const saved = progress.candidate_page_progress;
    const savedPage = storeOrdersPageIdentity(saved?.page_url);
    const samePageProgress = saved
      && String(saved.run_id) === String(item.run_id)
      && String(saved.item_id) === String(item.id)
      && saved.target_date === item.target_date
      && String(saved.store_id) === storeId
      && saved.page_url === pageUrl
      && savedPage?.page === routePage.page;
    const candidatePageProgress = samePageProgress
      ? saved
      : {
        run_id: item.run_id,
        item_id: item.id,
        store_id: storeId,
        target_date: item.target_date,
        page_url: pageUrl,
        products: {},
      };
    if (!candidatePageProgress.run_id) candidatePageProgress.run_id = item.run_id;
    if (!candidatePageProgress.item_id) candidatePageProgress.item_id = item.id;
    candidatePageProgress.products = {
      ...(candidatePageProgress.products || {}),
      [productId]: product,
    };
    progress.candidate_page_progress = candidatePageProgress;
    addVisitedStorePage(progress, pageUrl);
    await chrome.storage.local.set({ [key]: progress });
    sendResponse({ ok: true });
  } catch {
    sendResponse({ ok: false });
  }
}

async function waitForStorePage(
  tabId,
  expectedUrl,
  timeoutMs = STORE_PAGE_NAVIGATION_TIMEOUT_MS,
  expectedStoreId = null,
  approvedStoreUrlIdentities = [],
  monitoredProductIds = [],
  expectedStoreName = null,
) {
  const expectedPage = storeOrdersPageIdentity(expectedUrl);
  const deadline = Date.now() + timeoutMs;
  let rootListingRecoveryAttempted = false;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab && [tab.url, tab.pendingUrl].some(isAliExpressChallengePage)) {
      const error = new Error("AliExpress 要求人工完成验证");
      error.code = "PLATFORM_CHALLENGE";
      throw error;
    }
    if (tab?.status === "complete" && !tab.pendingUrl) {
      const targetStoreId = String(expectedStoreId || expectedPage?.storeId || "");
      const rootListingUrl = !rootListingRecoveryAttempted
        ? storeOrdersListingUrlFromRoot(tab.url, targetStoreId, approvedStoreUrlIdentities)
        : null;
      if (rootListingUrl) {
        rootListingRecoveryAttempted = true;
        await chrome.tabs.update(tabId, { url: rootListingUrl, active: true }).catch(() => null);
        await new Promise((resolve) => setTimeout(resolve, 250));
        continue;
      }
      const currentPage = storeOrdersPageIdentity(tab.url);
      if (expectedPage && currentPage && currentPage.page === expectedPage.page) {
        if (currentPage.storeId === targetStoreId) {
          return {
            ...currentPage,
            store_identity: {
              target_store_id: targetStoreId,
              route_store_id: currentPage.storeId,
              page_url: tab.url,
              heading_store_id: null,
              labeled_store_ids: [],
            },
          };
        }
        const identity = await sendCollectMessage(tabId, "STORE_IDENTITY", null, undefined, {
          expectedStoreId: targetStoreId,
          expectedStoreName,
          identityMonitoredProductIds: monitoredProductIds,
          approvedStoreUrlIdentities,
        }).catch(() => null);
        const proof = identity?.store_identity;
        const labeledStoreIds = Array.isArray(proof?.labeled_store_ids)
          ? proof.labeled_store_ids.map(String)
          : [];
        const headingMismatch = proof?.heading_store_id != null
          && String(proof.heading_store_id) !== targetStoreId;
        const labeledMismatch = labeledStoreIds.some((id) => id !== targetStoreId);
        const matchingDisplayedId = String(proof?.heading_store_id || "") === targetStoreId
          || labeledStoreIds.includes(targetStoreId);
        const approvedAlias = approvedStoreUrlIdentity(tab.url, targetStoreId, approvedStoreUrlIdentities);
        const savedName = approvedAlias?.evidence?.ixspy_store_name;
        const currentName = proof?.ixspy_store_name;
        const namesConflict = Boolean(savedName && currentName
          && normalizedStoreName(savedName) !== normalizedStoreName(currentName));
        const expectedApprovedMethod = currentPage.storeId === targetStoreId
          ? "backend_approved_url_identity"
          : "backend_approved_url_alias";
        const backendAliasProof = Boolean(approvedAlias
          && identity?.state === "ready"
          && identity.platform_store_id === targetStoreId
          && proof?.identity_method === expectedApprovedMethod
          && proof?.target_store_id === targetStoreId
          && String(proof.route_store_id) === currentPage.storeId
          && proof.page_url === tab.url
          && !storeIdentityProofHasConflict(
            proof, targetStoreId, monitoredProductIds, expectedStoreName, approvedStoreUrlIdentities,
          )
          && !namesConflict);
        const fingerprintProof = Boolean(identity?.state === "ready"
          && identity.platform_store_id === targetStoreId
          && proof?.target_store_id === targetStoreId
          && String(proof.route_store_id) === currentPage.storeId
          && proof.page_url === tab.url
          && storeIdentityFingerprintIsVerified(
            proof, monitoredProductIds, expectedStoreName, approvedStoreUrlIdentities,
          ));
        if (identity?.state === "ready" && identity.platform_store_id === targetStoreId
          && proof?.target_store_id === targetStoreId
          && String(proof.route_store_id) === currentPage.storeId
          && proof.page_url === tab.url
          && ((matchingDisplayedId && !headingMismatch && !labeledMismatch) || fingerprintProof)) {
          return { ...currentPage, store_identity: proof };
        }
        if (backendAliasProof) {
          return { ...currentPage, store_identity: proof };
        }
        if (identity?.state === "identity_review"
          || identity?.error_type === "store_identity_mismatch"
          || identity?.error_type === "store_identity_unverified") {
          const error = new Error(identity.message || "页面店铺身份与任务目标不一致");
          error.code = "STORE_IDENTITY_REVIEW";
          error.error_type = identity.error_type || "store_identity_unverified";
          error.page_url = identity.page_url || tab.url;
          error.evidence = identity.evidence || {
            route_store_id: currentPage.storeId,
            heading_store_id: proof?.heading_store_id || null,
            labeled_store_ids: labeledStoreIds,
            page_title: null,
            ixspy_store_name: proof?.ixspy_store_name || null,
            identity_method: "unverified",
          };
          throw error;
        }
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("店铺下一页未在限定时间内加载完成");
}

function storeCollectionWorkRemaining(startedAt) {
  return Math.max(0, STORE_COLLECTION_WORK_MS - (Date.now() - startedAt));
}

async function collectStorePages(tabId, item) {
  const storeId = new URL(item.target_url).pathname.match(/^\/store\/(\d+)\/pages\/all-items\.html$/)?.[1];
  const targets = [...new Set((item.monitored_product_ids || []).map(String))].sort();
  if (!storeId || !safeStoreOrdersUrl(item.target_url, storeId)) {
    return { state: "failed", message: "店铺任务缺少有效的 Orders 地址" };
  }
  const targetSet = new Set(targets);
  const { key: progressKey, progress, products } = await loadStoreProgress(item, storeId, targets);
  const approvedStoreUrlIdentities = item.approved_store_url_identities || [];
  let routeStoreId = storeId;
  let storeIdentity = null;
  const visited = new Set(progress.visited_page_urls.filter((pageUrl) =>
    storeOrdersUrlAllowedForTask(
      pageUrl, storeId, approvedStoreUrlIdentities, progress.store_identity, targets, item.title,
    )));
  const visitedThisAttempt = new Set();
  const startedAt = Date.now();
  let collectionInterrupted = false;
  for (let page = 0; targets.length && products.size < targets.length
    && page < 10 && Date.now() - startedAt < STORE_COLLECTION_WORK_MS; page += 1) {
    let tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) return { state: "failed", message: "采集标签页已关闭，无法恢复目标店铺 Orders 页" };
    if (isAliExpressChallengePage(tab.url)) {
      return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    }
    const currentPage = storeOrdersPageIdentity(tab.url);
    if (!storeIdentity && currentPage && currentPage.storeId !== storeId
      && storeOrdersUrlAllowedForTask(
        tab.url, storeId, approvedStoreUrlIdentities, progress.store_identity, targets, item.title,
      )) {
      storeIdentity = progress.store_identity;
    }
    const isKnownStorePage = storeOrdersUrlAllowedForTask(
      tab.url, storeId, approvedStoreUrlIdentities, storeIdentity, targets, item.title,
    );
    const isPotentialAliasPage = currentPage && currentPage.storeId !== storeId;
    if (!isKnownStorePage) {
      const recoveryBudgetMs = Math.min(STORE_PAGE_NAVIGATION_TIMEOUT_MS, storeCollectionWorkRemaining(startedAt));
      if (recoveryBudgetMs <= 0) {
        return { state: "failed", message: "店铺采集页地址异常，任务时间不足以恢复 Orders 页" };
      }
      try {
        if (!isPotentialAliasPage && !isPendingStorePage(tab, item.target_url)) {
          await chrome.tabs.update(tabId, { url: item.target_url, active: true });
        }
        const recoveryUrl = isPotentialAliasPage ? tab.url : item.target_url;
        const readyPage = await waitForStorePage(
          tabId, recoveryUrl, recoveryBudgetMs, storeId, approvedStoreUrlIdentities, targets, item.title,
        );
        routeStoreId = readyPage.storeId;
        storeIdentity = readyPage.store_identity || storeIdentity;
      } catch (error) {
        if (error?.code === "PLATFORM_CHALLENGE") {
          return { state: "challenge", message: error.message };
        }
        if (error?.code === "STORE_IDENTITY_REVIEW") {
          return {
            state: "identity_review",
            error_type: error.error_type,
            message: error.message,
            page_url: error.page_url,
            evidence: error.evidence,
          };
        }
        if (error?.code === "STORE_IDENTITY_UNVERIFIED") {
          return { state: "store_identity_unverified", error_type: "store_identity_unverified", message: error.message };
        }
        const currentTab = await chrome.tabs.get(tabId).catch(() => null);
        const currentUrl = String(currentTab?.url || "标签页已关闭").slice(0, 240);
        return {
          state: "failed",
          error_type: error?.code === "STORE_IDENTITY_MISMATCH" ? "store_identity_mismatch" : "page_not_ready",
          message: error?.code === "STORE_IDENTITY_MISMATCH"
            ? `${error.message}，当前页面：${currentUrl}；已停止采集，请核实监控店铺链接。`
            : `验证后未能恢复目标店铺 Orders 页（当前页面：${currentUrl}；${error?.message || "导航失败"}）`,
        };
      }
      tab = await chrome.tabs.get(tabId).catch(() => null);
      if (!tab || !storeOrdersUrlAllowedForTask(
        tab.url, storeId, approvedStoreUrlIdentities, storeIdentity, targets, item.title,
      )) {
        const currentUrl = String(tab?.url || "标签页已关闭").slice(0, 240);
        return { state: "failed", message: `恢复后仍未到达目标店铺 Orders 页（当前页面：${currentUrl}）` };
      }
    }
    if (visitedThisAttempt.has(tab.url)) break;
    const pageResponseBudgetMs = Math.min(
      STORE_PAGE_RESPONSE_TIMEOUT_MS,
      storeCollectionWorkRemaining(startedAt),
    );
    if (pageResponseBudgetMs <= 0) {
      collectionInterrupted = true;
      break;
    }
    visitedThisAttempt.add(tab.url);
    visited.add(tab.url);
    addVisitedStorePage(progress, tab.url);
    let response;
    try {
      response = await withTimeout(
        sendCollectMessage(tabId, "STORE", item.target_date,
          targets.filter((productId) => !products.has(productId)), {
            itemId: item.id,
            claimToken: (await automationState()).automationClaimToken,
            monitoredProducts: item.monitored_products || [],
            expectedStoreId: storeId,
            expectedStoreName: item.title,
            identityMonitoredProductIds: targets,
            verifiedStoreIdentity: storeIdentity,
            approvedStoreUrlIdentities,
          }),
        pageResponseBudgetMs,
        "店铺列表页读取超时",
      );
    } catch (error) {
      if (error?.code !== "COLLECTION_TIMEOUT") throw error;
      const latest = await loadStoreProgress(item, storeId, targets);
      for (const product of latest.products.values()) mergeStoreProduct(products, product);
      for (const pageUrl of latest.progress.visited_page_urls) visited.add(pageUrl);
      for (const scan of latest.progress.page_scans) {
        progress.page_scans = mergeStoreDiagnosticRecord(progress.page_scans, scan, "url", 10);
      }
      for (const chartRead of latest.progress.chart_reads) {
        progress.chart_reads = mergeStoreDiagnosticRecord(progress.chart_reads, chartRead, "product_id", 20);
      }
      collectionInterrupted = true;
      break;
    }
    const diagnostics = response?.diagnostics || response?.page_result?.diagnostics;
    mergeStoreDiagnostics(progress, diagnostics);
    if (response?.state !== "ready" || !response.page_result) {
      const latest = await loadStoreProgress(item, storeId, targets);
      for (const product of latest.products.values()) mergeStoreProduct(products, product);
      for (const pageUrl of latest.progress.visited_page_urls) visited.add(pageUrl);
      for (const scan of latest.progress.page_scans) {
        progress.page_scans = mergeStoreDiagnosticRecord(progress.page_scans, scan, "url", 10);
      }
      for (const chartRead of latest.progress.chart_reads) {
        progress.chart_reads = mergeStoreDiagnosticRecord(progress.chart_reads, chartRead, "product_id", 20);
      }
      progress.products = Object.fromEntries(products);
      progress.visited_page_urls = [...visited];
      await chrome.storage.local.set({ [progressKey]: progress });
      return response;
    }
    const result = response.page_result;
    if (result.platform_store_id !== storeId || result.url !== tab.url
      || (result.store_identity?.target_store_id && result.store_identity.target_store_id !== storeId)) {
      return { state: "failed", message: "店铺列表页与目标店铺不匹配" };
    }
    const resultRouteId = storeOrdersPageIdentity(tab.url)?.storeId
      || storeOrdersUrlIdentity(tab.url)?.route_store_id;
    if (!resultRouteId || (routeStoreId !== storeId && routeStoreId !== resultRouteId)) {
      return { state: "failed", error_type: "store_identity_mismatch", message: "店铺路由别名在任务中发生变化" };
    }
    routeStoreId = resultRouteId;
    storeIdentity = result.store_identity || storeIdentity;
    progress.route_store_id = routeStoreId;
    progress.store_identity = storeIdentity;
    for (const product of result.products || []) {
      if (!targetSet.has(String(product.platform_product_id))) {
        return { state: "failed", message: "页面返回非监控池商品" };
      }
      mergeStoreProduct(products, product);
    }
    mergeStoreDiagnostics(progress, result.diagnostics);
    const nextUrl = result.next_page_url || (result.has_next_page
      ? inferredNextStoreOrdersUrl(tab.url, storeId, routeStoreId)
      : null);
    const pageScan = result.diagnostics?.page_scan || {};
    const matchedProductIds = (result.products || [])
      .map((product) => String(product.platform_product_id))
      .filter((productId) => targetSet.has(productId));
    progress.page_scans = mergeStoreDiagnosticRecord(progress.page_scans, {
      ...pageScan,
      url: pageScan.url || tab.url,
      scan_source: pageScan.url ? (pageScan.scan_source || "page_collector") : "worker_fallback",
      result_product_count: (result.products || []).length,
      matched_product_ids: [...new Set([...(pageScan.matched_product_ids || []), ...matchedProductIds].map(String))],
      unmatched_target_ids: targets.filter((productId) => !products.has(productId)),
      pagination_known: pageScan.pagination_known === true
        || result.pagination_known === true
        || result.has_next_page === true
        || Boolean(result.next_page_url),
      has_next_page: Boolean(result.has_next_page),
      next_page_url: nextUrl,
      route_store_id: routeStoreId,
      store_identity: pageScan.store_identity || storeIdentity,
    }, "url", 10);
    progress.products = Object.fromEntries(products);
    progress.visited_page_urls = [...visited];
    await chrome.storage.local.set({ [progressKey]: progress });
    if (result.diagnostics?.collection_interrupted === true) {
      collectionInterrupted = true;
      break;
    }
    if (products.size === targets.length || !result.has_next_page) break;
    if (!nextUrl || !storeOrdersUrlAllowedForTask(
      nextUrl, storeId, approvedStoreUrlIdentities, storeIdentity, targets, item.title,
    ) || visitedThisAttempt.has(nextUrl)) break;
    const navigationBudgetMs = Math.min(STORE_PAGE_NAVIGATION_TIMEOUT_MS, storeCollectionWorkRemaining(startedAt));
    if (navigationBudgetMs <= 0) {
      collectionInterrupted = true;
      break;
    }
    await chrome.tabs.update(tabId, { url: nextUrl, active: true });
    const readyPage = await waitForStorePage(
      tabId, nextUrl, navigationBudgetMs, storeId, approvedStoreUrlIdentities, targets, item.title,
    );
    routeStoreId = readyPage.storeId;
    storeIdentity = readyPage.store_identity || storeIdentity;
  }

  let candidatePage = null;
  const discoveryUrl = item.discovery_page_url || item.target_url;
  if (!collectionInterrupted && targets.length < 20 && discoveryUrl) {
    const incompleteCandidatePage = (outcome, message = null, diagnostics = {}) => ({
      page_url: discoveryUrl,
      pagination_known: false,
      has_next_page: false,
      next_page_url: null,
      scan_complete: false,
      candidates: [],
      diagnostics: { outcome, ...(message ? { message: String(message).slice(0, 300) } : {}), ...diagnostics },
    });
    if (!storeOrdersUrlIdentity(discoveryUrl)) {
      candidatePage = incompleteCandidatePage("invalid_cursor_url");
    } else {
      const cursorIsVerified = storeOrdersUrlAllowedForTask(
        discoveryUrl, storeId, approvedStoreUrlIdentities, storeIdentity, targets, item.title,
      );
      const cursorIsVisited = visited.has(discoveryUrl);
      if (!cursorIsVisited && visited.size >= 11) {
        candidatePage = incompleteCandidatePage("unique_page_limit_reached");
      } else if (storeCollectionWorkRemaining(startedAt) <= 0) {
        candidatePage = incompleteCandidatePage("work_budget_exceeded");
      } else {
        let pageReady = false;
        const tab = await chrome.tabs.get(tabId).catch(() => null);
        if (!tab) {
          candidatePage = incompleteCandidatePage("collector_tab_unavailable");
        } else {
          try {
            const needsCursorNavigation = tab.url !== discoveryUrl || tab.pendingUrl;
            if (needsCursorNavigation) {
              if (!isPendingStorePage(tab, discoveryUrl)) {
                await chrome.tabs.update(tabId, { url: discoveryUrl, active: true });
              }
            }
            if (needsCursorNavigation || !cursorIsVerified) {
              const navigationBudgetMs = Math.min(STORE_PAGE_NAVIGATION_TIMEOUT_MS, storeCollectionWorkRemaining(startedAt));
              if (navigationBudgetMs <= 0) throw new Error("店铺任务工作时间预算已用完");
              const readyPage = await waitForStorePage(
                tabId, discoveryUrl, navigationBudgetMs, storeId, approvedStoreUrlIdentities, targets, item.title,
              );
              routeStoreId = readyPage.storeId;
              storeIdentity = readyPage.store_identity || storeIdentity;
            }
            pageReady = true;
          } catch (error) {
            if (error?.code === "PLATFORM_CHALLENGE") {
              return { state: "challenge", message: error.message };
            }
            if (error?.code === "STORE_IDENTITY_UNVERIFIED") {
              return { state: "store_identity_unverified", error_type: "store_identity_unverified", message: error.message };
            }
            if (error?.code === "STORE_IDENTITY_REVIEW") {
              return {
                state: "identity_review",
                error_type: error.error_type,
                message: error.message,
                page_url: error.page_url,
                evidence: error.evidence,
              };
            }
            if (error?.code === "STORE_IDENTITY_MISMATCH") {
              return {
                state: "failed",
                error_type: "store_identity_mismatch",
                message: `身份探测：${error.message}`,
              };
            }
            candidatePage = incompleteCandidatePage(
              error?.code === "STORE_IDENTITY_MISMATCH" ? "page_identity_mismatch" : "page_load_timeout",
              error?.message,
            );
          }
        }
        if (pageReady) {
          const currentTab = await chrome.tabs.get(tabId).catch(() => null);
          if (!currentTab || !storeOrdersUrlAllowedForTask(
            currentTab.url, storeId, approvedStoreUrlIdentities, storeIdentity, targets, item.title,
          )
            || storeOrdersPageIdentity(currentTab.url)?.page !== storeOrdersPageIdentity(discoveryUrl)?.page) {
            candidatePage = incompleteCandidatePage("cursor_page_mismatch");
          } else {
            const candidatePageUrl = currentTab.url;
            visited.add(candidatePageUrl);
            addVisitedStorePage(progress, candidatePageUrl);
            const savedCandidateProgress = progress.candidate_page_progress;
            const savedPageIdentity = storeOrdersPageIdentity(savedCandidateProgress?.page_url);
            const currentPageIdentity = storeOrdersPageIdentity(candidatePageUrl);
            const priorCandidates = savedCandidateProgress
              && savedCandidateProgress.target_date === item.target_date
              && String(savedCandidateProgress.store_id) === storeId
              && savedPageIdentity?.page === currentPageIdentity?.page
              && String(savedPageIdentity?.storeId) === String(currentPageIdentity?.storeId)
              ? Object.values(savedCandidateProgress.products || {})
              : [];
            try {
              const responseBudgetMs = Math.min(
                STORE_CANDIDATE_SCAN_RESPONSE_TIMEOUT_MS,
                storeCollectionWorkRemaining(startedAt),
              );
              if (responseBudgetMs <= 0) throw new Error("店铺任务工作时间预算已用完");
              const response = await withTimeout(
                sendStoreCandidateScanMessage(
                  tabId, item, storeId, candidatePageUrl, storeIdentity, priorCandidates,
                ),
                responseBudgetMs,
                "店铺候选页读取超时",
              );
              if (["challenge", "ixspy_login", "store_identity_unverified", "identity_review"].includes(response?.state)) {
                return response;
              }
              if (response?.error_type === "store_identity_unverified") {
                return { state: "store_identity_unverified", error_type: "store_identity_unverified", message: response.message };
              }
              if (response?.error_type === "store_identity_mismatch") {
                return { state: "failed", error_type: "store_identity_mismatch", message: response.message };
              }
              const pageResult = response?.page_result;
              if (response?.state !== "ready" || !pageResult) {
                candidatePage = incompleteCandidatePage(
                  "page_scan_failed", response?.message || "候选页未能完整读取",
                  response?.diagnostics ? { collector: response.diagnostics } : {},
                );
              } else if (String(pageResult.platform_store_id) !== storeId || pageResult.url !== candidatePageUrl) {
                candidatePage = incompleteCandidatePage("page_identity_mismatch", null, {
                  actual_store_id: pageResult.platform_store_id || null,
                  actual_url: pageResult.url || null,
                });
              } else {
                const scanComplete = pageResult.scan_complete === true;
                storeIdentity = pageResult.store_identity || storeIdentity;
                progress.route_store_id = routeStoreId;
                progress.store_identity = storeIdentity;
              const nextPageUrl = storeOrdersUrlAllowedForTask(
                pageResult.next_page_url, storeId, approvedStoreUrlIdentities, storeIdentity, targets, item.title,
              )
                  ? pageResult.next_page_url
                  : null;
                candidatePage = {
                  page_url: candidatePageUrl,
                  pagination_known: pageResult.pagination_known === true,
                  has_next_page: Boolean(pageResult.has_next_page),
                  next_page_url: nextPageUrl,
                  scan_complete: scanComplete,
                  candidates: (scanComplete || pageResult.diagnostics?.outcome === "chart_read_timeout")
                    && Array.isArray(pageResult.candidates)
                    ? pageResult.candidates.slice(0, 50)
                    : [],
                  diagnostics: pageResult.diagnostics || {},
                };
                if (pageResult.has_next_page && !nextPageUrl) {
                  candidatePage.scan_complete = false;
                  candidatePage.candidates = [];
                  candidatePage.diagnostics = {
                    ...candidatePage.diagnostics,
                    outcome: "invalid_next_page_url",
                  };
                }
              }
            } catch (error) {
              const latest = await loadStoreProgress(item, storeId, targets);
              const saved = latest.progress.candidate_page_progress;
              const savedPage = storeOrdersPageIdentity(saved?.page_url);
              const currentPage = storeOrdersPageIdentity(candidatePageUrl);
              const samePageCheckpoint = saved
                && String(saved.run_id) === String(item.run_id)
                && String(saved.item_id) === String(item.id)
                && saved.target_date === item.target_date
                && String(saved.store_id) === storeId
                && saved.page_url === candidatePageUrl
                && savedPage?.page === currentPage?.page
                && String(savedPage?.storeId) === String(currentPage?.storeId);
              const expectedDates = expectedStoreCollectionDates(item.target_date);
              const checkpointCandidates = samePageCheckpoint
                ? Object.values(saved.products || {}).filter((candidate) => {
                  const productId = String(candidate?.platform_product_id || "");
                  const identity = candidate?.store_identity;
                  const routeAliasVerified = currentPage?.storeId !== storeId
                    && storeRouteAliasIsVerified(
                      candidatePageUrl, storeId, approvedStoreUrlIdentities, identity, targets, item.title,
                    );
                  return productId
                    && productIdFromUrl(candidate?.url) === productId
                    && identity?.target_store_id === storeId
                    && String(identity.route_store_id) === String(currentPage?.storeId)
                    && identity.page_url === candidatePageUrl
                    && (currentPage?.storeId === storeId || routeAliasVerified)
                    && candidateHistoryCheckpointFailures(candidate, expectedDates).length === 0;
                }).slice(0, 50)
                : [];
              candidatePage = incompleteCandidatePage(
                error?.code === "COLLECTION_TIMEOUT" ? "page_response_timeout" : "page_scan_error",
                error?.message,
                {
                  checkpoint_candidate_count: checkpointCandidates.length,
                  checkpoint_candidate_ids: checkpointCandidates.map((candidate) =>
                    String(candidate.platform_product_id)),
                },
              );
              candidatePage.page_url = candidatePageUrl;
              candidatePage.candidates = checkpointCandidates;
            }
          }
        }
      }
    }
  }

  if (!visited.size) {
    const currentTab = await chrome.tabs.get(tabId).catch(() => null);
    const outcome = candidatePage?.diagnostics?.outcome || "orders_page_not_read";
    const probeDetail = String(candidatePage?.diagnostics?.message || "").slice(0, 220);
    const currentUrl = String(currentTab?.url || "标签页已关闭").slice(0, 240);
    const pendingUrl = String(currentTab?.pendingUrl || "无").slice(0, 240);
    return {
      state: "failed",
      error_type: outcome === "page_identity_mismatch" ? "store_identity_mismatch"
        : ["page_load_timeout", "cursor_page_mismatch"].includes(outcome) ? "page_not_ready"
          : "extension_collection_error",
      message: `目标店铺 Orders 页未读取（阶段：${outcome}${probeDetail ? `；身份探测：${probeDetail}` : ""}；当前 URL：${currentUrl}；待加载 URL：${pendingUrl}；标签页状态：${currentTab?.status || "unknown"}）`,
    };
  }

  const workerIdentity = await browserCollectionWorkerIdentity();
  const orderedProducts = targets.map((productId) => products.get(productId)).filter(Boolean);
  return {
    state: "ready",
    payload: {
      platform_store_id: storeId,
      url: item.target_url,
      products: orderedProducts,
      ...(candidatePage ? { candidate_page: candidatePage } : {}),
      raw_data: {
        collection_mode: "monitored_store_daily",
        ...(storeIdentity ? { store_identity: storeIdentity } : {}),
        worker_id: workerIdentity.worker_id,
        worker_label: workerIdentity.worker_label,
        monitored_product_ids: targets,
        visited_page_urls: [...visited],
        page_scans: progress.page_scans.slice(-10),
        chart_reads: progress.chart_reads.slice(-20),
        history_target_date: item.target_date,
        extractor_version: chrome.runtime.getManifest().version,
        ...(collectionInterrupted ? { collection_interrupted: true } : {}),
      },
    },
  };
}

function withTimeout(promise, timeoutMs, message) {
  let timerId;
  const timeout = new Promise((_, reject) => {
    timerId = setTimeout(() => {
      const error = new Error(message);
      error.code = "COLLECTION_TIMEOUT";
      reject(error);
    }, timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timerId));
}

async function sendCollectReviewsMessage(tabId, knownKeys, maxNewReviews) {
  const message = { type: "collect-visible-reviews", known_keys: knownKeys, max_new_reviews: maxNewReviews };
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch {
    await injectCollector(tabId);
    return chrome.tabs.sendMessage(tabId, message);
  }
}

async function closeItemTab(tabId) {
  const state = await automationState();
  if (state.automationTabId === tabId && state.automationItem) {
    await restoreFocusAfterSingleProductTest(state.automationItem.id);
  }
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const trackedTaskTab = tab
    && tabId !== state.automationDashboardTabId
    && (state.automationTabId === tabId || state.prefetchedTabId === tabId);
  const ownedWorkerWindow = tab
    && state.automationWindowOwned === true
    && state.automationWindowBackground === true
    && tab.windowId === state.automationWindowId
    && tabId !== state.automationDashboardTabId;
  const workerWindow = ownedWorkerWindow
    ? await chrome.windows.get(state.automationWindowId).catch(() => null)
    : null;
  await releaseAutomationTab(tabId);
  if (trackedTaskTab) {
    const nextState = await automationState();
    const keepAsIdle = workerWindow && nextState.idleAutomationTabId === tabId;
    if (!keepAsIdle) {
      await chrome.tabs.remove(tabId).catch(() => undefined);
    }
    if (ownedWorkerWindow && workerWindow && !nextState.automationItem && !nextState.prefetchedItem) {
      await ensureIdleAutomationTab(workerWindow, await automationState());
    }
  }
}

async function discardStaleAutomationClaims() {
  let state = await automationState();
  if (state.prefetchedTabId) await closeItemTab(state.prefetchedTabId);
  state = await automationState();
  if (state.automationTabId) await closeItemTab(state.automationTabId);
}

async function restoreFocusAfterSingleProductTest(itemId) {
  const state = await chrome.storage.local.get([
    "automationWindowId",
    "foregroundSingleTestItemId",
    "foregroundSingleTestReturnWindowId",
    "foregroundSingleTestFocusConfirmed",
  ]);
  if (Number(state.foregroundSingleTestItemId) !== Number(itemId)) return;
  await chrome.storage.local.remove([
    "foregroundSingleTestItemId",
    "foregroundSingleTestReturnWindowId",
    "foregroundSingleTestFocusConfirmed",
  ]);
  const focusedWindow = await chrome.windows.getLastFocused({ populate: false }).catch(() => null);
  const returnWindowId = state.foregroundSingleTestReturnWindowId;
  if (Number.isInteger(returnWindowId) && focusedWindow?.id === state.automationWindowId) {
    await chrome.windows.update(returnWindowId, { focused: true }).catch(() => undefined);
  }
}

async function focusWindowForSingleProductTest(item, workerWindowId, prefetch, { deferFocus = false } = {}) {
  if (prefetch || item.target_key !== `product:${item.product_id}:single`) return;
  const state = await chrome.storage.local.get([
    "foregroundSingleTestItemId",
    "foregroundSingleTestReturnWindowId",
  ]);
  if (Number(state.foregroundSingleTestItemId) !== Number(item.id)) {
    const focusedWindow = await chrome.windows.getLastFocused({ populate: false }).catch(() => null);
    const returnWindowId = Number.isInteger(focusedWindow?.id) && focusedWindow.id !== workerWindowId
      ? focusedWindow.id
      : null;
    await chrome.storage.local.set({
      foregroundSingleTestItemId: item.id,
      foregroundSingleTestReturnWindowId: returnWindowId,
    });
  }
  if (deferFocus) return;

  const workerWindow = await chrome.windows.get(workerWindowId).catch(() => null);
  await chrome.storage.local.set({ foregroundSingleTestFocusConfirmed: workerWindow?.focused === true });
}

async function singleProductFocusDiagnostics(item, tabId) {
  if (item.target_key !== `product:${item.product_id}:single`) return null;
  const state = await chrome.storage.local.get("foregroundSingleTestFocusConfirmed");
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const taskWindow = tab?.windowId
    ? await chrome.windows.get(tab.windowId).catch(() => null)
    : null;
  const focusedWindow = await chrome.windows.getLastFocused({ populate: false }).catch(() => null);
  return {
    requested: true,
    task_window_focused: taskWindow?.focused === true,
    task_tab_active: tab?.active === true,
    focus_request_confirmed: state.foregroundSingleTestFocusConfirmed === true,
    task_window_id: tab?.windowId ?? null,
    last_focused_window_id: focusedWindow?.id ?? null,
  };
}

async function closeAutomationWindow() {
  const state = await automationState();
  await chrome.storage.local.remove([
    "automationWindowId",
    "automationWindowOwned",
    "automationWindowBackground",
    "automationTabGroupId",
    "automationTabGroupWindowId",
    "automationDashboardTabId",
    "automationTabId",
    "automationItem",
    "automationItemPrefetched",
    "automationTaskKind",
    "prefetchedTabId",
    "prefetchedItem",
    "prefetchedTaskKind",
    "prefetchedReviewKnownKeys",
    "idleAutomationTabId",
    "reviewKnownKeys",
    "reviewProbeResult",
    "challengePaused",
    "challengeProvider",
    "firstProductPresentedRunId",
  ]);
  const tabs = [state.automationTabId, state.prefetchedTabId, state.idleAutomationTabId]
    .filter((tabId) => tabId && tabId !== state.automationDashboardTabId);
  await Promise.all([...new Set(tabs)].map((tabId) => chrome.tabs.remove(tabId).catch(() => undefined)));
}

async function notifyChallenge(item, { provider = "ALIEXPRESS", message = null } = {}) {
  const identityReview = provider === "STORE_IDENTITY";
  await chrome.notifications.create(NOTIFICATION_ID, {
    type: "basic",
    iconUrl: "icon.svg",
    title: identityReview ? "店铺身份待核验" : "AliExpress 自动采集已暂停",
    message: identityReview
      ? `${message || `${item.title || "当前店铺"}的页面身份需要核实。`} 请在监控台确认后继续。`
      : `${item.title || "当前页面"}需要人工完成平台验证，通过后任务会自动继续。`,
    priority: 2,
    requireInteraction: true,
  }).catch(() => undefined);
}

async function pauseForIxspyLogin(tabId, message) {
  const state = await automationState();
  if (!state.automationItem || state.automationTabId !== tabId || state.challengePaused) return;
  await api(`/api/browser-collection/items/${state.automationItem.id}/challenge`, {
    method: "POST",
    body: JSON.stringify({
      ...(await browserCollectionWorkerIdentity()),
      claim_token: state.automationClaimToken,
      error_message: message || "IXSPY 登录已过期，请正常登录后继续",
    }),
  });
  await chrome.storage.local.set({ challengePaused: true, challengeProvider: "IXSPY" });
  await holdPrefetchedItem("IXSPY 登录需要人工完成，预加载商品已退回待处理");
  await chrome.notifications.create("aliexpress-monitor-ixspy-login", {
    type: "basic",
    iconUrl: "icon.svg",
    title: "IXSPY 需要登录",
    message: "请在当前商品页的 IXSPY 面板正常登录。完成后会继续同一队列任务。",
    priority: 2,
    requireInteraction: true,
  }).catch(() => undefined);
  await presentChallengeTab(tabId);
}

async function presentChallengeTab(tabId) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab?.windowId) return;
  const state = await automationState();
  await chrome.windows.update(tab.windowId, {
    focused: true,
    state: state.browserCollectionWorkerMode === true ? "maximized" : "normal",
  }).catch(() => undefined);
  await chrome.tabs.update(tabId, { active: true }).catch(() => undefined);
}

async function pauseForAliExpressChallenge(tabId, message = "AliExpress 要求人工完成验证", provider = "ALIEXPRESS") {
  const state = await automationState();
  if (!state.automationItem || state.automationTabId !== tabId || state.challengePaused) return;
  const item = state.automationItem;
  const reviewTask = state.automationTaskKind === "REVIEW";
  await api(reviewTask
    ? `/api/reviews/collection-tasks/${item.id}/challenge`
    : `/api/browser-collection/items/${item.id}/challenge`, {
    method: "POST",
    body: JSON.stringify(reviewTask ? { error_message: message } : {
      ...(await browserCollectionWorkerIdentity()),
      claim_token: state.automationClaimToken,
      error_message: message,
    }),
  });
  await chrome.storage.local.set({ challengePaused: true, challengeProvider: provider });
  if (!reviewTask) await holdPrefetchedItem(provider === "STORE_IDENTITY"
    ? "店铺身份待核验，先暂停预加载项"
    : "检测到平台验证，先暂停预加载项");
  await notifyChallenge(item, { provider, message });
  await presentChallengeTab(tabId);
}

async function deferStoreIdentityReview(tabId, message) {
  const state = await automationState();
  if (!state.automationItem || state.automationTaskKind === "REVIEW"
    || state.automationTaskKind === "POOL_RESET"
    || state.automationTabId !== tabId || state.identityReviewPaused) return;
  const item = state.automationItem;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const observedPageUrl = message?.observed_page_url || message?.page_url || tab?.url || "";
  const reviewTarget = storeIdentityReviewTarget(observedPageUrl)
    || storeIdentityReviewTarget(item.target_url);
  const pageUrl = reviewTarget?.page_url || "";
  const evidenceInput = message?.evidence && typeof message.evidence === "object"
    ? message.evidence
    : {
      route_store_id: reviewTarget?.route_store_id || "",
      heading_store_id: null,
      labeled_store_ids: [],
      page_title: null,
      ixspy_store_name: null,
      identity_method: "unverified",
    };
  const reportedRouteStoreId = String(evidenceInput.route_store_id || "");
  const evidence = {
    ...evidenceInput,
    route_store_id: reviewTarget?.route_store_id || reportedRouteStoreId,
  };
  const identityErrorType = ["store_identity_mismatch", "store_identity_unverified"]
    .includes(message?.error_type) ? message.error_type : "store_identity_unverified";
  if (!reviewTarget || !evidence.route_store_id) {
    await finishAutomationFailure(
      state,
      "store_identity_evidence_invalid",
      `无法安全记录店铺身份待核验；当前页面：${String(observedPageUrl || "未识别").slice(0, 240)}`,
    );
    return;
  }
  const reviewMessage = [
    String(message?.message || "店铺页面身份需要人工核实").slice(0, 1500),
    observedPageUrl && observedPageUrl !== pageUrl
      ? `实际读取 URL：${String(observedPageUrl).slice(0, 400)}` : "",
    reportedRouteStoreId && reportedRouteStoreId !== reviewTarget.route_store_id
      ? `采集器报告路由 ID：${reportedRouteStoreId}；页面 URL 路由 ID：${reviewTarget.route_store_id}` : "",
  ].filter(Boolean).join("；").slice(0, 2000);
  await api(`/api/browser-collection/items/${item.id}/identity-review`, {
    method: "POST",
    body: JSON.stringify({
      ...(await browserCollectionWorkerIdentity()),
      claim_token: state.automationClaimToken,
      page_url: pageUrl,
      identity_error_type: identityErrorType,
      error_message: reviewMessage,
      evidence,
    }),
  });
  await chrome.storage.local.set({ identityReviewPaused: false, challengePaused: false });
  await chrome.storage.local.remove("challengeProvider");
  await chrome.notifications.clear(NOTIFICATION_ID).catch(() => undefined);
  await closeItemTab(tabId);
}

async function notifyFirstProduct(item) {
  await chrome.notifications.create(FIRST_PRODUCT_NOTIFICATION_ID, {
    type: "basic",
    iconUrl: "icon.svg",
    title: "AliExpress 首个商品已打开",
    message: `${item.title || "第一个商品"}已显示。如平台要求验证，请正常完成；通过后会自动继续采集。`,
    priority: 1,
  }).catch(() => undefined);
}

async function notifyDianxiaomiConfirmation(message) {
  await chrome.notifications.create(DIANXIAOMI_NOTIFICATION_ID, {
    type: "basic",
    iconUrl: "icon.svg",
    title: "店小秘采集已暂停",
    message: `${message || "店小秘需要人工确认"}，完成后任务会自动继续。`,
    priority: 2,
    requireInteraction: true,
  }).catch(() => undefined);
}

async function finishAutomationFailure(state, errorType, message) {
  if (!state.automationItem) return;
  if (state.automationTaskKind === "REVIEW") {
    await api(`/api/reviews/collection-tasks/${state.automationItem.id}/result`, {
      method: "POST",
      body: JSON.stringify({
        status: "FAILED",
        reviews: [],
        scanned_count: 0,
        error_type: errorType,
        error_message: message,
      }),
    }).catch(() => undefined);
    if (state.automationTabId) await closeItemTab(state.automationTabId);
    return;
  }
  await api(`/api/browser-collection/items/${state.automationItem.id}/failure`, {
    method: "POST",
    body: JSON.stringify({
      ...(await browserCollectionWorkerIdentity()),
      claim_token: state.automationClaimToken,
      error_type: errorType,
      error_message: message,
    }),
  }).catch(() => undefined);
  if (state.automationTabId) await closeItemTab(state.automationTabId);
}

async function processAutomationTabInternal(tabId, generation = null) {
  const state = await automationState();
  const item = state.automationItem;
  if (!item || state.automationTabId !== tabId) return;
  if (state.automationTaskKind !== "REVIEW" && item.target_key === `product:${item.product_id}:single`) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.windowId) {
      await chrome.tabs.update(tabId, { active: true }).catch(() => undefined);
      await focusWindowForSingleProductTest(item, tab.windowId, false);
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  if (state.automationItemPrefetched) {
    await chrome.storage.local.remove("automationItemPrefetched");
  }
  if (state.automationTaskKind === "REVIEW") {
    const reviewResult = state.reviewProbeResult || await sendCollectReviewsMessage(
      tabId,
      state.reviewKnownKeys || [],
      item.max_new_reviews || 100,
    );
    if (generation !== null && generation !== automationGeneration) return;
    if (state.reviewProbeResult) await chrome.storage.local.remove("reviewProbeResult");
    if (reviewResult?.state === "challenge") {
      await pauseForAliExpressChallenge(tabId, reviewResult.message || "AliExpress 要求人工完成验证");
      return;
    }
    const taskStatus = reviewResult?.state === "ready"
      ? "SUCCEEDED"
      : reviewResult?.state === "partial" ? "PARTIAL" : "FAILED";
    await api(`/api/reviews/collection-tasks/${item.id}/result`, {
      method: "POST",
      body: JSON.stringify({
        status: taskStatus,
        reviews: reviewResult?.reviews || [],
        scanned_count: reviewResult?.scanned_count || 0,
        error_type: taskStatus === "FAILED" ? "review_parse_failed" : taskStatus === "PARTIAL" ? "review_parse_partial" : null,
        error_message: reviewResult?.message || null,
        raw_data: reviewResult?.raw_data || {},
      }),
    });
    await chrome.notifications.clear(NOTIFICATION_ID).catch(() => undefined);
    await closeItemTab(tabId);
    return;
  }
  if (state.automationTaskKind === "POOL_RESET") {
    await processPoolResetTab(tabId, state);
    return;
  }
  const result = await withTimeout(
    item.target_type === "STORE"
      ? collectStorePages(tabId, item)
      : sendCollectMessage(tabId, item.target_type, item.target_date),
    item.target_type === "STORE" ? STORE_COLLECTION_RESPONSE_TIMEOUT_MS : COLLECTION_RESPONSE_TIMEOUT_MS,
    item.target_type === "STORE"
      ? "店铺页采集超过 10 分钟未返回，已释放任务并自动重试"
      : "商品页采集超过 90 秒未返回，已释放任务并自动重试",
  );
  if (generation !== null && generation !== automationGeneration) return;

  const identityError = ["store_identity_mismatch", "store_identity_unverified"]
    .includes(result?.error_type);
  const identityReviewState = ["identity_review", "store_identity_unverified"]
    .includes(result?.state);
  if (identityReviewState || identityError) {
    const currentTab = await chrome.tabs.get(tabId).catch(() => null);
    const identityPageUrl = result?.page_url || currentTab?.url;
    if (storeIdentityReviewTarget(identityPageUrl) || storeIdentityReviewTarget(item.target_url)) {
      await deferStoreIdentityReview(tabId, {
        ...result,
        page_url: identityPageUrl || item.target_url,
        observed_page_url: identityPageUrl || currentTab?.url || "",
      });
      return;
    }
  }

  if (result?.state === "challenge") {
    await pauseForAliExpressChallenge(tabId, result.message || "AliExpress 要求人工完成验证");
    return;
  }

  if (result?.state === "ixspy_login") {
    await pauseForIxspyLogin(tabId, result.message);
    return;
  }

  if (!result?.payload) {
    await finishAutomationFailure(
      state,
      result?.error_type || (result?.state === "partial" ? "observable_data_missing" : "extension_collection_error"),
      result?.message || "页面在限定时间内没有出现可读取的公开数据",
    );
    return;
  }

  const focusDiagnostics = await singleProductFocusDiagnostics(item, tabId);
  if (focusDiagnostics) {
    result.payload.raw_data = {
      ...result.payload.raw_data,
      browser_collection_focus: focusDiagnostics,
    };
  }

  const endpoint = item.target_type === "STORE"
    ? `/api/browser-collection/items/${item.id}/store-discovery`
    : `/api/browser-collection/items/${item.id}/snapshot`;
  const identity = await browserCollectionWorkerIdentity();
  const submittedRun = await api(endpoint, {
    method: "POST",
    body: JSON.stringify({
      ...result.payload,
      ...identity,
      claim_token: state.automationClaimToken,
    }),
  });
  if (item.target_type === "STORE") {
    const submittedItem = submittedRun?.items?.find((entry) => Number(entry.id) === Number(item.id));
    if (submittedItem?.status === "PARTIAL") {
      const progressKey = storeProgressKey(item.id);
      const stored = await chrome.storage.local.get(progressKey);
      if (stored[progressKey]) {
        await chrome.storage.local.set({
          [progressKey]: retainCompleteStoreProgress(stored[progressKey], item.target_date),
        });
      }
    } else if (submittedItem?.status === "SUCCEEDED") {
      await chrome.storage.local.remove(storeProgressKey(item.id));
    }
  }
  await chrome.notifications.clear(NOTIFICATION_ID).catch(() => undefined);
  await chrome.notifications.clear(FIRST_PRODUCT_NOTIFICATION_ID).catch(() => undefined);
  await closeItemTab(tabId);
}

async function processAutomationTab(tabId) {
  if (!(await browserCollectionWorkerEnabled())) return;
  if (automationBusy) return;
  const startingState = await automationState();
  const itemId = Number(startingState.automationItem?.id);
  const generation = ++automationGeneration;
  automationBusy = true;
  try {
    if (startingState.automationItem
      && !["REVIEW", "POOL_RESET"].includes(startingState.automationTaskKind)) {
      const run = await sendBrowserCollectionHeartbeat().catch(() => null);
      await prefetchNextBrowserItem(run).catch(() => undefined);
    }
    await processAutomationTabInternal(tabId, generation);
  } catch (error) {
    if (generation !== automationGeneration) return;
    const state = await automationState();
    if (Number(state.automationItem?.id) !== itemId) return;
    if (error?.code === "PLATFORM_CHALLENGE") {
      if (state.automationTaskKind === "POOL_RESET") {
        await pauseForPoolResetChallenge(tabId, error.message || "AliExpress 要求人工验证");
        return;
      }
      await pauseForAliExpressChallenge(tabId, error.message || "AliExpress 要求人工完成验证");
      return;
    }
    if (error?.code === "STORE_IDENTITY_REVIEW") {
      await deferStoreIdentityReview(tabId, {
        error_type: error.error_type,
        message: error.message,
        page_url: error.page_url,
        evidence: error.evidence,
      });
      return;
    }
    if (state.automationTaskKind === "POOL_RESET") {
      await reportPoolResetFailure(
        state,
        error?.code === "STORE_IDENTITY_MISMATCH" ? "store_identity_mismatch" : "pool_reset_extension_error",
        error.message || "监控池重建扩展执行失败",
      );
      return;
    }
    await finishAutomationFailure(
      state,
      error?.code === "STORE_IDENTITY_MISMATCH"
        ? "store_identity_mismatch"
        : error?.code === "COLLECTION_TIMEOUT" ? "browser_item_stalled" : "extension_collection_error",
      error.message || "扩展采集失败",
    );
  } finally {
    if (generation === automationGeneration) automationBusy = false;
  }
  if (generation === automationGeneration) void pollAutomation();
}

async function recoverSettledBusyItem() {
  const run = await sendBrowserCollectionHeartbeat().catch(() => null);
  if (!run) return;
  let state = await automationState();
  const itemId = Number(state.automationItem?.id);
  if (!Number.isInteger(itemId) || state.automationTaskKind === "REVIEW") return;
  const serverItem = (run.items || []).find((item) => Number(item.id) === itemId);
  if (serverItem && ["RUNNING", "NEEDS_VERIFICATION"].includes(serverItem.status)) return;

  automationGeneration += 1;
  automationBusy = false;
  if (state.automationTabId) await releaseAutomationTab(state.automationTabId);
  else await clearAutomationItem();
  void pollAutomation();
}

async function openAutomationItem(item, taskKind = "PRODUCT", knownKeys = [], { prefetch = false, claimToken = null } = {}) {
  let state = await automationState();
  if (prefetch && state.prefetchedItem) return;
  if (!prefetch && state.automationItem) return;
  const startUrl = item.target_type === "STORE" ? storeCollectionStartUrl(item) : item.target_url;
  const workerWindow = await ensureAutomationWindow({
    ensureIdle: !prefetch && state.browserCollectionRemoteProfileEnabled !== true,
  });
  if (!workerWindow) {
    throw new Error(state.browserCollectionRemoteProfileEnabled === true
      ? "请在当前采集 Chrome 窗口打开并保留监控台标签，再继续采集"
      : "请先在本机 Chrome 打开一个普通窗口，再继续后台采集");
  }
  if (!prefetch) await focusWindowForSingleProductTest(item, workerWindow.id, prefetch, { deferFocus: true });
  const currentState = await automationState();
  const idleTab = !prefetch && currentState.idleAutomationTabId
    ? await chrome.tabs.get(currentState.idleAutomationTabId).catch(() => null)
    : null;
  const reusableIdleTab = idleTab?.windowId === workerWindow.id
    && (idleTab.url === "about:blank" || isAliExpressPage(idleTab.url))
    ? idleTab
    : null;
  const tab = reusableIdleTab
    ? await chrome.tabs.update(reusableIdleTab.id, { url: startUrl, active: true })
    : await chrome.tabs.create({
      windowId: workerWindow.id,
      url: startUrl,
      active: !prefetch,
    });
  if (reusableIdleTab) await chrome.storage.local.remove("idleAutomationTabId");
  const windowId = tab.windowId;
  await groupAutomationTabs([tab.id], windowId);
  if (!prefetch) await focusWindowForSingleProductTest(item, windowId, prefetch);
  await chrome.storage.local.set({
    automationWindowId: windowId,
    ...(prefetch
      ? {
        prefetchedTabId: tab.id,
        prefetchedItem: item,
        ...(claimToken ? { prefetchedClaimToken: claimToken } : {}),
        prefetchedTaskKind: taskKind,
        ...(taskKind === "REVIEW" ? { prefetchedReviewKnownKeys: knownKeys } : {}),
      }
      : {
        automationTabId: tab.id,
        automationItem: item,
        ...(claimToken ? { automationClaimToken: claimToken } : {}),
        automationItemPrefetched: false,
        automationTaskKind: taskKind,
        ...(taskKind === "REVIEW" ? { reviewKnownKeys: knownKeys } : {}),
        challengePaused: false,
        identityReviewPaused: false,
        challengeProvider: null,
      }),
  });
}

async function prefetchNextBrowserItem(run) {
  if (!run || !["RUNNING", "PENDING"].includes(run.status)) return;
  const state = await automationState();
  if (run.phase !== "PRODUCT_COLLECTION" || state.automationItem?.target_type !== "PRODUCT"
    || state.prefetchedItem || state.automationTaskKind === "REVIEW"
    || state.challengePaused
    || state.automationItem?.target_key === `product:${state.automationItem.product_id}:single`) return;
  const workerWindow = state.automationWindowId
    ? await chrome.windows.get(state.automationWindowId).catch(() => null)
    : null;
  if (!workerWindow?.id) return;
  const tabs = await chrome.tabs.query({ windowId: workerWindow.id }).catch(() => []);
  if (tabs.filter((tab) => isAliExpressPage(tab.url)).length >= MAX_OPEN_COLLECTION_PAGES) return;
  const claimed = await api("/api/browser-collection/items/claim", {
    method: "POST",
    body: JSON.stringify(await browserCollectionWorkerIdentity()),
  });
  if (claimed.item) await openAutomationItem(claimed.item, "PRODUCT", [], {
    prefetch: true,
    claimToken: claimed.claim_token,
  });
}

async function sendBrowserCollectionHeartbeat() {
  const state = await automationState();
  const activeClaims = [
    state.automationTaskKind !== "REVIEW" && state.automationItem && state.automationClaimToken
      ? { item_id: Number(state.automationItem.id), claim_token: state.automationClaimToken }
      : null,
    state.prefetchedTaskKind !== "REVIEW" && state.prefetchedItem && state.prefetchedClaimToken
      ? { item_id: Number(state.prefetchedItem.id), claim_token: state.prefetchedClaimToken }
      : null,
  ].filter((claim) => claim && Number.isInteger(claim.item_id));
  const identity = await browserCollectionWorkerIdentity();
  try {
    return await api("/api/browser-collection/heartbeat", {
      method: "POST",
      body: JSON.stringify({
        ...identity,
        extension_version: chrome.runtime.getManifest().version,
        capabilities: [POOL_RESET_CAPABILITY],
        active_claims: activeClaims,
      }),
    });
  } catch (error) {
    if (error?.status !== 409) throw error;
    await discardStaleAutomationClaims();
    return null;
  }
}

async function sendPoolResetHeartbeat(claimToken = null) {
  const identity = await browserCollectionWorkerIdentity();
  return api("/api/browser-collection/pool-resets/heartbeat", {
    method: "POST",
    body: JSON.stringify({
      ...identity,
      extension_version: chrome.runtime.getManifest().version,
      capabilities: [POOL_RESET_CAPABILITY],
      ...(claimToken ? { claim_token: claimToken } : {}),
    }),
  });
}

async function reportPoolResetFailure(state, errorType, message) {
  if (!state.automationItem || state.automationTaskKind !== "POOL_RESET") return;
  await api(`/api/browser-collection/pool-resets/tasks/${state.automationItem.id}/failure`, {
    method: "POST",
    body: JSON.stringify({
      ...(await browserCollectionWorkerIdentity()),
      claim_token: state.automationClaimToken,
      error_type: errorType,
      error_message: message || "店铺页扫描失败",
    }),
  });
  if (state.automationTabId) await closeItemTab(state.automationTabId);
}

async function pauseForPoolResetChallenge(tabId, message) {
  const state = await automationState();
  if (!state.automationItem || state.automationTaskKind !== "POOL_RESET"
    || state.automationTabId !== tabId || state.challengePaused) return;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  await api(`/api/browser-collection/pool-resets/tasks/${state.automationItem.id}/challenge`, {
    method: "POST",
    body: JSON.stringify({
      ...(await browserCollectionWorkerIdentity()),
      claim_token: state.automationClaimToken,
      error_message: message || "AliExpress 需要人工验证",
      page_url: tab?.url || state.automationItem.target_url,
    }),
  });
  await chrome.storage.local.set({ challengePaused: true, challengeProvider: "ALIEXPRESS" });
  await notifyChallenge(state.automationItem);
  await presentChallengeTab(tabId);
}

async function submitPoolResetPage(state, pageResult) {
  const item = state.automationItem;
  const storeId = String(item?.platform_store_id || "");
  const tab = state.automationTabId
    ? await chrome.tabs.get(state.automationTabId).catch(() => null)
    : null;
  if (!item || state.automationTaskKind !== "POOL_RESET" || !storeId || !tab
    || pageResult?.platform_store_id !== storeId || pageResult?.url !== tab.url
    || !safeStoreOrdersUrl(tab.url, storeId)) {
    throw new Error("店铺重建页身份与后端任务不一致");
  }
  const candidates = pageResult.candidates || [];
  if (pageResult.scan_complete !== true || pageResult.pagination_known !== true) {
    await reportPoolResetFailure(state, "incomplete_orders_page", "商品卡片或分页状态未完整读取；保留原监控池并重试当前页");
    return false;
  }
  const diagnostics = {
    ...(pageResult.diagnostics || {}),
    scanned_candidate_count: candidates.length,
  };
  await api(`/api/browser-collection/pool-resets/tasks/${item.id}/page`, {
    method: "POST",
    body: JSON.stringify({
      ...(await browserCollectionWorkerIdentity()),
      claim_token: state.automationClaimToken,
      expected_store_id: storeId,
      page: {
        page_url: pageResult.url,
        pagination_known: pageResult.pagination_known,
        has_next_page: pageResult.has_next_page,
        next_page_url: pageResult.next_page_url,
        scan_complete: pageResult.scan_complete,
        candidates,
        diagnostics,
      },
    }),
  });
  await chrome.notifications.clear(NOTIFICATION_ID).catch(() => undefined);
  await closeItemTab(state.automationTabId);
  return true;
}

async function processPoolResetTab(tabId, state) {
  const item = state.automationItem;
  const storeId = String(item?.platform_store_id || "");
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab || !storeId || !safeStoreOrdersUrl(tab.url, storeId)) {
    await reportPoolResetFailure(state, "store_identity_mismatch", "打开的页面不是本任务指定的店铺 Orders 页");
    return;
  }
  const result = await sendStoreCandidateScanMessage(
    tabId,
    item.target_date,
    storeId,
    item.target_url,
  );
  if (result?.state === "challenge") {
    await pauseForPoolResetChallenge(tabId, result.message);
    return;
  }
  if (result?.state === "ixspy_login") {
    await pauseForPoolResetChallenge(tabId, result.message || "IXSPY 登录需要人工完成");
    return;
  }
  if (result?.state !== "ready" || !result.page_result) {
    await reportPoolResetFailure(
      state,
      result?.error_type || "orders_page_scan_failed",
      result?.message || "未能完整读取店铺商品页",
    );
    return;
  }
  await submitPoolResetPage(state, result.page_result);
}

async function pollPoolResetAutomation(batch, initialState) {
  if (initialState.automationItem && initialState.automationTaskKind !== "POOL_RESET") return;
  await sendPoolResetHeartbeat(initialState.automationTaskKind === "POOL_RESET"
    ? initialState.automationClaimToken
    : null);
  let state = await automationState();
  if (state.automationItem && state.automationTaskKind === "POOL_RESET") {
    const tab = state.automationTabId
      ? await chrome.tabs.get(state.automationTabId).catch(() => null)
      : null;
    if (!tab) {
      const workerWindow = await ensureAutomationWindow();
      if (!workerWindow?.id) return;
      const reopened = await chrome.tabs.create({
        windowId: workerWindow.id,
        url: state.automationItem.target_url,
        active: true,
      });
      await chrome.storage.local.set({ automationTabId: reopened.id, challengePaused: false });
      return;
    }
    if (batch.status === "NEEDS_VERIFICATION" || state.challengePaused) {
      if (isAliExpressChallengePage(tab.url) || tab.status !== "complete") return;
      const result = await sendStoreCandidateScanMessage(
        tab.id,
        state.automationItem.target_date,
        String(state.automationItem.platform_store_id),
        state.automationItem.target_url,
      );
      if (result?.state === "challenge" || result?.state === "ixspy_login") return;
      if (result?.state !== "ready" || !result.page_result?.scan_complete) return;
      await api(`/api/browser-collection/pool-resets/tasks/${state.automationItem.id}/resume`, {
        method: "POST",
        body: JSON.stringify({
          ...(await browserCollectionWorkerIdentity()),
          claim_token: state.automationClaimToken,
          page_url: result.page_result.url,
        }),
      });
      await chrome.storage.local.set({ challengePaused: false });
      state = await automationState();
      await submitPoolResetPage(state, result.page_result);
      return;
    }
    if (tab.status === "complete") await processPoolResetTab(tab.id, state);
    return;
  }
  if (batch.status !== "RUNNING" || !batch.dispatch_enabled) return;
  const claimed = await api("/api/browser-collection/pool-resets/claim", {
    method: "POST",
    body: JSON.stringify(await browserCollectionWorkerIdentity()),
  });
  if (!claimed.store || !claimed.claim_token || !claimed.target_url) return;
  const item = {
    id: claimed.store.id,
    target_type: "STORE",
    target_key: `pool-reset:${claimed.batch.id}:${claimed.store.id}`,
    platform_store_id: claimed.store.platform_store_id,
    store_id: claimed.store.store_id,
    title: claimed.store.store_name,
    target_url: claimed.target_url,
    target_date: claimed.target_date,
  };
  await openAutomationItem(item, "POOL_RESET", [], { claimToken: claimed.claim_token });
}

async function pollAutomation() {
  await ensureWorkerModesInitialized();
  if (!(await browserCollectionWorkerEnabled())) return;
  let initialState = await automationState();
  let latestReset = null;
  try {
    latestReset = await api("/api/browser-collection/pool-resets/latest");
  } catch {
    latestReset = null;
  }
  const resetActive = latestReset?.dispatch_enabled === true
    && ["RUNNING", "NEEDS_VERIFICATION"].includes(latestReset.status);
  if (initialState.automationTaskKind === "POOL_RESET" && !resetActive) {
    if (initialState.automationTabId) await closeItemTab(initialState.automationTabId);
    else await clearAutomationItem();
    initialState = await automationState();
  }
  if (resetActive) {
    if (automationBusy) return;
    automationBusy = true;
    try {
      await pollPoolResetAutomation(latestReset, initialState);
    } catch (error) {
      if (initialState.automationTaskKind === "POOL_RESET") {
        await reportPoolResetFailure(
          initialState,
          "pool_reset_extension_error",
          error?.message || "监控池重建扩展执行失败",
        ).catch(() => undefined);
      }
    } finally {
      automationBusy = false;
    }
    return;
  }
  if (automationBusy) {
    await recoverSettledBusyItem();
    return;
  }
  automationBusy = true;
  try {
    const run = await sendBrowserCollectionHeartbeat();
    if (!run) return;
    let state = await resetStaleAutomationState(run);
    state = await migrateTrackedAutomationTabs(state);

    if (state.automationItem && state.automationTabId) {
      const activeTaskTab = await chrome.tabs.get(state.automationTabId).catch(() => null);
      if (activeTaskTab && isAliExpressChallengePage(activeTaskTab.url)) {
        await pauseForAliExpressChallenge(state.automationTabId);
        return;
      }
    }

    if (run.status === "PAUSED" && run.paused_reason === "manual_pause") {
      await holdPrefetchedItem("队列已手动暂停，预加载项已退回待处理");
      state = await automationState();
      if (state.automationItemPrefetched && state.automationItem) {
        await api(`/api/browser-collection/items/${state.automationItem.id}/failure`, {
          method: "POST",
          body: JSON.stringify({
            ...(await browserCollectionWorkerIdentity()),
            claim_token: state.automationClaimToken,
            error_type: "queue_paused",
            error_message: "队列手动暂停，预加载商品已退回待处理",
          }),
        }).catch(() => undefined);
        if (state.automationTabId) {
          await clearAutomationItem();
          await chrome.storage.local.set({ idleAutomationTabId: state.automationTabId });
        }
        return;
      }
      const activeTab = state.automationTabId
        ? await chrome.tabs.get(state.automationTabId).catch(() => null)
        : null;
      if (state.automationItem && activeTab?.status === "complete") {
        automationBusy = false;
        void processAutomationTab(activeTab.id);
      }
      return;
    }

    if (state.identityReviewPaused) {
      const reviewedItem = run.items?.find((item) =>
        Number(item.id) === Number(state.automationItem?.id));
      if (run.status === "NEEDS_VERIFICATION"
        && run.paused_reason === "store_identity_review") return;
      if (run.status === "RUNNING" && reviewedItem?.status === "RUNNING"
        && state.automationTabId) {
        const tab = await chrome.tabs.get(state.automationTabId).catch(() => null);
        if (tab) {
          await chrome.storage.local.set({
            automationItem: reviewedItem,
            identityReviewPaused: false,
          });
          await chrome.notifications.clear(NOTIFICATION_ID).catch(() => undefined);
          state = await automationState();
          automationBusy = false;
          void processAutomationTab(tab.id);
          return;
        }
      }
      if (state.automationTabId) await closeItemTab(state.automationTabId);
      else await clearAutomationItem();
      state = await automationState();
    }

    if (state.challengePaused && run.status === "RUNNING") {
      const resumedItem = run.items?.find((item) => Number(item.id) === Number(state.automationItem?.id));
      if (resumedItem?.status === "RUNNING" && state.automationTabId) {
        await chrome.storage.local.set({ challengePaused: false });
        await chrome.notifications.clear(NOTIFICATION_ID).catch(() => undefined);
        await chrome.notifications.clear("aliexpress-monitor-ixspy-login").catch(() => undefined);
        await chrome.storage.local.remove("challengeProvider");
        state = await automationState();
      }
    }

    if (state.challengePaused && state.automationTaskKind === "REVIEW") {
      const tab = state.automationTabId
        ? await chrome.tabs.get(state.automationTabId).catch(() => null)
        : null;
      if (!tab) {
        const workerWindow = await ensureAutomationWindow();
        if (!workerWindow) return;
        const reopened = await chrome.tabs.create({
          windowId: workerWindow.id,
          url: state.automationItem.target_url,
          active: true,
        });
        await chrome.storage.local.set({ automationTabId: reopened.id });
        await chrome.windows.update(workerWindow.id, { focused: true, state: "normal" }).catch(() => undefined);
        return;
      }
      if (tab.status !== "complete") return;
      const probe = await sendCollectReviewsMessage(
        tab.id,
        state.reviewKnownKeys || [],
        state.automationItem?.max_new_reviews || 100,
      );
      if (probe?.state === "challenge") return;
      await api(`/api/reviews/collection-tasks/${state.automationItem.id}/resume`, {
        method: "POST",
        body: "{}",
      });
      await chrome.storage.local.set({ reviewProbeResult: probe });
      await chrome.storage.local.set({ challengePaused: false });
      await processAutomationTabInternal(tab.id);
      return;
    }
    if (state.challengePaused && run.status === "NEEDS_VERIFICATION") {
      const tab = state.automationTabId
        ? await chrome.tabs.get(state.automationTabId).catch(() => null)
        : null;
      if (!tab || tab.status !== "complete") return;
      const storeProbe = state.automationItem?.target_type === "STORE";
      let storeId = null;
      if (storeProbe) {
        storeId = new URL(state.automationItem.target_url).pathname
          .match(/^\/store\/(\d+)\/pages\/all-items\.html$/)?.[1];
        if (!storeId || isAliExpressChallengePage(tab.url)) return;
        if (!storeOrdersPageIdentity(tab.url)) {
          await chrome.tabs.update(tab.id, { url: state.automationItem.target_url, active: true });
          return;
        }
      }
      const probe = await sendCollectMessage(
        tab.id,
        storeProbe ? "STORE_IDENTITY" : state.automationItem?.target_type,
        state.automationItem?.target_date,
        undefined,
        storeProbe ? {
          expectedStoreId: storeId,
          expectedStoreName: state.automationItem?.title,
          identityMonitoredProductIds: state.automationItem?.monitored_product_ids || [],
          approvedStoreUrlIdentities: state.automationItem?.approved_store_url_identities || [],
        } : null,
      );
      if (probe?.state === "challenge") return;
      if (probe?.state === "ixspy_login") return;
      if (storeProbe) {
        if (probe?.state !== "ready" || !storeId || probe.platform_store_id !== storeId) return;
      } else if (!probe?.payload) return;
      await chrome.storage.local.set({ challengePaused: false });
      await chrome.notifications.clear(NOTIFICATION_ID).catch(() => undefined);
      await chrome.notifications.clear("aliexpress-monitor-ixspy-login").catch(() => undefined);
      await chrome.storage.local.remove("challengeProvider");
      state = await automationState();
    }
    if (state.challengePaused && run.status !== "NEEDS_VERIFICATION") {
      if (state.automationTabId) await closeItemTab(state.automationTabId);
      state = await automationState();
    }
    if (state.automationItem && state.automationTabId) {
      const tab = await chrome.tabs.get(state.automationTabId).catch(() => null);
      if (tab) {
        if (tab.status === "complete") {
          automationBusy = false;
          void processAutomationTab(tab.id);
        }
        return;
      }
      await finishAutomationFailure(state, "browser_tab_closed", "自动采集标签页被关闭");
    }

    if (!run.pending_count) return;
    const claimed = await api("/api/browser-collection/items/claim", {
      method: "POST",
      body: JSON.stringify(await browserCollectionWorkerIdentity()),
    });
    if (claimed.item) {
      await openAutomationItem(claimed.item, "PRODUCT", [], { claimToken: claimed.claim_token });
      setTimeout(() => void pollAutomation(), 500);
      return;
    }
    if (claimed.run.status === "NEEDS_VERIFICATION") return;

    const reviewClaim = await api("/api/reviews/collection-tasks/claim", {
      method: "POST",
      body: "{}",
    });
    if (reviewClaim.task) {
      await openAutomationItem({
        id: reviewClaim.task.id,
        target_type: "PRODUCT",
        target_url: reviewClaim.task.product_url,
        title: reviewClaim.task.product_title,
        max_new_reviews: reviewClaim.task.max_new_reviews,
      }, "REVIEW", reviewClaim.known_keys);
    }
  } catch {
    // The local backend may still be starting; the minute alarm retries without page side effects.
  } finally {
    automationBusy = false;
  }
}

async function sendDianxiaomiProductMessage(tabId, message) {
  let lastError;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      return await chrome.tabs.sendMessage(tabId, message);
    } catch (error) {
      lastError = error;
      await injectDianxiaomiProductBridge(tabId);
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
  }
  throw lastError || new Error("AliExpress 商品页的店小秘助手桥接尚未准备好");
}

async function waitForDianxiaomiProductTab(tabId, expectedProductId) {
  const expectedId = String(expectedProductId);
  let mismatchSince = 0;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error("店小秘工作窗口的商品页已关闭");
    const currentProductId = productIdFromUrl(tab.url);
    if (currentProductId === expectedId && !tab.pendingUrl) return tab;
    if (tab.pendingUrl) {
      mismatchSince = 0;
    } else if (currentProductId && currentProductId !== expectedId) {
      if (!mismatchSince) mismatchSince = Date.now();
      if (tab.status === "complete" || Date.now() - mismatchSince >= 1500) break;
    } else {
      mismatchSince = 0;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const actualProductId = productIdFromUrl(tab?.url);
  const error = new Error(
    `店小秘工作窗口未加载队列商品（目标商品 ID ${expectedProductId}，当前商品 ID ${actualProductId || "未识别"}）`,
  );
  const mismatch = dianxiaomiProductPageMismatch(
    `https://www.aliexpress.com/item/${expectedProductId}.html`,
    tab?.url,
  );
  if (mismatch) error.detail = { product_page_mismatch: mismatch };
  throw error;
}

function isDianxiaomiWorkerDashboardUrl(value) {
  try {
    const url = new URL(value);
    const explicitLocalWorker = ["127.0.0.1", "localhost"].includes(url.hostname)
      && url.searchParams.get("dianxiaomi_worker") === "1"
      && url.searchParams.get("dianxiaomi_local") === "1";
    const browserWorker = url.searchParams.get("browser_collection") === "1"
      && (!isLoopbackMonitorDashboard(url.href) || isIsolatedBrowserCollectorUrl(url.href));
    return explicitLocalWorker || browserWorker;
  } catch {
    return false;
  }
}

async function dianxiaomiWorkerDashboardContext() {
  await ensureWorkerModesInitialized();
  const state = await chrome.storage.local.get([
    "dianxiaomiWorkerMode",
    "dianxiaomiDashboardTabId",
    "dianxiaomiDashboardWindowId",
  ]);
  if (state.dianxiaomiWorkerMode !== true
    || !Number.isInteger(state.dianxiaomiDashboardTabId)
    || !Number.isInteger(state.dianxiaomiDashboardWindowId)) return null;
  const tab = await chrome.tabs.get(state.dianxiaomiDashboardTabId).catch(() => null);
  if (!tab || tab.windowId !== state.dianxiaomiDashboardWindowId
    || !isDianxiaomiWorkerDashboardUrl(tab.url)) return null;
  const window = await chrome.windows.get(tab.windowId).catch(() => null);
  return window?.id === tab.windowId ? { tab, windowId: window.id } : null;
}

async function ensureDianxiaomiProductTab(url, dashboardWindowId) {
  const dashboard = await dianxiaomiWorkerDashboardContext();
  if (!dashboard || dashboard.windowId !== dashboardWindowId) {
    throw new Error("监控台采集窗口已关闭或失效；未创建独立 Chrome 窗口");
  }
  const stored = await chrome.storage.local.get(["dianxiaomiTabId", "dianxiaomiWindowId"]);
  let tab = stored.dianxiaomiTabId
    ? await chrome.tabs.get(stored.dianxiaomiTabId).catch(() => null)
    : null;
  if (!tab || tab.windowId !== dashboardWindowId || tab.id === dashboard.tab.id) {
    tab = await chrome.tabs.create({ windowId: dashboardWindowId, url, active: true });
  } else if (productIdFromUrl(tab.url) !== productIdFromUrl(url)) {
    tab = await chrome.tabs.update(tab.id, { url, active: true });
  } else {
    tab = await chrome.tabs.update(tab.id, { active: true });
  }

  await chrome.storage.local.set({
    dianxiaomiDashboardTabId: dashboard.tab.id,
    dianxiaomiDashboardWindowId: dashboardWindowId,
    dianxiaomiWindowId: dashboardWindowId,
    dianxiaomiTabId: tab.id,
    dianxiaomiActiveTabId: tab.id,
    dianxiaomiQueueHasWork: true,
    dianxiaomiQueueProductTabId: tab.id,
    dianxiaomiQueueProductId: productIdFromUrl(url),
  });
  tab = await waitForDianxiaomiProductTab(tab.id, productIdFromUrl(url));
  return { tab, windowId: dashboardWindowId };
}

async function focusDianxiaomiProductTab(tabId) {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab) return;
  // Focus the tab without changing the user's current window size/state.
  await chrome.windows.update(tab.windowId, { focused: true }).catch(() => undefined);
  await chrome.tabs.update(tab.id, { active: true }).catch(() => undefined);
}

function isNonBlockingDianxiaomiProductMismatch(itemOrMessage) {
  const errorType = typeof itemOrMessage === "string" ? null : itemOrMessage?.error_type;
  const message = typeof itemOrMessage === "string"
    ? itemOrMessage
    : itemOrMessage?.error_message || itemOrMessage?.message || "";
  return errorType === "product_page_mismatch"
    || Boolean(itemOrMessage?.detail?.product_page_mismatch)
    || message.includes("店小秘工作窗口未加载队列商品")
    || message.includes("工作商品页已切换，无法确认当前队列商品的采集结果");
}

async function updateDianxiaomiHandoffs(baseUrl, handoffs, workerId, status, result = null) {
  const responses = await Promise.all(handoffs.map((handoff) => api(`/api/integrations/dianxiaomi/handoffs/${handoff.id}/status`, {
    method: "POST",
    body: JSON.stringify({
      status,
      worker_id: workerId,
      error_type: status === "FAILED"
        ? "page_operation_failed"
        : status === "NEEDS_CONFIRMATION"
          ? result?.error_type || (isNonBlockingDianxiaomiProductMismatch(result) ? "product_page_mismatch" : "manual_confirmation_required")
          : null,
      error_message: ["FAILED", "NEEDS_CONFIRMATION"].includes(status) ? result?.message || null : null,
      detail: result?.detail || undefined,
    }),
  }, baseUrl).catch(() => null)));
  return responses.every(Boolean);
}

async function clearDianxiaomiActiveHandoff(clearOperation = false) {
  const keys = ["dianxiaomiActiveHandoffId", "dianxiaomiActiveTabId", "dianxiaomiProductFlowHandoffId"];
  if (clearOperation) keys.push(DIANXIAOMI_PRODUCT_OPERATION_KEY);
  await chrome.storage.local.remove(keys);
}

async function closeDianxiaomiProductTabAfterQueueDrained(baseUrl) {
  let status;
  try {
    status = await api("/api/integrations/dianxiaomi/status", {}, baseUrl);
  } catch {
    return;
  }
  if (!status) return;
  const waitingForNextStart = status.paused && status.paused_reason === "awaiting_start";
  if (waitingForNextStart) {
    if (status.processing_count || status.submitted_count) return;
  } else if (status.paused || status.queued_count || status.processing_count || status.submitted_count) {
    return;
  }

  const state = await chrome.storage.local.get([
    "dianxiaomiQueueHasWork",
    "dianxiaomiQueueProductTabId",
    "dianxiaomiQueueProductId",
    "dianxiaomiWindowId",
    "dianxiaomiDashboardTabId",
  ]);
  if (state.dianxiaomiQueueHasWork !== true) return;

  const tabId = state.dianxiaomiQueueProductTabId;
  const tab = Number.isInteger(tabId) ? await chrome.tabs.get(tabId).catch(() => null) : null;
  const expectedProductId = String(state.dianxiaomiQueueProductId || "");
  if (tab
    && tab.id !== state.dianxiaomiDashboardTabId
    && tab.windowId === state.dianxiaomiWindowId
    && expectedProductId
    && productIdFromUrl(tab.url) === expectedProductId) {
    await chrome.tabs.remove(tab.id).catch(() => undefined);
  }
  await chrome.storage.local.remove([
    "dianxiaomiQueueHasWork",
    "dianxiaomiQueueProductTabId",
    "dianxiaomiQueueProductId",
    "dianxiaomiTabId",
    "dianxiaomiActiveTabId",
  ]);
}

async function finishDianxiaomiHandoff(baseUrl, handoff, workerId, result, tabId) {
  const status = result?.state === "succeeded"
    ? "SUCCEEDED"
    : ["collecting", "dispatching"].includes(result?.state)
      ? "COLLECTING"
      : ["waiting_helper", "not_started"].includes(result?.state)
        ? "OPENED"
        : "NEEDS_CONFIRMATION";
  let resultForUpdate = status === "NEEDS_CONFIRMATION" && !result?.message
    ? { ...(result || {}), message: "店小秘助手未明确确认采集成功，请检查采集箱后再决定是否重新排队" }
    : result;
  if (status === "NEEDS_CONFIRMATION" && !resultForUpdate?.detail?.product_page_mismatch && tabId) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    const mismatch = dianxiaomiProductPageMismatch(handoff.target_url, tab?.url);
    if (mismatch) {
      resultForUpdate = {
        ...(resultForUpdate || {}),
        error_type: "product_page_mismatch",
        detail: { ...(resultForUpdate?.detail || {}), product_page_mismatch: mismatch },
      };
    }
  }
  const classifiedResult = status === "NEEDS_CONFIRMATION" && !resultForUpdate?.error_type
    ? {
      ...(resultForUpdate || {}),
      error_type: isNonBlockingDianxiaomiProductMismatch(resultForUpdate)
        ? "product_page_mismatch"
        : "manual_confirmation_required",
    }
    : resultForUpdate;
  const updated = await updateDianxiaomiHandoffs(baseUrl, [handoff], workerId, status, classifiedResult);
  if (!updated) return;

  if (status === "NEEDS_CONFIRMATION") {
    await clearDianxiaomiActiveHandoff();
    if (!isNonBlockingDianxiaomiProductMismatch(classifiedResult)) {
      await notifyDianxiaomiConfirmation(classifiedResult.message);
    }
    // Keep this handoff pending for later human review, but do not block the
    // rest of the queue or pull its product page into the foreground.
    setTimeout(() => void pollDianxiaomiHandoffs(baseUrl), 250);
    return;
  }
  if (["SUCCEEDED", "FAILED"].includes(status)) {
    await clearDianxiaomiActiveHandoff(true);
    await chrome.notifications.clear(DIANXIAOMI_NOTIFICATION_ID).catch(() => undefined);
    setTimeout(() => void pollDianxiaomiHandoffs(baseUrl), 250);
    return;
  }
  setTimeout(() => void pollDianxiaomiHandoffs(baseUrl), 2000);
}

async function pauseDianxiaomiHandoff(baseUrl, handoff, workerId, message, tabId = null, detail = null) {
  let mismatch = detail?.product_page_mismatch || null;
  if (!mismatch && tabId && isNonBlockingDianxiaomiProductMismatch(message)) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    mismatch = dianxiaomiProductPageMismatch(handoff.target_url, tab?.url);
  }
  const result = {
    message,
    error_type: mismatch || isNonBlockingDianxiaomiProductMismatch(message)
      ? "product_page_mismatch"
      : "manual_confirmation_required",
    ...(mismatch ? { detail: { product_page_mismatch: mismatch } } : {}),
  };
  const updated = await updateDianxiaomiHandoffs(baseUrl, [handoff], workerId, "NEEDS_CONFIRMATION", result);
  if (!updated) return;
  await clearDianxiaomiActiveHandoff();
  if (!isNonBlockingDianxiaomiProductMismatch(result)) {
    await notifyDianxiaomiConfirmation(message);
  }
  // This item remains available in the dashboard for review; continue with
  // the next queued product without waiting for a decision on this one.
  setTimeout(() => void pollDianxiaomiHandoffs(baseUrl), 250);
}

async function processDianxiaomiHandoff(baseUrl, handoff, workerId, dashboardWindowId, justClaimed = false) {
  const stored = await chrome.storage.local.get([
    "dianxiaomiActiveHandoffId",
    "dianxiaomiActiveTabId",
    "dianxiaomiProductFlowHandoffId",
  ]);
  let tab = null;
  let tabId = stored.dianxiaomiActiveTabId;

  if (justClaimed || handoff.status === "CLAIMED") {
    await chrome.storage.local.remove(DIANXIAOMI_PRODUCT_OPERATION_KEY);
    await chrome.storage.local.set({
      dianxiaomiActiveHandoffId: handoff.id,
      dianxiaomiProductFlowHandoffId: handoff.id,
    });
    if (!isAliExpressProductPage(handoff.target_url)) {
      await finishDianxiaomiHandoff(baseUrl, handoff, workerId, {
        state: "failed",
        message: "队列链接不是有效的 AliExpress 商品详情页",
      }, null);
      return;
    }
    const ensured = await ensureDianxiaomiProductTab(handoff.target_url, dashboardWindowId);
    tab = ensured.tab;
    tabId = tab.id;
    await chrome.storage.local.set({ dianxiaomiActiveTabId: tabId });
    const opened = await updateDianxiaomiHandoffs(baseUrl, [handoff], workerId, "OPENED");
    if (!opened) return;
    handoff.status = "OPENED";
  } else {
    if (!["OPENED", "FILLED", "COLLECTING"].includes(handoff.status)) return;
    if (String(stored.dianxiaomiProductFlowHandoffId || "") !== String(handoff.id)) {
      await pauseDianxiaomiHandoff(
        baseUrl,
        handoff,
        workerId,
        "此任务是在商品页助手流程启用前提交的旧采集任务。请先检查店小秘采集箱是否已有结果，再决定是否重新排队。",
        tabId,
      );
      return;
    }
    if (!tabId) {
      await pauseDianxiaomiHandoff(baseUrl, handoff, workerId, "工作商品页已关闭，无法确认助手采集结果；请检查店小秘后再重新排队");
      return;
    }
    tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) {
      await pauseDianxiaomiHandoff(baseUrl, handoff, workerId, "工作商品页已关闭，无法确认助手采集结果；请检查店小秘后再重新排队");
      return;
    }
    if (productIdFromUrl(tab.url) !== productIdFromUrl(handoff.target_url)) {
      await pauseDianxiaomiHandoff(baseUrl, handoff, workerId, "工作商品页已切换，无法确认当前队列商品的采集结果；请检查后再重新排队", tab.id);
      return;
    }
  }

  let result;
  if (justClaimed || (handoff.status === "OPENED" && tab?.status === "complete")) {
    result = await sendDianxiaomiProductMessage(tabId, {
      type: "dianxiaomi-start-product",
      handoff_id: handoff.id,
      target_url: handoff.target_url,
    });
  } else {
    result = await sendDianxiaomiProductMessage(tabId, {
      type: "dianxiaomi-product-status",
      handoff_id: handoff.id,
    });
    if (result?.state === "not_started" && handoff.status === "OPENED") {
      result = await sendDianxiaomiProductMessage(tabId, {
        type: "dianxiaomi-start-product",
        handoff_id: handoff.id,
        target_url: handoff.target_url,
      });
    } else if (result?.state === "not_started" && handoff.status === "COLLECTING") {
      result = {
        state: "needs_confirmation",
        message: "店小秘助手的运行状态丢失，无法安全判断任务是否已提交；请检查采集箱后再重新排队",
      };
    }
  }
  await finishDianxiaomiHandoff(baseUrl, handoff, workerId, result, tabId);
}

async function pollDianxiaomiHandoffs(requestedBaseUrl = null) {
  await ensureWorkerModesInitialized();
  if (dianxiaomiBusy) return;
  if (!dianxiaomiWorkerModeReady) return;
  const dashboard = await dianxiaomiWorkerDashboardContext();
  if (!dashboard) return;
  const identity = await browserCollectionWorkerIdentity();
  dianxiaomiBusy = true;
  const workerId = identity.worker_id;
  const baseUrl = requestedBaseUrl || (await chrome.storage.local.get("activeApiBase")).activeApiBase || API_BASE;
  let handoff = null;
  try {
    if (requestedBaseUrl) await chrome.storage.local.set({ activeApiBase: requestedBaseUrl });
    await refreshApiTokenFromDashboard(baseUrl);
    let heartbeat = null;
    try {
      heartbeat = await api("/api/integrations/dianxiaomi/queue/heartbeat", {
        method: "POST",
        body: JSON.stringify({ worker_id: workerId }),
      }, baseUrl);
    } catch (error) {
      if (error.status !== 404) throw error;
    }
    if (heartbeat?.paused) {
      if (heartbeat.paused_reason === "awaiting_start") {
        await closeDianxiaomiProductTabAfterQueueDrained(baseUrl);
      }
      return;
    }
    const existing = await api("/api/integrations/dianxiaomi/handoffs?limit=500", {}, baseUrl);

    const active = existing.find((item) => item.worker_id === workerId && ["CLAIMED", "OPENED", "FILLED", "COLLECTING"].includes(item.status));
    if (active) {
      handoff = active;
      await processDianxiaomiHandoff(baseUrl, active, workerId, dashboard.windowId);
      return;
    }

    const localState = await chrome.storage.local.get("dianxiaomiActiveHandoffId");
    if (localState.dianxiaomiActiveHandoffId) await clearDianxiaomiActiveHandoff(true);
    const claimed = await api("/api/integrations/dianxiaomi/handoffs/claim-batch", {
      method: "POST",
      body: JSON.stringify({ worker_id: workerId, limit: 1 }),
    }, baseUrl);
    if (!claimed?.length) {
      await closeDianxiaomiProductTabAfterQueueDrained(baseUrl);
      return;
    }
    handoff = claimed[0];
    await processDianxiaomiHandoff(baseUrl, handoff, workerId, dashboard.windowId, true);
  } catch (error) {
    if (handoff) {
      await pauseDianxiaomiHandoff(
        baseUrl,
        handoff,
        workerId,
        `${error.message || "店小秘助手页面操作失败"}。请检查商品页和店小秘采集箱后再决定是否重新排队。`,
        (await chrome.storage.local.get("dianxiaomiActiveTabId")).dianxiaomiActiveTabId,
        error.detail || null,
      );
    }
    // The dashboard may be offline; claimed tasks remain protected by the backend lease.
  } finally {
    dianxiaomiBusy = false;
  }
}

chrome.runtime.onInstalled.addListener(() => {
  void syncDedicatedWorkerMode();
  void ensureAlarm();
  void injectOpenAliExpressTabs();
  void pollAutomation();
  void pollDianxiaomiHandoffs();
});

chrome.runtime.onStartup.addListener(() => {
  void syncDedicatedWorkerMode();
  void ensureAlarm();
  void injectOpenAliExpressTabs();
  setTimeout(() => void pollAutomation(), 1500);
  setTimeout(() => void pollDianxiaomiHandoffs(), 2000);
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) void pollAutomation();
  if (alarm.name === POLL_ALARM) void pollDianxiaomiHandoffs();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  rememberTabProduct(tabId, changeInfo.url || tab.url);
  if (changeInfo.status === "complete" && isAliExpressChallengePage(tab.url)) {
    void pauseForAliExpressChallenge(tabId).catch(() => undefined);
    return;
  }
  if (changeInfo.status === "complete" && isAliExpressPage(tab.url)) {
    void injectCollector(tabId);
    void browserCollectionWorkerEnabled().then((enabled) => enabled ? automationState() : null).then((state) => {
      if (!state) return;
      if (state.automationTabId === tabId) {
        if (state.challengePaused) void pollAutomation();
        else void processAutomationTab(tabId);
      }
    });
  }
  if (changeInfo.status === "complete" && isAliExpressProductPage(tab.url)) {
    void injectDianxiaomiProductBridge(tabId);
  }
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (tab && isAliExpressPage(tab.url)) void injectCollector(tabId);
  if (tab && isAliExpressProductPage(tab.url)) void injectDianxiaomiProductBridge(tabId);
});

chrome.webNavigation.onCommitted.addListener(({ tabId, url }) => {
  rememberTabProduct(tabId, url);
});

chrome.webNavigation.onHistoryStateUpdated.addListener(({ tabId, url }) => {
  rememberTabProduct(tabId, url);
  if (isAliExpressChallengePage(url)) void pauseForAliExpressChallenge(tabId).catch(() => undefined);
  else if (isAliExpressPage(url)) void injectCollector(tabId);
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabProductIds.delete(tabId);
  void browserCollectionWorkerEnabled().then((enabled) => enabled ? automationState() : null).then(async (state) => {
    if (!state) return;
    if (state.automationTabId === tabId && !state.challengePaused) {
      void finishAutomationFailure(state, "browser_tab_closed", "自动采集标签页被关闭");
    } else if (state.prefetchedTabId === tabId && state.prefetchedItem) {
      void api(`/api/browser-collection/items/${state.prefetchedItem.id}/failure`, {
        method: "POST",
        body: JSON.stringify({
          ...(await browserCollectionWorkerIdentity()),
          claim_token: state.prefetchedClaimToken,
          error_type: "browser_tab_closed",
          error_message: "预加载商品页标签被关闭，任务已退回队列",
        }),
      }).catch(() => undefined).finally(() => clearPrefetchedItem());
    } else if (state.idleAutomationTabId === tabId) {
      void chrome.storage.local.remove("idleAutomationTabId");
    }
  });
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId >= 0) void guardAutomationWindowFocus(windowId);
});

chrome.notifications.onClicked.addListener(async (notificationId) => {
  if (![NOTIFICATION_ID, FIRST_PRODUCT_NOTIFICATION_ID].includes(notificationId)) return;
  const state = await automationState();
  const targetTabId = state.automationTabId;
  const targetTab = targetTabId ? await chrome.tabs.get(targetTabId).catch(() => null) : null;
  if (targetTab?.windowId) {
    await chrome.windows.update(targetTab.windowId, { focused: true, state: "normal" }).catch(() => undefined);
  } else if (state.automationWindowId) {
    await chrome.windows.update(state.automationWindowId, { focused: true, state: "normal" }).catch(() => undefined);
  }
  if (targetTabId) await chrome.tabs.update(targetTabId, { active: true }).catch(() => undefined);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "store-collection-product-progress") {
    void saveStoreProductProgress(message, sender, sendResponse);
    return true;
  }
  if (message?.type === "store-candidate-progress") {
    void saveStoreCandidateProgress(message, sender, sendResponse);
    return true;
  }
  if (message?.type === "dashboard-context") {
    void (async () => {
      const dashboardUrl = sender?.tab?.url || "http://127.0.0.1";
      const explicitLocalDianxiaomiWorker = message.worker_mode === true
        && isLocalMonitorDashboard(dashboardUrl)
        && (() => {
          try {
            const url = new URL(dashboardUrl);
            return url.searchParams.get("dianxiaomi_worker") === "1"
              && url.searchParams.get("dianxiaomi_local") === "1";
          } catch { return false; }
        })();
      const dianxiaomiBrowserWorker = message.dianxiaomi_browser_worker_mode === true;
      const dianxiaomiWorkerEnabled = dianxiaomiBrowserWorker || explicitLocalDianxiaomiWorker;
      const stored = await chrome.storage.local.get("apiAccessTokens");
      const workerMode = message.worker_mode === true;
      if (message.browser_collection_mode_explicit) {
        const parsedDashboardUrl = new URL(dashboardUrl);
        const loopbackDashboard = isLoopbackMonitorDashboard(parsedDashboardUrl.href);
        const isolatedLocalEnabled = message.browser_collection_mode === true
          && isIsolatedBrowserCollectorUrl(parsedDashboardUrl.href);
        const remoteWorkerEnabled = message.browser_collection_mode === true
          && !loopbackDashboard && !isolatedLocalEnabled;
        const workerEnabled = isolatedLocalEnabled || remoteWorkerEnabled;
        await chrome.storage.local.set({
          browserCollectionWorkerMode: workerEnabled,
          browserCollectionLocalProfileEnabled: isolatedLocalEnabled,
          browserCollectionIsolatedProfileAuthorized: isolatedLocalEnabled,
          browserCollectionRemoteProfileEnabled: remoteWorkerEnabled,
          browserCollectionWorkerLabel: remoteWorkerEnabled
            ? browserCollectionWorkerLabelFromUrl(dashboardUrl.href)
            : "",
          ...(workerEnabled && Number.isInteger(sender?.tab?.id)
            ? { automationDashboardTabId: sender.tab.id }
            : {}),
        });
        browserCollectionModeReady = true;
        if (!workerEnabled) await closeAutomationWindow();
      }
      if (message.dianxiaomi_mode_explicit) {
        await chrome.storage.local.set({
          dianxiaomiWorkerMode: workerMode,
          dianxiaomiLocalProfileEnabled: workerMode,
        });
        dianxiaomiWorkerModeReady = true;
      }
      if (dianxiaomiWorkerEnabled && Number.isInteger(sender?.tab?.id)
        && Number.isInteger(sender?.tab?.windowId)) {
        await chrome.storage.local.set({
          dianxiaomiWorkerMode: true,
          dianxiaomiLocalProfileEnabled: explicitLocalDianxiaomiWorker,
          dianxiaomiDashboardTabId: sender.tab.id,
          dianxiaomiDashboardWindowId: sender.tab.windowId,
        });
        dianxiaomiWorkerModeReady = true;
      } else if (message.dianxiaomi_mode_explicit && !workerMode) {
        const current = await chrome.storage.local.get("dianxiaomiDashboardTabId");
        if (current.dianxiaomiDashboardTabId === sender?.tab?.id) {
          await chrome.storage.local.remove(["dianxiaomiDashboardTabId", "dianxiaomiDashboardWindowId"]);
        }
        await chrome.storage.local.set({ dianxiaomiWorkerMode: false, dianxiaomiLocalProfileEnabled: false });
        dianxiaomiWorkerModeReady = true;
      }
      const nextState = {
        ...(message.api_base_url ? { activeApiBase: message.api_base_url } : {}),
      };
      if (message.api_access_token && message.api_base_url) {
        nextState.apiAccessToken = message.api_access_token;
        nextState.apiAccessTokens = {
          ...(stored.apiAccessTokens || {}),
          [message.api_base_url]: message.api_access_token,
        };
      }
      await chrome.storage.local.set(nextState);
      if (dianxiaomiWorkerEnabled) await pollDianxiaomiHandoffs(message.api_base_url || null);
    })();
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type === "dianxiaomi-handoff-created") {
    void (async () => {
      if (message.api_base_url) await chrome.storage.local.set({ activeApiBase: message.api_base_url });
      await pollDianxiaomiHandoffs(message.api_base_url || null);
    })();
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type === "browser-collection-run-requested") {
    void (async () => {
      if (message.api_base_url) await chrome.storage.local.set({ activeApiBase: message.api_base_url });
      if (await browserCollectionWorkerEnabled()) await pollAutomation();
    })();
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type === "review-collection-created") {
    if (message.api_base_url) void chrome.storage.local.set({ activeApiBase: message.api_base_url });
    void pollAutomation();
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type !== "sync-product-snapshot") return false;
  const monitoredProductId = sender?.tab?.id ? tabProductIds.get(sender.tab.id) : null;
  const payload = {
    ...message.payload,
    raw_data: {
      ...(message.payload?.raw_data || {}),
      ...(monitoredProductId ? { monitored_product_id: monitoredProductId } : {}),
    },
  };
  api("/api/collection/browser-extension", {
    method: "POST",
    body: JSON.stringify(payload),
  })
    .then((body) => sendResponse({ ok: true, status: 201, body }))
    .catch((error) => sendResponse({ ok: false, status: 0, body: { detail: error.message || "无法连接本地监控台" } }));
  return true;
});

void ensureAlarm();
void syncDedicatedWorkerMode();
void injectOpenAliExpressTabs();
setTimeout(() => void pollAutomation(), 1500);
setTimeout(() => void pollDianxiaomiHandoffs(), 2000);
