const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

function eventSlot() {
  return { addListener(callback) { this.callback = callback; } };
}

let workerIdSequence = 0;

function makeWorkerHarness({
  dedicatedProfile = true,
  collectionResult = null,
  collectionResults = null,
  storeDiscoveryStatus = "SUCCEEDED",
  dianxiaomiQueue = [],
  dashboardUrl = null,
  dashboardTabs = [],
  dashboardTokensByTabId = {},
  legacyDianxiaomiHeartbeat = false,
  expiredToken = null,
  refreshedToken = null,
  queueItems = null,
  windowCreationStealsFocus = false,
  activeTabCreationStealsFocus = false,
  activeTabActivationStealsFocus = false,
  initialDashboardWindowState = "normal",
  claimsHeld = false,
  heartbeatConflict = false,
  poolResetBatch = null,
  poolResetClaim = null,
  clockNow = () => Date.now(),
  cloneStorage = false,
  realTimers = false,
} = {}) {
  const listeners = {
    message: eventSlot(),
    updated: eventSlot(),
    removed: eventSlot(),
    activated: eventSlot(),
    committed: eventSlot(),
    history: eventSlot(),
    focusChanged: eventSlot(),
  };
  const storage = {};
  const cloneStorageValue = (value) => cloneStorage ? structuredClone(value) : value;
  if (expiredToken) {
    storage.apiAccessToken = expiredToken;
    storage.apiAccessTokens = { "http://127.0.0.1:8000": expiredToken };
  }
  const tabs = new Map([[1, {
    id: 1,
    windowId: 7,
    url: dashboardUrl || (dedicatedProfile
      ? "http://127.0.0.1:3000/?dianxiaomi_worker=1&browser_collection=1&browser_collection_profile=isolated"
      : "http://127.0.0.1:3000/"),
    status: "complete",
    active: true,
  }]]);
  for (const tab of dashboardTabs) tabs.set(tab.id, { status: "complete", active: false, ...tab });
  const createdTabs = [];
  const updatedTabs = [];
  const removedTabs = [];
  const windowUpdates = [];
  const windowCreates = [];
  const windowRemoves = [];
  const requests = [];
  const collectionMessages = [];
  const candidateScanMessages = [];
  const notifications = [];
  const scriptCalls = [];
  const pendingTimers = [];
  let nextTabId = 10;
  let nextItem = 0;
  let groupId = 1;
  let challengePaused = false;
  let identityReviewPaused = false;
  let currentRunStatus = "RUNNING";
  let focusedWindowId = 7;
  const windowStates = new Map([[7, initialDashboardWindowState]]);
  let currentCollectionResult = collectionResult;
  let currentHeartbeatConflict = heartbeatConflict;
  let currentPoolResetBatch = poolResetBatch;
  let currentPoolResetClaim = poolResetClaim;
  const queue = (queueItems ?? [
    { id: 101, target_type: "PRODUCT", target_url: "https://www.aliexpress.com/item/100000000101.html", title: "Product 1", target_date: "2026-09-29" },
    { id: 102, target_type: "PRODUCT", target_url: "https://www.aliexpress.com/item/100000000102.html", title: "Product 2" },
    { id: 103, target_type: "PRODUCT", target_url: "https://www.aliexpress.com/item/100000000103.html", title: "Product 3" },
  ]).map((item, index) => ({ ...item, run_id: 9, position: index + 1, attempt_count: 1, status: "PENDING" }));
  const dianxiaomiItems = dianxiaomiQueue.map((item) => ({ ...item, status: item.status || "QUEUED" }));

  function runPayload() {
    const items = queue.map((item) => ({ ...item }));
    const current = items.find((item) => item.status === "RUNNING") || null;
    return {
      id: 9,
      status: challengePaused || identityReviewPaused ? "NEEDS_VERIFICATION" : currentRunStatus,
      phase: "PRODUCT_COLLECTION",
      paused_reason: challengePaused ? "platform_challenge" : identityReviewPaused ? "store_identity_review" : null,
      current_item: current || items.find((item) => item.status === "NEEDS_VERIFICATION") || null,
      pending_count: items.filter((item) => item.status === "PENDING").length,
      items,
    };
  }

  const chrome = {
    runtime: {
      getManifest: () => ({ version: "0.6.12" }),
      onInstalled: eventSlot(),
      onStartup: eventSlot(),
      onMessage: listeners.message,
      sendMessage: async () => ({}),
    },
    storage: {
      local: {
        async get(keys) {
          if (typeof keys === "string") return { [keys]: cloneStorageValue(storage[keys]) };
          if (Array.isArray(keys)) return Object.fromEntries(keys.filter((key) => key in storage)
            .map((key) => [key, cloneStorageValue(storage[key])]));
          return Object.fromEntries(Object.entries(storage).map(([key, value]) => [key, cloneStorageValue(value)]));
        },
        async set(values) { Object.assign(storage, cloneStorageValue(values)); },
        async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) delete storage[key]; },
      },
    },
    alarms: { get: async () => null, create() {}, onAlarm: eventSlot() },
    tabs: {
      onUpdated: listeners.updated,
      onRemoved: listeners.removed,
      onActivated: listeners.activated,
      async query(query = {}) {
        return [...tabs.values()].filter((tab) => (!query.windowId || tab.windowId === query.windowId)
          && (!query.url || query.url.some((pattern) => tab.url.startsWith(pattern.replace("*", "")))));
      },
      async get(id) { return tabs.get(id) || null; },
      async create(options) {
        const tab = { id: nextTabId++, windowId: options.windowId, url: options.url, status: "complete", active: options.active === true };
        tabs.set(tab.id, tab);
        createdTabs.push({ ...tab });
        if (options.active === true && activeTabCreationStealsFocus) focusedWindowId = options.windowId;
        return tab;
      },
      async update(id, changes) {
        const tab = tabs.get(id);
        Object.assign(tab, changes, { status: "complete" });
        updatedTabs.push({ id, ...changes });
        if (changes.active === true && activeTabActivationStealsFocus) focusedWindowId = tab.windowId;
        return tab;
      },
      async move(ids, options) {
        const moved = (Array.isArray(ids) ? ids : [ids]).map((id) => {
          const tab = tabs.get(id);
          Object.assign(tab, { windowId: options.windowId });
          return tab;
        });
        return Array.isArray(ids) ? moved : moved[0];
      },
      async remove(id) { removedTabs.push(id); tabs.delete(id); },
      async group() { return groupId; },
      async sendMessage(_tabId, message) {
        if (message.type === "collect-visible-page") collectionMessages.push({ ...message, tab_id: _tabId });
        if (message.type === "STORE_CANDIDATE_SCAN") {
          candidateScanMessages.push({ ...message });
          if (currentCollectionResult) return currentCollectionResult;
        }
        if (message.type === "dianxiaomi-check-ready") return { state: "ready" };
        if (message.type === "dianxiaomi-submit-links") return collectionResult || { state: "succeeded" };
        return collectionResults?.length ? collectionResults.shift()
          : currentCollectionResult || { state: "ready", payload: { sold_count: 20, captured_at: "2026-09-25T10:00:00+08:00" } };
      },
    },
    tabGroups: { get: async () => null, update: async () => ({}) },
    windows: {
      onFocusChanged: listeners.focusChanged,
      async get(id) { return { id, focused: id === focusedWindowId, state: windowStates.get(id) || "normal" }; },
      async getLastFocused() { return { id: focusedWindowId, type: "normal" }; },
      async getAll() { return [{ id: 7, type: "normal" }]; },
      async create(options) {
        windowCreates.push(options);
        windowStates.set(8, options.state || "normal");
        const tab = { id: nextTabId++, windowId: 8, url: options.url || "about:blank", status: "complete", active: true };
        tabs.set(tab.id, tab);
        if (options.focused === true || windowCreationStealsFocus) focusedWindowId = 8;
        return { id: 8, type: "normal", state: options.state || "normal", focused: focusedWindowId === 8 };
      },
      async update(id, changes) {
        windowUpdates.push({ id, ...changes, collectionMessageCount: collectionMessages.length });
        if (changes.state) windowStates.set(id, changes.state);
        if (changes.focused === true) focusedWindowId = id;
        else if (changes.focused === false && focusedWindowId === id) focusedWindowId = 7;
      },
      async remove(id) { windowRemoves.push(id); },
    },
    notifications: {
      create: async (id, options) => { notifications.push({ id, ...options }); return id; },
      clear: async () => {},
      onClicked: eventSlot(),
    },
    scripting: {
      executeScript: async (details) => {
        scriptCalls.push(details.target?.tabId);
        const token = Object.hasOwn(dashboardTokensByTabId, details.target?.tabId)
          ? dashboardTokensByTabId[details.target.tabId]
          : refreshedToken;
        return [{ result: details.args?.length && token ? { access_token: token } : null }];
      },
    },
    webNavigation: { onCommitted: listeners.committed, onHistoryStateUpdated: listeners.history },
  };

  const WorkerDate = class extends Date {
    static now() { return clockNow(); }
  };
  const context = {
    chrome,
    crypto: { randomUUID: () => `test-worker-${++workerIdSequence}` },
    URL,
    Map,
    Date: WorkerDate,
    Promise,
    setTimeout(callback, delay) {
      if (realTimers) return globalThis.setTimeout(callback, delay);
      pendingTimers.push({ callback, delay });
      return pendingTimers.length;
    },
    setInterval() { return 0; },
    clearTimeout(timerId) { if (realTimers) globalThis.clearTimeout(timerId); },
    importScripts() {},
    async fetch(url, options) {
      const parsedUrl = new URL(url);
      const pathname = parsedUrl.pathname;
      const token = options.headers?.Authorization?.replace(/^Bearer\s+/i, "") || null;
      requests.push({ origin: parsedUrl.origin, pathname, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null, token });
      const validRefreshedTokens = new Set([refreshedToken, ...Object.values(dashboardTokensByTabId)].filter(Boolean));
      if (expiredToken && !validRefreshedTokens.has(token)) {
        return { ok: false, status: 401, json: async () => ({ detail: "Unauthorized" }) };
      }
      let body = {};
      if (pathname === "/api/integrations/dianxiaomi/queue/heartbeat" && legacyDianxiaomiHeartbeat) {
        return { ok: false, status: 404, json: async () => ({ detail: "Not Found" }) };
      } else if (pathname === "/api/browser-collection/pool-resets/latest") {
        body = currentPoolResetBatch;
      } else if (pathname === "/api/browser-collection/pool-resets/heartbeat") {
        body = { worker_id: JSON.parse(options.body).worker_id };
      } else if (pathname === "/api/browser-collection/pool-resets/claim") {
        body = currentPoolResetClaim || { batch: currentPoolResetBatch, store: null, claim_token: null };
        currentPoolResetClaim = null;
      } else if (/\/pool-resets\/tasks\/\d+\/page$/.test(pathname)) {
        body = currentPoolResetBatch || {};
      } else if (/\/pool-resets\/tasks\/\d+\/(?:challenge|resume|failure)$/.test(pathname)) {
        body = currentPoolResetBatch || {};
      } else if (pathname === "/api/integrations/dianxiaomi/queue/heartbeat") {
        body = { paused: false };
      } else if (pathname === "/api/integrations/dianxiaomi/handoffs" && options.method !== "POST") {
        body = dianxiaomiItems.map((item) => ({ ...item }));
      } else if (pathname.endsWith("/handoffs/claim-batch")) {
        const item = dianxiaomiItems.find((candidate) => candidate.status === "QUEUED");
        if (item) {
          item.status = "CLAIMED";
          item.worker_id = JSON.parse(options.body).worker_id;
        }
        body = item ? [{ ...item }] : [];
      } else if (/\/handoffs\/\d+\/status$/.test(pathname)) {
        const id = Number(pathname.match(/handoffs\/(\d+)\/status/)?.[1]);
        const item = dianxiaomiItems.find((candidate) => candidate.id === id);
        if (item) Object.assign(item, JSON.parse(options.body));
        body = item || {};
      } else if (pathname.endsWith("/heartbeat")) {
        if (currentHeartbeatConflict && JSON.parse(options.body || "{}").active_claims?.length) {
          return { ok: false, status: 409, json: async () => ({ detail: "stale claim" }) };
        }
        body = runPayload();
      } else if (pathname.endsWith("/items/claim")) {
        const item = claimsHeld ? null : queue.find((candidate) => candidate.status === "PENDING");
        if (item) item.status = "RUNNING";
        body = {
          run: runPayload(),
          item: item ? { ...item } : null,
          claim_token: item ? `claim-token-${item.id}-${item.attempt_count}` : null,
        };
      } else if (pathname.includes("/items/") && pathname.endsWith("/identity-review")) {
        const id = Number(pathname.match(/items\/(\d+)\/identity-review/)?.[1]);
        const item = queue.find((candidate) => candidate.id === id);
        if (item) {
          item.status = "SKIPPED";
          item.error_type = "store_identity_deferred";
        }
        challengePaused = false;
        body = runPayload();
      } else if (pathname.includes("/items/") && pathname.endsWith("/challenge")) {
        const id = Number(pathname.match(/items\/(\d+)\/challenge/)?.[1]);
        const item = queue.find((candidate) => candidate.id === id);
        if (item) item.status = "NEEDS_VERIFICATION";
        challengePaused = true;
        body = runPayload();
      } else if (pathname.includes("/snapshot")) {
        const id = Number(pathname.match(/items\/(\d+)\/snapshot/)?.[1]);
        const item = queue.find((candidate) => candidate.id === id);
        item.status = "SUCCEEDED";
        body = runPayload();
      } else if (pathname.endsWith("/store-discovery")) {
        const id = Number(pathname.match(/items\/(\d+)\/store-discovery/)?.[1]);
        const item = queue.find((candidate) => candidate.id === id);
        if (item) item.status = storeDiscoveryStatus;
        body = runPayload();
      } else if (pathname.includes("/items/") && pathname.endsWith("/failure")) {
        const id = Number(pathname.match(/items\/(\d+)\/failure/)?.[1]);
        const item = queue.find((candidate) => candidate.id === id);
        if (item) item.status = "PENDING";
        body = runPayload();
      }
      return { ok: true, status: 200, json: async () => body };
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../extension/background.js"), "utf8"), context);

  async function flush() {
    for (let index = 0; index < 30; index += 1) await new Promise((resolve) => setImmediate(resolve));
  }
  return {
    chrome, context, listeners, storage, queue, dianxiaomiQueue: dianxiaomiItems,
    createdTabs, updatedTabs, removedTabs, windowUpdates, windowCreates, windowRemoves,
    requests, notifications, collectionMessages, candidateScanMessages, scriptCalls, pendingTimers, flush,
    windowStateFor: (windowId) => windowStates.get(windowId),
    get focusedWindowId() { return focusedWindowId; },
    setCollectionResult(value) { currentCollectionResult = value; },
    setHeartbeatConflict(value) { currentHeartbeatConflict = value; },
    resumeChallenge() {
      challengePaused = false;
      currentRunStatus = "RUNNING";
      for (const item of queue) {
        if (item.status === "NEEDS_VERIFICATION") item.status = "RUNNING";
      }
    },
    confirmIdentityReview(itemId, aliases) {
      identityReviewPaused = false;
      currentRunStatus = "RUNNING";
      const item = queue.find((candidate) => Number(candidate.id) === Number(itemId));
      if (item) {
        item.status = "RUNNING";
        item.approved_store_url_identities = aliases;
      }
    },
    setCollectionResults(values) {
      collectionResults = values;
    },
    setPoolResetBatch(value) { currentPoolResetBatch = value; },
  };
}

test("ordinary local dashboard cannot enable queue polling from a run button", async () => {
  const dashboardUrl = "http://127.0.0.1:3000/?browser_collection=1";
  const worker = makeWorkerHarness({ dashboardUrl });
  worker.storage.browserCollectionLocalProfileEnabled = true;
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
  }, { tab: { id: 1, windowId: 7, url: dashboardUrl } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, {
    tab: { id: 1, windowId: 7, url: dashboardUrl },
  }, () => {});
  await worker.flush();

  assert.equal(worker.storage.browserCollectionWorkerMode, false);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/claim")), false);
  assert.equal(worker.windowCreates.length, 0);
});

test("legacy local worker setting does not activate collection in the everyday profile", async () => {
  const worker = makeWorkerHarness({ dedicatedProfile: false });
  worker.storage.browserCollectionLocalProfileEnabled = true;
  worker.chrome.runtime.onStartup.callback();
  await worker.flush();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.storage.browserCollectionWorkerMode, false);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/claim")), false);
});

test("isolated local dashboard URL restores worker mode after an MV3 service-worker restart", async () => {
  const worker = makeWorkerHarness({
    dashboardUrl: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated",
  });
  worker.chrome.runtime.onStartup.callback();
  await worker.flush();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.storage.browserCollectionWorkerMode, true);
  assert.ok(worker.requests.some((request) => request.pathname.endsWith("/items/claim")));
});

test("stale isolated authorization cannot activate the everyday Chrome profile", async () => {
  const worker = makeWorkerHarness({ dedicatedProfile: false });
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  worker.chrome.runtime.onStartup.callback();
  await worker.flush();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.storage.browserCollectionWorkerMode, false);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/claim")), false);
});

test("isolated local collector profile can claim queued work", async () => {
  const dashboardUrl = "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated";
  const worker = makeWorkerHarness({ dashboardUrl });
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
  }, { tab: { id: 1, windowId: 7, url: dashboardUrl } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, {
    tab: { id: 1, windowId: 7, url: dashboardUrl },
  }, () => {});
  await worker.flush();

  assert.equal(worker.storage.browserCollectionWorkerMode, true);
  assert.ok(worker.requests.some((request) => request.pathname.endsWith("/items/claim")));
});

