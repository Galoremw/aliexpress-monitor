importScripts("config.js");

const API_BASE = globalThis.ALIEXPRESS_MONITOR_API_BASE || "http://127.0.0.1:8000";
const POLL_ALARM = "aliexpress-monitor-browser-collection";
const NOTIFICATION_ID = "aliexpress-monitor-verification";
const FIRST_PRODUCT_NOTIFICATION_ID = "aliexpress-monitor-first-product";
const DIANXIAOMI_URL = "https://www.dianxiaomi.com/web/productCrawl/dataAcquisition";
const tabProductIds = new Map();
let automationBusy = false;
let dianxiaomiBusy = false;

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

function isAutomationBootstrap(url) {
  try {
    const parsed = new URL(url);
    return ["127.0.0.1", "localhost"].includes(parsed.hostname)
      && parsed.searchParams.get("browser_collection") === "1";
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

async function api(path, options = {}, requestedBaseUrl = null) {
  const stored = await chrome.storage.local.get(["apiAccessToken", "apiAccessTokens", "activeApiBase"]);
  const baseUrl = requestedBaseUrl || stored.activeApiBase || API_BASE;
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const token = stored.apiAccessTokens?.[baseUrl] || stored.apiAccessToken;
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    credentials: "include",
    headers,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `本地监控台请求失败 (${response.status})`);
  return body;
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
    "automationTabId",
    "automationItem",
    "challengePaused",
    "firstProductPresentedRunId",
  ]);
}

async function clearAutomationItem() {
  await chrome.storage.local.remove(["automationTabId", "automationItem", "challengePaused"]);
}

async function rememberAutomationWindow(tab) {
  if (tab?.windowId && isAutomationBootstrap(tab.url)) {
    await chrome.storage.local.set({ automationWindowId: tab.windowId });
  }
}

async function ensureAlarm() {
  const alarm = await chrome.alarms.get(POLL_ALARM);
  if (!alarm) chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
}

async function sendCollectMessage(tabId, targetType) {
  try {
    return await chrome.tabs.sendMessage(tabId, {
      type: "collect-visible-page",
      target_type: targetType,
    });
  } catch {
    await injectCollector(tabId);
    return chrome.tabs.sendMessage(tabId, {
      type: "collect-visible-page",
      target_type: targetType,
    });
  }
}

async function closeItemTab(tabId) {
  await clearAutomationItem();
  await chrome.tabs.remove(tabId).catch(() => undefined);
}

