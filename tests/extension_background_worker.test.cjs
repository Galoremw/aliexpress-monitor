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
  dianxiaomiQueue = [],
  dashboardUrl = null,
  legacyDianxiaomiHeartbeat = false,
  expiredToken = null,
  refreshedToken = null,
  queueItems = null,
  windowCreationStealsFocus = false,
  activeTabCreationStealsFocus = false,
  activeTabActivationStealsFocus = false,
  claimsHeld = false,
  heartbeatConflict = false,
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
  const createdTabs = [];
  const updatedTabs = [];
  const removedTabs = [];
  const windowUpdates = [];
  const windowCreates = [];
  const windowRemoves = [];
  const requests = [];
  const collectionMessages = [];
  const notifications = [];
  const pendingTimers = [];
  let nextTabId = 10;
  let nextItem = 0;
  let groupId = 1;
  let challengePaused = false;
  let focusedWindowId = 7;
  const windowStates = new Map([[7, "normal"]]);
  let currentCollectionResult = collectionResult;
  let currentHeartbeatConflict = heartbeatConflict;
  const queue = (queueItems ?? [
    { id: 101, target_type: "PRODUCT", target_url: "https://www.aliexpress.com/item/100000000101.html", title: "Product 1", target_date: "2026-09-29" },
    { id: 102, target_type: "PRODUCT", target_url: "https://www.aliexpress.com/item/100000000102.html", title: "Product 2" },
    { id: 103, target_type: "PRODUCT", target_url: "https://www.aliexpress.com/item/100000000103.html", title: "Product 3" },
  ]).map((item, index) => ({ ...item, run_id: 9, position: index + 1, attempt_count: 1, status: "PENDING" }));
  const dianxiaomiItems = dianxiaomiQueue.map((item) => ({ ...item, status: "QUEUED" }));

  function runPayload() {
    const items = queue.map((item) => ({ ...item }));
    const current = items.find((item) => item.status === "RUNNING") || null;
    return {
      id: 9,
      status: challengePaused ? "NEEDS_VERIFICATION" : "RUNNING",
      phase: "PRODUCT_COLLECTION",
      paused_reason: null,
      current_item: current,
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
          if (typeof keys === "string") return { [keys]: storage[keys] };
          if (Array.isArray(keys)) return Object.fromEntries(keys.filter((key) => key in storage).map((key) => [key, storage[key]]));
          return { ...storage };
        },
        async set(values) { Object.assign(storage, values); },
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
      async remove(id) { removedTabs.push(id); tabs.delete(id); },
      async group() { return groupId; },
      async sendMessage(_tabId, message) {
        if (message.type === "collect-visible-page") collectionMessages.push({ ...message });
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
        if (windowCreationStealsFocus) focusedWindowId = 8;
        return { id: 8, type: "normal", state: options.state || "normal", focused: focusedWindowId === 8 };
      },
      async update(id, changes) {
        windowUpdates.push({ id, ...changes });
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
      executeScript: async (details) => [{
        result: details.args?.length ? (refreshedToken ? { access_token: refreshedToken } : null) : null,
      }],
    },
    webNavigation: { onCommitted: listeners.committed, onHistoryStateUpdated: listeners.history },
  };

  const context = {
    chrome,
    crypto: { randomUUID: () => `test-worker-${++workerIdSequence}` },
    URL,
    Map,
    Date,
    Promise,
    setTimeout(callback, delay) { pendingTimers.push({ callback, delay }); return pendingTimers.length; },
    setInterval() { return 0; },
    clearTimeout() {},
    importScripts() {},
    async fetch(url, options) {
      const parsedUrl = new URL(url);
      const pathname = parsedUrl.pathname;
      const token = options.headers?.Authorization?.replace(/^Bearer\s+/i, "") || null;
      requests.push({ origin: parsedUrl.origin, pathname, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null, token });
      if (expiredToken && token !== refreshedToken) {
        return { ok: false, status: 401, json: async () => ({ detail: "Unauthorized" }) };
      }
      let body = {};
      if (pathname === "/api/integrations/dianxiaomi/queue/heartbeat" && legacyDianxiaomiHeartbeat) {
        return { ok: false, status: 404, json: async () => ({ detail: "Not Found" }) };
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
    requests, notifications, collectionMessages, pendingTimers, flush,
    get focusedWindowId() { return focusedWindowId; },
    setCollectionResult(value) { currentCollectionResult = value; },
    setHeartbeatConflict(value) { currentHeartbeatConflict = value; },
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

test("isolated local worker marker survives an MV3 service-worker restart", async () => {
  const worker = makeWorkerHarness({ dedicatedProfile: false });
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  worker.chrome.runtime.onStartup.callback();
  await worker.flush();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.storage.browserCollectionWorkerMode, true);
  assert.ok(worker.requests.some((request) => request.pathname.endsWith("/items/claim")));
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

test("stale heartbeat discards local claims and closes only the task tab", async () => {
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
  assert.equal((await worker.chrome.tabs.get(taskTab.id)).url, "about:blank");
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/claim")), false);
});

test("LAN collector dashboard remains a queue worker", async () => {
  const dashboardUrl = "http://192.168.66.107:3000/?browser_collection=1";
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
      has_next_page: true, next_page_url: secondUrl } },
    { state: "ready", page_result: { platform_store_id: "123456", url: secondUrl,
      products: [{ platform_product_id: "1005000000000002", historical_sales: [] }],
      has_next_page: false, next_page_url: null } },
  ] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: firstUrl, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    target_url: firstUrl, target_date: "2026-09-29",
    monitored_product_ids: ["1005000000000001", "1005000000000002"],
  });

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.products.map((product) => product.platform_product_id))),
    ["1005000000000001", "1005000000000002"]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [firstUrl, secondUrl]);
  assert.equal(worker.updatedTabs.some((update) => /\/item\//.test(update.url || "")), false);
  assert.equal(worker.collectionMessages.length, 2);
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
  const worker = makeWorkerHarness({ collectionResults: [{ state: "challenge", message: "verification" }] });
  const tab = await worker.chrome.tabs.create({ windowId: 8, url: firstUrl, active: true });
  const result = await worker.context.collectStorePages(tab.id, {
    target_url: firstUrl, target_date: "2026-09-29", monitored_product_ids: ["1005000000000001"],
  });
  assert.equal(result.state, "challenge");
  assert.equal(result.payload, undefined);
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
  const timeout = worker.pendingTimers.find((timer) => timer.delay === 4 * 60 * 1000);
  assert.ok(timeout, "each page must finish within four minutes");
  timeout.callback();
  const result = await collecting;
  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.products)), []);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [url]);
  assert.equal(result.payload.raw_data.collection_interrupted, true);
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
  const pageTimeout = worker.pendingTimers.find((timer) => timer.delay === 4 * 60 * 1000);
  assert.ok(pageTimeout);
  pageTimeout.callback();
  await processing;

  assert.deepEqual(JSON.parse(JSON.stringify(progressAck)), { ok: true });
  const resultRequest = worker.requests.find((request) => request.pathname.endsWith("/items/401/store-discovery"));
  assert.ok(resultRequest, "the completed product must be submitted with a partial store result");
  assert.deepEqual(resultRequest.body.products.map((entry) => entry.platform_product_id), [product.platform_product_id]);
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/items/401/failure")), false);
  assert.equal(worker.storage["browserCollectionStoreProgress:401"], undefined);
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
  const timeouts = worker.pendingTimers.filter((timer) => timer.delay === 4 * 60 * 1000);
  assert.equal(timeouts.length, 2);
  timeouts[1].callback();
  const result = await collecting;
  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.visited_page_urls)), [firstUrl, secondUrl]);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.products.map((product) => product.platform_product_id))),
    ["1005000000000001"]);
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

