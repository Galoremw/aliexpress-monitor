importScripts("config.js");

const API_BASE = globalThis.ALIEXPRESS_MONITOR_API_BASE || "http://127.0.0.1:8000";
const POLL_ALARM = "aliexpress-monitor-browser-collection";
const NOTIFICATION_ID = "aliexpress-monitor-verification";
const MAX_OPEN_COLLECTION_PAGES = 2;
const AUTH_REQUIRED_NOTIFICATION_ID = "aliexpress-monitor-auth-required";
const FIRST_PRODUCT_NOTIFICATION_ID = "aliexpress-monitor-first-product";
const DIANXIAOMI_NOTIFICATION_ID = "aliexpress-monitor-dianxiaomi-confirmation";
const DIANXIAOMI_URL = "https://www.dianxiaomi.com/web/productCrawl/dataAcquisition";
const COLLECTION_RESPONSE_TIMEOUT_MS = 90 * 1000;
const STORE_COLLECTION_RESPONSE_TIMEOUT_MS = 18 * 60 * 1000;
const STORE_PAGE_RESPONSE_TIMEOUT_MS = 4 * 60 * 1000;
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

function isAliExpressChallengePage(url) {
  try {
    const parsed = new URL(url);
    return /(?:^|\.)aliexpress\.com$/i.test(parsed.hostname)
      && /(?:^|\/)(?:punish|captcha|challenge|verification|verify|security-check)(?:\/|$)/i.test(parsed.pathname);
  } catch {
    return false;
  }
}

function isDianxiaomiPage(url) {
  try {
    const parsed = new URL(url);
    return /(?:^|\.)dianxiaomi\.com$/i.test(parsed.hostname)
      && /\/web\/productCrawl\/dataAcquisition/i.test(parsed.pathname);
  } catch {
    return false;
  }
}

function isLocalMonitorDashboard(url) {
  try {
    const parsed = new URL(url);
    const isLanCollectorHost = parsed.hostname === "192.168.66.107" && parsed.port === "3000";
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
    return isLoopbackMonitorDashboard(parsed.href)
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
      "http://192.168.66.107:3000/*",
      "http://127.0.0.1:8000/*",
      "http://localhost:8000/*",
      "https://galoremw.github.io/*",
      "https://*.onrender.com/*",
    ],
  });
  const dashboard = tabs.find((tab) => tab.url?.includes("dianxiaomi_worker=1")) || tabs[0];
  if (!dashboard?.id) return false;
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
    if (!token) return false;
    await chrome.storage.local.set({
      apiAccessToken: token,
      apiAccessTokens: {
        ...(stored.apiAccessTokens || {}),
        [baseUrl]: token,
      },
    });
    return true;
  } catch {
    return false;
  }
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

async function injectDianxiaomiBridge(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!isDianxiaomiPage(tab.url)) return;
    await chrome.scripting.executeScript({ target: { tabId }, files: ["dianxiaomi_bridge.js"] });
  } catch {
    // The tab may still be loading; the next alarm retries without touching credentials.
  }
}

async function injectOpenAliExpressTabs() {
  const tabs = await chrome.tabs.query({
    url: ["https://aliexpress.com/*", "https://*.aliexpress.com/*"],
  });
  await Promise.all(tabs.map((tab) => injectCollector(tab.id)));
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
  const label = state.browserCollectionLocalProfileEnabled === true
    ? "Galore主机"
    : state.browserCollectionRemoteProfileEnabled === true ? "第二台电脑" : "Chrome采集器";
  return { worker_id: state.browserCollectionWorkerId, worker_label: label };
}