test("pool reset scans the claimed Orders page without touching the daily queue", async () => {
  const pageUrl = "https://www.aliexpress.com/store/1100000092/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc";
  const batch = { id: 41, status: "RUNNING", dispatch_enabled: true, target_date: "2026-10-16" };
  const worker = makeWorkerHarness({
    poolResetBatch: batch,
    poolResetClaim: {
      batch,
      store: {
        id: 701,
        store_id: 92,
        platform_store_id: "1100000092",
        store_name: "Reset Test Store",
        next_page_url: pageUrl,
      },
      claim_token: "pool-reset-token-701",
      candidate_limit: 50,
      target_url: pageUrl,
      target_date: "2026-10-16",
    },
    collectionResult: {
      state: "ready",
      page_result: {
        platform_store_id: "1100000092",
        url: pageUrl,
        candidates: [],
        pagination_known: true,
        has_next_page: false,
        next_page_url: null,
        scan_complete: true,
        diagnostics: { page_scan: { visible_product_count: 0 } },
      },
    },
  });
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
  }, {
    tab: {
      id: 1,
      windowId: 7,
      url: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated",
    },
  }, () => {});
  await worker.flush();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.storage.automationTaskKind, "POOL_RESET");
  const taskTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  assert.equal(taskTab.url, pageUrl);
  worker.listeners.updated.callback(taskTab.id, { status: "complete" }, taskTab);
  await worker.flush();

  const scan = worker.candidateScanMessages[0];
  assert.equal(scan.expectedStoreId, "1100000092");
  assert.equal(scan.expectedPageUrl, pageUrl);
  const submitted = worker.requests.find((request) => /\/pool-resets\/tasks\/701\/page$/.test(request.pathname));
  assert.equal(submitted.body.expected_store_id, "1100000092");
  assert.equal(submitted.body.claim_token, "pool-reset-token-701");
  assert.equal(submitted.body.page.page_url, pageUrl);
  const pageRequestIndex = worker.requests.indexOf(submitted);
  assert.equal(worker.requests.slice(0, pageRequestIndex).some((request) =>
    request.pathname === "/api/browser-collection/heartbeat"
      || request.pathname === "/api/browser-collection/items/claim"), false);
  assert.equal(worker.createdTabs.some((tab) => /\/item\//.test(tab.url)), false);
  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/pool-resets/heartbeat"));
});

test("stale heartbeat discards local claims but keeps the task page instead of blanking it", async () => {
  const worker = makeWorkerHarness({ heartbeatConflict: true });
  worker.chrome.runtime.onStartup.callback();
  await worker.flush();
  const taskTab = await worker.chrome.tabs.create({
    windowId: 7,
    url: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc",
    active: false,
  });
  worker.storage.automationDashboardTabId = 1;
  worker.storage.automationWindowId = 7;
  worker.storage.automationWindowOwned = true;
  worker.storage.automationWindowBackground = true;
  worker.storage.automationTabId = taskTab.id;
  worker.storage.automationItem = { ...worker.queue[0], status: "RUNNING" };
  worker.storage.automationClaimToken = "claim-token-101-1";
  worker.storage.automationTaskKind = "PRODUCT";

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.storage.automationItem, undefined);
  assert.equal(worker.storage.automationClaimToken, undefined);
  assert.equal((await worker.chrome.tabs.get(taskTab.id)).url,
    "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc");
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/claim")), false);
});

test("LAN collector dashboard remains a queue worker", async () => {
  const dashboardUrl = "http://192.168.66.115:3000/?browser_collection=1";
  const worker = makeWorkerHarness({ dashboardUrl });
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: false,
  }, { tab: { id: 1, windowId: 7, url: dashboardUrl } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, {
    tab: { id: 1, windowId: 7, url: dashboardUrl },
  }, () => {});
  await worker.flush();

  assert.equal(worker.storage.browserCollectionWorkerMode, true);
  assert.ok(worker.requests.some((request) => request.pathname.endsWith("/items/claim")));
});

test("store worker merges two Orders pages without opening product detail URLs", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const secondUrl = `${firstUrl}&page=2`;
  const worker = makeWorkerHarness({ collectionResults: [
    { state: "ready", page_result: { platform_store_id: "123456", url: firstUrl,
      products: [{ platform_product_id: "1005000000000001", historical_sales: [] }],
      diagnostics: {
        page_scan: { url: firstUrl, candidate_node_count: 4, visible_product_count: 3,
          matched_product_ids: ["1005000000000001"], unmatched_target_ids: ["1005000000000002"] },
        chart_reads: [{ product_id: "1005000000000001", outcome: "partial", source: "ixspy_store_product_chart_unavailable", point_count: 0 }],
      },
      has_next_page: true, next_page_url: secondUrl } },
    { state: "ready", page_result: { platform_store_id: "123456", url: secondUrl,
      products: [{ platform_product_id: "1005000000000002", historical_sales: [] }],
      diagnostics: {
        page_scan: { url: secondUrl, candidate_node_count: 5, visible_product_count: 4,
          matched_product_ids: ["1005000000000002"], unmatched_target_ids: [] },
        chart_reads: [{ product_id: "1005000000000002", outcome: "complete", source: "ixspy_visible_daily_increment", point_count: 7 }],
      },
      has_next_page: false, next_page_url: null } },
  ] });
  worker.storage.browserCollectionLocalProfileEnabled = false;
  worker.storage.browserCollectionRemoteProfileEnabled = true;
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: firstUrl, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    target_url: firstUrl, target_date: "2026-09-29",
    monitored_product_ids: ["1005000000000001", "1005000000000002"],
    monitored_products: [{
      platform_product_id: "1005000000000001", title: "Tracked item",
      url: "https://www.aliexpress.com/item/1005000000000001.html",
    }],
  });

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.products.map((product) => product.platform_product_id))),
    ["1005000000000001", "1005000000000002"]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [firstUrl, secondUrl]);
  assert.ok(result.payload.raw_data.worker_id);
  assert.ok(result.payload.raw_data.worker_label);
  assert.equal(result.payload.raw_data.page_scans.length, 2);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.chart_reads.map((read) => read.outcome))),
    ["partial", "complete"]);
  assert.equal(worker.updatedTabs.some((update) => /\/item\//.test(update.url || "")), false);
  assert.equal(worker.collectionMessages.length, 2);
  assert.deepEqual(JSON.parse(JSON.stringify(worker.collectionMessages[0].monitored_products)), [{
    platform_product_id: "1005000000000001", title: "Tracked item",
    url: "https://www.aliexpress.com/item/1005000000000001.html",
  }]);
});

test("store worker follows a signaled next page without a URL and records fallback scan diagnostics", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const secondUrl = `${firstUrl}&page=2`;
  const firstId = "1005000000000001";
  const secondId = "1005000000000002";
  const worker = makeWorkerHarness({ collectionResults: [
    { state: "ready", page_result: { platform_store_id: "123456", url: firstUrl,
      products: [{ platform_product_id: firstId, historical_sales: [] }],
      has_next_page: true, next_page_url: null } },
    { state: "ready", page_result: { platform_store_id: "123456", url: secondUrl,
      products: [{ platform_product_id: secondId, historical_sales: [] }],
      has_next_page: false, next_page_url: null } },
  ] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: firstUrl, active: true });

  const result = await worker.context.collectStorePages(tab.id, {
    target_url: firstUrl,
    target_date: "2026-09-29",
    monitored_product_ids: [firstId, secondId],
  });

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.products.map((product) => product.platform_product_id))),
    [firstId, secondId]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [firstUrl, secondUrl]);
  assert.equal(result.payload.raw_data.page_scans.length, 2);
  assert.equal(result.payload.raw_data.page_scans[0].next_page_url, secondUrl);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.page_scans[0].unmatched_target_ids)), [secondId]);
  assert.equal(result.payload.raw_data.page_scans[0].scan_source, "worker_fallback");
  assert.equal(worker.updatedTabs[0].url, secondUrl);
});

test("store worker restores the target Orders page when verification leaves its tab blank", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const monitoredIds = Array.from({ length: 20 }, (_, index) => `1005000000000${String(index + 1).padStart(3, "0")}`);
  let now = 0;
  const worker = makeWorkerHarness({ clockNow: () => now, collectionResult: {
    state: "ready",
    page_result: {
      platform_store_id: "123456",
      url,
      products: monitoredIds.map((platform_product_id) => ({ platform_product_id, historical_sales: [] })),
      has_next_page: false,
      next_page_url: null,
    },
  } });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: "about:blank", active: true });

  const result = await worker.context.collectStorePages(tab.id, {
    id: 413, run_id: 9, target_url: url, target_date: "2026-09-29",
    monitored_product_ids: monitoredIds,
  });

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [url]);
  assert.equal(worker.updatedTabs[0].url, url, "recovers the same worker tab to the claimed Orders page");
  assert.equal(worker.collectionMessages.length, 1);
});

test("store worker fails explicitly when its collector tab disappeared instead of submitting an empty visit record", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness();
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  await worker.chrome.tabs.remove(tab.id);

  const result = await worker.context.collectStorePages(tab.id, {
    id: 414, run_id: 9, target_url: url, target_date: "2026-09-29",
    monitored_product_ids: Array.from({ length: 20 }, (_, index) => `1005000000000${String(index + 1).padStart(3, "0")}`),
  });

  assert.equal(result.state, "failed");
  assert.match(result.message, /采集标签页已关闭/);
  assert.equal(result.payload, undefined, "never submits a success-shaped result with no visited Orders page");
});

test("empty monitored pool scans exactly the backend cursor page without opening product details", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const cursorUrl = `${firstUrl}&page=2`;
  const worker = makeWorkerHarness({ collectionResults: [{
    state: "ready",
    page_result: {
      platform_store_id: "123456",
      url: cursorUrl,
      candidates: [{ platform_product_id: "1005000000000009", historical_sales: [] }],
      has_next_page: true,
      next_page_url: `${firstUrl}&page=3`,
      scan_complete: true,
      diagnostics: { chart_reads: [{ product_id: "1005000000000009", outcome: "unavailable" }] },
    },
  }] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: firstUrl, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    id: 405,
    run_id: 9,
    target_url: firstUrl,
    discovery_page_url: cursorUrl,
    target_date: "2026-09-29",
    monitored_product_ids: [],
  });

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.products)), []);
  assert.equal(result.payload.candidate_page.page_url, cursorUrl);
  assert.equal(result.payload.candidate_page.scan_complete, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [cursorUrl]);
  assert.equal(worker.collectionMessages.length, 0, "an empty pool does not invoke the monitored-product reader");
  assert.equal(worker.candidateScanMessages.length, 1);
  assert.equal(worker.candidateScanMessages[0].expectedPageUrl, cursorUrl);
  assert.deepEqual(worker.updatedTabs.map((update) => update.url), [cursorUrl]);
  assert.equal(worker.updatedTabs.some((update) => /\/item\//.test(update.url || "")), false);
});

test("empty initial pool scans the task Orders page when the backend has no continuation cursor", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({ collectionResults: [{
    state: "ready",
    page_result: {
      platform_store_id: "123456",
      url,
      candidates: [{ platform_product_id: "1005000000000009", historical_sales: [] }],
      has_next_page: true,
      next_page_url: `${url}&page=2`,
      scan_complete: true,
      pagination_known: true,
    },
  }] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    id: 404,
    run_id: 9,
    target_url: url,
    discovery_page_url: null,
    target_date: "2026-09-29",
    monitored_product_ids: [],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.payload.candidate_page.page_url, url);
  assert.equal(result.payload.candidate_page.scan_complete, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [url]);
  assert.equal(worker.candidateScanMessages.length, 1);
  assert.equal(worker.candidateScanMessages[0].expectedPageUrl, url);
  assert.equal(worker.collectionMessages.length, 0, "does not run per-product monitored collection for an empty pool");
  assert.equal(worker.updatedTabs.length, 0, "scans the already-open task page without navigation");
});

test("candidate scan waits for a matching pending Orders navigation instead of navigating twice", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({ realTimers: true, collectionResult: {
    state: "ready",
    page_result: {
      platform_store_id: "123456",
      url,
      candidates: [],
      has_next_page: false,
      next_page_url: null,
      scan_complete: true,
      pagination_known: true,
    },
  } });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: "about:blank", active: true });
  tab.status = "loading";
  tab.pendingUrl = url;
  worker.context.waitForStorePage = async () => {
    tab.url = url;
    delete tab.pendingUrl;
    tab.status = "complete";
    return { storeId: "123456" };
  };

  const result = await worker.context.collectStorePages(tab.id, {
    id: 403,
    run_id: 9,
    target_url: url,
    discovery_page_url: url,
    target_date: "2026-09-29",
    monitored_product_ids: [],
  });

  assert.equal(result.state, "ready");
  assert.equal(worker.updatedTabs.length, 0, "does not restart a navigation already targeting the required Orders page");
  assert.equal(worker.candidateScanMessages.length, 1);
});

test("empty pool preserves the page-load diagnosis when no Orders page is reached", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness();
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: "about:blank", active: true });
  worker.context.waitForStorePage = async () => {
    throw new Error("店铺下一页未在限定时间内加载完成");
  };

  const result = await worker.context.collectStorePages(tab.id, {
    id: 402,
    run_id: 9,
    target_url: url,
    discovery_page_url: url,
    target_date: "2026-09-29",
    monitored_product_ids: [],
  });

  assert.equal(result.state, "failed");
  assert.equal(result.error_type, "page_not_ready");
  assert.match(result.message, /阶段：page_load_timeout/);
  assert.match(result.message, /当前 URL：https:\/\/www\.aliexpress\.com\/store\/123456/);
});

test("empty-pool identity failures include the Store ID probe evidence", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const aliasUrl = "https://www.aliexpress.com/store/789012/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness();
  const tab = await worker.chrome.tabs.create({
    windowId: 8,
    url: aliasUrl,
    active: true,
  });
  worker.context.waitForStorePage = async () => {
    const error = new Error("任务目标店铺 ID 123456 与页面显示店铺 ID 未识别；Store ID 字段：未识别");
    error.code = "STORE_IDENTITY_REVIEW";
    error.error_type = "store_identity_mismatch";
    error.page_url = "https://www.aliexpress.com/store/789012/pages/all-items.html?shop_sortType=orders_desc";
    error.evidence = {
      route_store_id: "789012",
      heading_store_id: "123456",
      labeled_store_ids: [],
      page_title: "AliExpress Store",
      ixspy_store_name: null,
      identity_method: "unverified",
    };
    throw error;
  };

  const result = await worker.context.collectStorePages(tab.id, {
    id: 410,
    run_id: 9,
    target_url: url,
    discovery_page_url: aliasUrl,
    target_date: "2026-09-29",
    monitored_product_ids: [],
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.match(result.message, /任务目标店铺 ID 123456/);
  assert.match(result.message, /Store ID 字段：未识别/);
  assert.equal(result.evidence.route_store_id, "789012");
});

test("store worker reuses a visited cursor URL and submits candidate diagnostics once", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({ collectionResults: [
    { state: "ready", page_result: { platform_store_id: "123456", url, products: [], has_next_page: false } },
    { state: "ready", page_result: {
      platform_store_id: "123456", url,
      candidates: [{ platform_product_id: "1005000000000009", historical_sales: [] }],
      has_next_page: false, next_page_url: null, scan_complete: true,
      diagnostics: { chart_reads: [{ product_id: "1005000000000009", outcome: "unavailable" }] },
    } },
  ] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    id: 406, run_id: 9, target_url: url, discovery_page_url: url,
    target_date: "2026-09-29", monitored_product_ids: ["1005000000000001"],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.payload.candidate_page.page_url, url);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [url]);
  assert.equal(worker.candidateScanMessages.length, 1);
  assert.equal(worker.updatedTabs.length, 0, "the current cursor page is reused without navigation");
  assert.equal(worker.updatedTabs.some((update) => /\/item\//.test(update.url || "")), false);
});

test("candidate page timeout submits an incomplete candidate scan without advancing the page", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc&page=2";
  const worker = makeWorkerHarness({ collectionResult: new Promise(() => {}) });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const collecting = worker.context.collectStorePages(tab.id, {
    id: 407, run_id: 9,
    target_url: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc",
    discovery_page_url: url, target_date: "2026-09-29", monitored_product_ids: [],
  });
  await worker.flush();
  assert.equal(worker.candidateScanMessages.length, 1);
  const timeout = worker.pendingTimers.find((timer) => timer.delay > 14 * 60 * 1000
    && timer.delay <= 15 * 60 * 1000);
  assert.ok(timeout, "candidate scan uses the bounded page response timeout");
  timeout.callback();
  const result = await collecting;

  assert.equal(result.state, "ready");
  assert.equal(result.payload.candidate_page.scan_complete, false);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.candidate_page.candidates)), []);
  assert.equal(result.payload.candidate_page.diagnostics.outcome, "page_response_timeout");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [url]);
});

test("candidate page scan has a 15-minute response window for serial chart reads", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({ collectionResult: new Promise(() => {}) });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const collecting = worker.context.collectStorePages(tab.id, {
    id: 411, run_id: 9, target_url: url, discovery_page_url: url,
    target_date: "2026-09-29", monitored_product_ids: [],
  });
  await worker.flush();

  const timeout = worker.pendingTimers.find((timer) => timer.delay >= 14 * 60 * 1000);
  assert.ok(timeout, "the scan timeout must allow 15 minutes for sequential IXSPY chart reads");
  timeout.callback();
  const result = await collecting;

  assert.equal(result.payload.candidate_page.scan_complete, false);
  assert.equal(result.payload.candidate_page.diagnostics.outcome, "page_response_timeout");
});