test("scheduled items reuse the blank page in a visible unfocused worker window and close completed tabs", async () => {
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
  assert.equal(worker.windowCreates[0].focused, false);
  assert.equal(worker.windowCreates[0].state, "normal");
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
  assert.deepEqual(worker.removedTabs, [11, 12], "prefetched product tabs are closed after use");
  assert.equal(worker.storage.idleAutomationTabId, 10, "the original worker tab is restored to a blank idle page");
  assert.deepEqual(
    (await worker.chrome.tabs.query({ windowId: 8 })).map((tab) => tab.url),
    ["about:blank"],
  );
  assert.deepEqual(worker.windowRemoves, [], "collection does not close the worker window");
  assert.equal(worker.windowUpdates.some((update) => update.focused === true), false);
  assert.ok(worker.windowUpdates.every((update) => update.id === 8 && update.state === "normal"));
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

test("a local dashboard handoff wakes Dianxiaomi collection in the same Chrome window", async () => {
  const worker = makeWorkerHarness({
    dedicatedProfile: false,
    legacyDianxiaomiHeartbeat: true,
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

  assert.ok(worker.requests.some((request) => request.pathname.endsWith("/queue/heartbeat")));
  assert.ok(worker.requests.some((request) => request.pathname.endsWith("/handoffs/claim-batch")));
  assert.equal(worker.createdTabs.length, 1);
  assert.equal(worker.createdTabs[0].windowId, 7);
  assert.equal(worker.createdTabs[0].active, false);
  assert.equal(worker.storage.dianxiaomiWorkerMode, true);
  assert.equal(worker.dianxiaomiQueue[0]?.status, "SUCCEEDED");
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
    dashboardUrl: "http://192.168.66.107:3000/?browser_collection=1",
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

test("store-page batch gets a longer bounded response window", async () => {
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

  assert.ok(worker.pendingTimers.some((timer) => timer.delay === 10 * 60 * 1000));
  assert.equal(worker.pendingTimers.some((timer) => timer.delay === 90 * 1000), false);
  assert.equal(worker.collectionMessages[0].target_type, "STORE");
  assert.deepEqual(JSON.parse(JSON.stringify(worker.collectionMessages[0].monitored_product_ids)), ["1005000000000001"]);
  assert.ok(worker.windowUpdates.some((update) => update.state === "normal" && update.focused === false));
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

test("manual start leaves the dashboard window alone and creates a background worker window", async () => {
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
  assert.equal(worker.windowUpdates.some((update) => update.focused === true), false);
});

test("isolated collection stays visible without switching the user's active Chrome window", async () => {
  const worker = makeWorkerHarness({ dedicatedProfile: false });
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
  assert.equal(worker.windowCreates[0].focused, false);
  assert.equal(worker.windowCreates[0].state, "normal");
  assert.equal(worker.windowUpdates.some((update) => update.id === 7), false);
  assert.equal((await worker.chrome.tabs.get(worker.storage.automationTabId)).active, true);
});

test("keeps the isolated worker visible but unfocused if Chrome delays surfacing it", async () => {
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
  assert.equal(worker.focusedWindowId, 7, "the worker yields focus to the previously foreground app");
  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.state === "normal" && update.focused === false));

  const startTimer = worker.pendingTimers.find((timer) => timer.delay === 500 && !timer.executed);
  startTimer.executed = true;
  startTimer.callback();
  await worker.flush();
  finishCollection({ state: "ready", payload: { sold_count: 20, captured_at: "2026-09-30T10:00:00+08:00" } });
  await worker.flush();

  assert.equal(worker.focusedWindowId, 7, "promoting the next product tab does not leave the collector in front");
});

test("verification restores the worker window and shows the challenge tab without resizing it", async () => {
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

  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true && update.state === "normal"));
  assert.ok(worker.updatedTabs.some((update) => update.id === challengeTab.id && update.active === true));
  assert.equal(worker.removedTabs.includes(challengeTab.id), false,
    "keeps the challenge page open for human verification");
  assert.equal(worker.removedTabs.length, 0, "recycles the unused prefetched page as the idle blank tab");
  assert.equal((await worker.chrome.tabs.get(worker.storage.idleAutomationTabId)).url, "about:blank");
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

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.deepEqual(worker.collectionMessages.map((message) => message.target_type),
    ["STORE", "STORE_IDENTITY", "STORE"]);
  assert.equal(worker.collectionMessages.filter((message) => message.target_type === "STORE"
    && !message.monitored_product_ids).length, 0);
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
  const prefetchedTabId = worker.storage.idleAutomationTabId;

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/challenge"
    && /IXSPY 登录已过期/.test(request.body.error_message)));
  assert.equal(worker.storage.challengeProvider, "IXSPY");
  assert.equal(worker.storage.automationTabId, loginTab.id);
  assert.equal(worker.windowCreates.length, 1, "keeps the task page in the worker window");
  assert.equal(worker.queue.find((item) => item.id === 102).status, "PENDING",
    "returns the prefetched task to the queue while login is pending");
  assert.ok(worker.removedTabs.includes(prefetchedTabId) === false,
    "recycles the unused prefetched page as the idle blank tab");
  assert.equal((await worker.chrome.tabs.get(prefetchedTabId)).url, "about:blank");
  assert.equal(worker.removedTabs.includes(loginTab.id), false, "keeps the page open while login is pending");
  assert.equal(worker.notifications.some((notice) => notice.title === "IXSPY 需要登录" && notice.requireInteraction), true);
  assert.ok(worker.updatedTabs.some((update) => update.id === loginTab.id && update.active === true));
  assert.equal(worker.requests.some((request) => request.pathname.endsWith("/snapshot")), false);

  worker.setCollectionResult({ state: "ready", payload: { sold_count: 22, captured_at: "2026-09-28T09:00:00+08:00" } });
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/101/snapshot"));
  assert.ok(worker.removedTabs.includes(loginTab.id), "closes the product tab after the resumed item succeeds");
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
  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true && update.state === "normal"));
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
  assert.ok(worker.windowUpdates.some((update) => update.id === 8 && update.focused === true && update.state === "normal"));
  assert.ok(worker.updatedTabs.some((update) => update.id === challengeTab.id && update.active === true));
});

