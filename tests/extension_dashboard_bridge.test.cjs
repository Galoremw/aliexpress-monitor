const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

test("LAN dashboard bridge uses its same-origin API and enables the remote worker", async () => {
  const url = "http://192.168.66.115:3000/?browser_collection=1";
  const messages = [];
  const fetches = [];
  const context = {
    URL,
    location: new URL(url),
    document: { addEventListener() {} },
    window: { addEventListener() {} },
    chrome: {
      runtime: {
        sendMessage: async (message) => { messages.push(message); },
      },
    },
    fetch: async (target, options) => {
      fetches.push({ target, options });
      return { ok: true, json: async () => ({ access_token: "session-token" }) };
    },
  };

  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../extension/dashboard_bridge.js"), "utf8"), context);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(fetches[0].target, "http://192.168.66.115:3000/api/auth/extension-token");
  assert.equal(messages.length, 1);
  assert.equal(messages[0].api_base_url, "http://192.168.66.115:3000");
  assert.equal(messages[0].browser_collection_mode, true);
  assert.equal(messages[0].browser_collection_local_mode, false);
  assert.equal(messages[0].dianxiaomi_browser_worker_mode, true);
  assert.equal(messages[0].api_access_token, "session-token");
});

test("ordinary local dashboard explicitly disables browser collection mode", async () => {
  const messages = [];
  const context = {
    URL,
    location: new URL("http://127.0.0.1:3000/"),
    document: { addEventListener() {} },
    window: { addEventListener() {} },
    chrome: { runtime: { sendMessage: async (message) => { messages.push(message); } } },
    fetch: async () => ({ ok: true, json: async () => ({ access_token: "session-token" }) }),
  };

  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../extension/dashboard_bridge.js"), "utf8"), context);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(messages.length, 1);
  assert.equal(messages[0].browser_collection_mode, false);
  assert.equal(messages[0].browser_collection_local_mode, false);
  assert.equal(messages[0].browser_collection_mode_explicit, true);
});

test("isolated local collector marker enables only the local collector profile", async () => {
  const messages = [];
  const context = {
    URL,
    location: new URL("http://127.0.0.1:3000/?browser_collection=1&browser_collection_profile=isolated"),
    document: { addEventListener() {} },
    window: { addEventListener() {} },
    chrome: { runtime: { sendMessage: async (message) => { messages.push(message); } } },
    fetch: async () => ({ ok: true, json: async () => ({ access_token: "session-token" }) }),
  };

  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../extension/dashboard_bridge.js"), "utf8"), context);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(messages[0].browser_collection_mode, true);
  assert.equal(messages[0].browser_collection_local_mode, true);
  assert.equal(messages[0].browser_collection_mode_explicit, true);
});

test("extension permissions are scoped to the configured collector PC dashboard", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "../extension/manifest.json"), "utf8"));
  const dashboardUrl = "http://192.168.66.115:3000/*";
  const dashboardScript = manifest.content_scripts.find((item) => item.js.includes("dashboard_bridge.js"));

  assert.ok(manifest.host_permissions.includes(dashboardUrl));
  assert.ok(dashboardScript.matches.includes(dashboardUrl));
  assert.equal(manifest.host_permissions.includes("http://*/*"), false);
});

test("NAS LAN address supports the isolated host collector profile", async () => {
  const messages = [];
  const context = {
    URL,
    location: new URL("http://192.168.66.115:3000/?browser_collection=1&browser_collection_profile=isolated"),
    document: { addEventListener() {} },
    window: { addEventListener() {} },
    chrome: { runtime: { sendMessage: async (message) => { messages.push(message); } } },
    fetch: async () => ({ ok: true, json: async () => ({ access_token: "session-token" }) }),
  };

  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../extension/dashboard_bridge.js"), "utf8"), context);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(messages[0].browser_collection_mode, true);
  assert.equal(messages[0].browser_collection_local_mode, true);
  assert.equal(messages[0].api_base_url, "http://192.168.66.115:3000");
});