test("incomplete candidate page never forwards its partial candidates to the backend", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc&page=2";
  const worker = makeWorkerHarness({ realTimers: true, collectionResult: {
    state: "ready",
    page_result: {
      platform_store_id: "123456", url,
      candidates: [{ platform_product_id: "1005000000000009", historical_sales: [] }],
      has_next_page: true,
      next_page_url: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc&page=3",
      scan_complete: false,
      diagnostics: { outcome: "page_structure_incomplete" },
    },
  } });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    id: 408, run_id: 9,
    target_url: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc",
    discovery_page_url: url, target_date: "2026-09-29", monitored_product_ids: [],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.payload.candidate_page.scan_complete, false);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.candidate_page.candidates)), []);
  assert.equal(result.payload.candidate_page.next_page_url,
    "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc&page=3",
    JSON.stringify(result.payload.candidate_page));
});

test("store worker stops at a cross-store next link and leaves missing IDs pending", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({ collectionResults: [{
    state: "ready", page_result: { platform_store_id: "123456", url: firstUrl,
      products: [], has_next_page: true,
      next_page_url: "https://www.aliexpress.com/store/999999/pages/all-items.html?shop_sortType=orders_desc&page=2" },
  }] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: firstUrl, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    target_url: firstUrl, target_date: "2026-09-29", monitored_product_ids: ["1005000000000001"],
  });
  assert.equal(result.state, "ready");
  assert.equal(result.payload.products.length, 0);
  assert.equal(worker.updatedTabs.length, 0);
});

test("store worker returns a challenge without submitting partial data", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({ collectionResults: [{
    state: "challenge",
    message: "verification",
    diagnostics: {
      page_scan: { url: firstUrl, candidate_node_count: 6, visible_product_count: 5 },
      chart_reads: [{ product_id: "1005000000000001", outcome: "partial", source: "ixspy_visible_daily_increment", point_count: 2 }],
    },
  }] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: firstUrl, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    id: 401, run_id: 9, target_url: firstUrl, target_date: "2026-09-29", monitored_product_ids: ["1005000000000001"],
  });
  assert.equal(result.state, "challenge");
  assert.equal(result.payload, undefined);
  const progress = worker.storage["browserCollectionStoreProgress:401"];
  assert.ok(progress);
  assert.equal(progress.page_scans[0].candidate_node_count, 6);
  assert.equal(progress.chart_reads[0].point_count, 2);
});

test("challenge response preserves product progress saved during the active page read", async () => {
  let finishRead;
  const pendingRead = new Promise((resolve) => { finishRead = resolve; });
  const worker = makeWorkerHarness({ collectionResult: pendingRead, cloneStorage: true });
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const productId = "1005000000000041";
  const item = {
    id: 411, run_id: 9, target_url: url, target_date: "2026-09-29",
    monitored_product_ids: [productId],
  };
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  worker.storage.automationItem = { ...item, target_type: "STORE" };
  worker.storage.automationTabId = tab.id;
  worker.storage.automationClaimToken = "claim-411";
  const collecting = worker.context.collectStorePages(tab.id, item);
  await worker.flush();

  const product = {
    platform_product_id: productId,
    url: `https://www.aliexpress.com/item/${productId}.html`,
    historical_sales: [{ date: "2026-09-28", value: 5, value_type: "daily_increment" }],
  };
  const progressAck = await new Promise((resolve) => {
    const handled = worker.listeners.message.callback({
      type: "store-collection-product-progress",
      item_id: item.id,
      claim_token: "claim-411",
      product,
    }, { tab: { id: tab.id, url } }, resolve);
    if (!handled) resolve({ not_handled: true });
  });
  assert.deepEqual(JSON.parse(JSON.stringify(progressAck)), { ok: true });
  finishRead({
    state: "challenge",
    message: "verification",
    diagnostics: { chart_reads: [{ product_id: productId, outcome: "partial", point_count: 1 }] },
  });

  const result = await collecting;
  const saved = worker.storage["browserCollectionStoreProgress:411"];
  assert.equal(result.state, "challenge");
  assert.ok(saved.products[productId], "the incremental product checkpoint survives the challenge response");
});

test("candidate discovery resumes after challenge without an empty monitored-page request", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const productId = "1005000000000042";
  const worker = makeWorkerHarness({ collectionResults: [
    { state: "ready", page_result: { platform_store_id: "123456", url,
      products: [{ platform_product_id: productId, historical_sales: [{ date: "2026-09-28", value: 4 }] }],
      has_next_page: false, next_page_url: null } },
    { state: "challenge", message: "verification" },
  ] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const item = {
    id: 412, run_id: 9, target_url: url, discovery_page_url: url,
    target_date: "2026-09-29", monitored_product_ids: [productId],
  };

  const firstAttempt = await worker.context.collectStorePages(tab.id, item);
  assert.equal(firstAttempt.state, "challenge");
  assert.ok(worker.storage["browserCollectionStoreProgress:412"].products[productId]);

  worker.setCollectionResult({ state: "ready", page_result: {
    platform_store_id: "123456", url, candidates: [], scan_complete: true,
    pagination_known: true, has_next_page: false, next_page_url: null,
  } });
  const resumed = worker.context.collectStorePages(tab.id, item);
  await worker.flush();
  const result = await resumed;

  assert.equal(result.state, "ready");
  assert.equal(worker.collectionMessages.length, 1, "completed monitored targets are not sent again as an empty request");
  assert.equal(worker.candidateScanMessages.length, 2, "the candidate page is retried after verification");
});

test("store retry preserves prior product identity evidence when a rescan omits the completed target", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const targetId = "1005000000000043";
  const observedId = "1005000000000044";
  const pendingId = "1005000000000045";
  const identityMatch = {
    monitored_product_id: targetId,
    observed_product_id: observedId,
    match_method: "exact_title",
    page_url: url,
  };
  const worker = makeWorkerHarness({ collectionResult: {
    state: "challenge",
    message: "verification",
    diagnostics: { page_scan: {
      url,
      visible_product_ids: [pendingId],
      matched_product_ids: [],
      unmatched_target_ids: [targetId, pendingId],
      identity_matches: [],
    } },
  } });
  const item = {
    id: 414, run_id: 9, target_url: url, target_date: "2026-09-29",
    monitored_product_ids: [targetId, pendingId],
  };
  worker.storage["browserCollectionStoreProgress:414"] = {
    run_id: 9,
    item_id: 414,
    store_id: "123456",
    target_date: "2026-09-29",
    target_ids: [targetId, pendingId],
    products: {
      [targetId]: {
        platform_product_id: targetId,
        observed_product_id: observedId,
        observed_url: `https://www.aliexpress.com/item/${observedId}.html`,
        observed_page_url: url,
        identity_match_method: "exact_title",
        historical_sales: [],
      },
    },
    visited_page_urls: [url],
    page_scans: [{
      url,
      visible_product_ids: [observedId],
      matched_product_ids: [targetId],
      unmatched_target_ids: [pendingId],
      identity_matches: [identityMatch],
    }],
    chart_reads: [],
  };
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });

  const result = await worker.context.collectStorePages(tab.id, item);

  assert.equal(result.state, "challenge");
  const scan = worker.storage["browserCollectionStoreProgress:414"].page_scans[0];
  assert.ok(scan.visible_product_ids.includes(observedId));
  assert.ok(scan.visible_product_ids.includes(pendingId));
  assert.deepEqual(JSON.parse(JSON.stringify(scan.identity_matches)), [identityMatch]);
});

test("selected remote worker remains maximized and foreground after the store task", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({ collectionResult: {
    state: "ready",
    page_result: { platform_store_id: "123456", url, products: [], has_next_page: false },
  } });
  worker.storage.browserCollectionWorkerMode = true;
  worker.storage.browserCollectionRemoteProfileEnabled = true;
  worker.storage.automationWindowId = 8;
  worker.storage.automationWindowOwned = true;
  worker.storage.automationWindowBackground = true;
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  worker.storage.automationTabId = tab.id;
  worker.storage.automationItem = {
    id: 402,
    run_id: 9,
    target_type: "STORE",
    target_key: "store:123456",
    target_url: url,
    target_date: "2026-09-29",
    monitored_product_ids: ["1005000000000001"],
  };
  worker.storage.automationClaimToken = "claim-402";
  worker.storage.automationTaskKind = "STORE";

  await worker.context.processAutomationTabInternal(tab.id);

  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true
    && update.collectionMessageCount === 0), "the selected worker window is focused before collection begins");
  assert.equal(worker.windowUpdates.some((update) => update.focused === false && update.collectionMessageCount === 0), false,
    "the worker window must not be blurred before IXSPY collection");
  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true
    && update.state === "maximized"), "the store chart is read in a maximized foreground window");
  const finalWindowUpdate = worker.windowUpdates.filter((update) => update.id === 8).at(-1);
  assert.equal(finalWindowUpdate.focused, true);
  assert.equal(finalWindowUpdate.state, "maximized");
});

test("store worker returns a partial payload when the first page exceeds its response budget", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({ collectionResult: new Promise(() => {}) });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const collecting = worker.context.collectStorePages(tab.id, {
    id: 401, run_id: 9, target_url: url, target_date: "2026-09-29",
    monitored_product_ids: ["1005000000000001"],
  });
  await worker.flush();
  const timeout = worker.pendingTimers.find((timer) => timer.delay === 10 * 60 * 1000);
  assert.ok(timeout, "each page must finish within ten minutes");
  timeout.callback();
  const result = await collecting;
  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.products)), []);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [url]);
  assert.equal(result.payload.raw_data.collection_interrupted, true);
});

test("monitored-page timeout does not start a candidate scan on the same IXSPY controls", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({ collectionResult: new Promise(() => {}) });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const collecting = worker.context.collectStorePages(tab.id, {
    id: 409, run_id: 9, target_url: url, discovery_page_url: url,
    target_date: "2026-09-29", monitored_product_ids: ["1005000000000001"],
  });
  await worker.flush();
  worker.pendingTimers.find((timer) => timer.delay === 10 * 60 * 1000).callback();
  await worker.flush();
  const candidateScanStarted = worker.candidateScanMessages.length > 0;
  if (candidateScanStarted) {
    worker.pendingTimers.filter((timer) => timer.delay === 10 * 60 * 1000).at(-1)?.callback();
  }
  const result = await collecting;

  assert.equal(candidateScanStarted, false);
  assert.equal(result.payload.raw_data.collection_interrupted, true);
});

test("a chart timeout submits completed page data without opening another page", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const secondUrl = `${firstUrl}&page=2`;
  const worker = makeWorkerHarness({ collectionResult: {
    state: "ready",
    page_result: {
      platform_store_id: "123456",
      url: firstUrl,
      products: [{
        platform_product_id: "1005000000000001",
        historical_sales: [],
        history_read_diagnostic: {
          product_id: "1005000000000001",
          outcome: "unavailable",
          source: "ixspy_store_product_chart_read_timeout",
          timed_out: true,
          point_count: 0,
          missing_dates: [],
        },
      }],
      has_next_page: true,
      next_page_url: secondUrl,
      diagnostics: { collection_interrupted: true, chart_reads: [] },
    },
  } });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: firstUrl, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    id: 413, run_id: 9, target_url: firstUrl, discovery_page_url: firstUrl,
    target_date: "2026-09-29", monitored_product_ids: ["1005000000000001"],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.collection_interrupted, true);
  assert.equal(worker.collectionMessages.length, 1);
  assert.equal(worker.candidateScanMessages.length, 0);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [firstUrl]);
});

test("candidate page response timeout is bounded by the remaining store work budget", async () => {
  let clockReads = 0;
  const worker = makeWorkerHarness({
    collectionResult: new Promise(() => {}),
    clockNow: () => (clockReads++ === 0 ? 0 : 14 * 60 * 1000),
  });
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc&page=2";
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const collecting = worker.context.collectStorePages(tab.id, {
    id: 410, run_id: 9,
    target_url: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc",
    discovery_page_url: url, target_date: "2026-09-29", monitored_product_ids: [],
  });
  await worker.flush();
  const boundedTimeout = worker.pendingTimers.find((timer) => timer.delay === 60 * 1000);
  const unboundedTimeout = worker.pendingTimers.find((timer) => timer.delay === 10 * 60 * 1000);
  const timeout = boundedTimeout || unboundedTimeout;
  assert.ok(timeout, "candidate scan has a bounded response timeout");
  timeout.callback();
  const result = await collecting;

  assert.ok(boundedTimeout, "a scan starting at minute fourteen only receives the remaining one-minute budget");
  assert.equal(result.payload.candidate_page.scan_complete, false);
});

test("candidate page timeout includes only same-task saved checkpoints for diagnosis", async () => {
  const storeId = "123456";
  const productId = "1005000000000009";
  const url = `https://www.aliexpress.com/store/${storeId}/pages/all-items.html?shop_sortType=orders_desc&page=2`;
  const targetDate = "2026-09-29";
  const expectedDates = [
    "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
    "2026-09-26", "2026-09-27", "2026-09-28",
  ];
  const candidate = {
    platform_product_id: productId,
    url: `https://www.aliexpress.com/item/${productId}.html`,
    historical_sales: expectedDates.map((date, index) => ({
      date, value: index + 1, value_type: "daily_increment",
    })),
    history_source: "ixspy_visible_daily_increment",
    history_daily_capture_complete: true,
    history_expected_dates: expectedDates,
    history_missing_dates: [],
    history_read_diagnostic: {
      product_id: productId,
      outcome: "complete",
      source: "ixspy_visible_daily_increment",
      point_count: 7,
      missing_dates: [],
    },
    store_identity: {
      target_store_id: storeId,
      route_store_id: storeId,
      page_url: url,
    },
  };
  const worker = makeWorkerHarness({ collectionResult: new Promise(() => {}) });
  worker.storage[`browserCollectionStoreProgress:412`] = {
    run_id: 9,
    item_id: 412,
    store_id: storeId,
    target_date: targetDate,
    target_ids: [],
    products: {},
    visited_page_urls: [url],
    page_scans: [],
    chart_reads: [],
    candidate_page_progress: {
      run_id: 9,
      item_id: 412,
      store_id: storeId,
      target_date: targetDate,
      page_url: url,
      products: {
        [productId]: candidate,
        "1005000000000010": {
          ...candidate,
          platform_product_id: "1005000000000010",
          url: "https://www.aliexpress.com/item/1005000000000010.html",
          history_read_diagnostic: {
            ...candidate.history_read_diagnostic,
            product_id: "1005000000000010",
          },
          store_identity: { ...candidate.store_identity, target_store_id: "999999" },
        },
      },
    },
  };
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  const collecting = worker.context.collectStorePages(tab.id, {
    id: 412, run_id: 9,
    target_url: `https://www.aliexpress.com/store/${storeId}/pages/all-items.html?shop_sortType=orders_desc`,
    discovery_page_url: url, target_date: targetDate, monitored_product_ids: [],
  });
  await worker.flush();
  worker.pendingTimers.find((timer) => timer.delay >= 14 * 60 * 1000).callback();
  const result = await collecting;

  assert.equal(result.payload.candidate_page.scan_complete, false);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.payload.candidate_page.candidates.map((entry) => entry.platform_product_id))),
    [productId],
    JSON.stringify(result.payload.candidate_page),
  );
  assert.equal(result.payload.candidate_page.diagnostics.outcome, "page_response_timeout");
  assert.equal(result.payload.candidate_page.diagnostics.checkpoint_candidate_count, 1);
  assert.equal(result.payload.candidate_page.page_url, url);
});

test("store timeout submits saved product progress instead of restarting the whole store", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const product = {
    platform_product_id: "1005000000000001",
    url: "https://www.aliexpress.com/item/1005000000000001.html",
    historical_sales: [{ date: "2026-10-10", value: 4, value_type: "daily_increment" }],
    history_source: "ixspy_visible_daily_increment",
  };
  const worker = makeWorkerHarness({
    collectionResult: new Promise(() => {}),
    storeDiscoveryStatus: "PARTIAL",
    queueItems: [{ id: 401, target_type: "STORE", target_key: "store:1", store_id: 1,
      target_url: url, target_date: "2026-10-11", monitored_product_ids: [
        "1005000000000001", "1005000000000002",
      ] }],
  });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });
  worker.storage.automationDashboardTabId = 1;
  worker.storage.automationWindowId = 8;
  worker.storage.automationWindowOwned = true;
  worker.storage.automationWindowBackground = true;
  worker.storage.automationTabId = tab.id;
  worker.storage.automationItem = { ...worker.queue[0], status: "RUNNING" };
  worker.storage.automationClaimToken = "claim-401";
  worker.storage.automationTaskKind = "STORE";

  const processing = worker.context.processAutomationTabInternal(tab.id);
  await worker.flush();
  assert.equal(worker.collectionMessages[0].item_id, 401);
  assert.equal(worker.collectionMessages[0].claim_token, "claim-401");
  const progressResponse = new Promise((resolve) => {
    const handled = worker.listeners.message.callback({
    type: "store-collection-product-progress",
    item_id: 401,
    claim_token: "claim-401",
    product,
    }, { tab: { id: tab.id, url } }, resolve);
    if (!handled) resolve({ not_handled: true });
  });
  const progressAck = await progressResponse;
  const pageTimeout = worker.pendingTimers.find((timer) => timer.delay === 10 * 60 * 1000);
  assert.ok(pageTimeout);
  pageTimeout.callback();
  await processing;

  assert.deepEqual(JSON.parse(JSON.stringify(progressAck)), { ok: true });
  const resultRequest = worker.requests.find((request) => request.pathname.endsWith("/items/401/store-discovery"));
  assert.ok(resultRequest, "the completed product must be submitted with a partial store result");
  assert.deepEqual(resultRequest.body.products.map((entry) => entry.platform_product_id), [product.platform_product_id]);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/401/failure")), false);
  assert.deepEqual(Object.keys(worker.storage["browserCollectionStoreProgress:401"].products), []);
});