test("alarm wake restores persisted worker mode after the MV3 service worker restarts", async () => {
  const worker = makeWorkerHarness({ dedicatedProfile: false });
  const dashboard = await worker.chrome.tabs.get(1);
  dashboard.url = "http://127.0.0.1:3000/";
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;

  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/heartbeat"));
  assert.ok(worker.requests.some((request) => request.pathname === "/api/browser-collection/items/claim"));
  assert.equal(worker.windowCreates.length, 1, "the alarm wake resumes in a background worker window");
});

test("alarm wake discovers a LAN dashboard and polls its same-origin API", async () => {
  const origin = "http://192.168.66.107:3000";
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

test("scheduled collection creates an unfocused worker window instead of taking over the user's window", async () => {
  const worker = makeWorkerHarness({ dedicatedProfile: false });
  await worker.flush();
  worker.storage.browserCollectionIsolatedProfileAuthorized = true;
  worker.storage.automationWindowId = 88;
  worker.storage.automationWindowOwned = true;
  const dashboard = await worker.chrome.tabs.get(1);
  dashboard.url = "http://127.0.0.1:3000/";

  worker.chrome.runtime.onStartup.callback();
  await worker.flush();
  worker.chrome.alarms.onAlarm.callback({ name: "aliexpress-monitor-browser-collection" });
  await worker.flush();

  assert.equal(worker.windowCreates.length, 1);
  assert.equal(worker.windowCreates[0].focused, false);
  assert.equal(worker.windowCreates[0].state, "normal");
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
