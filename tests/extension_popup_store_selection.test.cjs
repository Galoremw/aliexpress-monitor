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

test("popup retries only incomplete store candidates and submits merged seven-day histories", async () => {
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

  const retryMessage = messages.find((message) => message.target_type === "STORE_SELECT_PAGE");
  assert.deepEqual(disabledDuringMessages, [true, true]);
  assert.deepEqual(Array.from(retryMessage.skip_product_ids), ["1005007707021"]);
  assert.equal(retryMessage.target_date, today);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].products.length, 2);
  assert.deepEqual(posts[0].products.map((item) => item.historical_sales.length), [7, 7]);
  assert.match(elements.get("#result").textContent, /入池完成/);
  assert.equal(storage[storageKey], undefined);
});