test("partial store retry keeps only complete seven-day product checkpoints", async () => {
  const worker = makeWorkerHarness();
  const dates = Array.from({ length: 7 }, (_, index) => `2026-10-${String(index + 4).padStart(2, "0")}`);
  const completeId = "1005000000000001";
  const unavailableId = "1005000000000002";
  const complete = {
    platform_product_id: completeId,
    history_expected_dates: dates,
    history_missing_dates: [],
    history_daily_capture_complete: true,
    historical_sales: dates.map((date) => ({ date, value: 1, value_type: "daily_increment" })),
    history_read_diagnostic: {
      product_id: completeId, outcome: "complete", source: "ixspy_visible_daily_increment",
      point_count: 7, missing_dates: [],
    },
  };
  const unavailable = {
    platform_product_id: unavailableId,
    history_expected_dates: dates,
    history_missing_dates: dates,
    history_daily_capture_complete: false,
    historical_sales: [],
    history_read_diagnostic: {
      product_id: unavailableId, outcome: "unavailable", source: "ixspy_data_unavailable",
      point_count: 0, missing_dates: dates,
    },
  };
  const progress = { products: { [completeId]: complete, [unavailableId]: unavailable } };

  worker.context.retainCompleteStoreProgress(progress, "2026-10-11");

  assert.deepEqual(Object.keys(progress.products), [completeId]);
});

test("store worker retains completed pages when a later page stalls", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const secondUrl = `${firstUrl}&page=2`;
  const worker = makeWorkerHarness({ collectionResults: [
    { state: "ready", page_result: { platform_store_id: "123456", url: firstUrl,
      products: [{ platform_product_id: "1005000000000001", historical_sales: [] }],
      has_next_page: true, next_page_url: secondUrl } },
    new Promise(() => {}),
  ] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: firstUrl, active: true });
  const collecting = worker.context.collectStorePages(tab.id, {
    target_url: firstUrl, target_date: "2026-09-29",
    monitored_product_ids: ["1005000000000001", "1005000000000002"],
  });
  await worker.flush();
  const timeouts = worker.pendingTimers.filter((timer) => timer.delay === 10 * 60 * 1000);
  assert.equal(timeouts.length, 2);
  timeouts[1].callback();
  const result = await collecting;
  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [firstUrl, secondUrl]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.products.map((product) => product.platform_product_id))),
    ["1005000000000001"]);
});

test("store worker follows a persisted route-alias candidate cursor after verifying page identity", async () => {
  const canonicalUrl = "https://www.aliexpress.com/store/1105161198/pages/all-items.html?shop_sortType=orders_desc";
  const aliasUrl = "https://www.aliexpress.com/store/1105170187/pages/all-items.html?page=2&shop_sortType=orders_desc";
  const aliasPageThree = "https://www.aliexpress.com/store/1105170187/pages/all-items.html?page=3&shop_sortType=orders_desc";
  const productId = "1005000000000001";
  const routeAliasIdentity = {
    target_store_id: "1105161198",
    route_store_id: "1105170187",
    heading_store_id: "1105161198",
    page_url: aliasUrl,
  };
  const worker = makeWorkerHarness({
    realTimers: true,
    collectionResults: [
      { state: "ready", page_result: { platform_store_id: "1105161198", url: canonicalUrl,
        products: [{ platform_product_id: productId, historical_sales: [] }],
        has_next_page: false, next_page_url: null } },
      { state: "ready", platform_store_id: "1105161198", store_identity: routeAliasIdentity },
      { state: "ready", page_result: { platform_store_id: "1105161198", url: aliasUrl,
        store_identity: routeAliasIdentity, candidates: [], pagination_known: true,
        has_next_page: true, next_page_url: aliasPageThree, scan_complete: true } },
    ],
  });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: canonicalUrl, active: true });

  const result = await worker.context.collectStorePages(tab.id, {
    id: 402,
    target_type: "STORE",
    target_url: canonicalUrl,
    discovery_page_url: aliasUrl,
    target_date: "2026-09-29",
    monitored_product_ids: [productId],
  });

  assert.equal(result.state, "ready");
  assert.equal(worker.collectionMessages[0].expected_store_id, "1105161198");
  assert.equal(worker.candidateScanMessages[0].expectedStoreId, "1105161198");
  assert.equal(worker.candidateScanMessages[0].expectedPageUrl, aliasUrl);
  assert.equal(result.payload.candidate_page.page_url, aliasUrl);
  assert.equal(result.payload.candidate_page.next_page_url, aliasPageThree);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.store_identity)), routeAliasIdentity);
});

test("expired extension authorization is refreshed from the signed-in dashboard and retried once", async () => {
  const worker = makeWorkerHarness({ expiredToken: "expired-token", refreshedToken: "fresh-token" });

  const result = await worker.context.api("/api/browser-collection/runs/today", {}, "http://127.0.0.1:8000");

  assert.deepEqual(result, {});
  assert.deepEqual(worker.requests.map((request) => request.token), ["expired-token", "fresh-token"]);
  assert.equal(worker.storage.apiAccessTokens["http://127.0.0.1:8000"], "fresh-token");
  assert.equal(worker.notifications.length, 0);
});

test("expired authorization asks for dashboard login when renewal is unavailable", async () => {
  const worker = makeWorkerHarness({ expiredToken: "expired-token" });

  await assert.rejects(
    worker.context.api("/api/browser-collection/runs/today", {}, "http://127.0.0.1:8000"),
    (error) => error.status === 401,
  );

  assert.equal(worker.requests.length, 1, "does not retry with the rejected token");
  assert.equal(worker.notifications.length, 1);
  assert.match(worker.notifications[0].message, /登录本地监控台/);
});

test("expired authorization tries a dashboard tab on the API host before an unrelated worker tab", async () => {
  const worker = makeWorkerHarness({
    expiredToken: "expired-token",
    dashboardUrl: "http://127.0.0.1:3000/?dianxiaomi_worker=1",
    dashboardTabs: [{
      id: 2,
      windowId: 7,
      url: "http://192.168.66.115:3000/?browser_collection=1",
    }],
    dashboardTokensByTabId: { 1: null, 2: "fresh-token" },
  });

  const result = await worker.context.api(
    "/api/browser-collection/runs/today",
    {},
    "http://192.168.66.115:3000",
  );

  assert.deepEqual(result, {});
  assert.deepEqual(worker.scriptCalls, [2]);
  assert.deepEqual(worker.requests.map((request) => request.token), ["expired-token", "fresh-token"]);
  assert.equal(worker.storage.apiAccessTokens["http://192.168.66.115:3000"], "fresh-token");
});

test("scheduled items reuse one foreground worker tab without blanking the final page", async () => {
  const worker = makeWorkerHarness();
  await worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?dianxiaomi_worker=1&browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();

  const firstTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  assert.equal(worker.windowCreates.length, 1);
  assert.equal(worker.windowCreates[0].focused, true);
  assert.equal(worker.windowCreates[0].state, "maximized");
  assert.equal(worker.windowCreates[0].type, "normal");
  assert.equal(worker.windowCreates[0].url, "about:blank");
  assert.equal(firstTab.windowId, 8);
  assert.equal(firstTab.active, true, "IXSPY gets an active page so its chart can render");
  assert.equal(firstTab.id, 10, "the existing blank worker tab is navigated to the first product");
  assert.equal(worker.createdTabs.some((tab) => tab.url.includes("100000000101")), false,
    "does not create a second tab for the first product");

  for (let index = 0; index < 6; index += 1) {
    const timer = worker.pendingTimers.find((candidate) => candidate.delay === 500 && !candidate.executed);
    if (!timer) break;
    timer.executed = true;
    timer.callback();
    await worker.flush();
  }

  assert.deepEqual(
    worker.requests.filter((request) => request.pathname.includes("/snapshot")).map((request) => Number(request.pathname.match(/items\/(\d+)\/snapshot/)?.[1])),
    [101, 102, 103],
  );
  assert.equal(worker.collectionMessages[0].history_target_date, "2026-09-29");
  assert.ok(worker.updatedTabs.some((tab) => tab.url?.endsWith("100000000101.html")),
    "collection advances by navigating the recycled worker tab");
  assert.equal(worker.createdTabs.length, 2, "only the one-page prefetches need new tabs");
  assert.ok(worker.updatedTabs.some((tab) => tab.id === 10 && tab.url === "https://www.aliexpress.com/item/100000000101.html"),
    "the first product reuses the initial blank tab");
  assert.deepEqual(worker.removedTabs, [10, 11], "replaces completed pages without leaving blank tabs behind");
  assert.equal(worker.storage.idleAutomationTabId, 12, "keeps the final completed page for the next task");
  assert.deepEqual(
    (await worker.chrome.tabs.query({ windowId: 8 })).map((tab) => tab.url),
    ["https://www.aliexpress.com/item/100000000103.html"],
  );
  assert.deepEqual(worker.windowRemoves, [], "collection does not close the worker window");
  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true));
  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true && update.state === "maximized"),
    "the worker stays maximized and focused while the item is processed");
  const finalWorkerWindowUpdate = worker.windowUpdates.filter((update) => update.id === 8).at(-1);
  assert.equal(finalWorkerWindowUpdate.focused, true);
  assert.equal(finalWorkerWindowUpdate.state, "maximized", "the completed worker window stays in front");
});

test("closes tracked collection tabs even if worker-window ownership flags were lost", async () => {
  const worker = makeWorkerHarness({
    queueItems: [{
      id: 101,
      product_id: 101,
      target_type: "PRODUCT",
      target_url: "https://www.aliexpress.com/item/100000000101.html",
      title: "Product 1",
      target_date: "2026-09-30",
    }],
  });
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();

  const productTabId = worker.storage.automationTabId;
  worker.storage.automationWindowOwned = false;
  worker.storage.automationWindowBackground = false;
  const productTab = await worker.chrome.tabs.get(productTabId);
  worker.listeners.updated.callback(productTabId, { status: "complete" }, productTab);
  await worker.flush();

  assert.ok(worker.removedTabs.includes(productTabId), "removes the explicitly tracked product tab");
  assert.ok(await worker.chrome.tabs.get(1), "does not close the dashboard tab");
});

test("loaded store route alias is accepted only when the page identity matches the task store", async () => {
  const worker = makeWorkerHarness({
    realTimers: true,
    collectionResult: {
      state: "ready",
      platform_store_id: "1105161198",
      store_identity: {
        target_store_id: "1105161198",
        route_store_id: "1105170187",
        heading_store_id: "1105161198",
        page_url: "https://www.aliexpress.com/store/1105170187/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc",
      },
    },
  });
  const tab = await worker.chrome.tabs.create({
    windowId: 8,
    url: "https://www.aliexpress.com/store/1105170187/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc",
    active: true,
  });

  const resolved = await worker.context.waitForStorePage(
    tab.id,
    "https://www.aliexpress.com/store/1105161198/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc",
    1000,
    "1105161198",
  );

  assert.equal(resolved.storeId, "1105170187");
  assert.deepEqual(JSON.parse(JSON.stringify(resolved.store_identity)), {
    target_store_id: "1105161198",
    route_store_id: "1105170187",
    heading_store_id: "1105161198",
    page_url: "https://www.aliexpress.com/store/1105170187/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc",
  });
  assert.equal(worker.collectionMessages[0].expected_store_id, "1105161198");
});

test("store progress accepts a route alias verified by the explicit Store ID label", async () => {
  const storeId = "1105179516";
  const routeStoreId = "1105182474";
  const productId = "1005001234567890";
  const targetUrl = `https://www.aliexpress.com/store/${storeId}/pages/all-items.html?shop_sortType=orders_desc`;
  const pageUrl = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const worker = makeWorkerHarness();
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: pageUrl, active: true });
  Object.assign(worker.storage, {
    automationItem: {
      id: 501,
      run_id: 9,
      target_type: "STORE",
      target_url: targetUrl,
      target_date: "2026-10-17",
      monitored_product_ids: [productId],
    },
    automationTabId: tab.id,
    automationClaimToken: "claim-501",
  });
  let response;

  worker.listeners.message.callback({
    type: "store-collection-product-progress",
    item_id: 501,
    claim_token: "claim-501",
    product: {
      platform_product_id: productId,
      url: `https://www.aliexpress.com/item/${productId}.html`,
      historical_sales: [],
      store_identity: {
        target_store_id: storeId,
        route_store_id: routeStoreId,
        heading_store_id: null,
        labeled_store_ids: [storeId],
      },
    },
  }, { tab }, (value) => { response = value; });
  await worker.flush();

  assert.deepEqual(JSON.parse(JSON.stringify(response)), { ok: true });
  assert.deepEqual(
    JSON.parse(JSON.stringify(worker.storage["browserCollectionStoreProgress:501"].visited_page_urls)),
    [pageUrl],
  );
});

test("store product progress accepts a page identity approved for this store", async () => {
  const storeId = "1105179516";
  const routeStoreId = "1105182474";
  const productId = "1005001234567890";
  const targetUrl = `https://www.aliexpress.com/store/${storeId}/pages/all-items.html?shop_sortType=orders_desc`;
  const pageUrl = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const worker = makeWorkerHarness();
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: pageUrl, active: true });
  Object.assign(worker.storage, {
    automationItem: {
      id: 503,
      run_id: 9,
      target_type: "STORE",
      target_url: targetUrl,
      target_date: "2026-10-17",
      monitored_product_ids: [productId],
      approved_store_url_identities: [{
        route_store_id: routeStoreId,
        normalized_page_identity: `www.aliexpress.com/store/${routeStoreId}/pages/all-items.html`,
        evidence: { ixspy_store_name: "Reviewed Store" },
      }],
    },
    automationTabId: tab.id,
    automationClaimToken: "claim-503",
  });
  let response;

  worker.listeners.message.callback({
    type: "store-collection-product-progress",
    item_id: 503,
    claim_token: "claim-503",
    product: {
      platform_product_id: productId,
      url: `https://www.aliexpress.com/item/${productId}.html`,
      historical_sales: [],
      store_identity: {
        target_store_id: storeId,
        route_store_id: routeStoreId,
        page_url: pageUrl,
        heading_store_id: null,
        labeled_store_ids: [],
        ixspy_store_name: " Reviewed Store ",
        identity_method: "backend_approved_url_alias",
      },
    },
  }, { tab }, (value) => { response = value; });
  await worker.flush();

  assert.deepEqual(JSON.parse(JSON.stringify(response)), { ok: true });
  assert.ok(worker.storage["browserCollectionStoreProgress:503"].products[productId]);
});

test("candidate progress accepts a page identity approved for this store", async () => {
  const storeId = "1105179516";
  const routeStoreId = "1105182474";
  const productId = "1005001234567890";
  const targetUrl = `https://www.aliexpress.com/store/${storeId}/pages/all-items.html?shop_sortType=orders_desc`;
  const pageUrl = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const worker = makeWorkerHarness();
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: pageUrl, active: true });
  Object.assign(worker.storage, {
    automationItem: {
      id: 504,
      run_id: 9,
      target_type: "STORE",
      target_url: targetUrl,
      discovery_page_url: targetUrl,
      target_date: "2026-10-17",
      monitored_product_ids: [],
      approved_store_url_identities: [{
        route_store_id: routeStoreId,
        normalized_page_identity: `www.aliexpress.com/store/${routeStoreId}/pages/all-items.html`,
        evidence: { ixspy_store_name: "Reviewed Store" },
      }],
    },
    automationTabId: tab.id,
    automationClaimToken: "claim-504",
  });
  let response;

  worker.listeners.message.callback({
    type: "store-candidate-progress",
    item_id: 504,
    claim_token: "claim-504",
    expected_store_id: storeId,
    expected_page_url: pageUrl,
    product: {
      platform_product_id: productId,
      url: `https://www.aliexpress.com/item/${productId}.html`,
      historical_sales: [
        "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13",
        "2026-10-14", "2026-10-15", "2026-10-16",
      ].map((date, index) => ({ date, value: index + 1, value_type: "daily_increment" })),
      history_source: "ixspy_visible_daily_increment",
      history_daily_capture_complete: true,
      history_expected_dates: [
        "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13",
        "2026-10-14", "2026-10-15", "2026-10-16",
      ],
      history_missing_dates: [],
      history_read_diagnostic: {
        product_id: productId,
        outcome: "complete",
        source: "ixspy_visible_daily_increment",
        point_count: 7,
        missing_dates: [],
      },
      store_identity: {
        target_store_id: storeId,
        route_store_id: routeStoreId,
        page_url: pageUrl,
        heading_store_id: null,
        labeled_store_ids: [],
        ixspy_store_name: "Reviewed Store",
        identity_method: "backend_approved_url_alias",
      },
    },
  }, { tab }, (value) => { response = value; });
  await worker.flush();

  assert.deepEqual(JSON.parse(JSON.stringify(response)), { ok: true });
  assert.ok(worker.storage["browserCollectionStoreProgress:504"].candidate_page_progress.products[productId]);
});