async function closeAutomationWindow() {
  const state = await automationState();
  await chrome.storage.local.remove([
    "automationWindowId",
    "automationTabId",
    "automationItem",
    "challengePaused",
    "firstProductPresentedRunId",
  ]);
  if (state.automationWindowId) {
    await chrome.windows.remove(state.automationWindowId).catch(() => undefined);
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

async function notifyFirstProduct(item) {
  await chrome.notifications.create(FIRST_PRODUCT_NOTIFICATION_ID, {
    type: "basic",
    iconUrl: "icon.svg",
    title: "AliExpress 首个商品已打开",
    message: `${item.title || "第一个商品"}已显示。如平台要求验证，请正常完成；通过后会自动继续采集。`,
    priority: 1,
  }).catch(() => undefined);
}

async function finishAutomationFailure(state, errorType, message) {
  if (!state.automationItem) return;
  await api(`/api/browser-collection/items/${state.automationItem.id}/failure`, {
    method: "POST",
    body: JSON.stringify({ error_type: errorType, error_message: message }),
  }).catch(() => undefined);
  if (state.automationTabId) await closeItemTab(state.automationTabId);
}

async function processAutomationTabInternal(tabId) {
  const state = await automationState();
  const item = state.automationItem;
  if (!item || state.automationTabId !== tabId) return;
  const result = await sendCollectMessage(tabId, item.target_type);

  if (result?.state === "challenge") {
    await api(`/api/browser-collection/items/${item.id}/challenge`, {
      method: "POST",
      body: JSON.stringify({ error_message: result.message || "AliExpress 要求人工完成验证" }),
    });
    await chrome.storage.local.set({ challengePaused: true });
    await notifyChallenge(item);
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (tab?.windowId) {
      await chrome.windows.update(tab.windowId, { focused: true, state: "normal" }).catch(() => undefined);
      await chrome.tabs.update(tabId, { active: true }).catch(() => undefined);
    }
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

  const endpoint = item.target_type === "STORE"
    ? `/api/browser-collection/items/${item.id}/store-discovery`
    : `/api/browser-collection/items/${item.id}/snapshot`;
  await api(endpoint, { method: "POST", body: JSON.stringify(result.payload) });
  await chrome.notifications.clear(NOTIFICATION_ID).catch(() => undefined);
  await chrome.notifications.clear(FIRST_PRODUCT_NOTIFICATION_ID).catch(() => undefined);
  await closeItemTab(tabId);
}

async function processAutomationTab(tabId) {
  if (automationBusy) return;
  automationBusy = true;
  try {
    await processAutomationTabInternal(tabId);
  } catch (error) {
    const state = await automationState();
    await finishAutomationFailure(state, "extension_collection_error", error.message || "扩展采集失败");
  } finally {
    automationBusy = false;
  }
  void pollAutomation();
}

async function openAutomationItem(item) {
  let state = await automationState();
  let windowId = state.automationWindowId;
  const presentFirstProduct = item.target_type === "PRODUCT"
    && state.firstProductPresentedRunId !== item.run_id;
  if (windowId) {
    const existingWindow = await chrome.windows.get(windowId).catch(() => null);
    if (!existingWindow) windowId = null;
  }

  let tab;
  if (!windowId) {
    const created = await chrome.windows.create({
      url: "http://127.0.0.1:3000/?browser_collection=1",
      focused: presentFirstProduct,
      state: "normal",
    });
    windowId = created.id;
    await chrome.storage.local.set({ automationWindowId: windowId });
  }
  tab = await chrome.tabs.create({ windowId, url: item.target_url, active: true });
  await chrome.storage.local.set({
    automationTabId: tab.id,
    automationItem: item,
    challengePaused: false,
    ...(presentFirstProduct ? { firstProductPresentedRunId: item.run_id } : {}),
  });
  if (presentFirstProduct) {
    await chrome.windows.update(windowId, { focused: true, state: "normal" }).catch(() => undefined);
    await chrome.tabs.update(tab.id, { active: true }).catch(() => undefined);
    await notifyFirstProduct(item);
  } else {
    await chrome.windows.update(windowId, { state: "minimized" }).catch(() => undefined);
  }
}

async function pollAutomation() {
  if (automationBusy) return;
  automationBusy = true;
  try {
    const run = await api("/api/browser-collection/heartbeat", {
      method: "POST",
      body: JSON.stringify({ extension_version: chrome.runtime.getManifest().version }),
    });
    let state = await automationState();

    if (state.challengePaused && run.status === "NEEDS_VERIFICATION") {
      const tab = state.automationTabId
        ? await chrome.tabs.get(state.automationTabId).catch(() => null)
        : null;
      if (!tab || tab.status !== "complete") return;
      const probe = await sendCollectMessage(tab.id, state.automationItem?.target_type);
      if (!probe?.payload) return;
      await chrome.storage.local.set({ challengePaused: false });
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

    const claimed = await api("/api/browser-collection/items/claim", {
      method: "POST",
      body: "{}",
    });
    if (claimed.item) {
      await openAutomationItem(claimed.item);
    } else if (["COMPLETED", "PARTIAL", "FAILED"].includes(claimed.run.status)) {
      await closeAutomationWindow();
    }
  } catch {
    // The local backend may still be starting; the minute alarm retries without page side effects.
  } finally {
    automationBusy = false;
  }
}

async function sendDianxiaomiMessage(tabId, urls) {
  let lastError;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      return await chrome.tabs.sendMessage(tabId, { type: "dianxiaomi-submit-links", urls });
    } catch (error) {
      lastError = error;
      await injectDianxiaomiBridge(tabId);
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
  }
  throw lastError || new Error("店小秘页面尚未准备好");
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
  const stored = await chrome.storage.local.get("dianxiaomiWindowId");
  let windowId = stored.dianxiaomiWindowId;
  let window = windowId ? await chrome.windows.get(windowId).catch(() => null) : null;
  if (!window) {
    window = await chrome.windows.create({
      url: DIANXIAOMI_URL,
      focused: false,
      state: "minimized",
      type: "normal",
    });
    windowId = window.id;
    await chrome.storage.local.set({ dianxiaomiWindowId: windowId });
  }
  const tabs = await chrome.tabs.query({
    windowId,
    url: ["https://www.dianxiaomi.com/*", "https://dianxiaomi.com/*"],
  });
  let tab = tabs.find((candidate) => isDianxiaomiPage(candidate.url));
  if (!tab) tab = await chrome.tabs.create({ windowId, url: DIANXIAOMI_URL, active: true });
  await waitForDianxiaomiTab(tab.id);
  await chrome.windows.update(windowId, { state: "minimized", focused: false }).catch(() => undefined);
  return { tab, windowId };
}

async function updateDianxiaomiHandoffs(baseUrl, handoffs, workerId, status, result = null) {
  await Promise.all(handoffs.map((handoff) => api(`/api/integrations/dianxiaomi/handoffs/${handoff.id}/status`, {
    method: "POST",
    body: JSON.stringify({
      status,
      worker_id: workerId,
      error_type: status === "FAILED" ? "page_operation_failed" : status === "NEEDS_CONFIRMATION" ? "manual_confirmation_required" : null,
      error_message: status === "COLLECTING" ? null : result?.message || null,
    }),
  }, baseUrl).catch(() => undefined)));
}

async function pollDianxiaomiHandoffs(requestedBaseUrl = null) {
  if (dianxiaomiBusy) return;
  const workerState = await chrome.storage.local.get("dianxiaomiWorkerMode");
  if (!workerState.dianxiaomiWorkerMode) return;
  dianxiaomiBusy = true;
  const workerId = `chrome-${chrome.runtime.id}`;
  let baseUrl = requestedBaseUrl || (await chrome.storage.local.get("activeApiBase")).activeApiBase || API_BASE;
  let handoffs = [];
  try {
    if (requestedBaseUrl) await chrome.storage.local.set({ activeApiBase: requestedBaseUrl });
    handoffs = await api("/api/integrations/dianxiaomi/handoffs/claim-batch", {
      method: "POST",
      body: JSON.stringify({ worker_id: workerId, limit: 20 }),
    }, baseUrl);
    if (!handoffs?.length) return;
    const { tab } = await ensureDianxiaomiTab();
    await updateDianxiaomiHandoffs(baseUrl, handoffs, workerId, "OPENED");
    const result = await sendDianxiaomiMessage(tab.id, handoffs.map((handoff) => handoff.target_url));
    const status = result?.state === "submitted"
      ? "COLLECTING"
      : result?.state === "needs_confirmation" ? "NEEDS_CONFIRMATION" : "FAILED";
    await updateDianxiaomiHandoffs(baseUrl, handoffs, workerId, status, result);
    if (result?.state === "needs_confirmation") {
      await chrome.windows.update(tab.windowId, { focused: true, state: "normal" }).catch(() => undefined);
      await chrome.tabs.update(tab.id, { active: true }).catch(() => undefined);
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
  void ensureAlarm();
  void injectOpenAliExpressTabs();
  void pollAutomation();
  void pollDianxiaomiHandoffs();
});

chrome.runtime.onStartup.addListener(() => {
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
  void rememberAutomationWindow(tab);
  if (changeInfo.status === "complete" && isAliExpressPage(tab.url)) {
    void injectCollector(tabId);
    void automationState().then((state) => {
      if (state.automationTabId === tabId) {
        if (state.challengePaused) void pollAutomation();
        else void processAutomationTab(tabId);
      }
    });
  }
  if (changeInfo.status === "complete" && isDianxiaomiPage(tab.url)) void injectDianxiaomiBridge(tabId);
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
  if (isAliExpressPage(url)) void injectCollector(tabId);
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabProductIds.delete(tabId);
  void automationState().then((state) => {
    if (state.automationTabId === tabId && !state.challengePaused) {
      void finishAutomationFailure(state, "browser_tab_closed", "自动采集标签页被关闭");
    }
  });
});

chrome.notifications.onClicked.addListener(async (notificationId) => {
  if (![NOTIFICATION_ID, FIRST_PRODUCT_NOTIFICATION_ID].includes(notificationId)) return;
  const state = await automationState();
  if (state.automationWindowId) {
    await chrome.windows.update(state.automationWindowId, { focused: true, state: "normal" }).catch(() => undefined);
  }
  if (state.automationTabId) await chrome.tabs.update(state.automationTabId, { active: true }).catch(() => undefined);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "dashboard-context") {
    void chrome.storage.local.set({
      dianxiaomiWorkerMode: message.worker_mode === true,
      ...(message.api_base_url ? { activeApiBase: message.api_base_url } : {}),
    }).then(() => pollDianxiaomiHandoffs(message.api_base_url || null));
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type === "dianxiaomi-handoff-created") {
    void pollDianxiaomiHandoffs(message.api_base_url || null);
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
setTimeout(() => void pollAutomation(), 1500);
setTimeout(() => void pollDianxiaomiHandoffs(), 2000);
