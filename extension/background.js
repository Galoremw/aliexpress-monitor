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
const STORE_COLLECTION_RESPONSE_TIMEOUT_MS = 10 * 60 * 1000;
const STORE_PAGE_RESPONSE_TIMEOUT_MS = 4 * 60 * 1000;
const STORE_COLLECTION_WORK_MS = 8 * 60 * 1000;
const STORE_PROGRESS_STORAGE_PREFIX = "browserCollectionStoreProgress:";
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
    && trackedIdleTab.url === "about:blank"
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

async function ensureAutomationWindow({ ensureIdle = true, focus = null } = {}) {
  const state = await automationState();
  const foregroundCollection = focus ?? state.browserCollectionWorkerMode === true;
  const windowState = foregroundCollection
    ? "maximized"
    : state.browserCollectionIsolatedProfileAuthorized === true ? "normal" : "minimized";
  let workerWindow = state.automationWindowId && state.automationWindowBackground
    ? await chrome.windows.get(state.automationWindowId).catch(() => null)
    : null;
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
  const isolatedLocalTab = tabs.some((tab) => isIsolatedBrowserCollectorUrl(tab.url));
  const remoteWorkerTab = tabs.some((tab) => {
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
  const isolatedLocalEnabled = isolatedLocalTab || state.browserCollectionIsolatedProfileAuthorized === true;
  const remoteWorkerEnabled = remoteWorkerTab || state.browserCollectionRemoteProfileEnabled === true;
  await chrome.storage.local.set({
    browserCollectionWorkerMode: isolatedLocalEnabled || remoteWorkerEnabled,
    browserCollectionLocalProfileEnabled: isolatedLocalEnabled,
    browserCollectionIsolatedProfileAuthorized: isolatedLocalEnabled,
    browserCollectionRemoteProfileEnabled: remoteWorkerEnabled,
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
    "challengePaused", "challengeProvider",
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
  const idleTabId = existingIdleTab?.windowId === state.automationWindowId && existingIdleTab.url === "about:blank"
    ? existingIdleTab.id
    : tabId;
  if (state.automationTabId === tabId) {
    if (state.prefetchedItem && state.prefetchedTabId) {
      await chrome.storage.local.set({
        automationTabId: state.prefetchedTabId,
        automationItem: state.prefetchedItem,
        ...(state.prefetchedClaimToken ? { automationClaimToken: state.prefetchedClaimToken } : {}),
        automationItemPrefetched: true,
        automationTaskKind: state.prefetchedTaskKind || "PRODUCT",
        idleAutomationTabId: idleTabId,
        ...(state.prefetchedTaskKind === "REVIEW" ? { reviewKnownKeys: state.prefetchedReviewKnownKeys || [] } : {}),
      });
      await chrome.storage.local.remove([
        "prefetchedTabId", "prefetchedItem", "prefetchedClaimToken", "prefetchedTaskKind", "prefetchedReviewKnownKeys",
      ]);
      await chrome.tabs.update(state.prefetchedTabId, { active: true }).catch(() => undefined);
      return;
    }
    await clearAutomationItem();
    await chrome.storage.local.set({ idleAutomationTabId: idleTabId });
    return;
  }
  if (state.prefetchedTabId === tabId) {
    await clearPrefetchedItem();
    await chrome.storage.local.set({ idleAutomationTabId: idleTabId });
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
    await closeItemTab(state.prefetchedTabId);
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
    } : {}),
  };
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch {
    await injectCollector(tabId);
    return chrome.tabs.sendMessage(tabId, message);
  }
}

async function sendStoreCandidateScanMessage(tabId, targetDate, storeId, pageUrl) {
  const message = {
    type: "STORE_CANDIDATE_SCAN",
    targetDate,
    expectedStoreId: storeId,
    expectedPageUrl: pageUrl,
  };
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch {
    await injectCollector(tabId);
    return chrome.tabs.sendMessage(tabId, message);
  }
}

function safeStoreOrdersUrl(url, storeId) {
  try {
    const parsed = new URL(url);
    return /(?:^|\.)aliexpress\.com$/i.test(parsed.hostname)
      && parsed.protocol === "https:"
      && parsed.pathname === `/store/${storeId}/pages/all-items.html`
      && parsed.searchParams.get("shop_sortType") === "orders_desc";
  } catch {
    return false;
  }
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

function mergeStoreDiagnosticRecord(records, diagnostic, key, limit) {
  if (!diagnostic || !diagnostic[key]) return Array.isArray(records) ? records : [];
  const existing = Array.isArray(records) ? records : [];
  return [...existing.filter((record) => record?.[key] !== diagnostic[key]), diagnostic].slice(-limit);
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
    if (!item || item.target_type !== "STORE"
      || Number(item.id) !== Number(message.item_id)
      || state.automationTabId !== sender?.tab?.id
      || state.automationClaimToken !== message.claim_token
      || !storeId || !targets.includes(productId)
      || !safeStoreOrdersUrl(pageUrl, storeId)
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

async function waitForStorePage(tabId, expectedUrl, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.status === "complete" && tab.url === expectedUrl) return;
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
  const visited = new Set(progress.visited_page_urls);
  const visitedThisAttempt = new Set();
  const startedAt = Date.now();
  let collectionInterrupted = false;
  for (let page = 0; targets.length && products.size < targets.length
    && page < 10 && Date.now() - startedAt < STORE_COLLECTION_WORK_MS; page += 1) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab || !safeStoreOrdersUrl(tab.url, storeId) || visitedThisAttempt.has(tab.url)) break;
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
    if (result.platform_store_id !== storeId || result.url !== tab.url) {
      return { state: "failed", message: "店铺列表页与目标店铺不匹配" };
    }
    for (const product of result.products || []) {
      if (!targetSet.has(String(product.platform_product_id))) {
        return { state: "failed", message: "页面返回非监控池商品" };
      }
      mergeStoreProduct(products, product);
    }
    mergeStoreDiagnostics(progress, result.diagnostics);
    progress.products = Object.fromEntries(products);
    progress.visited_page_urls = [...visited];
    await chrome.storage.local.set({ [progressKey]: progress });
    if (products.size === targets.length || !result.has_next_page) break;
    const nextUrl = result.next_page_url;
    if (!nextUrl || !safeStoreOrdersUrl(nextUrl, storeId) || visitedThisAttempt.has(nextUrl)) break;
    const navigationBudgetMs = Math.min(30000, storeCollectionWorkRemaining(startedAt));
    if (navigationBudgetMs <= 0) {
      collectionInterrupted = true;
      break;
    }
    await chrome.tabs.update(tabId, { url: nextUrl, active: true });
    await waitForStorePage(tabId, nextUrl, navigationBudgetMs);
  }

  let candidatePage = null;
  const discoveryUrl = item.discovery_page_url;
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
    if (!safeStoreOrdersUrl(discoveryUrl, storeId)) {
      candidatePage = incompleteCandidatePage("invalid_cursor_url");
    } else {
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
            if (tab.url !== discoveryUrl) {
              await chrome.tabs.update(tabId, { url: discoveryUrl, active: true });
              const navigationBudgetMs = Math.min(30000, storeCollectionWorkRemaining(startedAt));
              if (navigationBudgetMs <= 0) throw new Error("店铺任务工作时间预算已用完");
              await waitForStorePage(tabId, discoveryUrl, navigationBudgetMs);
            }
            pageReady = true;
          } catch (error) {
            candidatePage = incompleteCandidatePage("page_load_timeout", error?.message);
          }
        }
        if (pageReady) {
          const currentTab = await chrome.tabs.get(tabId).catch(() => null);
          if (!currentTab || currentTab.url !== discoveryUrl || !safeStoreOrdersUrl(currentTab.url, storeId)) {
            candidatePage = incompleteCandidatePage("cursor_page_mismatch");
          } else {
            visited.add(discoveryUrl);
            addVisitedStorePage(progress, discoveryUrl);
            try {
              const responseBudgetMs = Math.min(
                STORE_PAGE_RESPONSE_TIMEOUT_MS,
                storeCollectionWorkRemaining(startedAt),
              );
              if (responseBudgetMs <= 0) throw new Error("店铺任务工作时间预算已用完");
              const response = await withTimeout(
                sendStoreCandidateScanMessage(tabId, item.target_date, storeId, discoveryUrl),
                responseBudgetMs,
                "店铺候选页读取超时",
              );
              if (response?.state === "challenge" || response?.state === "ixspy_login") {
                return response;
              }
              const pageResult = response?.page_result;
              if (response?.state !== "ready" || !pageResult) {
                candidatePage = incompleteCandidatePage(
                  "page_scan_failed", response?.message || "候选页未能完整读取",
                  response?.diagnostics ? { collector: response.diagnostics } : {},
                );
              } else if (String(pageResult.platform_store_id) !== storeId || pageResult.url !== discoveryUrl) {
                candidatePage = incompleteCandidatePage("page_identity_mismatch", null, {
                  actual_store_id: pageResult.platform_store_id || null,
                  actual_url: pageResult.url || null,
                });
              } else {
                const scanComplete = pageResult.scan_complete === true;
                const nextPageUrl = safeStoreOrdersUrl(pageResult.next_page_url, storeId)
                  ? pageResult.next_page_url
                  : null;
                candidatePage = {
                  page_url: discoveryUrl,
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
    if (keepAsIdle) {
      await chrome.tabs.update(tabId, { url: "about:blank", active: false }).catch(async () => {
        await chrome.tabs.remove(tabId).catch(() => undefined);
        await chrome.storage.local.remove("idleAutomationTabId");
      });
    } else {
      await chrome.tabs.remove(tabId).catch(() => undefined);
    }
    if (ownedWorkerWindow && workerWindow) {
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

async function minimizeAutomationWindow() {
  const state = await automationState();
  if (state.automationWindowBackground && state.automationWindowId) {
    await chrome.windows.update(state.automationWindowId, {
      focused: false,
      state: state.browserCollectionWorkerMode === true
        ? "minimized"
        : state.browserCollectionIsolatedProfileAuthorized === true ? "normal" : "minimized",
    }).catch(() => undefined);
  }
}

async function notifyChallenge(item) {
  await chrome.notifications.create(NOTIFICATION_ID, {
    type: "basic",
    iconUrl: "icon.svg",
    title: "AliExpress 自动采集已暂停",
    message: `${item.title || "当前页面"}需要人工完成平台验证，通过后任务会自动继续。`,
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

async function pauseForAliExpressChallenge(tabId, message = "AliExpress 要求人工完成验证") {
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
  await chrome.storage.local.set({ challengePaused: true, challengeProvider: "ALIEXPRESS" });
  if (!reviewTask) await holdPrefetchedItem("检测到平台验证，先暂停预加载项");
  await notifyChallenge(item);
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
    await minimizeAutomationWindow();
    await closeItemTab(tabId);
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

  if (result?.state === "ixspy_login") {
    await pauseForIxspyLogin(tabId, result.message);
    return;
  }

  if (!result?.payload) {
    await finishAutomationFailure(
      state,
      result?.state === "partial" ? "observable_data_missing" : "page_not_ready",
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
  await minimizeAutomationWindow();
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
    if (startingState.automationItem && startingState.automationTaskKind !== "REVIEW") {
      const run = await sendBrowserCollectionHeartbeat().catch(() => null);
      await prefetchNextBrowserItem(run).catch(() => undefined);
    }
    await processAutomationTabInternal(tabId, generation);
  } catch (error) {
    if (generation !== automationGeneration) return;
    const state = await automationState();
    if (Number(state.automationItem?.id) !== itemId) return;
    await finishAutomationFailure(
      state,
      error?.code === "COLLECTION_TIMEOUT" ? "browser_item_stalled" : "extension_collection_error",
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
  const reusableIdleTab = idleTab?.windowId === workerWindow.id && idleTab.url === "about:blank"
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
  const saved = await chrome.storage.local.get("automationTabGroupId");
  let groupId = saved.automationTabGroupId;
  try {
    if (!groupId || !(await chrome.tabGroups.get(groupId).catch(() => null))) {
      groupId = await chrome.tabs.group({ tabIds: [tab.id] });
      await chrome.storage.local.set({ automationTabGroupId: groupId });
    } else {
      await chrome.tabs.group({ tabIds: [tab.id], groupId });
    }
    await chrome.tabGroups.update(groupId, { title: "AliExpress 采集任务", color: "green", collapsed: true });
  } catch {
    // Tab grouping is optional; keep collection in the background if grouping is unavailable.
  }
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
        challengeProvider: null,
      }),
  });
}

async function prefetchNextBrowserItem(run) {
  if (!run || !["RUNNING", "PENDING"].includes(run.status)) return;
  const state = await automationState();
  if (state.prefetchedItem || state.automationTaskKind === "REVIEW"
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
        active_claims: activeClaims,
      }),
    });
  } catch (error) {
    if (error?.status !== 409) throw error;
    await discardStaleAutomationClaims();
    return null;
  }
}

async function pollAutomation() {
  await ensureWorkerModesInitialized();
  if (!(await browserCollectionWorkerEnabled())) return;
  if (automationBusy) {
    await recoverSettledBusyItem();
    return;
  }
  automationBusy = true;
  try {
    const run = await sendBrowserCollectionHeartbeat();
    if (!run) return;
    let state = await resetStaleAutomationState(run);

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
      const probe = await sendCollectMessage(
        tab.id,
        storeProbe ? "STORE_IDENTITY" : state.automationItem?.target_type,
        state.automationItem?.target_date,
      );
      if (probe?.state === "challenge") return;
      if (probe?.state === "ixspy_login") return;
      if (storeProbe) {
        const storeId = new URL(state.automationItem.target_url).pathname
          .match(/^\/store\/(\d+)\/pages\/all-items\.html$/)?.[1];
        if (probe?.state !== "ready" || !storeId || probe.platform_store_id !== storeId) return;
        if (!safeStoreOrdersUrl(tab.url, storeId)) {
          await chrome.tabs.update(tab.id, { url: state.automationItem.target_url, active: true });
          return;
        }
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
  if (message?.type === "dashboard-context") {
    void (async () => {
      const localDianxiaomiDashboard = message.dianxiaomi_local_dashboard === true
        && isLocalMonitorDashboard(sender?.tab?.url);
      if (localDianxiaomiDashboard) await ensureWorkerModesInitialized();
      const stored = await chrome.storage.local.get("apiAccessTokens");
      const workerMode = message.worker_mode === true;
      if (message.browser_collection_mode_explicit) {
        const currentAutomation = await automationState();
        const dashboardUrl = new URL(sender?.tab?.url || "http://127.0.0.1");
        const loopbackDashboard = isLoopbackMonitorDashboard(dashboardUrl.href);
        const isolatedLocalEnabled = message.browser_collection_mode === true
          && isIsolatedBrowserCollectorUrl(dashboardUrl.href);
        const remoteWorkerEnabled = message.browser_collection_mode === true && !loopbackDashboard;
        const workerEnabled = isolatedLocalEnabled || remoteWorkerEnabled;
        const reuseDashboardWindow = workerEnabled
          && Number.isInteger(sender?.tab?.windowId)
          && !currentAutomation.automationWindowOwned;
        await chrome.storage.local.set({
          browserCollectionWorkerMode: workerEnabled,
          browserCollectionLocalProfileEnabled: isolatedLocalEnabled,
          browserCollectionIsolatedProfileAuthorized: isolatedLocalEnabled,
          browserCollectionRemoteProfileEnabled: remoteWorkerEnabled,
          ...(workerEnabled && Number.isInteger(sender?.tab?.id)
            ? { automationDashboardTabId: sender.tab.id }
            : {}),
          ...(reuseDashboardWindow ? {
            automationWindowId: sender.tab.windowId,
            automationWindowOwned: false,
            automationWindowBackground: false,
          } : {}),
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