test("candidate progress checkpoints a valid partial IXSPY history without stopping the scan", async () => {
  const storeId = "990901";
  const productId = "1005001234567891";
  const pageUrl = `https://www.aliexpress.com/store/${storeId}/pages/all-items.html?shop_sortType=orders_desc`;
  const expectedDates = [
    "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13",
    "2026-10-14", "2026-10-15", "2026-10-16",
  ];
  const history = expectedDates.slice(0, 6).map((date, index) => ({
    date, value: index + 1, value_type: "daily_increment",
  }));
  const worker = makeWorkerHarness();
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: pageUrl, active: true });
  Object.assign(worker.storage, {
    automationItem: {
      id: 505,
      run_id: 9,
      target_type: "STORE",
      target_url: pageUrl,
      discovery_page_url: pageUrl,
      target_date: "2026-10-17",
      monitored_product_ids: [],
    },
    automationTabId: tab.id,
    automationClaimToken: "claim-505",
  });
  let response;

  worker.listeners.message.callback({
    type: "store-candidate-progress",
    item_id: 505,
    claim_token: "claim-505",
    expected_store_id: storeId,
    expected_page_url: pageUrl,
    product: {
      platform_product_id: productId,
      url: `https://www.aliexpress.com/item/${productId}.html`,
      historical_sales: history,
      history_source: "ixspy_visible_daily_increment",
      history_daily_capture_complete: false,
      history_expected_dates: expectedDates,
      history_missing_dates: ["2026-10-16"],
      history_read_diagnostic: {
        product_id: productId,
        outcome: "partial",
        source: "ixspy_visible_daily_increment",
        point_count: 6,
        missing_dates: ["2026-10-16"],
      },
      store_identity: {
        target_store_id: storeId,
        route_store_id: storeId,
        page_url: pageUrl,
        heading_store_id: null,
        labeled_store_ids: [storeId],
        identity_method: "explicit_store_id",
      },
    },
  }, { tab }, (value) => { response = value; });
  await worker.flush();

  assert.deepEqual(JSON.parse(JSON.stringify(response)), { ok: true });
  const checkpoint = worker.storage["browserCollectionStoreProgress:505"].candidate_page_progress;
  assert.equal(checkpoint.products[productId].history_daily_capture_complete, false);
  assert.deepEqual(checkpoint.products[productId].history_missing_dates, ["2026-10-16"]);

  const unreadableProduct = {
    ...checkpoint.products[productId],
    historical_sales: [],
    history_source: "ixspy_store_candidate_chart_read_error",
    history_daily_capture_complete: false,
    history_missing_dates: expectedDates,
    history_read_diagnostic: {
      product_id: productId,
      outcome: "unavailable",
      source: "ixspy_store_candidate_chart_read_error",
      point_count: 0,
      missing_dates: expectedDates,
      error: "IXSPY chart timed out",
    },
  };
  worker.listeners.message.callback({
    type: "store-candidate-progress",
    item_id: 505,
    claim_token: "claim-505",
    expected_store_id: storeId,
    expected_page_url: pageUrl,
    product: unreadableProduct,
  }, { tab }, (value) => { response = value; });
  await worker.flush();
  assert.deepEqual(JSON.parse(JSON.stringify(response)), { ok: true });

  const invalidPartialProduct = {
    ...unreadableProduct,
    historical_sales: [history[0], history[0]],
    history_source: "ixspy_visible_daily_increment",
    history_missing_dates: expectedDates.slice(1),
    history_read_diagnostic: {
      product_id: productId,
      outcome: "partial",
      source: "ixspy_visible_daily_increment",
      point_count: 2,
      missing_dates: expectedDates.slice(1),
    },
  };
  worker.listeners.message.callback({
    type: "store-candidate-progress",
    item_id: 505,
    claim_token: "claim-505",
    expected_store_id: storeId,
    expected_page_url: pageUrl,
    product: invalidPartialProduct,
  }, { tab }, (value) => { response = value; });
  await worker.flush();
  assert.equal(response.ok, false);
  assert.equal(response.reason, "candidate_history_invalid");
  assert.deepEqual(JSON.parse(JSON.stringify(response.failed_checks)), ["daily_increment_points"]);
});

test("candidate chart checkpoints persist under the claimed store page for retry", async () => {
  const storeId = "1105179516";
  const routeStoreId = "1105182474";
  const productId = "1005001234567890";
  const targetUrl = `https://www.aliexpress.com/store/${storeId}/pages/all-items.html?shop_sortType=orders_desc`;
  const pageUrl = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const worker = makeWorkerHarness();
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: pageUrl, active: true });
  Object.assign(worker.storage, {
    automationItem: {
      id: 502,
      run_id: 9,
      target_type: "STORE",
      target_url: targetUrl,
      discovery_page_url: targetUrl,
      target_date: "2026-10-17",
      monitored_product_ids: [],
    },
    automationTabId: tab.id,
    automationClaimToken: "claim-502",
  });
  let response;

  worker.listeners.message.callback({
    type: "store-candidate-progress",
    item_id: 502,
    claim_token: "claim-502",
    expected_store_id: storeId,
    expected_page_url: pageUrl,
    page_url: pageUrl,
    product: {
      platform_product_id: productId,
      url: `https://www.aliexpress.com/item/${productId}.html`,
      historical_sales: [
        "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13",
        "2026-10-14", "2026-10-15", "2026-10-16",
      ].map((date, index) => ({ date, value: index + 1, value_type: "daily_increment" })),
      history_source: "ixspy_visible_daily_increment",
      history_daily_capture_complete: true,
      history_expected_dates: [
        "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13",
        "2026-10-14", "2026-10-15", "2026-10-16",
      ],
      history_missing_dates: [],
      history_read_diagnostic: {
        product_id: productId,
        outcome: "complete",
        source: "ixspy_visible_daily_increment",
        point_count: 7,
        missing_dates: [],
      },
      store_identity: {
        target_store_id: storeId,
        route_store_id: routeStoreId,
        heading_store_id: null,
        labeled_store_ids: [storeId],
        page_url: pageUrl,
      },
    },
  }, { tab }, (value) => { response = value; });
  await worker.flush();

  assert.deepEqual(JSON.parse(JSON.stringify(response)), { ok: true });
  const checkpoint = worker.storage["browserCollectionStoreProgress:502"].candidate_page_progress;
  assert.equal(checkpoint.target_date, "2026-10-17");
  assert.equal(checkpoint.page_url, pageUrl);
  assert.equal(checkpoint.products[productId].history_daily_capture_complete, true);
});

test("route alias matching the requested URL still requires target-store identity", async () => {
  const worker = makeWorkerHarness({
    realTimers: true,
    collectionResult: {
      state: "ready",
      platform_store_id: "1105161198",
      store_identity: {
        target_store_id: "1105161198",
        route_store_id: "1105170187",
        heading_store_id: "1105161198",
        page_url: "https://www.aliexpress.com/store/1105170187/pages/all-items.html?shop_sortType=orders_desc",
      },
    },
  });
  const aliasUrl = "https://www.aliexpress.com/store/1105170187/pages/all-items.html?shop_sortType=orders_desc";
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: aliasUrl, active: true });

  const resolved = await worker.context.waitForStorePage(tab.id, aliasUrl, 1000, "1105161198");

  assert.equal(resolved.storeId, "1105170187");
  assert.equal(worker.collectionMessages[0].expected_store_id, "1105161198");
});

test("loaded route alias is rejected when the page identity belongs to another store", async () => {
  const worker = makeWorkerHarness({
    realTimers: true,
    collectionResult: {
      state: "failed",
      error_type: "store_identity_mismatch",
      message: "任务目标店铺 ID 与页面标题不一致",
    },
  });
  const tab = await worker.chrome.tabs.create({
    windowId: 8,
    url: "https://www.aliexpress.com/store/1105182474/pages/all-items.html?shop_sortType=orders_desc",
    active: true,
  });

  await assert.rejects(
    worker.context.waitForStorePage(
      tab.id,
      "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc",
      1000,
      "1105179516",
    ),
    (error) => error.code === "STORE_IDENTITY_REVIEW" && error.error_type === "store_identity_mismatch",
  );
});

test("waits for manual review when a route alias has no explicit Store ID evidence", async () => {
  const worker = makeWorkerHarness({
    realTimers: true,
    collectionResult: {
      state: "failed",
      error_type: "store_identity_unverified",
      message: "网址路由 ID 1105182474 不同，页面 Store ID 未识别，无法确认是否为同一店铺",
    },
  });
  const aliasUrl = "https://www.aliexpress.com/store/1105182474/pages/all-items.html?shop_sortType=orders_desc";
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: aliasUrl, active: true });

  await assert.rejects(
    worker.context.waitForStorePage(
      tab.id,
      "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc",
      1000,
      "1105179516",
    ),
    (error) => error.code === "STORE_IDENTITY_REVIEW" && error.error_type === "store_identity_unverified",
  );
});

test("store identity review records evidence without pausing for a manual decision", async () => {
  const targetUrl = "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc";
  const evidence = {
    route_store_id: "1105179516",
    heading_store_id: null,
    labeled_store_ids: [],
    page_title: "AMZSTEM Pop Toys Store - AliExpress",
    ixspy_store_name: "AMZSTEM Pop Toys Store",
    identity_method: "unverified",
  };
  const message = "页面未显示可确认店铺的 Store ID，请人工核实店铺身份后继续";
  let now = 0;
  const worker = makeWorkerHarness({ clockNow: () => now, collectionResult: {
    state: "identity_review",
    error_type: "store_identity_unverified",
    message,
    page_url: targetUrl,
    evidence,
  } });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: targetUrl, active: true });
  Object.assign(worker.storage, {
    automationItem: {
      id: 817, run_id: 9, target_type: "STORE", target_url: targetUrl,
      target_date: "2026-10-18", monitored_product_ids: ["1005001234567890"],
      title: "Test store",
    },
    automationTabId: tab.id,
    automationTaskKind: "STORE",
    automationClaimToken: "claim-817",
    browserCollectionWorkerMode: true,
  });

  await worker.context.processAutomationTabInternal(tab.id);

  assert.notEqual(worker.storage.identityReviewPaused, true);
  assert.notEqual(worker.storage.challengePaused, true);
  const reviewRequest = worker.requests.find((request) => request.pathname.endsWith("/items/817/identity-review"));
  assert.ok(reviewRequest);
  assert.equal(reviewRequest.body.page_url, targetUrl);
  assert.deepEqual(JSON.parse(JSON.stringify(reviewRequest.body.evidence)), evidence);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/817/challenge")), false);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/817/failure")), false);
  assert.equal(worker.notifications.some((notice) => notice.title === "店铺身份待核验"), false);
  assert.equal(worker.storage.automationTabId, undefined);
  assert.equal(worker.storage.idleAutomationTabId, tab.id);
});

test("store identity errors are routed to manual review even when the collector state is failed", async () => {
  const targetUrl = "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc";
  const evidence = {
    route_store_id: "1105179516",
    heading_store_id: null,
    labeled_store_ids: [],
    page_title: "AMZSTEM Pop Toys Store - AliExpress",
    ixspy_store_name: "AMZSTEM Pop Toys Store",
    identity_method: "unverified",
  };
  const worker = makeWorkerHarness({
    collectionResult: {
      state: "failed",
      error_type: "store_identity_unverified",
      message: "Store ID 未识别，需要人工核实",
      page_url: targetUrl,
      evidence,
    },
  });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: targetUrl, active: true });
  Object.assign(worker.storage, {
    automationItem: {
      id: 818, run_id: 9, target_type: "STORE", target_url: targetUrl,
      target_date: "2026-10-19", monitored_product_ids: ["1005001234567890"],
      title: "Test store",
    },
    automationTabId: tab.id,
    automationWindowId: tab.windowId,
    automationTaskKind: "STORE",
    automationClaimToken: "claim-818",
    browserCollectionWorkerMode: true,
  });

  await worker.context.processAutomationTabInternal(tab.id);

  assert.notEqual(worker.storage.identityReviewPaused, true);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/818/identity-review")), true);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/818/failure")), false);
});

test("unverified identity stays in manual review when AliExpress drops the Orders sort query", async () => {
  const targetUrl = "https://www.aliexpress.com/store/1105272224/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc";
  const observedUrl = "https://www.aliexpress.com/store/1105272224/pages/all-items.html?sortType=bestmatch_sort";
  const evidence = {
    route_store_id: "1105272224",
    heading_store_id: null,
    labeled_store_ids: [],
    page_title: "3D_ZeapoAI 3DPrinted Store - AliExpress",
    ixspy_store_name: null,
    identity_method: "unverified",
  };
  const worker = makeWorkerHarness({
    collectionResult: {
      state: "failed",
      error_type: "store_identity_unverified",
      message: "Store ID 未识别，需要人工核实",
      page_url: observedUrl,
      evidence,
    },
  });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: observedUrl, active: true });
  Object.assign(worker.storage, {
    automationItem: {
      id: 819, run_id: 9, target_type: "STORE", target_url: targetUrl,
      target_date: "2026-10-20", monitored_product_ids: ["1005001234567890"],
      title: "AliExpress Store 1105272224",
    },
    automationTabId: tab.id,
    automationWindowId: tab.windowId,
    automationTaskKind: "STORE",
    automationClaimToken: "claim-819",
    browserCollectionWorkerMode: true,
  });

  await worker.context.processAutomationTabInternal(tab.id);

  assert.notEqual(worker.storage.identityReviewPaused, true);
  const reviewRequest = worker.requests.find((request) => request.pathname.endsWith("/items/819/identity-review"));
  assert.ok(reviewRequest);
  assert.equal(reviewRequest.body.page_url, observedUrl);
  assert.equal(reviewRequest.body.evidence.route_store_id, "1105272224");
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/819/failure")), false);
});

test("unverified identity stays in manual review when AliExpress rewrites the store path", async () => {
  const targetUrl = "https://www.aliexpress.com/store/1105272224/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc";
  const observedUrl = "https://www.aliexpress.com/store/1105272224/";
  const evidence = {
    route_store_id: "1105272224",
    heading_store_id: null,
    labeled_store_ids: [],
    page_title: "3D_ZeapoAI 3DPrinted Store - AliExpress",
    ixspy_store_name: null,
    identity_method: "unverified",
  };
  const worker = makeWorkerHarness({
    collectionResult: {
      state: "identity_review",
      error_type: "store_identity_unverified",
      message: "店铺 Store ID 未识别，需要人工核实",
      page_url: observedUrl,
      evidence,
    },
  });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: observedUrl, active: true });
  Object.assign(worker.storage, {
    automationItem: {
      id: 820, run_id: 9, target_type: "STORE", target_url: targetUrl,
      target_date: "2026-10-20", monitored_product_ids: ["1005001234567890"],
      title: "AliExpress Store 1105272224",
    },
    automationTabId: tab.id,
    automationWindowId: tab.windowId,
    automationTaskKind: "STORE",
    automationClaimToken: "claim-820",
    browserCollectionWorkerMode: true,
  });

  await worker.context.processAutomationTabInternal(tab.id);

  const reviewRequest = worker.requests.find((request) => request.pathname.endsWith("/items/820/identity-review"));
  assert.ok(reviewRequest);
  assert.equal(reviewRequest.body.page_url, targetUrl);
  assert.ok(reviewRequest.body.error_message.includes(`实际读取 URL：${observedUrl}`));
  assert.notEqual(worker.storage.identityReviewPaused, true);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/820/failure")), false);
});

test("identity_review_defers_item_releases_store_tab_and_claims_next", async () => {
  const targetUrl = "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({
    dashboardUrl: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated",
    collectionResult: {
      state: "identity_review",
      error_type: "store_identity_unverified",
      message: "店铺身份需要人工核实",
      page_url: targetUrl,
      evidence: {
        route_store_id: "1105179516",
        heading_store_id: null,
        labeled_store_ids: [],
        page_title: "AMZSTEM Pop Toys Store - AliExpress",
        ixspy_store_name: "AMZSTEM Pop Toys Store",
        identity_method: "unverified",
      },
    },
    queueItems: [
      { id: 821, target_type: "STORE", target_url: targetUrl, title: "Store A" },
      { id: 822, target_type: "STORE", target_url: "https://www.aliexpress.com/store/1105272224/pages/all-items.html?shop_sortType=orders_desc", title: "Store B" },
    ],
  });
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  await worker.context.syncDedicatedWorkerMode();
  worker.queue[0].status = "RUNNING";
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: targetUrl, active: true });
  Object.assign(worker.storage, {
    automationWindowId: 8,
    automationItem: { id: 821, run_id: 9, target_type: "STORE", target_url: targetUrl, title: "Store A" },
    automationTabId: tab.id,
    automationTaskKind: "STORE",
    automationClaimToken: "claim-821",
    browserCollectionWorkerMode: true,
  });

  await worker.context.processAutomationTabInternal(tab.id);

  assert.equal(worker.storage.identityReviewPaused, undefined);
  assert.notEqual(worker.storage.challengePaused, true);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/821/challenge")), false);
  assert.equal(worker.queue[0].status, "SKIPPED");
  assert.equal(worker.storage.automationTabId, undefined);
  assert.equal(worker.storage.idleAutomationTabId, tab.id);
  assert.equal(worker.notifications.some((notice) => notice.title === "店铺身份待核验"), false);

  await worker.context.pollAutomation();

  assert.equal(worker.queue[1].status, "RUNNING");
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/claim")), true);
});

test("waitForStorePage accepts an alias only when the backend approved that exact page identity", async () => {
  const targetStoreId = "1105179516";
  const routeStoreId = "1105182474";
  const url = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const approved = [{
    route_store_id: routeStoreId,
    normalized_page_identity: `www.aliexpress.com/store/${routeStoreId}/pages/all-items.html`,
    evidence: { ixspy_store_name: "AMZSTEM Pop Toys Store" },
  }];
  let now = 0;
  const worker = makeWorkerHarness({ clockNow: () => now, collectionResult: {
    state: "ready",
    platform_store_id: targetStoreId,
    store_identity: {
      target_store_id: targetStoreId,
      route_store_id: routeStoreId,
      page_url: url,
      heading_store_id: null,
      labeled_store_ids: [],
      ixspy_store_name: "AMZSTEM Pop Toys Store",
      identity_method: "backend_approved_url_alias",
    },
  } });
  worker.context.setTimeout = (callback, delay) => { now += delay; callback(); return 0; };
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });

  const page = await worker.context.waitForStorePage(tab.id, url, 1000, targetStoreId, approved);

  assert.equal(page.storeId, routeStoreId);
  assert.equal(page.store_identity.identity_method, "backend_approved_url_alias");
  assert.deepEqual(worker.collectionMessages[0].approved_store_url_identities, approved);
});

