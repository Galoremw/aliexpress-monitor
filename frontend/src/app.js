const config = window.MONITOR_CONFIG || { apiBaseUrl: "", mode: "demo" };
const app = document.querySelector("#app");
let dianxiaomiRefreshTimer = null;

function api(path, options = {}) {
  if (!config.apiBaseUrl) return Promise.reject(new Error("Backend 尚未配置"));
  return fetch(`${config.apiBaseUrl.replace(/\/$/, "")}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    credentials: "include",
    ...options,
  }).then(async (response) => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || `Backend 请求失败 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  });
}

function currentRoute() { return location.hash.replace(/^#/, "") || "/dashboard"; }

async function render() {
  app.innerHTML = `<div class="loading">正在连接 Backend…</div>`;
  try {
    const route = currentRoute();
    if (!config.apiBaseUrl) return renderError("BACKEND_NOT_CONFIGURED", "当前 Pages 构建没有配置 Backend 地址。");
    if (route === "/login") return loginPage();
    try { await api("/api/auth/me"); } catch (error) {
      if (error.status === 401) return loginPage();
      throw error;
    }
    if (route === "/dashboard") return dashboard();
    if (route === "/stores") return storesPage();
    if (route === "/products") return productsPage();
    if (route === "/manual" || route === "/pending") return pendingPage();
    const [, type, id] = route.split("/");
    if (type === "stores" && id) return storePage(Number(id));
    if (type === "products" && id) return productPage(Number(id));
    location.hash = "#/dashboard";
  } catch (error) {
    renderError("Backend 连接失败", `${error.message}。请确认云端 Backend 健康检查正常，并已允许当前前端域名跨域访问。`);
  }
}

async function dashboard() {
  const [status, stores, products, browserRun, dianxiaomi] = await Promise.all([
    api("/api/collection/status/today"), api("/api/stores?status=active"), api("/api/products?status=active"),
    api("/api/browser-collection/runs/today").catch(() => null),
    api("/api/integrations/dianxiaomi/status").catch(() => null),
  ]);
  app.innerHTML = shell("监控台", `<section class="hero"><div><p class="eyebrow">HOSTED BACKEND</p><h1>AliExpress 竞品监控</h1><p class="lead">前端已连接托管 Backend，监控数据不依赖当前电脑上的本地服务。</p></div><span class="status-badge ok">BACKEND_CONNECTED</span></section>${metrics(status)}${browserCollectionPanel(browserRun)}${dianxiaomiPanel(dianxiaomi)}<section class="section"><div class="section-heading"><h2>活跃店铺</h2><a class="button" href="#/stores">查看全部</a></div><div class="list">${stores.map(storeRow).join("") || empty("暂无活跃店铺")}</div></section><section class="section"><div class="section-heading"><h2>快捷操作</h2></div><div class="action-row"><button class="button primary" data-action="collect-all">立即采集全部活跃商品</button><a class="button" href="#/manual">进入人工处理</a></div></section>`);
  bindActions();
  if (!dianxiaomiRefreshTimer) {
    dianxiaomiRefreshTimer = window.setInterval(async () => {
      if (currentRoute() !== "/dashboard") return;
      const panel = document.querySelector(".dianxiaomi-band");
      if (!panel) return;
      const status = await api("/api/integrations/dianxiaomi/status").catch(() => null);
      if (!status) return;
      panel.outerHTML = dianxiaomiPanel(status);
      bindDianxiaomiQueueActions();
    }, 5000);
  }
  void products;
}

async function storesPage() {
  const stores = await api("/api/stores");
  app.innerHTML = shell("监控店铺", `<section class="page-heading"><div><p class="eyebrow">STORES</p><h1>监控店铺</h1><p>点击店铺查看商品、昨日估算销量和历史指标。</p></div><button class="button primary" data-action="add-store">添加店铺</button></section><div class="list">${stores.map(storeRow).join("") || empty("暂无店铺")}</div>`);
  bindActions();
}

async function productsPage() {
  const [products, stores] = await Promise.all([api("/api/products"), api("/api/stores")]);
  app.innerHTML = shell("监控商品", `<section class="page-heading"><div><p class="eyebrow">PRODUCTS</p><h1>监控商品</h1><p>商品快照由 Auto、Chrome Extension 或 Manual 渠道写入。</p></div><button class="button primary" data-action="add-product">添加商品</button></section><div class="list">${products.map((product) => productRow(product, stores)).join("") || empty("暂无商品")}</div>`);
  bindActions();
}

async function pendingPage() {
  const [pending, status] = await Promise.all([api("/api/collection/pending"), api("/api/collection/status/today")]);
  app.innerHTML = shell("待人工补采", `<section class="page-heading"><div><p class="eyebrow">MANUAL FALLBACK</p><h1>待人工补采</h1><p>自动采集失败的商品需要在 Chrome 商品页主动补采。</p></div></section>${metrics(status)}<div class="list">${pending.map((row) => `<article class="row"><div><a href="#/products/${row.product_id}"><strong>${row.title || row.platform_product_id}</strong></a><span>${row.store_name} · ${row.failure_reason || "自动采集失败"}</span></div><a class="button" href="${trackedPendingUrl(row)}" target="_blank" rel="noreferrer">打开商品</a></article>`).join("") || empty("当前没有待人工补采商品")}</div>`);
}

async function storePage(id) {
  const [store, products, metrics, top] = await Promise.all([api(`/api/stores/${id}`), api(`/api/products?store_id=${id}`), api(`/api/stores/${id}/daily-metrics`), api(`/api/stores/${id}/top-products`)]);
  app.innerHTML = shell(store.name, `<section class="page-heading"><div><p class="eyebrow">STORE ${store.aliexpress_store_id || store.id}</p><h1 class="store-detail-title"><span data-store-name="${store.id}">${store.name}</span><button class="icon-button" data-action="edit-store" data-id="${store.id}" title="编辑店铺名称" aria-label="编辑店铺名称">✎</button></h1><p>仅汇总已监控商品范围内的公开数据推算。</p></div><div class="action-row"><button class="button primary dianxiaomi-store" data-product-ids="${products.filter((product) => product.status === "active").map((product) => product.id).join(",")}">采集到店小秘</button><a class="button" href="${store.url}" target="_blank" rel="noreferrer">打开店铺</a><button class="button" data-action="discover" data-id="${id}">发现商品</button></div></section><section class="section"><div class="section-heading"><h2>昨日 Top 商品</h2><span>${top.status}</span></div><div class="list">${(top.products || []).map((product) => `<article class="row"><div><a href="#/products/${product.product_id}"><strong>${product.title || product.aliexpress_product_id}</strong></a><span>${product.url}</span></div><b>估算 ${product.estimated_sales}</b></article>`).join("") || empty("暂无连续快照基线")}</div></section><section class="section"><div class="section-heading"><h2>监控商品</h2><span>${products.length} 个</span></div><div class="list">${products.map((product) => productRow(product, [store])).join("") || empty("暂无商品")}</div></section><section class="section"><div class="section-heading"><h2>店铺日销量估算</h2></div>${metricTable(metrics)}</section>`);
  bindActions();
}

async function productPage(id) {
  const [product, snapshots, metrics] = await Promise.all([api(`/api/products/${id}`), api(`/api/products/${id}/snapshots`), api(`/api/products/${id}/daily-metrics`)]);
  app.innerHTML = shell(product.title || product.aliexpress_product_id, `<section class="page-heading"><div><p class="eyebrow">PRODUCT ${product.aliexpress_product_id}</p><h1>${product.title || "未命名商品"}</h1><p>公开数据快照与估算销量趋势。</p></div><div class="action-row"><a class="button primary" href="${trackedProductUrl(product)}" target="_blank" rel="noreferrer">打开商品链接</a>${product.status === "active" ? `<button class="button dianxiaomi-product" data-product-id="${product.id}">采集到店小秘</button><button class="button danger" data-action="deactivate" data-id="${product.id}">删除监控</button>` : ""}</div></section><section class="section"><div class="section-heading"><h2>日销量估算</h2><span>非真实后台订单量</span></div>${metricTable(metrics)}</section><section class="section"><div class="section-heading"><h2>原始快照历史</h2><span>只追加，不覆盖</span></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>来源</th><th>状态</th><th>累计 sold</th><th>价格</th><th>评价数</th></tr></thead><tbody>${snapshots.map((snapshot) => `<tr><td>${formatDate(snapshot.captured_at || snapshot.collected_at)}</td><td>${snapshot.source || "AUTO"}</td><td>${snapshot.status || snapshot.parse_status}</td><td>${snapshot.sold_count ?? snapshot.cumulative_sold ?? "—"}</td><td>${snapshot.price ?? snapshot.price_amount ?? "—"}</td><td>${snapshot.review_count ?? "—"}</td></tr>`).join("") || `<tr><td colspan="6">暂无快照</td></tr>`}</tbody></table></div></section>`);
  bindActions();
}

function shell(title, content) { return `<header class="topbar"><a class="brand" href="#/dashboard">AliExpress 竞品监控</a><nav><a href="#/dashboard">监控台</a><a href="#/stores">店铺</a><a href="#/products">商品</a><a href="#/manual">人工处理</a><button class="nav-logout" data-action="logout">退出登录</button></nav></header><main><div class="page-title">${title}</div>${content}</main>`; }
function loginPage() { app.innerHTML = `<main class="auth-page"><section class="auth-panel"><p class="eyebrow">PRIVATE MONITOR</p><h1>登录监控台</h1><p class="auth-hint">登录后访问云端店铺、商品和历史快照。</p><form id="hosted-login"><label>账号<input name="username" autocomplete="username" required></label><label>密码<input name="password" type="password" autocomplete="current-password" required></label><button class="button primary" type="submit">登录</button></form><p id="hosted-login-error" class="warning-text" hidden></p></section></main>`; document.querySelector("#hosted-login").addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget; const error = document.querySelector("#hosted-login-error"); error.hidden = true; try { await api("/api/auth/login", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form))) }); location.hash = "#/dashboard"; await render(); } catch (err) { error.textContent = err.message; error.hidden = false; } }); }
function metrics(s) { return `<section class="metric-grid">${metric("活跃商品", s.total_products, "今日")}${metric("自动成功", s.auto_success, "今日")}${metric("自动失败", s.auto_failed, "今日")}${metric("手动完成", s.manual_completed, "今日")}${metric("待人工补采", s.pending_manual, "当前")}${metric("成功率", `${s.success_rate}%`, "公开数据采集")}</section>`; }
function browserCollectionPanel(run) { if (!run) return `<section class="section browser-collection"><div class="section-heading"><h2>每日浏览器采集</h2><span>今日任务尚未创建</span></div><div class="action-row"><button class="button primary" data-action="ensure-browser">立即运行今日任务</button></div></section>`; const phase = { STORE_DISCOVERY: "刷新店铺前 20", PRODUCT_COLLECTION: "采集商品数据", COMPLETED: "今日任务完成" }[run.phase] || run.phase; const challenge = run.status === "NEEDS_VERIFICATION" ? `<p class="warning-text">AliExpress 要求人工完成验证。完成验证后点击继续任务。</p>` : ""; const resume = run.status === "NEEDS_VERIFICATION" ? `<button class="button primary" data-action="resume-browser" data-id="${run.id}">验证完成，继续任务</button>` : ""; return `<section class="section browser-collection"><div class="section-heading"><h2>每日浏览器采集</h2><span>${run.target_date} · ${phase}</span></div><div class="browser-summary"><span>Chrome：${run.chrome_online ? "在线" : "离线"}</span><span>总任务：${run.total_count}</span><span>成功：${run.succeeded_count}</span><span>部分：${run.partial_count}</span><span>失败：${run.failed_count}</span><span>待处理：${run.pending_count}</span></div>${challenge}<div class="action-row"><button class="button" data-action="ensure-browser">立即运行今日任务</button>${resume}</div></section>`; }
function metric(label, value, note) { return `<article class="metric"><span>${label}</span><strong>${value ?? "—"}</strong><small>${note}</small></article>`; }
function storeRow(store) { return `<article class="row"><div><span class="store-name-line"><a href="#/stores/${store.id}"><strong data-store-name="${store.id}">${store.name}</strong></a><button class="icon-button" data-action="edit-store" data-id="${store.id}" title="编辑店铺名称" aria-label="编辑店铺名称">✎</button></span><span>${store.aliexpress_store_id || "未识别店铺 ID"} · ${store.status}</span></div><a class="button" href="#/stores/${store.id}">展开</a></article>`; }
function trackedProductUrl(product) { return `${product.url}#monitor_product_id=${encodeURIComponent(product.aliexpress_product_id)}`; }
function trackedPendingUrl(row) { return `${row.product_url}#monitor_product_id=${encodeURIComponent(row.platform_product_id)}`; }
function productRow(product, stores) { const store = stores.find((item) => item.id === product.store_id); return `<article class="row"><div><a href="#/products/${product.id}"><strong>${product.title || product.aliexpress_product_id}</strong></a><span>${store?.name || "店铺"} · ${product.aliexpress_product_id}</span></div><div class="row-actions"><a class="button" href="${trackedProductUrl(product)}" target="_blank" rel="noreferrer">打开商品页</a>${product.status === "active" ? `<button class="button dianxiaomi-product" data-product-id="${product.id}">采集到店小秘</button><button class="button danger" data-action="deactivate" data-id="${product.id}">删除监控</button>` : ""}</div></article>`; }
const dianxiaomiStatusLabels = { QUEUED: "待发送", CLAIMED: "扩展已领取", OPENED: "后台页面已打开", FILLED: "链接已填入", COLLECTING: "店小秘采集中", SUCCEEDED: "店小秘已完成", FAILED: "失败", NEEDS_CONFIRMATION: "需要人工处理", CANCELED: "已取消" };
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character])); }
function dianxiaomiItem(item) { const status = dianxiaomiStatusLabels[item.status] || item.status; const statusClass = item.status === "SUCCEEDED" ? "success" : ["FAILED", "NEEDS_CONFIRMATION"].includes(item.status) ? "error" : ""; const action = ["NEEDS_CONFIRMATION", "FAILED"].includes(item.status) ? `<button class="button" data-action="dianxiaomi-resume" data-id="${item.id}">重新排队</button>` : ["QUEUED", "CLAIMED", "OPENED", "FILLED", "COLLECTING"].includes(item.status) ? `<button class="button" data-action="dianxiaomi-cancel" data-id="${item.id}">取消</button>` : ""; const title = escapeHtml(item.product_title || item.platform_product_id || `商品 #${item.product_id}`); const store = escapeHtml(item.store_name || `店铺 #${item.store_id}`); const url = escapeHtml(item.target_url); const error = item.error_message ? ` · ${escapeHtml(item.error_message)}` : ""; return `<article class="dianxiaomi-item"><div class="dianxiaomi-item-main"><strong class="dianxiaomi-item-title">${title}</strong><span class="dianxiaomi-item-meta">${store} · <a href="${url}" target="_blank" rel="noreferrer">${url}</a></span><span class="dianxiaomi-item-meta">${formatDate(item.requested_at)}${error}</span></div><div class="dianxiaomi-item-side"><span class="dianxiaomi-status ${statusClass}">${status}</span><span class="dianxiaomi-item-actions">${action}</span></div></article>`; }
function dianxiaomiPanel(status) { const values = status || {}; const items = values.items || values.latest || []; return `<section class="section dianxiaomi-band"><div class="section-heading"><h2>店小秘采集队列</h2><span>通过已登录 Chrome 的正常页面提交</span></div><div class="browser-summary"><span>待发送：${values.queued || 0}</span><span>处理中：${values.processing || 0}</span><span>已提交：${values.submitted || 0}</span><span>失败：${values.failed || 0}</span><span>待人工确认：${values.needs_confirmation || 0}</span></div><p class="muted-value">${items[0] ? `${escapeHtml(items[0].product_title || items[0].platform_product_id || "商品")} · ${dianxiaomiStatusLabels[items[0].status] || items[0].status}` : "尚未提交店小秘采集任务。"}</p><div class="dianxiaomi-items">${items.map(dianxiaomiItem).join("") || empty("当前没有店小秘任务")}</div></section>`; }
function metricTable(rows) { return `<div class="table-wrap"><table><thead><tr><th>日期</th><th>估算销量</th><th>状态</th><th>依据</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${row.metric_date}</td><td>${row.is_estimable ? row.estimated_sales : "不可估算"}</td><td>${row.is_estimable ? "可估算" : "待基线"}</td><td>${row.reason || row.estimate_type || "public_observable_data"}</td></tr>`).join("") || `<tr><td colspan="4">暂无数据</td></tr>`}</tbody></table></div>`; }
function empty(text) { return `<div class="empty-state"><strong>${text}</strong></div>`; }
function formatDate(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—"; }
function renderError(title, detail) { app.innerHTML = `<main class="error-page"><p class="eyebrow">${title}</p><h1>监控台暂时无法连接</h1><p>${detail}</p><p>Backend 地址：<code>${config.apiBaseUrl || "未配置"}</code></p><button class="button primary" onclick="location.reload()">重新连接</button></main>`; }

function bindActions() {
  document.querySelectorAll("[data-action='logout']").forEach((button) => button.addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => null); location.hash = "#/login"; await render(); }));
  document.querySelectorAll("[data-action='collect']").forEach((button) => button.addEventListener("click", async () => { await action(button, `/api/products/${button.dataset.id}/collect`, "采集任务已完成"); }));
  document.querySelectorAll("[data-action='collect-all']").forEach((button) => button.addEventListener("click", async () => { await action(button, "/api/jobs/collect-now", "全量采集任务已完成"); }));
  document.querySelectorAll("[data-action='discover']").forEach((button) => button.addEventListener("click", async () => { await action(button, `/api/stores/${button.dataset.id}/discover?limit=20`, "已自动加入销量排序前 20 个商品"); }));
  document.querySelectorAll("[data-action='add-store']").forEach((button) => button.addEventListener("click", addStore));
  document.querySelectorAll("[data-action='add-product']").forEach((button) => button.addEventListener("click", addProduct));
  document.querySelectorAll("[data-action='deactivate']").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm("确定删除这个商品的监控吗？历史快照会保留，但它将不再参与自动采集和店铺汇总。")) return;
    await action(button, `/api/products/${button.dataset.id}/deactivate`, "商品已移出监控，历史数据已保留");
  }));
  document.querySelectorAll("[data-action='ensure-browser']").forEach((button) => button.addEventListener("click", async () => { await action(button, "/api/browser-collection/runs/ensure", "今日浏览器采集任务已创建", { trigger: "MANUAL" }); }));
  document.querySelectorAll("[data-action='resume-browser']").forEach((button) => button.addEventListener("click", async () => { await action(button, `/api/browser-collection/runs/${button.dataset.id}/resume`, "浏览器采集任务已恢复"); }));
  document.querySelectorAll(".dianxiaomi-product").forEach((button) => button.addEventListener("click", () => submitDianxiaomi([button.dataset.productId], button)));
  document.querySelectorAll(".dianxiaomi-store").forEach((button) => button.addEventListener("click", () => submitDianxiaomi((button.dataset.productIds || "").split(","), button)));
  bindDianxiaomiQueueActions();
  document.querySelectorAll("[data-action='edit-store']").forEach((button) => button.addEventListener("click", async () => editStoreName(button.dataset.id)));
}
function bindDianxiaomiQueueActions() { document.querySelectorAll("[data-action='dianxiaomi-resume']").forEach((button) => button.addEventListener("click", async () => { await action(button, `/api/integrations/dianxiaomi/handoffs/${button.dataset.id}/resume`, "店小秘任务已重新排队"); })); document.querySelectorAll("[data-action='dianxiaomi-cancel']").forEach((button) => button.addEventListener("click", async () => { await action(button, `/api/integrations/dianxiaomi/handoffs/${button.dataset.id}/cancel`, "店小秘任务已取消"); })); }
async function action(button, path, message, payload = null) { button.disabled = true; button.textContent = "处理中…"; try { await api(path, { method: "POST", ...(payload ? { body: JSON.stringify(payload) } : {}) }); alert(message); await render(); } catch (error) { alert(error.message); button.disabled = false; button.textContent = "重试"; } }
async function addStore() { const name = prompt("监控店铺名称（可选）", ""); const url = prompt("AliExpress 店铺链接"); if (!url) return; try { const store = await api("/api/stores", { method: "POST", body: JSON.stringify({ name, url }) }); const discovery = await api(`/api/stores/${store.id}/discover?limit=20`, { method: "POST" }); if (discovery.parse_status === "failed") alert(`店铺已添加，但前 20 个商品发现失败：${discovery.error_message || discovery.error_type}`); else alert(`店铺已添加，已加入销量排序前 ${discovery.added_count} 个商品`); await render(); } catch (error) { alert(error.message); } }
async function addProduct() { const storeId = prompt("所属店铺 ID（留空则归入“自定义监控”）"); const url = prompt("AliExpress 商品链接"); if (!url) return; try { await api("/api/products", { method: "POST", body: JSON.stringify({ store_id: storeId ? Number(storeId) : null, url }) }); await render(); } catch (error) { alert(error.message); } }

async function submitDianxiaomi(productIds, button) {
  const ids = [...new Set(productIds.map((value) => Number(value)).filter(Boolean))];
  if (!ids.length) return alert("没有可发送的有效监控商品");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "加入队列…";
  try {
    const result = await api("/api/integrations/dianxiaomi/handoffs", { method: "POST", body: JSON.stringify({ product_ids: ids }) });
    alert(`已加入店小秘队列：${result.queued_count} 个${result.reused_count ? `，${result.reused_count} 个已在队列中` : ""}`);
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; button.textContent = original; }
}

async function editStoreName(storeId) {
  const nameNode = document.querySelector(`[data-store-name="${storeId}"]`);
  if (!nameNode) return;
  const current = nameNode.textContent.trim();
  const name = window.prompt("店铺名称", current);
  if (!name || name.trim() === current) return;
  try { await api(`/api/stores/${storeId}`, { method: "PATCH", body: JSON.stringify({ name: name.trim() }) }); await render(); }
  catch (error) { alert(error.message); }
}

window.addEventListener("hashchange", render);
render();
