const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const selectionSource = fs.readFileSync(path.join(__dirname, "../extension/store_selection.js"), "utf8");
const popupSource = fs.readFileSync(path.join(__dirname, "../extension/popup/popup.js"), "utf8");

function metricDates(targetDate) {
  const target = Date.parse(`${targetDate}T00:00:00Z`);
  return Array.from({ length: 7 }, (_, index) =>
    new Date(target - (7 - index) * 86400000).toISOString().slice(0, 10));
}

function candidate(id, dates, value, complete = true) {
  return {
    platform_product_id: id,
    url: `https://www.aliexpress.com/item/${id}.html`,
    orders_rank: Number(id.slice(-1)),
    history_source: "ixspy_visible_daily_increment",
    history_daily_capture_complete: complete,
    history_expected_dates: dates,
    history_missing_dates: complete ? [] : dates.slice(1),
    historical_sales: (complete ? dates : dates.slice(0, 1))
      .map((date) => ({ date, value, value_type: "daily_increment" })),
  };
}

test("popup submits only the active Orders page instead of accumulating staged pages", async () => {
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const dates = metricDates(today);
  const url = "https://www.aliexpress.com/store/770702?shop_sortType=orders_desc";
  const previousPage = {
    platform_store_id: "770702",
    url,
    has_next_page: false,
    raw_data: { history_target_date: today },
    candidates: [candidate("1005007707021", dates, 4), candidate("1005007707022", dates, 5, false)],
  };
  const storageKey = "storeSelectionProgress:v2:770702";
  const storage = { [storageKey]: { pages: [previousPage] } };
  const elements = new Map();
  for (const id of ["page-status", "result", "collect", "test-store", "test-result", "heading", "login-panel", "collector-panel", "login-form", "login-result"]) {
    elements.set(`#${id}`, {
      textContent: "",
      className: "",
      hidden: false,
      disabled: false,
      addEventListener(type, callback) { this[type] = callback; },
    });
  }
  const messages = [];
  const disabledDuringMessages = [];
  const posts = [];
  const context = {
    URL,
    Date,
    Promise,
    document: { querySelector(selector) { return elements.get(selector); } },
    chrome: {
      tabs: {
        async query() { return [{ id: 7, url }]; },
        async sendMessage(_tabId, message) {
          messages.push(message);
          disabledDuringMessages.push(elements.get("#collect").disabled);
          if (message.target_type === "STORE_IDENTITY") return { state: "ready", platform_store_id: "770702" };
          return {
            state: "ready",
            page_result: {
              platform_store_id: "770702",
              url,
              has_next_page: false,
              next_page_url: null,
              raw_data: { history_target_date: today },
              candidates: [
                { platform_product_id: "1005007707021", url: previousPage.candidates[0].url, orders_rank: 1 },
                candidate("1005007707022", dates, 6),
              ],
            },
          };
        },
      },
      storage: {
        local: {
          async get(keys) {
            if (Array.isArray(keys)) return Object.fromEntries(keys.filter((key) => key in storage)
              .map((key) => [key, storage[key]]));
            return { [keys]: storage[keys] };
          },
          async set(values) { Object.assign(storage, values); },
          async remove(key) { delete storage[key]; },
        },
      },
      scripting: { async executeScript() { throw new Error("unexpected reinjection"); } },
    },
    fetch: async (requestUrl, options) => {
      if (requestUrl.endsWith("/api/auth/me")) return { ok: true, json: async () => ({}) };
      posts.push(JSON.parse(options.body));
      return { ok: true, json: async () => ({ added_count: 0, existing_count: 2 }) };
    },
    globalThis: null,
  };
  context.globalThis = context;
  vm.runInNewContext(selectionSource, context, { filename: "store_selection.js" });
  vm.runInNewContext(popupSource, context, { filename: "popup.js" });
  await new Promise((resolve) => setImmediate(resolve));

  await elements.get("#collect").click();

  const pageMessage = messages.find((message) => message.target_type === "STORE_SELECT_PAGE");
  assert.deepEqual(disabledDuringMessages, [true, true]);
  assert.equal(pageMessage.target_date, today);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].products.length, 1);
  assert.deepEqual(posts[0].products.map((item) => item.platform_product_id), ["1005007707022"]);
  assert.equal(posts[0].raw_data.candidate_count, 2);
  assert.deepEqual(posts[0].raw_data.page_urls, [url]);
  assert.equal(posts[0].raw_data.has_next_page, false);
  assert.match(elements.get("#result").textContent, /入池完成/);
  assert.equal(storage[storageKey], undefined);
});

