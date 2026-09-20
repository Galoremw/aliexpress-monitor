const config = window.MONITOR_CONFIG || { apiBaseUrl: "", mode: "demo" };
const app = document.querySelector("#app");

function api(path, options = {}) {
  if (!config.apiBaseUrl) return Promise.reject(new Error("Backend 尚未配置"));
  return fetch(`${config.apiBaseUrl.replace(/\/$/, "")}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  }).then(async (response) => {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Backend 请求失败 (${response.status})`);
    return data;
  });
}

function currentRoute() { return location.hash.replace(/^#/, "") || "/dashboard"; }

async function render() {
  app.innerHTML = `<div class="loading">正在连接本机 Backend…</div>`;
  try {
    const route = currentRoute();
    if (!config.apiBaseUrl) return renderError("BACKEND_NOT_CONFIGURED", "当前 Pages 构建没有配置 Backend 地址。");
    if (route === "/dashboard") return dashboard();
    if (route === "/stores") return storesPage();
    if (route === "/products") return productsPage();
    if (route === "/pending") return pendingPage();
    const [, type, id] = route.split("/");
    if (type === "stores" && id) return storePage(Number(id));
    if (type === "products" && id) return productPage(Number(id));
    location.hash = "#/dashboard";
  } catch (error) {
    renderError("Backend 连接失败", `${error.message}。请确认本机 Docker/Backend 正在运行，并允许 GitHub Pages 访问 8000 端口。`);
  }
}

async function dashboard() {
  const [status, stores, products] = await Promise.all([
    api("/api/collection/status/today"), api("/api/stores?status=active"), api("/api/products?status=active"),
  ]);
  app.innerHTML = shell("监控台", `<section class="hero"><div><p class="eyebrow">LIVE LOCAL BACKEND</p><h1>AliExpress 竞品监控</h1><p class="lead">GitHub Pages 前端已连接本机 FastAPI Backend。</p></div><span class="status-badge ok">BACKEND_CONNECTED</span></section>${metrics(status)}<section class="section"><div class="section-heading"><h2>活跃店铺</h2><a class="button" href="#/stores">查看全部</a></div><div class="list">${stores.map(storeRow).join("") || empty("暂无活跃店铺")}</div></section><section class="section"><div class="section-heading"><h2>快捷操作</h2></div><div class="action-row"><button class="button primary" data-action="collect-all">立即采集全部活跃商品</button><a class="button" href="#/pending">查看待人工补采</a></div></section>`);
  bindActions();
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
  app.innerHTML = shell("待人工补采", `<section class="page-heading"><div><p class="eyebrow">MANUAL FALLBACK</p><h1>待人工补采</h1><p>自动采集失败的商品需要在 Chrome 商品页主动补采。</p></div></section>${metrics(status)}<div class="list">${pending.map((row) => `<article class="row"><div><a href="#/products/${row.product_id}"><strong>${row.title || row.platform_product_id}</strong></a><span>${row.store_name} · ${row.failure_reason || "自动采集失败"}</span></div><a class="button" href="${row.product_url}" target="_blank" rel="noreferrer">打开商品</a></article>`).join("") || empty("当前没有待人工补采商品")}</div>`);
}

async function storePage(id) {
  const [store, products, metrics, top] = await Promise.all([api(`/api/stores/${id}`), api(`/api/products?store_id=${id}`), api(`/api/stores/${id}/daily-metrics`), api(`/api/stores/${id}/top-products`)]);
  app.innerHTML = shell(store.name, `<section class="page-heading"><div><p class="eyebrow">STORE ${store.aliexpress_store_id || store.id}</p><h1>${store.name}</h1><p>仅汇总已监控商品范围内的公开数据推算。</p></div><div class="action-row"><a class="button" href="${store.url}" target="_blank" rel="noreferrer">打开店铺</a><button class="button primary" data-action="discover" data-id="${id}">发现商品</button></div></section><section class="section"><div class="section-heading"><h2>昨日 Top 商品</h2><span>${top.status}</span></div><div class="list">${(top.products || []).map((product) => `<article class="row"><div><a href="#/products/${product.product_id}"><strong>${product.title || product.aliexpress_product_id}</strong></a><span>${product.url}</span></div><b>估算 ${product.estimated_sales}</b></article>`).join("") || empty("暂无连续快照基线")}</div></section><section class="section"><div class="section-heading"><h2>监控商品</h2><span>${products.length} 个</span></div><div class="list">${products.map((product) => productRow(product, [store])).join("") || empty("暂无商品")}</div></section><section class="section"><div class="section-heading"><h2>店铺日销量估算</h2></div>${metricTable(metrics)}</section>`);
  bindActions();
}

async function productPage(id) {
  const [product, snapshots, metrics] = await Promise.all([api(`/api/products/${id}`), api(`/api/products/${id}/snapshots`), api(`/api/products/${id}/daily-metrics`)]);
  app.innerHTML = shell(product.title || product.aliexpress_product_id, `<section class="page-heading"><div><p class="eyebrow">PRODUCT ${product.aliexpress_product_id}</p><h1>${product.title || "未命名商品"}</h1><p>公开数据快照与估算销量趋势。</p></div><div class="action-row"><a class="button" href="${product.url}" target="_blank" rel="noreferrer">在 Chrome 打开</a><button class="button primary" data-action="collect" data-id="${id}">立即采集</button></div></section><section class="section"><div class="section-heading"><h2>日销量估算</h2><span>非真实后台订单量</span></div>${metricTable(metrics)}</section><section class="section"><div class="section-heading"><h2>原始快照历史</h2><span>只追加，不覆盖</span></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>来源</th><th>状态</th><th>累计 sold</th><th>价格</th><th>评价数</th></tr></thead><tbody>${snapshots.map((snapshot) => `<tr><td>${formatDate(snapshot.captured_at || snapshot.collected_at)}</td><td>${snapshot.source || "AUTO"}</td><td>${snapshot.status || snapshot.parse_status}</td><td>${snapshot.sold_count ?? snapshot.cumulative_sold ?? "—"}</td><td>${snapshot.price ?? snapshot.price_amount ?? "—"}</td><td>${snapshot.review_count ?? "—"}</td></tr>`).join("") || `<tr><td colspan="6">暂无快照</td></tr>`}</tbody></table></div></section>`);
  bindActions();
}

function shell(title, content) { return `<header class="topbar"><a class="brand" href="#/dashboard">AliExpress 竞品监控</a><nav><a href="#/dashboard">监控台</a><a href="#/stores">店铺</a><a href="#/products">商品</a><a href="#/pending">待补采</a></nav></header><main><div class="page-title">${title}</div>${content}</main>`; }
function metrics(s) { return `<section class="metric-grid">${metric("活跃商品", s.total_products, "今日")}${metric("自动成功", s.auto_success, "今日")}${metric("自动失败", s.auto_failed, "今日")}${metric("手动完成", s.manual_completed, "今日")}${metric("待人工补采", s.pending_manual, "当前")}${metric("成功率", `${s.success_rate}%`, "公开数据采集")}</section>`; }
function metric(label, value, note) { return `<article class="metric"><span>${label}</span><strong>${value ?? "—"}</strong><small>${note}</small></article>`; }
function storeRow(store) { return `<article class="row"><div><a href="#/stores/${store.id}"><strong>${store.name}</strong></a><span>${store.aliexpress_store_id || "未识别店铺 ID"} · ${store.status}</span></div><a class="button" href="#/stores/${store.id}">展开</a></article>`; }
function productRow(product, stores) { const store = stores.find((item) => item.id === product.store_id); return `<article class="row"><div><a href="#/products/${product.id}"><strong>${product.title || product.aliexpress_product_id}</strong></a><span>${store?.name || "店铺"} · ${product.aliexpress_product_id}</span></div><button class="button" data-action="collect" data-id="${product.id}">采集</button></article>`; }
function metricTable(rows) { return `<div class="table-wrap"><table><thead><tr><th>日期</th><th>估算销量</th><th>状态</th><th>依据</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${row.metric_date}</td><td>${row.is_estimable ? row.estimated_sales : "不可估算"}</td><td>${row.is_estimable ? "可估算" : "待基线"}</td><td>${row.reason || row.estimate_type || "public_observable_data"}</td></tr>`).join("") || `<tr><td colspan="4">暂无数据</td></tr>`}</tbody></table></div>`; }
function empty(text) { return `<div class="empty-state"><strong>${text}</strong></div>`; }
function formatDate(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—"; }
function renderError(title, detail) { app.innerHTML = `<main class="error-page"><p class="eyebrow">${title}</p><h1>监控台暂时无法连接</h1><p>${detail}</p><p>Backend 地址：<code>${config.apiBaseUrl || "未配置"}</code></p><button class="button primary" onclick="location.reload()">重新连接</button></main>`; }

function bindActions() {
  document.querySelectorAll("[data-action='collect']").forEach((button) => button.addEventListener("click", async () => { await action(button, `/api/products/${button.dataset.id}/collect`, "采集任务已完成"); }));
  document.querySelectorAll("[data-action='collect-all']").forEach((button) => button.addEventListener("click", async () => { await action(button, "/api/jobs/collect-now", "全量采集任务已完成"); }));
  document.querySelectorAll("[data-action='discover']").forEach((button) => button.addEventListener("click", async () => { await action(button, `/api/stores/${button.dataset.id}/discover`, "商品发现任务已完成"); }));
  document.querySelectorAll("[data-action='add-store']").forEach((button) => button.addEventListener("click", addStore));
  document.querySelectorAll("[data-action='add-product']").forEach((button) => button.addEventListener("click", addProduct));
}
async function action(button, path, message) { button.disabled = true; button.textContent = "处理中…"; try { await api(path, { method: "POST" }); alert(message); await render(); } catch (error) { alert(error.message); button.disabled = false; button.textContent = "重试"; } }
async function addStore() { const name = prompt("监控店铺名称（可选）", ""); const url = prompt("AliExpress 店铺链接"); if (!url) return; try { await api("/api/stores", { method: "POST", body: JSON.stringify({ name, url }) }); await render(); } catch (error) { alert(error.message); } }
async function addProduct() { const storeId = prompt("所属店铺 ID"); const url = prompt("AliExpress 商品链接"); if (!storeId || !url) return; try { await api("/api/products", { method: "POST", body: JSON.stringify({ store_id: Number(storeId), url }) }); await render(); } catch (error) { alert(error.message); } }

window.addEventListener("hashchange", render);
render();
