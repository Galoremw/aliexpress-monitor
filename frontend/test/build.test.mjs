import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

test("static demo source includes hash routes and backend-not-configured state", async () => {
  const html = await readFile(new URL("../src/index.html", import.meta.url), "utf8");
  const app = await readFile(new URL("../src/app.js", import.meta.url), "utf8");
  assert.match(html, /__PAGES_BASE__/);
  assert.match(app, /Backend 尚未配置/);
  assert.match(app, /api\/collection\/status\/today/);
  assert.match(app, /api\/products\/\$\{id\}\/snapshots/);
  assert.match(app, /#\/stores/);
});