test("failed page submission retries the identical payload without rereading another page", async () => {
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const dates = metricDates(today);
  const url = "https://www.aliexpress.com/store/770703/pages/all-items.html?page=2&sortType=bestmatch_sort&shop_sortType=orders_desc";
  const nextUrl = "https://www.aliexpress.com/store/770703/pages/all-items.html?page=3&sortType=bestmatch_sort&shop_sortType=orders_desc";
  const storageKey = "storeSelectionProgress:v2:770703";
  const storage = {};
  const elements = new Map();
  for (const id of ["page-status", "result", "collect", "test-store", "test-result", "heading", "login-panel", "collector-panel", "login-form", "login-result"]) {
    elements.set(`#${id}`, {
      textContent: "",
      className: "",
      hidden: false,
      disabled: false,
      addEventListener(type, callback) { this[type] = callback; },
    });
  }
  const messages = [];
  const posts = [];
  let failPost = true;
  let activeUrl = url;
  const context = {
    URL,
    Date,
    Promise,
    document: { querySelector(selector) { return elements.get(selector); } },
    chrome: {
      tabs: {
        async query() { return [{ id: 9, url: activeUrl }]; },
        async sendMessage(_tabId, message) {
          messages.push(message);
          if (message.target_type === "STORE_IDENTITY") return { state: "ready", platform_store_id: "770703" };
          return {
            state: "ready",
            page_result: {
              platform_store_id: "770703",
              url,
              has_next_page: true,
              next_page_url: nextUrl,
              raw_data: { history_target_date: today },
              candidates: [candidate("1005007707031", dates, 0, false)],
            },
          };
        },
      },
      storage: {
        local: {
          async get(keys) {
            if (Array.isArray(keys)) return Object.fromEntries(keys.filter((key) => key in storage)
              .map((key) => [key, storage[key]]));
            return { [keys]: storage[keys] };
          },
          async set(values) { Object.assign(storage, values); },
          async remove(key) { delete storage[key]; },
        },
      },
      scripting: { async executeScript() { throw new Error("unexpected reinjection"); } },
    },
    fetch: async (requestUrl, options) => {
      if (requestUrl.endsWith("/api/auth/me")) return { ok: true, json: async () => ({}) };
      posts.push(JSON.parse(options.body));
      if (failPost) {
        failPost = false;
        throw new Error("temporary network failure");
      }
      return {
        ok: true,
        json: async () => ({ added_count: 0, existing_count: 0, skipped_count: 1, continuation_saved: true }),
      };
    },
    globalThis: null,
  };
  context.globalThis = context;
  vm.runInNewContext(selectionSource, context, { filename: "store_selection.js" });
  vm.runInNewContext(popupSource, context, { filename: "popup.js" });
  await new Promise((resolve) => setImmediate(resolve));

  await elements.get("#collect").click();

  const pending = storage[storageKey]?.pending_payload;
  assert.ok(pending);
  assert.equal(pending.products.length, 0);
  assert.equal(pending.raw_data.candidate_count, 1);
  assert.equal(pending.raw_data.has_next_page, true);
  assert.equal(pending.raw_data.next_page_url, nextUrl);
  assert.deepEqual(Array.from(pending.raw_data.page_urls), [url]);
  assert.equal(posts.length, 1);

  activeUrl = nextUrl;
  await elements.get("#collect").click();
  assert.equal(posts.length, 1);
  assert.ok(storage[storageKey]?.pending_payload);

  activeUrl = url;
  await elements.get("#collect").click();

  assert.equal(posts.length, 2);
  assert.deepEqual(posts[1], posts[0]);
  assert.equal(messages.filter((message) => message.target_type === "STORE_SELECT_PAGE").length, 1);
  assert.equal(storage[storageKey], undefined);
  assert.match(elements.get("#result").textContent, /继续|补采|续页/);
});
