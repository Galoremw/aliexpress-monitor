const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../extension/content.js"), "utf8");

function makeMessageListener(collect, sendRuntimeMessage = async () => ({ ok: true }), candidateScan = null) {
  let listener;
  const window = {};
  window.top = window;
  vm.runInNewContext(source, {
    AliExpressMonitorPageCollector: {
      collect,
      collectStoreCandidatePage: candidateScan || (async () => ({ state: "failed", message: "not configured" })),
    },
    chrome: {
      runtime: {
        onMessage: { addListener(callback) { listener = callback; } },
        sendMessage(message, callback) {
          const response = Promise.resolve(sendRuntimeMessage(message));
          if (callback) response.then(callback);
          return response;
        },
      },
    },
    window,
    location: { pathname: "/store/1103845415" },
  }, { filename: "content.js" });
  return listener;
}

function request(listener, targetType, options = {}) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("content script did not respond")), 1000);
    try {
      assert.equal(listener({ type: "collect-visible-page", target_type: targetType, ...options }, null, (result) => {
        clearTimeout(timeout);
        resolve(result);
      }), true);
    } catch (error) {
      clearTimeout(timeout);
      reject(error);
    }
  });
}

test("store identity responds when the collector returns synchronously", async () => {
  const listener = makeMessageListener(() => ({ state: "ready", platform_store_id: "1103845415" }));
  const result = await request(listener, "STORE_IDENTITY");
  assert.equal(result.state, "ready");
  assert.equal(result.platform_store_id, "1103845415");
});

test("store selection responds when the collector returns a promise", async () => {
  const listener = makeMessageListener(async () => ({ state: "ready", page_result: { candidates: [] } }));
  const result = await request(listener, "STORE_SELECT_PAGE");
  assert.equal(result.state, "ready");
  assert.equal(result.page_result.candidates.length, 0);
});

test("scheduled candidate scan routes its request to the page collector", async () => {
  let received;
  const expectedPageUrl = "https://www.aliexpress.com/store/1103845415/pages/all-items.html?shop_sortType=orders_desc&page=2";
  const listener = makeMessageListener(
    () => ({ state: "failed" }),
    async () => ({ ok: true }),
    async (message) => {
      received = message;
      return { state: "ready", page_result: { url: message.expectedPageUrl } };
    },
  );
  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("content script did not respond")), 1000);
    assert.equal(listener({
      type: "STORE_CANDIDATE_SCAN",
      targetDate: "2026-10-12",
      expectedStoreId: "1103845415",
      expectedPageUrl,
    }, null, (value) => {
      clearTimeout(timer);
      resolve(value);
    }), true);
  });
  assert.deepEqual(JSON.parse(JSON.stringify(received)), {
    type: "STORE_CANDIDATE_SCAN",
    targetDate: "2026-10-12",
    expectedStoreId: "1103845415",
    expectedPageUrl,
  });
  assert.equal(result.page_result.url, expectedPageUrl);
});

test("candidate history checkpoint rejection is typed so the page scan can skip one candidate", async () => {
  let callbackError;
  const listener = makeMessageListener(
    () => ({ state: "failed" }),
    async () => ({
      ok: false,
      reason: "candidate_history_invalid",
      failed_checks: ["history_dates"],
      message: "candidate history invalid",
    }),
    async (_message, options) => {
      try {
        await options.on_candidate_collected({ platform_product_id: "1005001234567890" });
      } catch (error) {
        callbackError = error;
      }
      return { state: "ready", page_result: {} };
    },
  );
  const result = await new Promise((resolve) => {
    listener({
      type: "STORE_CANDIDATE_SCAN",
      item_id: 3218,
      claim_token: "claim-3218",
      expectedStoreId: "1103864579",
      expectedPageUrl: "https://www.aliexpress.com/store/1103864579/pages/all-items.html?shop_sortType=orders_desc",
    }, null, resolve);
  });

  assert.equal(result.state, "ready");
  assert.equal(callbackError.code, "CANDIDATE_HISTORY_INVALID");
  assert.deepEqual(JSON.parse(JSON.stringify(callbackError.failed_checks)), ["history_dates"]);
});

test("synchronous collector errors return a failed response", async () => {
  const listener = makeMessageListener(() => { throw new Error("identity failed"); });
  const result = await request(listener, "STORE_IDENTITY");
  assert.equal(result.state, "failed");
  assert.equal(result.message, "identity failed");
});

test("store selection forwards same-day retry IDs to the page collector", async () => {
  let forwardedOptions;
  const listener = makeMessageListener((_type, _date, options) => {
    forwardedOptions = options;
    return { state: "ready", page_result: { candidates: [] } };
  });

  await request(listener, "STORE_SELECT_PAGE", {
    target_date: "2026-09-29",
    skip_product_ids: ["2501"],
  });

  assert.equal(forwardedOptions.skip_product_ids[0], "2501");
});

test("store collection forwards completed products to the worker checkpoint", async () => {
  let forwardedOptions;
  const progressMessages = [];
  const listener = makeMessageListener((_type, _date, options) => {
    forwardedOptions = options;
    return { state: "ready", page_result: { products: [] } };
  }, async (message) => {
    progressMessages.push(message);
    return { ok: true };
  });

  await request(listener, "STORE", {
    target_date: "2026-10-10",
    monitored_product_ids: ["1005001234567890"],
    item_id: 401,
    claim_token: "claim-401",
  });

  assert.equal(typeof forwardedOptions.on_product_collected, "function");
  const product = { platform_product_id: "1005001234567890", historical_sales: [] };
  await forwardedOptions.on_product_collected(product);
  assert.deepEqual(JSON.parse(JSON.stringify(progressMessages)), [{
    type: "store-collection-product-progress",
    item_id: 401,
    claim_token: "claim-401",
    product,
  }]);
});