async function ensureIdleAutomationTab(workerWindow, state) {
  if (!workerWindow?.id) return null;
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
    automationWindowOwned: true,
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

async function ensureAutomationWindow({ ensureIdle = true, focus = null } = {}) {
  const state = await automationState();
  const foregroundCollection = focus ?? state.browserCollectionWorkerMode === true;
  const windowState = foregroundCollection
    ? "maximized"
    : state.browserCollectionIsolatedProfileAuthorized === true ? "normal" : "minimized";
  const dashboardTab = state.automationDashboardTabId
    ? await chrome.tabs.get(state.automationDashboardTabId).catch(() => null)
    : null;
  let workerWindow = state.automationWindowId && state.automationWindowBackground
    ? await chrome.windows.get(state.automationWindowId).catch(() => null)
    : null;
  if (workerWindow?.id && workerWindow.id === dashboardTab?.windowId) workerWindow = null;
  if (workerWindow?.id) {
    await chrome.windows.update(workerWindow.id, { focused: foregroundCollection, state: windowState }).catch(() => undefined);
  } else {
    workerWindow = await chrome.windows.create({
      focused: foregroundCollection,
      state: windowState,
      type: "normal",
      url: "about:blank",
    }).catch(() => null);
  }
  if (!workerWindow?.id) return null;

  await chrome.storage.local.set({
    automationWindowId: workerWindow.id,
    automationWindowOwned: true,
    automationWindowBackground: true,
    automationWindowFocusGuardUntil: Date.now() + 5000,
  });
  if (ensureIdle) await ensureIdleAutomationTab(workerWindow, state);
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
  const tabsToMove = trackedTabs.filter((tab) => tab?.windowId === dashboardTab.windowId);
  if (!tabsToMove.length) return state;

  const workerWindow = await ensureAutomationWindow({ ensureIdle: false });
  if (!workerWindow) return state;
  for (const tab of tabsToMove) {
    await chrome.tabs.move(tab.id, { windowId: workerWindow.id, index: -1 });
    const movedTab = await chrome.tabs.get(tab.id).catch(() => null);
    if (movedTab?.windowId !== workerWindow.id) {
      throw new Error("采集页未能迁移到独立采集窗口，暂不继续处理在途任务");
    }
  }

  const taskTabIds = [state.automationTabId, state.prefetchedTabId]
    .filter((tabId) => Number.isInteger(tabId));
  await groupAutomationTabs(taskTabIds, workerWindow.id);
  if (Number.isInteger(state.automationTabId)) {
    await chrome.tabs.update(state.automationTabId, { active: true }).catch(() => undefined);
  }
  if (state.browserCollectionWorkerMode === true) {
    await chrome.windows.update(workerWindow.id, { focused: true, state: "maximized" }).catch(() => undefined);
  }
  const migratedState = await automationState();
  await ensureIdleAutomationTab(workerWindow, migratedState);
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
  const foregroundCollection = state.browserCollectionWorkerMode === true;
  await chrome.windows.update(windowId, {
    focused: foregroundCollection,
    state: foregroundCollection ? "maximized" : state.browserCollectionIsolatedProfileAuthorized === true
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
      "http://192.168.66.107:3000/*",
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
        && !["127.0.0.1", "localhost"].includes(url.hostname);
    } catch { return false; }
  });
  const localDianxiaomiWorker = tabs.some((tab) => {
    try {
      const url = new URL(tab.url);
      return ["127.0.0.1", "localhost"].includes(url.hostname)
        && url.searchParams.get("dianxiaomi_worker") === "1"
        && url.searchParams.get("dianxiaomi_local") === "1";
    } catch { return false; }
  });
  const state = await chrome.storage.local.get([
    "dianxiaomiLocalProfileEnabled",
    "browserCollectionIsolatedProfileAuthorized",
    "browserCollectionRemoteProfileEnabled",
  ]);
  const explicitNonLocalWorker = tabs.some((tab) => {
    try {
      const url = new URL(tab.url);
      return url.searchParams.get("dianxiaomi_worker") === "1"
        && url.searchParams.get("dianxiaomi_local") !== "1";
    } catch { return false; }
  });
  const dianxiaomiEnabled = localDianxiaomiWorker
    || (state.dianxiaomiLocalProfileEnabled === true && !explicitNonLocalWorker);
  const isolatedLocalEnabled = Boolean(isolatedLocalTab);
  const remoteWorkerEnabled = Boolean(remoteWorkerTab);
  await chrome.storage.local.set({
    browserCollectionWorkerMode: isolatedLocalEnabled || remoteWorkerEnabled,
    browserCollectionLocalProfileEnabled: isolatedLocalEnabled,
    browserCollectionIsolatedProfileAuthorized: isolatedLocalEnabled,
    browserCollectionRemoteProfileEnabled: remoteWorkerEnabled,
    ...((isolatedLocalTab || remoteWorkerTab)
      ? { automationDashboardTabId: (isolatedLocalTab || remoteWorkerTab).id }
      : {}),
    dianxiaomiWorkerMode: dianxiaomiEnabled,
    dianxiaomiLocalProfileEnabled: localDianxiaomiWorker
      || (state.dianxiaomiLocalProfileEnabled === true && !explicitNonLocalWorker),
  });
  browserCollectionModeReady = true;
  dianxiaomiWorkerModeReady = true;
  return { browserCollectionEnabled: isolatedLocalEnabled || remoteWorkerEnabled, localDianxiaomiWorker };
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
  const existingIdleIsReusable = existingIdleTab?.windowId === state.automationWindowId
    && existingIdleTab.id !== state.automationDashboardTabId;
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
  const page = storeOrdersPageIdentity(url);
  if (!page) return null;
  try {
    const parsed = new URL(url);
    if (parsed.username || parsed.password || (parsed.port && parsed.port !== "443")) return null;
    return {
      route_store_id: page.storeId,
      normalized_page_identity: `${parsed.hostname.toLowerCase()}${parsed.pathname}`,
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
    && identity?.normalized_page_identity === current.normalized_page_identity
    && String(storeId || "") !== current.route_store_id
  ) || null;
}

function normalizedStoreName(value) {
  return typeof value === "string"
    ? value.normalize("NFKC").trim().replace(/\s+/g, " ").toLocaleLowerCase()
    : "";
}

function storeIdentityProofHasConflict(proof, storeId) {
  const displayedIds = [proof?.heading_store_id, ...(proof?.labeled_store_ids || [])]
    .filter((value) => value != null).map(String);
  return displayedIds.some((id) => id !== String(storeId));
}

function storeRouteAliasIsVerified(url, storeId, approvedIdentities, proof) {
  const page = storeOrdersPageIdentity(url);
  if (!page || page.storeId === String(storeId) || !proof
    || proof.target_store_id !== String(storeId)
    || String(proof.route_store_id) !== page.storeId
    || storeIdentityProofHasConflict(proof, storeId)) return false;
  const matchingDisplayedId = String(proof.heading_store_id || "") === String(storeId)
    || (Array.isArray(proof.labeled_store_ids)
      && proof.labeled_store_ids.map(String).includes(String(storeId)));
  if (matchingDisplayedId) return true;
  if (proof.page_url !== url) return false;

  const approvedAlias = approvedStoreUrlIdentity(url, storeId, approvedIdentities);
  const savedName = approvedAlias?.evidence?.ixspy_store_name;
  const currentName = proof.ixspy_store_name;
  const namesConflict = Boolean(savedName && currentName
    && normalizedStoreName(savedName) !== normalizedStoreName(currentName));
  const proofPage = storeOrdersUrlIdentity(proof.page_url);
  const currentPage = storeOrdersUrlIdentity(url);
  return Boolean(approvedAlias
    && proof.identity_method === "backend_approved_url_alias"
    && proofPage?.route_store_id === currentPage?.route_store_id
    && proofPage?.normalized_page_identity === currentPage?.normalized_page_identity
    && !namesConflict);
}

function storeOrdersUrlAllowedForTask(url, storeId, approvedIdentities = [], identityProof = null) {
  const page = storeOrdersPageIdentity(url);
  if (!page) return false;
  if (page.storeId === String(storeId)) return true;
  if (approvedStoreUrlIdentity(url, storeId, approvedIdentities)) return true;
  const proofPage = storeOrdersUrlIdentity(identityProof?.page_url);
  const currentPage = storeOrdersUrlIdentity(url);
  const explicitIds = [identityProof?.heading_store_id, ...(identityProof?.labeled_store_ids || [])]
    .filter((value) => value != null).map(String);
  return Boolean(identityProof
    && identityProof.target_store_id === String(storeId)
    && proofPage?.route_store_id === currentPage?.route_store_id
    && proofPage?.normalized_page_identity === currentPage?.normalized_page_identity
    && explicitIds.includes(String(storeId))
    && !storeIdentityProofHasConflict(identityProof, storeId));
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
      && storeRouteAliasIsVerified(pageUrl, storeId, item?.approved_store_url_identities, identity);
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
        pageUrl, storeId, item?.approved_store_url_identities, identity,
      );
    const pageMatchesTask = Boolean(routePage && expectedPage && taskPage
      && routePage.page === expectedPage.page
      && routePage.page === taskPage.page
      && message.expected_page_url === pageUrl);
    const history = product?.historical_sales;
    const expectedDates = product?.history_expected_dates;
    const missingDates = product?.history_missing_dates;
    const chartRead = product?.history_read_diagnostic;
    const requiredDates = expectedStoreCollectionDates(item?.target_date);
    const completeRead = product?.history_daily_capture_complete === true
      && Array.isArray(expectedDates) && expectedDates.length === 7
      && expectedDates.every((date, index) => date === requiredDates[index])
      && Array.isArray(history) && history.length === 7
      && expectedDates.every((date) => history.some((point) => point?.date === date));
    const unavailableRead = chartRead?.outcome === "unavailable"
      && typeof chartRead.source === "string"
      && (/^ixspy_store_(?:card_|product_chart_unavailable)/.test(chartRead.source)
        || chartRead.source === "ixspy_data_unavailable")
      && !chartRead.error
      && Array.isArray(expectedDates) && expectedDates.length === 7
      && expectedDates.every((date, index) => date === requiredDates[index])
      && Array.isArray(missingDates) && expectedDates.every((date) => missingDates.includes(date))
      && Array.isArray(history) && history.length === 0;
    if (!item || item.target_type !== "STORE"
      || Number(item.id) !== Number(message.item_id)
      || state.automationTabId !== sender?.tab?.id
      || state.automationClaimToken !== message.claim_token
      || message.expected_store_id !== storeId
      || !pageMatchesTask
      || !safeStoreOrdersUrl(pageUrl, storeId, routeAliasIsVerified ? routePage.storeId : null)
      || !identity || identity.target_store_id !== storeId
      || String(identity.route_store_id) !== routePage?.storeId
      || identity.page_url !== pageUrl
      || productIdFromUrl(product?.url) !== productId
      || (!completeRead && !unavailableRead)) {
      sendResponse({ ok: false });
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
) {
  const expectedPage = storeOrdersPageIdentity(expectedUrl);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab && [tab.url, tab.pendingUrl].some(isAliExpressChallengePage)) {
      const error = new Error("AliExpress 要求人工完成验证");
      error.code = "PLATFORM_CHALLENGE";
      throw error;
    }
    if (tab?.status === "complete" && !tab.pendingUrl) {
      const currentPage = storeOrdersPageIdentity(tab.url);
      if (expectedPage && currentPage && currentPage.page === expectedPage.page) {
        const targetStoreId = String(expectedStoreId || expectedPage.storeId);
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
        const backendAliasProof = Boolean(approvedAlias
          && identity?.state === "ready"
          && identity.platform_store_id === targetStoreId
          && proof?.identity_method === "backend_approved_url_alias"
          && proof?.target_store_id === targetStoreId
          && String(proof.route_store_id) === currentPage.storeId
          && proof.page_url === tab.url
          && !storeIdentityProofHasConflict(proof, targetStoreId)
          && !namesConflict);
        if (identity?.state === "ready" && identity.platform_store_id === targetStoreId
          && proof?.target_store_id === targetStoreId
          && String(proof.route_store_id) === currentPage.storeId
          && proof.page_url === tab.url
          && matchingDisplayedId && !headingMismatch && !labeledMismatch) {
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
    storeOrdersUrlAllowedForTask(pageUrl, storeId, approvedStoreUrlIdentities)));
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
        tab.url, storeId, approvedStoreUrlIdentities, progress.store_identity,
      )) {
      storeIdentity = progress.store_identity;
    }
    const isKnownStorePage = storeOrdersUrlAllowedForTask(
      tab.url, storeId, approvedStoreUrlIdentities, storeIdentity,
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
          tabId, recoveryUrl, recoveryBudgetMs, storeId, approvedStoreUrlIdentities,
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
        tab.url, storeId, approvedStoreUrlIdentities, storeIdentity,
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
    const resultRouteId = storeOrdersPageIdentity(tab.url)?.storeId;
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
    if (products.size === targets.length || !result.has_next_page) break;
    if (!nextUrl || !storeOrdersUrlAllowedForTask(
      nextUrl, storeId, approvedStoreUrlIdentities, storeIdentity,
    ) || visitedThisAttempt.has(nextUrl)) break;
    const navigationBudgetMs = Math.min(STORE_PAGE_NAVIGATION_TIMEOUT_MS, storeCollectionWorkRemaining(startedAt));
    if (navigationBudgetMs <= 0) {
      collectionInterrupted = true;
      break;
    }
    await chrome.tabs.update(tabId, { url: nextUrl, active: true });
    const readyPage = await waitForStorePage(
      tabId, nextUrl, navigationBudgetMs, storeId, approvedStoreUrlIdentities,
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
        discoveryUrl, storeId, approvedStoreUrlIdentities, storeIdentity,
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
                tabId, discoveryUrl, navigationBudgetMs, storeId, approvedStoreUrlIdentities,
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
            currentTab.url, storeId, approvedStoreUrlIdentities, storeIdentity,
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
                pageResult.next_page_url, storeId, approvedStoreUrlIdentities, storeIdentity,
              )
                  ? pageResult.next_page_url
                  : null;
                candidatePage = {
                  page_url: candidatePageUrl,
                  pagination_known: pageResult.pagination_known === true,
                  has_next_page: Boolean(pageResult.has_next_page),
                  next_page_url: nextPageUrl,
                  scan_complete: scanComplete,
                  candidates: scanComplete && Array.isArray(pageResult.candidates)
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
              candidatePage = incompleteCandidatePage(
                error?.code === "COLLECTION_TIMEOUT" ? "page_response_timeout" : "page_scan_error",
                error?.message,
              );
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

  await chrome.windows.update(workerWindowId, { state: "normal" }).catch(() => undefined);
  await chrome.windows.update(workerWindowId, { focused: true }).catch(() => undefined);
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

async function keepAutomationWindowForeground() {
  const state = await automationState();
  if (state.automationWindowBackground && state.automationWindowId && state.browserCollectionWorkerMode === true) {
    await chrome.windows.update(state.automationWindowId, {
      focused: true,
      state: "maximized",
    }).catch(() => undefined);
  }
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

async function pauseForStoreIdentityReview(tabId, message) {
  const state = await automationState();
  if (!state.automationItem || state.automationTaskKind === "REVIEW"
    || state.automationTaskKind === "POOL_RESET"
    || state.automationTabId !== tabId || state.identityReviewPaused) return;
  const item = state.automationItem;
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const pageUrl = message?.page_url || tab?.url || "";
  const routeIdentity = storeOrdersUrlIdentity(pageUrl);
  const evidence = message?.evidence && typeof message.evidence === "object"
    ? message.evidence
    : {
      route_store_id: routeIdentity?.route_store_id || "",
      heading_store_id: null,
      labeled_store_ids: [],
      page_title: null,
      ixspy_store_name: null,
      identity_method: "unverified",
    };
  const identityErrorType = ["store_identity_mismatch", "store_identity_unverified"]
    .includes(message?.error_type) ? message.error_type : "store_identity_unverified";
  if (!routeIdentity || evidence.route_store_id !== routeIdentity.route_store_id) {
    await finishAutomationFailure(state, "store_identity_evidence_invalid", "店铺身份证据与当前页面地址不一致");
    return;
  }
  await api(`/api/browser-collection/items/${item.id}/identity-review`, {
    method: "POST",
    body: JSON.stringify({
      ...(await browserCollectionWorkerIdentity()),
      claim_token: state.automationClaimToken,
      page_url: pageUrl,
      identity_error_type: identityErrorType,
      error_message: String(message?.message || "店铺页面身份需要人工核实").slice(0, 2000),
      evidence,
    }),
  });
  await chrome.storage.local.set({ identityReviewPaused: true, challengePaused: false });
  await holdPrefetchedItem("店铺身份待人工审核，已退回预加载项");
  await notifyChallenge(item, { provider: "STORE_IDENTITY", message: message?.message });
  await presentChallengeTab(tabId);
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
    await keepAutomationWindowForeground();
    await closeItemTab(tabId);
    return;
  }
  if (state.automationTaskKind === "POOL_RESET") {
    await processPoolResetTab(tabId, state);
    return;
  }
  if (item.target_type === "STORE") {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.windowId) {
      await chrome.windows.update(tab.windowId, {
        focused: state.browserCollectionWorkerMode === true,
        state: state.browserCollectionWorkerMode === true ? "maximized" : "normal",
      }).catch(() => undefined);
    }
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

  if (result?.state === "challenge") {
    await pauseForAliExpressChallenge(tabId, result.message || "AliExpress 要求人工完成验证");
    return;
  }

  if (result?.state === "identity_review" || result?.state === "store_identity_unverified") {
    await pauseForStoreIdentityReview(tabId, result);
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
  await api(endpoint, {
    method: "POST",
    body: JSON.stringify({
      ...result.payload,
      ...identity,
      claim_token: state.automationClaimToken,
    }),
  });
  if (item.target_type === "STORE") {
    await chrome.storage.local.remove(storeProgressKey(item.id));
  }
  await chrome.notifications.clear(NOTIFICATION_ID).catch(() => undefined);
  await chrome.notifications.clear(FIRST_PRODUCT_NOTIFICATION_ID).catch(() => undefined);
  await keepAutomationWindowForeground();
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
      await pauseForStoreIdentityReview(tabId, {
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
  const singleProductCheck = !prefetch && item.target_key === `product:${item.product_id}:single`;
  const workerWindow = await ensureAutomationWindow({ ensureIdle: !prefetch, focus: singleProductCheck ? false : null });
  if (!workerWindow) throw new Error("请先在本机 Chrome 打开一个普通窗口，再继续后台采集");
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
    ? await chrome.tabs.update(reusableIdleTab.id, { url: item.target_url, active: true })
    : await chrome.tabs.create({
      windowId: workerWindow.id,
      url: item.target_url,
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
        storeProbe ? { expectedStoreId: storeId } : null,
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

async function sendDianxiaomiMessage(tabId, urls, type = "dianxiaomi-submit-links") {
  let lastError;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      return await chrome.tabs.sendMessage(tabId, { type, urls });
    } catch (error) {
      lastError = error;
      await injectDianxiaomiBridge(tabId);
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
  }
  throw lastError || new Error("店小秘页面尚未准备好");
}

async function checkDianxiaomiReady(tabId) {
  return sendDianxiaomiMessage(tabId, [], "dianxiaomi-check-ready");
}

async function waitForDianxiaomiTab(tabId) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error("店小秘后台页面已关闭");
    if (tab.status === "complete") return tab;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return chrome.tabs.get(tabId);
}

async function ensureDianxiaomiTab() {
  const stored = await chrome.storage.local.get([
    "dianxiaomiTabId",
    "dianxiaomiTabGroupId",
    "dianxiaomiDashboardTabId",
    "dianxiaomiDashboardWindowId",
  ]);
  let tab = stored.dianxiaomiTabId
    ? await chrome.tabs.get(stored.dianxiaomiTabId).catch(() => null)
    : null;
  if (tab && !isDianxiaomiPage(tab.url)) tab = null;
  if (!tab) {
    const workerTabs = await chrome.tabs.query({
      url: ["http://127.0.0.1:3000/*", "http://localhost:3000/*"],
    });
    const storedDashboard = stored.dianxiaomiDashboardTabId
      ? await chrome.tabs.get(stored.dianxiaomiDashboardTabId).catch(() => null)
      : null;
    const workerTab = workerTabs.find((candidate) => {
      try {
        const url = new URL(candidate.url);
        return url.searchParams.get("dianxiaomi_worker") === "1"
          && url.searchParams.get("dianxiaomi_local") === "1";
      } catch { return false; }
    }) || (isLocalMonitorDashboard(storedDashboard?.url) ? storedDashboard : null)
      || workerTabs.find((candidate) => isLocalMonitorDashboard(candidate.url))
      || (Number.isInteger(stored.dianxiaomiDashboardWindowId)
        ? { windowId: stored.dianxiaomiDashboardWindowId }
        : null);
    if (!workerTab?.windowId) throw new Error("请先在本机 Chrome 打开监控台，并确保店小秘已登录");
    tab = await chrome.tabs.create({ windowId: workerTab.windowId, url: DIANXIAOMI_URL, active: false });
  }
  await waitForDianxiaomiTab(tab.id);
  let groupId = stored.dianxiaomiTabGroupId;
  try {
    if (!groupId || !(await chrome.tabGroups.get(groupId).catch(() => null))) {
      groupId = await chrome.tabs.group({ tabIds: [tab.id] });
      await chrome.storage.local.set({ dianxiaomiTabGroupId: groupId });
    } else {
      await chrome.tabs.group({ tabIds: [tab.id], groupId });
    }
    await chrome.tabGroups.update(groupId, { title: "监控后台任务", color: "blue", collapsed: true });
  } catch {
    // Tab grouping is a convenience; the background tab still works without it.
  }
  await chrome.storage.local.set({ dianxiaomiTabId: tab.id });
  return { tab, windowId: tab.windowId };
}

async function updateDianxiaomiHandoffs(baseUrl, handoffs, workerId, status, result = null) {
  await Promise.all(handoffs.map((handoff) => api(`/api/integrations/dianxiaomi/handoffs/${handoff.id}/status`, {
    method: "POST",
    body: JSON.stringify({
      status,
      worker_id: workerId,
      error_type: status === "FAILED" ? "page_operation_failed" : status === "NEEDS_CONFIRMATION" ? "manual_confirmation_required" : null,
      error_message: ["COLLECTING", "SUCCEEDED"].includes(status) ? null : result?.message || null,
    }),
  }, baseUrl).catch(() => undefined)));
}

async function pollDianxiaomiHandoffs(requestedBaseUrl = null) {
  await ensureWorkerModesInitialized();
  if (dianxiaomiBusy) return;
  const workerState = await chrome.storage.local.get("dianxiaomiWorkerMode");
  if (!dianxiaomiWorkerModeReady || !workerState.dianxiaomiWorkerMode) return;
  dianxiaomiBusy = true;
  const workerId = `chrome-${chrome.runtime.id}`;
  let baseUrl = requestedBaseUrl || (await chrome.storage.local.get("activeApiBase")).activeApiBase || API_BASE;
  let handoffs = [];
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
    if (heartbeat?.paused && heartbeat.paused_reason !== "needs_confirmation") return;
    const existing = await api("/api/integrations/dianxiaomi/handoffs?limit=500", {}, baseUrl);
    if (existing.some((item) => item.status === "NEEDS_CONFIRMATION" && item.worker_id === workerId)) {
      await chrome.storage.local.set({ dianxiaomiNeedsConfirmation: true });
    }
    const workerStorage = await chrome.storage.local.get("dianxiaomiNeedsConfirmation");
    let resumeConfirmed = false;
    if (workerStorage.dianxiaomiNeedsConfirmation) {
      const { tab } = await ensureDianxiaomiTab();
      const ready = await checkDianxiaomiReady(tab.id);
      if (ready?.state !== "ready") return;
      resumeConfirmed = true;
      await chrome.storage.local.remove("dianxiaomiNeedsConfirmation");
    }
    handoffs = await api("/api/integrations/dianxiaomi/handoffs/claim-batch", {
      method: "POST",
      body: JSON.stringify({ worker_id: workerId, limit: 1, resume_confirmed: resumeConfirmed }),
    }, baseUrl);
    if (!handoffs?.length) return;
    const { tab } = await ensureDianxiaomiTab();
    await updateDianxiaomiHandoffs(baseUrl, handoffs, workerId, "OPENED");
    const result = await sendDianxiaomiMessage(tab.id, handoffs.map((handoff) => handoff.target_url));
    const status = result?.state === "succeeded"
      ? "SUCCEEDED"
      : result?.state === "submitted"
      ? "COLLECTING"
      : result?.state === "needs_confirmation" ? "NEEDS_CONFIRMATION" : "FAILED";
    await updateDianxiaomiHandoffs(baseUrl, handoffs, workerId, status, result);
    if (result?.state === "submitted") {
      await chrome.notifications.clear(DIANXIAOMI_NOTIFICATION_ID).catch(() => undefined);
    }
    if (result?.state === "needs_confirmation") {
      await chrome.storage.local.set({ dianxiaomiNeedsConfirmation: true });
      await notifyDianxiaomiConfirmation(result.message);
      await chrome.windows.update(tab.windowId, { focused: true, state: "normal" }).catch(() => undefined);
      await chrome.tabs.update(tab.id, { active: true }).catch(() => undefined);
    } else if (["succeeded", "failed"].includes(result?.state)) {
      setTimeout(() => void pollDianxiaomiHandoffs(baseUrl), 250);
    }
  } catch (error) {
    if (handoffs.length) {
      await updateDianxiaomiHandoffs(baseUrl, handoffs, workerId, "FAILED", {
        message: error.message || "店小秘后台窗口操作失败",
      });
    }
    // The dashboard may be offline; claimed tasks are also protected by a lease on the backend.
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
  if (changeInfo.status === "complete" && isDianxiaomiPage(tab.url)) {
    void injectDianxiaomiBridge(tabId);
  }
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (tab && isAliExpressPage(tab.url)) void injectCollector(tabId);
  if (tab && isDianxiaomiPage(tab.url)) void injectDianxiaomiBridge(tabId);
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
      const localDianxiaomiDashboard = message.dianxiaomi_local_dashboard === true
        && isLocalMonitorDashboard(sender?.tab?.url);
      if (localDianxiaomiDashboard) await ensureWorkerModesInitialized();
      const stored = await chrome.storage.local.get("apiAccessTokens");
      const workerMode = message.worker_mode === true;
      if (message.browser_collection_mode_explicit) {
        const dashboardUrl = new URL(sender?.tab?.url || "http://127.0.0.1");
        const loopbackDashboard = isLoopbackMonitorDashboard(dashboardUrl.href);
        const isolatedLocalEnabled = message.browser_collection_mode === true
          && isIsolatedBrowserCollectorUrl(dashboardUrl.href);
        const remoteWorkerEnabled = message.browser_collection_mode === true && !loopbackDashboard;
        const workerEnabled = isolatedLocalEnabled || remoteWorkerEnabled;
        await chrome.storage.local.set({
          browserCollectionWorkerMode: workerEnabled,
          browserCollectionLocalProfileEnabled: isolatedLocalEnabled,
          browserCollectionIsolatedProfileAuthorized: isolatedLocalEnabled,
          browserCollectionRemoteProfileEnabled: remoteWorkerEnabled,
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
      if (localDianxiaomiDashboard) {
        await chrome.storage.local.set({
          dianxiaomiWorkerMode: true,
          dianxiaomiLocalProfileEnabled: true,
          dianxiaomiDashboardTabId: sender.tab.id,
          dianxiaomiDashboardWindowId: sender.tab.windowId,
        });
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
      if (workerMode || localDianxiaomiDashboard) await pollDianxiaomiHandoffs(message.api_base_url || null);
    })();
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type === "dianxiaomi-handoff-created") {
    void (async () => {
      if (isLocalMonitorDashboard(sender?.tab?.url)) {
        await ensureWorkerModesInitialized();
        await chrome.storage.local.set({
          dianxiaomiWorkerMode: true,
          dianxiaomiLocalProfileEnabled: true,
          dianxiaomiDashboardTabId: sender.tab.id,
          dianxiaomiDashboardWindowId: sender.tab.windowId,
          ...(message.api_base_url ? { activeApiBase: message.api_base_url } : {}),
        });
        dianxiaomiWorkerModeReady = true;
      }
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
