const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

function makeProductBridge({ pageProductId = "100000000403" } = {}) {
  const storage = {};
  let clickCount = 0;
  let messageListener;
  const button = {
    isConnected: true,
    innerText: "本页采集",
    textContent: "本页采集",
    disabled: false,
    getAttribute: () => null,
    getBoundingClientRect: () => ({ width: 120, height: 28 }),
    click() {
      clickCount += 1;
      this.innerText = "采集成功";
      this.textContent = "采集成功";
    },
  };
  const chrome = {
    storage: {
      local: {
        async get(key) { return { [key]: storage[key] }; },
        async set(values) { Object.assign(storage, values); },
      },
    },
    runtime: { onMessage: { addListener(listener) { messageListener = listener; } } },
  };
  const context = {
    chrome,
    URL,
    Date,
    Promise,
    setTimeout,
    clearTimeout,
    globalThis: null,
    location: { href: `https://www.aliexpress.com/item/${pageProductId}.html` },
    document: {
      body: { innerText: "AliExpress product details" },
      querySelectorAll(selector) {
        return selector === ".dxmCurrentPag" ? [button] : [button];
      },
    },
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  };
  context.globalThis = context;
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "../extension/dianxiaomi_product_bridge.js"), "utf8"),
    context,
  );

  return {
    storage,
    get clickCount() { return clickCount; },
    async send(message) {
      return new Promise((resolve) => {
        messageListener(message, {}, resolve);
      });
    },
  };
}

async function flushMicrotasks() {
  for (let index = 0; index < 20; index += 1) await new Promise((resolve) => setImmediate(resolve));
}

test("Dianxiaomi product bridge clicks the visible assistant control on the matching product", async () => {
  const bridge = makeProductBridge();

  await bridge.send({
    type: "dianxiaomi-start-product",
    handoff_id: 403,
    product_id: "100000000403",
    target_url: "https://www.aliexpress.com/item/100000000403.html",
  });
  await flushMicrotasks();

  assert.equal(bridge.clickCount, 1);
  assert.equal(bridge.storage.dianxiaomiProductOperation.state, "succeeded");
});

test("Dianxiaomi product bridge never clicks the assistant on a mismatched product page", async () => {
  const bridge = makeProductBridge({ pageProductId: "100000000499" });

  await bridge.send({
    type: "dianxiaomi-start-product",
    handoff_id: 403,
    product_id: "100000000403",
    target_url: "https://www.aliexpress.com/item/100000000403.html",
  });
  await flushMicrotasks();

  assert.equal(bridge.clickCount, 0);
  assert.equal(bridge.storage.dianxiaomiProductOperation.state, "needs_confirmation");
});