test("store task opens its backend-approved URL while retaining the canonical target URL", async () => {
  const targetStoreId = "1105179516";
  const routeStoreId = "1105182474";
  const approvedUrl = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc`;
  const worker = makeWorkerHarness();
  const item = {
    id: 824,
    target_type: "STORE",
    target_url: `https://www.aliexpress.com/store/${targetStoreId}/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc`,
    collection_start_url: approvedUrl,
    approved_store_url_identities: [{
      route_store_id: routeStoreId,
      normalized_page_identity: `www.aliexpress.com/store/${routeStoreId}/pages/all-items.html`,
      evidence: { ixspy_store_name: "AMZSTEM Pop Toys Store" },
    }],
  };

  await worker.context.openAutomationItem(item, "STORE");

  assert.ok(
    worker.createdTabs.some((tab) => tab.url === approvedUrl)
      || worker.updatedTabs.some((tab) => tab.url === approvedUrl),
    JSON.stringify({ createdTabs: worker.createdTabs, updatedTabs: worker.updatedTabs }),
  );
  assert.equal(worker.storage.automationItem.target_url, item.target_url);
});

test("store task ignores a collection start URL that is not in its approved identities", async () => {
  const targetUrl = "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness();

  await worker.context.openAutomationItem({
    id: 825,
    target_type: "STORE",
    target_url: targetUrl,
    collection_start_url: "https://www.aliexpress.com/store/1105182474/pages/all-items.html?shop_sortType=orders_desc",
    approved_store_url_identities: [],
  }, "STORE");

  assert.ok(worker.updatedTabs.some((tab) => tab.url === targetUrl));
});

test("waitForStorePage refuses an alias omitted from this store's backend identity list", async () => {
  const targetStoreId = "1105179516";
  const routeStoreId = "1105182474";
  const url = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  let now = 0;
  const worker = makeWorkerHarness({ clockNow: () => now, collectionResult: {
    state: "identity_review",
    error_type: "store_identity_unverified",
    message: "店铺页面身份未核实",
    page_url: url,
    evidence: {
      route_store_id: routeStoreId,
      heading_store_id: null,
      labeled_store_ids: [],
      page_title: "AliExpress Store",
      ixspy_store_name: null,
      identity_method: "unverified",
    },
  } });
  worker.context.setTimeout = (callback, delay) => { now += delay; callback(); return 0; };
  const tab = await worker.chrome.tabs.create({ windowId: 8, url, active: true });

  await assert.rejects(
    worker.context.waitForStorePage(tab.id, url, 1000, targetStoreId, []),
    (error) => error.code === "STORE_IDENTITY_REVIEW"
      && error.error_type === "store_identity_unverified"
      && error.evidence?.route_store_id === routeStoreId,
  );
});

