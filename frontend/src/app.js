const config = window.MONITOR_CONFIG || { apiBaseUrl: "", mode: "demo" };
const app = document.querySelector("#app");

const demoStores = [
  { name: "监控店铺 A", products: 12, sales: "等待 Backend" },
  { name: "监控店铺 B", products: 8, sales: "等待 Backend" },
];

function render() {
  const route = location.hash.slice(1) || "/dashboard";
  const page = route.startsWith("/stores") ? storesPage() : route.startsWith("/products") ? productsPage() : dashboardPage();
  app.innerHTML = page;
}

function dashboardPage() {
  return `
    <section class="hero">
      <div>
        <p class="eyebrow">GITHUB PAGES DEMO</p>
        <h1>AliExpress 竞品监控</h1>
        <p class="lead">静态前端预览已部署，真实数据仍由本地或生产 FastAPI Backend 提供。</p>
      </div>
      <span class="status-badge">${config.mode === "demo" ? "DEMO / BACKEND_NOT_CONFIGURED" : "BACKEND_CONFIGURED"}</span>
    </section>
    <section class="metric-grid">
      ${metric("活跃店铺", "2", "Demo 数据")}
      ${metric("监控商品", "20", "Demo 数据")}
      ${metric("今日采集", "--", "Backend 未连接")}
      ${metric("估算销量", "--", "公开数据推算")}
    </section>
    <section class="content-section">
      <div class="section-heading"><h2>运行状态</h2><span>GitHub Pages 仅托管静态前端</span></div>
      <div class="notice"><strong>Backend 尚未配置</strong><p>当前页面用于验证 GitHub Pages 部署链路，不会伪造监控数据，也不会访问本地数据库。</p></div>
      <div class="action-row"><a class="button primary" href="#/stores">查看 Demo 店铺</a><a class="button" href="${config.apiBaseUrl || "#"}" ${config.apiBaseUrl ? "target=\"_blank\" rel=\"noreferrer\"" : "aria-disabled=\"true\""}>打开 Backend</a></div>
    </section>
  `;
}

function storesPage() {
  return `<section class="page-heading"><p class="eyebrow">ACTIVE STORES</p><h1>活跃店铺</h1><p>以下为静态 Demo 数据，真实店铺列表需要连接 FastAPI。</p></section><section class="list">${demoStores.map((store) => `<article class="row"><div><strong>${store.name}</strong><span>${store.products} 个监控商品</span></div><b>${store.sales}</b></article>`).join("")}</section>`;
}

function productsPage() {
  return `<section class="page-heading"><p class="eyebrow">MONITORED PRODUCTS</p><h1>监控商品</h1><p>GitHub Pages Demo 不保存或展示真实商品快照。</p></section><div class="empty-state"><strong>Backend 未配置</strong><span>连接生产 API 后，这里将显示商品快照和估算销量。</span></div>`;
}

function metric(label, value, note) {
  return `<article class="metric"><span>${label}</span><strong>${value}</strong><small>${note}</small></article>`;
}

window.addEventListener("hashchange", render);
render();