test("identity review does not reopen the store when the page is manually confirmed later", async () => {
  const targetStoreId = "1105179516";
  const routeStoreId = "1105182474";
  const targetUrl = `https://www.aliexpress.com/store/${targetStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const aliasUrl = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const worker = makeWorkerHarness({
    dashboardUrl: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated",
    queueItems: [{
      id: 818,
      target_type: "STORE",
      target_url: targetUrl,
      target_date: "2026-10-18",
      title: "AMZSTEM Pop Toys Store",
      monitored_product_ids: ["1005001234567890"],
    }],
    collectionResult: {
      state: "identity_review",
      error_type: "store_identity_unverified",
      message: "待人工核实",
      page_url: aliasUrl,
      evidence: {
        route_store_id: routeStoreId,
        heading_store_id: null,
        labeled_store_ids: [],
        page_title: "AMZSTEM Pop Toys Store - AliExpress",
        ixspy_store_name: "AMZSTEM Pop Toys Store",
        identity_method: "unverified",
      },
    },
  });
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  await worker.context.syncDedicatedWorkerMode();
  worker.queue[0].status = "RUNNING";
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: aliasUrl, active: true });
  Object.assign(worker.storage, {
    automationWindowId: 8,
    automationWindowOwned: true,
    automationWindowBackground: true,
    automationItem: {
      id: 818, run_id: 9, target_type: "STORE", target_url: targetUrl,
      target_date: "2026-10-18", title: "AMZSTEM Pop Toys Store",
      monitored_product_ids: ["1005001234567890"], monitored_products: [],
    },
    automationTabId: tab.id,
    automationTaskKind: "STORE",
    automationClaimToken: "claim-818",
    browserCollectionWorkerMode: true,
  });

  await worker.context.processAutomationTabInternal(tab.id);
  await worker.context.pollAutomation();
  await worker.flush();

  assert.equal(worker.queue[0].status, "SKIPPED");
  assert.equal(worker.storage.automationTabId, undefined);
  assert.equal(worker.storage.idleAutomationTabId, tab.id);
  assert.equal(worker.collectionMessages.some((message) => message.target_type === "STORE"), false);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/identity-review")), true);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/store-discovery")), false);
});

test("store navigation waits for the pending target instead of rejecting the previously committed store", async () => {
  const worker = makeWorkerHarness({ realTimers: true });
  const previousUrl = "https://www.aliexpress.com/store/1105182474/pages/all-items.html?shop_sortType=orders_desc";
  const targetUrl = "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc";
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: previousUrl, active: true });
  tab.pendingUrl = targetUrl;
  const navigation = worker.context.waitForStorePage(tab.id, targetUrl, 1000);
  setTimeout(() => {
    tab.url = targetUrl;
    delete tab.pendingUrl;
    tab.status = "complete";
  }, 50);

  await assert.doesNotReject(navigation);
});

test("store navigation recognizes an AliExpress challenge instead of timing out", async () => {
  const worker = makeWorkerHarness({ realTimers: true });
  const tab = await worker.chrome.tabs.create({
    windowId: 8,
    url: "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc",
    active: true,
  });
  tab.status = "loading";
  tab.pendingUrl = "https://www.aliexpress.com/punish?return=%2Fstore%2F1105179516%2Fpages%2Fall-items.html";

  await assert.rejects(
    worker.context.waitForStorePage(
      tab.id,
      "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc",
      750,
    ),
    (error) => error.code === "PLATFORM_CHALLENGE",
  );
});

test("store navigation allows slow Orders pages ninety seconds before failing", async () => {
  let now = 0;
  const worker = makeWorkerHarness({ clockNow: () => now });
  const url = "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc";
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: "about:blank", active: true });
  worker.context.setTimeout = (callback, delay) => {
    now += delay;
    callback();
    return 0;
  };

  await assert.rejects(
    worker.context.waitForStorePage(tab.id, url),
    /店铺下一页未在限定时间内加载完成/,
  );

  assert.equal(now, 90 * 1000);
});

test("store discovery does not prefetch another store task", async () => {
  const worker = makeWorkerHarness({
    queueItems: [
      { id: 101, target_type: "STORE", target_key: "store:1", target_url: "https://www.aliexpress.com/store/100001/pages/all-items.html?shop_sortType=orders_desc" },
      { id: 102, target_type: "STORE", target_key: "store:2", target_url: "https://www.aliexpress.com/store/100002/pages/all-items.html?shop_sortType=orders_desc" },
    ],
  });
  worker.storage.automationItem = { id: 101, target_type: "STORE", target_key: "store:1" };
  worker.storage.automationTaskKind = "PRODUCT";
  worker.storage.automationWindowId = 7;

  await worker.context.prefetchNextBrowserItem({ status: "RUNNING", phase: "STORE_DISCOVERY" });

  assert.equal(worker.requests.filter((request) => request.pathname === "/api/browser-collection/items/claim").length, 0);
  assert.equal(worker.queue[1].status, "PENDING");
  assert.equal(worker.storage.prefetchedItem, undefined);
});

test("prefetch is capped at one inactive product page and never samples its hidden IXSPY chart", async () => {
  let finishCollection;
  const collectionGate = new Promise((resolve) => { finishCollection = resolve; });
  const worker = makeWorkerHarness({ collectionResult: collectionGate });
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();

  const activeTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  const startTimer = worker.pendingTimers.find((timer) => timer.delay === 500 && !timer.executed);
  assert.ok(startTimer);
  startTimer.executed = true;
  startTimer.callback();
  await worker.flush();

  const prefetchedTab = await worker.chrome.tabs.get(worker.storage.prefetchedTabId);
  const productTabs = (await worker.chrome.tabs.query({ windowId: 8 }))
    .filter((tab) => tab.url.includes("aliexpress.com/item/"));
  assert.equal(productTabs.length, 2);
  assert.equal(activeTab.active, true, "the IXSPY chart page remains active");
  assert.equal(prefetchedTab.active, false, "the next page loads in the background");
  assert.equal(worker.storage.prefetchedItem.id, 102);
  assert.equal(worker.queue.find((item) => item.id === 103).status, "PENDING",
    "does not claim a third task while both page slots are occupied");
  assert.equal(worker.collectionMessages.length, 1, "only the active page is sampled");

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  assert.equal(worker.requests.filter((request) => request.pathname.endsWith("/items/claim")).length, 2);
  assert.equal(worker.collectionMessages.length, 1, "polling does not read the hidden page chart");

  finishCollection({ state: "ready", payload: { sold_count: 20, captured_at: "2026-09-30T10:00:00+08:00" } });
  await worker.flush();
  assert.ok(worker.updatedTabs.some((update) => update.id === prefetchedTab.id && update.active === true),
    "the prefetched page is activated only after the current result is submitted");
});

test("single-product IXSPY test briefly focuses the worker window and restores prior focus", async () => {
  const worker = makeWorkerHarness({
    queueItems: [{
      id: 358,
      product_id: 358,
      target_key: "product:358:single",
      target_type: "PRODUCT",
      target_url: "https://www.aliexpress.com/item/1005009457413038.html#monitor_product_id=1005009457413038",
      title: "IXSPY chart canary",
      target_date: "2026-09-29",
    }],
  });
  await worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();

  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();
  const itemTab = await worker.chrome.tabs.get(worker.storage.automationTabId);

  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true));
  assert.equal(worker.storage.foregroundSingleTestItemId, 358);
  assert.equal(worker.storage.foregroundSingleTestReturnWindowId, 7);
  assert.equal(itemTab.active, true);

  worker.listeners.updated.callback(itemTab.id, { status: "complete" }, itemTab);
  await worker.flush();
  const focusSettle = worker.pendingTimers.find((timer) => timer.delay === 250);
  assert.ok(focusSettle, "waits briefly after reactivating the product page");
  focusSettle.callback();
  await worker.flush();

  const snapshot = worker.requests.find((request) => request.pathname === "/api/browser-collection/items/358/snapshot");
  assert.ok(snapshot);
  assert.deepEqual(snapshot.body.raw_data.browser_collection_focus, {
    requested: true,
    task_window_focused: true,
    task_tab_active: true,
    focus_request_confirmed: true,
    task_window_id: 8,
    last_focused_window_id: 8,
  });
  assert.equal(worker.focusedWindowId, 7);
  assert.equal(worker.storage.foregroundSingleTestItemId, undefined);
  assert.equal(worker.storage.foregroundSingleTestReturnWindowId, undefined);
});

test("Dianxiaomi does not claim without a worker dashboard context", async () => {
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dianxiaomiQueue: [{
      id: 401,
      target_url: "https://www.aliexpress.com/item/100000000401.html",
      product_id: 401,
      status: "QUEUED",
    }],
  });

  await worker.listeners.message.callback({
    type: "dianxiaomi-handoff-created",
    api_base_url: "http://127.0.0.1:3000",
  }, {
    tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/" },
  }, () => {});
  await worker.flush();

  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/handoffs/claim-batch")), false);
  assert.equal(worker.createdTabs.length, 0);
  assert.equal(worker.windowCreates.length, 0);
});

test("Dianxiaomi uses the stable browser worker UUID for heartbeat, claim, and status", async () => {
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dashboardUrl: "http://192.168.66.115:3000/?browser_collection=1",
    dianxiaomiQueue: [{
      id: 402,
      target_url: "https://www.aliexpress.com/item/100000000402.html",
      product_id: 402,
      status: "QUEUED",
    }],
  });
  await worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    dianxiaomi_browser_worker_mode: true,
    api_base_url: "http://192.168.66.115:3000",
  }, {
    tab: { id: 1, windowId: 7, url: "http://192.168.66.115:3000/?browser_collection=1" },
  }, () => {});
  await worker.flush();

  assert.equal(worker.storage.dianxiaomiWorkerMode, true);
  assert.equal(worker.storage.dianxiaomiDashboardTabId, 1);
  assert.equal(worker.storage.dianxiaomiDashboardWindowId, 7);
  assert.ok(await worker.context.dianxiaomiWorkerDashboardContext());
  const workerId = worker.storage.browserCollectionWorkerId;
  const heartbeat = worker.requests.find((request) => request.pathname.endsWith("/queue/heartbeat"));
  const claim = worker.requests.find((request) => request.pathname.endsWith("/handoffs/claim-batch"));
  const status = worker.requests.find((request) => /\/handoffs\/402\/status$/.test(request.pathname));
  assert.ok(workerId);
  assert.equal(heartbeat.body.worker_id, workerId);
  assert.equal(claim.body.worker_id, workerId);
  assert.equal(status.body.worker_id, workerId);
});

test("Dianxiaomi keeps a product-page mismatch for review and continues to the next queued item", async () => {
  const workerId = "qx-test-worker";
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dashboardUrl: "http://192.168.66.115:3000/?browser_collection=1",
    dianxiaomiQueue: [
      {
        id: 410,
        status: "NEEDS_CONFIRMATION",
        worker_id: workerId,
        error_type: "manual_confirmation_required",
        error_message: "店小秘工作窗口未加载队列商品（目标商品 ID 3256812865076992，当前商品 ID 1005013051391744）",
      },
      {
        id: 411,
        status: "QUEUED",
        target_url: "https://www.aliexpress.com/item/100000000411.html",
        product_id: 411,
      },
    ],
  });
  await worker.chrome.storage.local.set({ browserCollectionWorkerId: workerId });

  await worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    dianxiaomi_browser_worker_mode: true,
    api_base_url: "http://192.168.66.115:3000",
  }, {
    tab: { id: 1, windowId: 7, url: "http://192.168.66.115:3000/?browser_collection=1" },
  }, () => {});
  await worker.flush();

  assert.equal(worker.dianxiaomiQueue[0].status, "NEEDS_CONFIRMATION");
  assert.notEqual(worker.dianxiaomiQueue[1].status, "QUEUED");
  assert.ok(worker.requests.some((request) => request.pathname.endsWith("/handoffs/claim-batch")));
});

test("Dianxiaomi reuses the worker dashboard window for its product tab", async () => {
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dashboardUrl: "http://192.168.66.115:3000/?browser_collection=1",
    dashboardUrl: "http://192.168.66.115:3000/?browser_collection=1",
    dianxiaomiQueue: [{
      id: 403,
      target_url: "https://www.aliexpress.com/item/100000000403.html",
      product_id: 403,
      status: "QUEUED",
    }],
  });
  await worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    dianxiaomi_browser_worker_mode: true,
    api_base_url: "http://192.168.66.115:3000",
  }, {
    tab: { id: 1, windowId: 7, url: "http://192.168.66.115:3000/?browser_collection=1" },
  }, () => {});
  await worker.flush();

  assert.equal(worker.windowCreates.length, 0);
  assert.equal(worker.createdTabs.length, 1);
  assert.equal(worker.createdTabs[0].windowId, 7);
  assert.equal(worker.storage.dianxiaomiDashboardTabId, 1);
  assert.equal(worker.storage.dianxiaomiDashboardWindowId, 7);
});

test("Dianxiaomi waits until a reused tab has loaded the target product before dispatching", async () => {
  const worker = makeWorkerHarness({ dedicatedProfile: false, realTimers: true });
  const oldUrl = "https://www.aliexpress.com/item/100000000499.html";
  const targetUrl = "https://www.aliexpress.com/item/100000000403.html";
  const tab = await worker.chrome.tabs.create({ windowId: 7, url: oldUrl, active: true });

  setTimeout(() => {
    tab.url = targetUrl;
    tab.status = "complete";
  }, 25);

  const ready = await worker.context.waitForDianxiaomiProductTab(tab.id, "100000000403");

  assert.equal(new URL(ready.url).pathname, new URL(targetUrl).pathname);
  assert.equal(ready.status, "complete");
});

test("Dianxiaomi focuses its product tab without restoring a maximized Chrome window", async () => {
  const worker = makeWorkerHarness({ initialDashboardWindowState: "maximized" });
  const tab = await worker.chrome.tabs.create({
    windowId: 7,
    url: "https://www.aliexpress.com/item/100000000404.html",
    active: true,
  });

  await worker.context.focusDianxiaomiProductTab(tab.id);

  assert.equal(worker.windowStateFor(7), "maximized");
  assert.deepEqual(worker.windowUpdates.at(-1), {
    id: 7,
    focused: true,
    collectionMessageCount: 0,
  });
});

test("collection heartbeat renews the active and prefetched task leases", async () => {
  const worker = makeWorkerHarness();
  worker.storage.automationItem = { id: 101 };
  worker.storage.prefetchedItem = { id: 102 };
  worker.storage.automationClaimToken = "claim-active";
  worker.storage.prefetchedClaimToken = "claim-prefetched";

  await worker.context.sendBrowserCollectionHeartbeat();

  const heartbeat = worker.requests.find((request) => request.pathname === "/api/browser-collection/heartbeat");
  assert.deepEqual(JSON.parse(JSON.stringify(heartbeat.body.active_claims)), [
    { item_id: 101, claim_token: "claim-active" },
    { item_id: 102, claim_token: "claim-prefetched" },
  ]);
  assert.ok(heartbeat.body.worker_id);
});

test("worker identity is stable per extension profile and heartbeat advertises its label", async () => {
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dashboardUrl: "http://192.168.66.115:3000/?browser_collection=1",
    queueItems: [{ id: 101, target_type: "STORE", target_url: "https://www.aliexpress.com/store/1" }],
    claimsHeld: true,
  });

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  const firstId = worker.storage.browserCollectionWorkerId;
  const firstHeartbeat = worker.requests.find((request) => request.pathname === "/api/browser-collection/heartbeat");
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  const secondHeartbeat = worker.requests.filter((request) => request.pathname === "/api/browser-collection/heartbeat").at(-1);

  assert.ok(firstId);
  assert.equal(worker.storage.browserCollectionWorkerId, firstId);
  assert.equal(firstHeartbeat.body.worker_id, firstId);
  assert.equal(firstHeartbeat.body.worker_label, "第二台电脑");
  assert.equal(secondHeartbeat.body.worker_id, firstId);

  const localWorker = makeWorkerHarness({
    claimsHeld: true,
    queueItems: [{ id: 101, target_type: "STORE", target_url: "https://www.aliexpress.com/store/1" }],
  });
  localWorker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await localWorker.flush();
  const localHeartbeat = localWorker.requests.find((request) => request.pathname === "/api/browser-collection/heartbeat");
  assert.notEqual(localWorker.storage.browserCollectionWorkerId, firstId);
  assert.equal(localHeartbeat.body.worker_label, "Galore主机");
});

test("custom collection URL advertises its own worker label after extension restart", async () => {
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dashboardUrl: "http://192.168.66.115:3000/?browser_collection=1&browser_collection_name=Qx%E7%9A%84computer",
    claimsHeld: true,
  });

  await worker.context.syncDedicatedWorkerMode();
  await worker.context.sendBrowserCollectionHeartbeat();

  const heartbeat = worker.requests.find((request) => request.pathname === "/api/browser-collection/heartbeat");
  assert.equal(heartbeat.body.worker_label, "Qx的computer");
});

test("dashboard context clears a custom worker label when returning to the legacy URL", async () => {
  const worker = makeWorkerHarness({ dedicatedProfile: false, claimsHeld: true });
  const dashboardContext = async (url) => {
    await worker.chrome.tabs.update(1, { url });
    worker.listeners.message.callback({
      type: "dashboard-context",
      browser_collection_mode: true,
      browser_collection_mode_explicit: true,
    }, { tab: { id: 1, windowId: 7, url } }, () => {});
    await worker.flush();
    await worker.context.sendBrowserCollectionHeartbeat();
    return worker.requests.filter((request) => request.pathname === "/api/browser-collection/heartbeat").at(-1);
  };

  const custom = await dashboardContext(
    "http://192.168.66.115:3000/?browser_collection=1&browser_collection_name=Qx%E7%9A%84computer",
  );
  const legacy = await dashboardContext("http://192.168.66.115:3000/?browser_collection=1");

  assert.equal(custom.body.worker_label, "Qx的computer");
  assert.equal(legacy.body.worker_label, "第二台电脑");
  assert.equal(custom.body.worker_id, legacy.body.worker_id);
});

test("a held browser worker does not create an automation window before a claim", async () => {
  const worker = makeWorkerHarness({
    claimsHeld: true,
    queueItems: [{ id: 101, target_type: "PRODUCT", target_url: "https://www.aliexpress.com/item/100000000101.html" }],
  });

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/heartbeat"));
  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/claim"));
  assert.equal(worker.windowCreates.length, 0);
  assert.equal(worker.createdTabs.length, 0);
});

test("claim sends worker identity and result write sends its claim token", async () => {
  const worker = makeWorkerHarness();
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  const itemTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  worker.listeners.updated.callback(itemTab.id, { status: "complete" }, itemTab);
  await worker.flush();

  const claim = worker.requests.find((request) => request.pathname === "/api/browser-collection/items/claim");
  const snapshot = worker.requests.find((request) => request.pathname === "/api/browser-collection/items/101/snapshot");
  assert.equal(claim.body.worker_id, worker.storage.browserCollectionWorkerId);
  assert.equal(snapshot.body.worker_id, worker.storage.browserCollectionWorkerId);
  assert.equal(snapshot.body.claim_token, "claim-token-101-1");
});

test("a content-script hang is bounded and the item is returned for retry", async () => {
  const neverReturns = new Promise(() => {});
  const worker = makeWorkerHarness({ collectionResult: neverReturns });
  await worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();

  const itemTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  worker.listeners.updated.callback(itemTab.id, { status: "complete" }, itemTab);
  await worker.flush();
  const timeout = worker.pendingTimers.find((timer) => timer.delay === 90 * 1000);
  assert.ok(timeout, "the background worker installs a hard response deadline");
  timeout.callback();
  await worker.flush();

  const failure = worker.requests.find((request) => request.pathname === "/api/browser-collection/items/101/failure");
  assert.equal(failure.body.error_type, "browser_item_stalled");
  assert.match(failure.body.error_message, /90 秒/);
});

test("store-page batch response window exceeds the candidate scan budget", async () => {
  const worker = makeWorkerHarness({
    collectionResult: new Promise(() => {}),
    queueItems: [{
      id: 101,
      target_type: "STORE",
      target_url: "https://www.aliexpress.com/store/990100/pages/all-items.html?shop_sortType=orders_desc",
      target_date: "2026-09-29",
      monitored_product_ids: ["1005000000000001"],
    }],
  });
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  const tab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  worker.listeners.updated.callback(tab.id, { status: "complete" }, tab);
  await worker.flush();

  assert.ok(worker.pendingTimers.some((timer) => timer.delay === 18 * 60 * 1000));
  assert.equal(worker.pendingTimers.some((timer) => timer.delay === 90 * 1000), false);
  assert.equal(worker.collectionMessages[0].target_type, "STORE");
  assert.deepEqual(JSON.parse(JSON.stringify(worker.collectionMessages[0].monitored_product_ids)), ["1005000000000001"]);
  assert.ok(worker.windowUpdates.some((update) => update.state === "maximized" && update.focused === true));
});

test("an alarm discards a hung promise after the backend settles its item and continues", async () => {
  const worker = makeWorkerHarness({ collectionResult: new Promise(() => {}) });
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  const firstTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  worker.listeners.updated.callback(firstTab.id, { status: "complete" }, firstTab);
  await worker.flush();
  worker.queue[0].status = "FAILED";

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.storage.automationItem.id, 102);
  const nextTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  assert.notEqual(nextTab.id, firstTab.id, "the next item uses a fresh tab after the previous one closes");
  assert.ok(nextTab.url.endsWith("100000000102.html"));
});

test("manual start leaves the dashboard window alone and maximizes the focused worker window", async () => {
  const worker = makeWorkerHarness();
  worker.storage.automationWindowId = 99;
  worker.storage.automationWindowOwned = true;
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
  }, { tab: {
    id: 1,
    windowId: 7,
    url: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated",
  } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({
    type: "browser-collection-run-requested",
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: {
    id: 1,
    windowId: 7,
    url: "http://127.0.0.1:8000/",
  } }, () => {});
  await worker.flush();

  assert.equal(worker.storage.automationWindowId, 8);
  assert.equal(worker.storage.automationWindowOwned, true);
  assert.equal(worker.storage.automationWindowBackground, true);
  assert.equal(worker.storage.automationDashboardTabId, 1);
  assert.equal(worker.windowCreates.length, 1);
  assert.equal(worker.storage.automationTabId, 10);
  assert.equal((await worker.chrome.tabs.get(10)).url, "https://www.aliexpress.com/item/100000000101.html",
    "the queue uses the initially blank worker tab");
  assert.equal((await worker.chrome.tabs.get(10)).windowId, 8);
  assert.equal((await worker.chrome.tabs.get(10)).active, true, "product is active inside its own window");
  assert.equal(worker.windowCreates[0].focused, true);
  assert.equal(worker.windowCreates[0].state, "maximized");
  assert.equal(worker.windowUpdates.some((update) => update.id === 7 && update.focused === true), false);
});

test("remote QX collection opens tasks in the monitor window and preserves its dashboard tab", async () => {
  const origin = "http://192.168.66.115:3000";
  const dashboardUrl = `${origin}/?browser_collection=1&browser_collection_name=Qx%E7%9A%84computer`;
  const worker = makeWorkerHarness({ dedicatedProfile: false, dashboardUrl });

  worker.listeners.message.callback({
    type: "dashboard-context",
    api_base_url: origin,
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
  }, { tab: { id: 1, windowId: 7, url: dashboardUrl } }, () => {});
  await worker.flush();
  assert.equal(worker.storage.browserCollectionRemoteProfileEnabled, true, "detects QX remote mode");
  worker.listeners.message.callback({ type: "browser-collection-run-requested", api_base_url: origin }, {
    tab: { id: 1, windowId: 7, url: dashboardUrl },
  }, () => {});
  await worker.flush();

  const taskTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  assert.equal(worker.windowCreates.length, 0, "remote collection does not open another Chrome window");
  assert.equal(worker.createdTabs.length, 1, "creates only the actual task tab, not an idle blank tab");
  assert.equal(worker.createdTabs[0].url, "https://www.aliexpress.com/item/100000000101.html");
  assert.equal(worker.storage.automationWindowId, 7);
  assert.equal(worker.storage.automationWindowOwned, false, "the monitor window is not owned by the collector");
  assert.equal(taskTab.windowId, 7);
  assert.equal(taskTab.active, true, "the task opens as a foreground tab beside the monitor");
  assert.equal((await worker.chrome.tabs.get(1)).url, dashboardUrl, "the monitor tab is not navigated or closed");
});

test("held remote QX worker creates no idle tab before collection starts", async () => {
  const origin = "http://192.168.66.115:3000";
  const dashboardUrl = `${origin}/?browser_collection=1&browser_collection_name=Qx%E7%9A%84computer`;
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dashboardUrl,
    claimsHeld: true,
  });

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.windowCreates.length, 0);
  assert.equal(worker.createdTabs.length, 0, "a held queue leaves the current monitor window untouched");
  assert.equal((await worker.chrome.tabs.get(1)).url, dashboardUrl);
});

test("remote QX collection keeps an existing task in the monitor window after extension reload", async () => {
  let finishCollection;
  const collectionGate = new Promise((resolve) => { finishCollection = resolve; });
  const origin = "http://192.168.66.115:3000";
  const dashboardUrl = `${origin}/?browser_collection=1&browser_collection_name=Qx%E7%9A%84computer`;
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dashboardUrl,
    collectionResult: collectionGate,
  });
  const task = worker.queue[0];
  task.status = "RUNNING";
  const taskTab = await worker.chrome.tabs.create({
    windowId: 8,
    url: task.target_url,
    active: true,
  });
  Object.assign(worker.storage, {
    browserCollectionWorkerMode: true,
    browserCollectionRemoteProfileEnabled: true,
    automationDashboardTabId: 1,
    automationWindowId: 8,
    automationWindowOwned: true,
    automationWindowBackground: true,
    automationTabId: taskTab.id,
    automationItem: task,
    automationClaimToken: "remote-reload-claim",
    automationTaskKind: "PRODUCT",
  });

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.windowCreates.length, 0, "reload recovery does not create a separate collector window");
  assert.equal((await worker.chrome.tabs.get(taskTab.id)).windowId, 7,
    "the in-flight task is moved into the existing monitor window");
  assert.equal((await worker.chrome.tabs.get(1)).url, dashboardUrl, "the dashboard remains intact");
  assert.equal(worker.storage.automationWindowOwned, false);
  assert.ok(worker.collectionMessages.length > 0, "collection resumes in the migrated tab");
  finishCollection({ state: "partial", payload: {} });
  await worker.flush();
});

test("remote QX collection corrects stale window ownership when its task is already beside the dashboard", async () => {
  let finishCollection;
  const collectionGate = new Promise((resolve) => { finishCollection = resolve; });
  const origin = "http://192.168.66.115:3000";
  const dashboardUrl = `${origin}/?browser_collection=1&browser_collection_name=Qx%E7%9A%84computer`;
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dashboardUrl,
    collectionResult: collectionGate,
  });
  const task = worker.queue[0];
  task.status = "RUNNING";
  const taskTab = await worker.chrome.tabs.create({ windowId: 7, url: task.target_url, active: true });
  Object.assign(worker.storage, {
    browserCollectionWorkerMode: true,
    browserCollectionRemoteProfileEnabled: true,
    automationDashboardTabId: 1,
    automationWindowId: 7,
    automationWindowOwned: true,
    automationWindowBackground: true,
    automationTabId: taskTab.id,
    automationItem: task,
    automationClaimToken: "same-window-reload-claim",
    automationTaskKind: "PRODUCT",
  });

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.windowCreates.length, 0);
  assert.equal(worker.storage.automationWindowOwned, false, "the shared monitor window is never treated as disposable");
  assert.equal((await worker.chrome.tabs.get(taskTab.id)).windowId, 7);
  assert.equal((await worker.chrome.tabs.get(1)).url, dashboardUrl);
  finishCollection({ state: "partial", payload: {} });
  await worker.flush();
});

test("a stale worker-window id that points at the dashboard is replaced with a separate task window", async () => {
  const worker = makeWorkerHarness({
    queueItems: [{ id: 101, target_type: "STORE", target_url: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc", target_date: "2026-09-29" }],
  });
  Object.assign(worker.storage, {
    browserCollectionIsolatedProfileAuthorized: true,
    automationDashboardTabId: 1,
    automationWindowId: 7,
    automationWindowOwned: true,
    automationWindowBackground: true,
    automationTabGroupId: 77,
    automationTabGroupWindowId: 7,
  });

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.windowCreates.length, 1, "does not reuse the dashboard's Chrome window");
  assert.equal(worker.storage.automationWindowId, 8);
  assert.equal((await worker.chrome.tabs.get(worker.storage.automationTabId)).windowId, 8);
  assert.equal(worker.storage.automationTabGroupWindowId, 8, "creates the task group in the dedicated worker window");
  assert.ok(await worker.chrome.tabs.get(1), "keeps the dashboard open");
});

test("an in-flight task tab left in the dashboard window is migrated before collection resumes", async () => {
  let finishCollection;
  const collectionGate = new Promise((resolve) => { finishCollection = resolve; });
  const worker = makeWorkerHarness({ collectionResult: collectionGate });
  const task = worker.queue[0];
  task.target_type = "PRODUCT";
  task.target_url = "https://www.aliexpress.com/item/100000000101.html";
  task.status = "RUNNING";
  const taskTab = await worker.chrome.tabs.create({
    windowId: 7,
    url: task.target_url,
    active: true,
  });
  const prefetchedItem = worker.queue[1];
  prefetchedItem.status = "RUNNING";
  const prefetchedTab = await worker.chrome.tabs.create({
    windowId: 7,
    url: prefetchedItem.target_url,
    active: false,
  });
  Object.assign(worker.storage, {
    automationDashboardTabId: 1,
    automationWindowId: 7,
    automationWindowOwned: true,
    automationWindowBackground: true,
    automationTabId: taskTab.id,
    automationItem: task,
    automationClaimToken: "in-flight-claim",
    automationTaskKind: "PRODUCT",
    prefetchedTabId: prefetchedTab.id,
    prefetchedItem,
    prefetchedClaimToken: "prefetched-claim",
    prefetchedTaskKind: "PRODUCT",
  });

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  const migratedTaskTab = await worker.chrome.tabs.get(taskTab.id);
  assert.ok(migratedTaskTab, "keeps the in-flight product tab open");
  assert.equal(migratedTaskTab.windowId, 8,
    "moves the already-open product page out of the dashboard window");
  assert.equal((await worker.chrome.tabs.get(prefetchedTab.id)).windowId, 8,
    "moves the prefetched page into the same collector window");
  assert.equal((await worker.chrome.tabs.get(1)).windowId, 7, "leaves the dashboard in its original window");
  assert.equal(worker.storage.automationTabId, taskTab.id, "preserves the current claim and task tab");
  assert.equal(worker.storage.prefetchedTabId, prefetchedTab.id, "preserves the prefetched claim and tab");
  assert.equal(worker.storage.automationTabGroupWindowId, 8, "recreates task grouping in the collector window");
  assert.ok(worker.collectionMessages.length > 0, "resumes collection in the migrated page");
  finishCollection({ state: "partial", payload: {} });
  await worker.flush();
});

test("NAS LAN collector profile keeps the Galore host identity", async () => {
  const origin = "http://192.168.66.115:3000";
  const dashboardUrl = `${origin}/?browser_collection=1&browser_collection_profile=isolated`;
  const worker = makeWorkerHarness({
    dashboardUrl,
    queueItems: [{ id: 101, target_type: "STORE", target_url: "https://www.aliexpress.com/store/1" }],
    claimsHeld: true,
  });

  worker.listeners.message.callback({
    type: "dashboard-context",
    api_base_url: origin,
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
  }, { tab: { id: 1, windowId: 7, url: dashboardUrl } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested", api_base_url: origin }, {
    tab: { id: 1, windowId: 7, url: dashboardUrl },
  }, () => {});
  await worker.flush();

  const heartbeat = worker.requests.find((request) => request.pathname === "/api/browser-collection/heartbeat");
  assert.equal(heartbeat?.origin, origin);
  assert.equal(heartbeat?.body.worker_label, "Galore主机");
});

test("does not collect an in-flight task from the dashboard window if migration fails", async () => {
  const worker = makeWorkerHarness();
  const task = worker.queue[0];
  task.status = "RUNNING";
  const taskTab = await worker.chrome.tabs.create({
    windowId: 7,
    url: task.target_url,
    active: true,
  });
  Object.assign(worker.storage, {
    automationDashboardTabId: 1,
    automationWindowId: 7,
    automationWindowOwned: true,
    automationWindowBackground: true,
    automationTabId: taskTab.id,
    automationItem: task,
    automationClaimToken: "in-flight-claim",
    automationTaskKind: "PRODUCT",
  });
  worker.chrome.tabs.move = async () => { throw new Error("simulated Chrome tab move failure"); };

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal((await worker.chrome.tabs.get(taskTab.id)).windowId, 7);
  assert.deepEqual(worker.collectionMessages, [], "never reads IXSPY in the dashboard window as a fallback");
  assert.equal(worker.storage.automationItem.id, task.id, "keeps the claimed task for a later migration retry");
});

test("selected collector uses a maximized foreground window without replacing the dashboard", async () => {
  const worker = makeWorkerHarness({
    dashboardUrl: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated",
  });
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();

  assert.equal(worker.windowCreates.length, 1);
  assert.equal(worker.windowCreates[0].focused, true);
  assert.equal(worker.windowCreates[0].state, "maximized");
  assert.equal(worker.windowUpdates.some((update) => update.id === 7), false);
  assert.equal((await worker.chrome.tabs.get(worker.storage.automationTabId)).active, true);
});

test("keeps the selected collector window in front while collection is running", async () => {
  let finishCollection;
  const collectionGate = new Promise((resolve) => { finishCollection = resolve; });
  const worker = makeWorkerHarness({
    collectionResult: collectionGate,
    windowCreationStealsFocus: true,
    activeTabCreationStealsFocus: true,
    activeTabActivationStealsFocus: true,
  });
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();
  worker.listeners.focusChanged.callback(8);
  await worker.flush();
  assert.equal(worker.focusedWindowId, 8, "the idle collector machine keeps the task window in front");
  assert.equal(worker.windowCreates[0].state, "maximized");
  assert.ok(!worker.windowUpdates.some((update) => update.id === 8 && update.focused === false));

  const startTimer = worker.pendingTimers.find((timer) => timer.delay === 500 && !timer.executed);
  startTimer.executed = true;
  startTimer.callback();
  await worker.flush();
  finishCollection({ state: "ready", payload: { sold_count: 20, captured_at: "2026-09-30T10:00:00+08:00" } });
  await worker.flush();
  const finalWorkerUpdate = worker.windowUpdates.filter((update) => update.id === 8).at(-1);
  assert.equal(finalWorkerUpdate.state, "maximized", "the worker remains visible after collection finishes");
  assert.equal(finalWorkerUpdate.focused, true);
});

test("verification restores the maximized worker window and shows the challenge tab", async () => {
  const worker = makeWorkerHarness({
    collectionResult: { state: "challenge", message: "请完成人工验证" },
  });
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?dianxiaomi_worker=1&browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();

  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();
  const challengeTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  worker.listeners.updated.callback(challengeTab.id, { status: "complete" }, challengeTab);
  await worker.flush();

  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true && update.state === "maximized"));
  assert.ok(worker.updatedTabs.some((update) => update.id === challengeTab.id && update.active === true));
  assert.equal(worker.removedTabs.includes(challengeTab.id), false,
    "keeps the challenge page open for human verification");
  assert.deepEqual(worker.removedTabs, [11], "removes the prefetched page while keeping the verification page open");
  assert.equal(worker.storage.idleAutomationTabId, undefined);
  assert.equal(worker.queue.find((item) => item.id === 102).status, "PENDING",
    "returns the prefetched task to pending when verification interrupts the pipeline");
  const restore = worker.windowUpdates.find((update) => update.id === 8 && update.focused === true);
  assert.equal("width" in restore, false);
  assert.equal("height" in restore, false);
  assert.equal("left" in restore, false);
  assert.equal("top" in restore, false);
});

test("store verification resumes with identity probe instead of legacy first-20 collection", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({
    queueItems: [{ id: 101, target_type: "STORE", target_url: url, target_date: "2026-09-29",
      monitored_product_ids: ["1005000000000001"] }],
    collectionResults: [
      { state: "challenge", message: "verification" },
      { state: "ready", platform_store_id: "123456" },
      { state: "ready", page_result: { platform_store_id: "123456", url,
        products: [{ platform_product_id: "1005000000000001", historical_sales: [] }],
        has_next_page: false, next_page_url: null } },
    ],
  });
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  const tab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  worker.listeners.updated.callback(tab.id, { status: "complete" }, tab);
  await worker.flush();

  const identityProbeCountBeforeBlank = worker.collectionMessages
    .filter((message) => message.target_type === "STORE_IDENTITY").length;
  tab.url = "about:blank";
  tab.status = "complete";
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  assert.equal(worker.updatedTabs.some((update) => update.id === tab.id && update.url === url), true,
    "restores a blank tab before sending the store identity probe");
  assert.equal(worker.collectionMessages.filter((message) => message.target_type === "STORE_IDENTITY").length,
    identityProbeCountBeforeBlank,
    "does not probe a blank page after verification");

  worker.listeners.updated.callback(tab.id, { status: "complete", url }, tab);
  await worker.flush();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.deepEqual(worker.collectionMessages.map((message) => message.target_type),
    ["STORE", "STORE_IDENTITY", "STORE"]);
  assert.equal(worker.collectionMessages.filter((message) => message.target_type === "STORE"
    && !message.monitored_product_ids).length, 0);
  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/store-discovery"),
    JSON.stringify({ requests: worker.requests.map(({ pathname, method, body }) => ({ pathname, method, body })),
      collectionMessages: worker.collectionMessages, storage: worker.storage }));
  assert.ok(worker.windowUpdates.some((update) => update.id === 8
    && update.focused === true && update.state === "maximized"),
  "store collection and its verification page use the maximized worker window");
});

test("stored Store ID proof resumes collection on the route alias without canonical navigation", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const aliasUrl = "https://www.aliexpress.com/store/123457/pages/all-items.html?shop_sortType=orders_desc";
  const targetIds = Array.from({ length: 20 }, (_, index) => `10050000000000${index + 1}`).sort();
  const identity = {
    target_store_id: "123456",
    route_store_id: "123457",
    heading_store_id: "123456",
    page_url: aliasUrl,
  };
  const worker = makeWorkerHarness({ collectionResult: {
    state: "ready",
    page_result: {
      platform_store_id: "123456",
      url: aliasUrl,
      store_identity: identity,
      products: targetIds.map((platform_product_id) => ({ platform_product_id, historical_sales: [] })),
      has_next_page: false,
      next_page_url: null,
    },
  } });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: aliasUrl, active: true });
  const item = {
    id: 101, run_id: 9, target_type: "STORE", target_url: url,
    target_date: "2026-09-29", monitored_product_ids: targetIds, monitored_products: [],
  };
  worker.storage["browserCollectionStoreProgress:101"] = {
    run_id: 9, item_id: 101, store_id: "123456", target_date: "2026-09-29",
    target_ids: targetIds, products: {}, visited_page_urls: [], page_scans: [], chart_reads: [],
    route_store_id: "123457", store_identity: identity,
  };

  const collecting = worker.context.collectStorePages(tab.id, item);
  await worker.flush();

  assert.ok(worker.collectionMessages.some((message) => message.target_type === "STORE"
    && message.verified_store_identity?.heading_store_id === "123456"));
  const result = await collecting;
  assert.equal(result.state, "ready");
  assert.equal(result.payload.platform_store_id, "123456");
  assert.equal(worker.updatedTabs.some((update) => update.id === tab.id && update.url === url), false);
});

test("store verification resumes in the same tab without minimizing or blanking it", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({
    queueItems: [{ id: 101, target_type: "STORE", target_url: url, target_date: "2026-09-29",
      monitored_product_ids: ["1005000000000001"] }],
    collectionResults: [
      { state: "challenge", message: "verification" },
      { state: "ready", platform_store_id: "123456" },
      { state: "ready", page_result: { platform_store_id: "123456", url,
        products: [{ platform_product_id: "1005000000000001", historical_sales: [] }],
        has_next_page: false, next_page_url: null } },
    ],
  });
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  const tab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  worker.listeners.updated.callback(tab.id, { status: "complete" }, tab);
  await worker.flush();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/challenge"));
  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/store-discovery"));
  assert.equal((await worker.chrome.tabs.get(tab.id)).url, url, "keeps the verified store page instead of replacing it with about:blank");
  assert.equal(worker.storage.idleAutomationTabId, tab.id);
  const finalWindowUpdate = worker.windowUpdates.filter((update) => update.id === 8).at(-1);
  assert.equal(finalWindowUpdate.focused, true);
  assert.equal(finalWindowUpdate.state, "maximized");
});

test("resuming a verified queue preserves its current store tab instead of reopening the store", async () => {
  const url = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const worker = makeWorkerHarness({
    queueItems: [{ id: 101, target_type: "STORE", target_url: url, target_date: "2026-09-29",
      monitored_product_ids: ["1005000000000001"] }],
    collectionResults: [
      { state: "challenge", message: "verification" },
      { state: "challenge", message: "verification still active" },
    ],
  });
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();
  const tabId = worker.storage.automationTabId;
  const tab = await worker.chrome.tabs.get(tabId);
  worker.listeners.updated.callback(tab.id, { status: "complete" }, tab);
  await worker.flush();
  assert.equal(worker.storage.challengePaused, true);
  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/challenge"));

  worker.setCollectionResult({ state: "ready", platform_store_id: "123456", page_result: {
    platform_store_id: "123456",
    url,
    products: [{ platform_product_id: "1005000000000001", historical_sales: [] }],
    has_next_page: false,
    next_page_url: null,
  } });
  const storeNavigationsBeforeResume = worker.updatedTabs
    .filter((update) => update.id === tabId && update.url === url).length;
  const createdTabsBeforeResume = worker.createdTabs.length;
  worker.resumeChallenge();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.storage.automationTabId || worker.storage.idleAutomationTabId, tabId);
  assert.equal(worker.removedTabs.includes(tabId), false);
  assert.equal(worker.updatedTabs.filter((update) => update.id === tabId && update.url === url).length,
    storeNavigationsBeforeResume,
    "does not navigate back to the store URL after the same tab passed verification");
  assert.equal(worker.createdTabs.length, createdTabsBeforeResume,
    "does not open a second tab for the same store task");
  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/store-discovery"));
});

test("IXSPY login expiry foregrounds the visible login page, pauses safely, and resumes the same item", async () => {
  const worker = makeWorkerHarness({
    collectionResult: { state: "ixspy_login", message: "IXSPY 登录已过期" },
  });
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();
  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();
  const loginTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  worker.listeners.updated.callback(loginTab.id, { status: "complete" }, loginTab);
  await worker.flush();
  const prefetchedTabId = worker.createdTabs.find((candidate) => candidate.id !== loginTab.id)?.id;

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/challenge"
    && /IXSPY 登录已过期/.test(request.body.error_message)));
  assert.equal(worker.storage.challengeProvider, "IXSPY");
  assert.equal(worker.storage.automationTabId, loginTab.id);
  assert.equal(worker.windowCreates.length, 1, "keeps the task page in the worker window");
  assert.equal(worker.queue.find((item) => item.id === 102).status, "PENDING",
    "returns the prefetched task to the queue while login is pending");
  assert.ok(worker.removedTabs.includes(prefetchedTabId), "closes the unused prefetched page without touching the login page");
  assert.equal(worker.storage.idleAutomationTabId, undefined);
  assert.equal(worker.removedTabs.includes(loginTab.id), false, "keeps the page open while login is pending");
  assert.equal(worker.notifications.some((notice) => notice.title === "IXSPY 需要登录" && notice.requireInteraction), true);
  assert.ok(worker.updatedTabs.some((update) => update.id === loginTab.id && update.active === true));
  assert.ok(worker.windowUpdates.some((update) => update.id === 8
    && update.focused === true && update.state === "maximized"),
  "IXSPY login verification is presented in the maximized worker window");
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/snapshot")), false);

  worker.setCollectionResult({ state: "ready", payload: { sold_count: 22, captured_at: "2026-09-28T09:00:00+08:00" } });
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/snapshot"));
  assert.equal(worker.removedTabs.includes(loginTab.id), false, "retains the successful page instead of replacing it with a blank tab");
  assert.equal(Boolean(worker.storage.challengeProvider), false);
});

test("a redirect to the AliExpress punish route pauses the task and foregrounds that page", async () => {
  const worker = makeWorkerHarness();
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?dianxiaomi_worker=1&browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();

  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();
  const challengeTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  challengeTab.url = "https://www.aliexpress.com/punish?return=https%3A%2F%2Fwww.aliexpress.com%2Fitem%2F100000000101.html";
  worker.listeners.updated.callback(challengeTab.id, { status: "complete", url: challengeTab.url }, challengeTab);
  await worker.flush();

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/challenge"));
  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true && update.state === "maximized"));
  assert.ok(worker.updatedTabs.some((update) => update.id === challengeTab.id && update.active === true));
});

test("polling recovers a challenge page that was already open during extension reload", async () => {
  const worker = makeWorkerHarness();
  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: true,
    browser_collection_mode_explicit: true,
    browser_collection_local_mode: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/?dianxiaomi_worker=1&browser_collection=1&browser_collection_profile=isolated" } }, () => {});
  await worker.flush();

  worker.listeners.message.callback({ type: "browser-collection-run-requested" }, { tab: { id: 1, windowId: 7 } }, () => {});
  await worker.flush();
  const challengeTab = await worker.chrome.tabs.get(worker.storage.automationTabId);
  challengeTab.url = "https://www.aliexpress.com/punish";
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/challenge"));
  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true && update.state === "maximized"));
  assert.ok(worker.updatedTabs.some((update) => update.id === challengeTab.id && update.active === true));
});

test("alarm wake restores persisted worker mode after the MV3 service worker restarts", async () => {
  const worker = makeWorkerHarness({
    dashboardUrl: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated",
  });

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/heartbeat"));
  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/claim"));
  assert.equal(worker.windowCreates.length, 1, "the alarm wake resumes in the dedicated worker window");
});

test("alarm wake discovers a LAN dashboard and polls its same-origin API", async () => {
  const origin = "http://192.168.66.115:3000";
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    dashboardUrl: `${origin}/?browser_collection=1`,
  });
  worker.storage.activeApiBase = origin;

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/heartbeat"
    && request.origin === origin));
  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/claim"
    && request.origin === origin));
});

test("authorized worker collection uses a maximized foreground window without resizing the dashboard", async () => {
  const worker = makeWorkerHarness({
    dashboardUrl: "http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated",
  });
  await worker.flush();
  worker.storage.automationWindowId = 88;
  worker.storage.automationWindowOwned = true;
  worker.storage.automationDashboardTabId = 1;

  worker.chrome.runtime.onStartup.callback();
  await worker.flush();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.windowCreates.length, 1);
  assert.equal(worker.windowCreates[0].focused, true);
  assert.equal(worker.windowCreates[0].state, "maximized");
  assert.equal((await worker.chrome.tabs.get(worker.storage.automationTabId)).windowId, 8);
  assert.equal((await worker.chrome.tabs.get(worker.storage.automationTabId)).active, true);
  assert.equal(worker.windowUpdates.some((update) => update.id === 7), false, "the user's window remains untouched");
});

test("startup does not close the worker window before its dashboard URL is observed", async () => {
  const worker = makeWorkerHarness();
  worker.storage.automationWindowId = 7;
  worker.storage.automationWindowOwned = true;
  worker.storage.browserCollectionLocalProfileEnabled = false;
  const dashboard = await worker.chrome.tabs.get(1);
  dashboard.url = "about:blank";
  dashboard.status = "loading";

  worker.chrome.runtime.onStartup.callback();
  await worker.flush();

  assert.deepEqual(worker.windowRemoves, []);
});

test("disabling worker mode clears collector tabs without closing its window", async () => {
  const worker = makeWorkerHarness();
  worker.storage.automationWindowId = 7;
  worker.storage.automationWindowOwned = true;
  worker.storage.automationDashboardTabId = 1;
  worker.storage.automationTabId = 11;
  worker.storage.idleAutomationTabId = 12;
  worker.chrome.tabs.create({ windowId: 7, url: "about:blank", active: false });
  worker.chrome.tabs.create({ windowId: 7, url: "https://www.aliexpress.com/item/100000000111.html", active: false });
  worker.chrome.tabs.create({ windowId: 7, url: "https://www.aliexpress.com/item/100000000112.html", active: false });

  worker.listeners.message.callback({
    type: "dashboard-context",
    browser_collection_mode: false,
    browser_collection_mode_explicit: true,
    api_base_url: "http://127.0.0.1:8000",
  }, { tab: { id: 1, windowId: 7, url: "http://127.0.0.1:3000/" } }, () => {});
  await worker.flush();

  assert.deepEqual(worker.windowRemoves, []);
  assert.ok(await worker.chrome.tabs.get(1), "dashboard tab remains open");
  assert.equal(await worker.chrome.tabs.get(11), null);
  assert.equal(await worker.chrome.tabs.get(12), null);
});
