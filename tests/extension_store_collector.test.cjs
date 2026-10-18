const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const targetDate = "2026-09-29";
const productId = "1005001234567890";

function loadStoreCollector({ panelProductId = productId, productTitle = "IXSPY Store Product", dailyValues = [5, 0, -1, 2, 7, 1, 3], staleDailyValues = null, staleHoverSamples = 0, extraProductIds = [], lazyExtraProductIds = [], multiProductCharts = false, moreActionDelayByProductId = {}, moreOutsideMiniChart = false, moreOutsideAncestors = false, moreInsideShadow = false, exactMoreMarkup = false, moreActionDelayMs = 0, chartInjectionDelayMs = 0, trendButtonDelayMs = 0, extraTrendInCard = false, missingMoreAction = false, throwOnMoreClick = false, misalignedChart = false, narrowTooltipTargets = false, firstChartPartial = false, ordersSorted = true, challengePage = false, storeReadinessDelayMs = 0, hasNextPage = false, paginationKnown = true, nextPageUrl = null, paginationEntries = null, pageNumber = 1, unrelatedNextControl = false, visibleStoreId = "123456", routeStoreId = "123456", storeInfoId, storeInfoLate = false, storeInfoOnHover = false, storeInfoHidden = false, storeInfoDelayMs = 0, ixspyStoreName = null, documentTitle = "IXSPY Store - AliExpress", fastClockStepMs = 0, productCardsReadyAfterScans = 0 } = {}) {
  const identityCardStoreId = storeInfoId === undefined ? routeStoreId : storeInfoId;
  let identityPanelVisibleAt = Infinity;
  let miniVisible = false;
  let lazyRevealed = false;
  let panelVisibleAt = Infinity;
  let chartMode = "cumulative_total";
  let fullYearOpened = false;
  let tooltipText = "";
  let dailyHoverSamples = 0;
  let moreClicks = 0;
  let productCardQueryCount = 0;
  let moreActionVisibleAt = 0;
  const extraChartStates = new Map(extraProductIds.map((id) => [id, {
    miniVisible: false,
    moreActionVisibleAt: 0,
  }]));
  let activePanelProductId = panelProductId;
  const trendButtonVisibleAt = Date.now() + trendButtonDelayMs;
  const withheldDate = new Date(Date.parse(`${targetDate}T00:00:00Z`) - 4 * 86400000).toISOString().slice(0, 10);
  const shouldWithhold = (metricDate) => firstChartPartial && moreClicks === 1 && metricDate === withheldDate;
  const rect = (width = 20, height = 20, left = 0, top = 0) => ({ left, top, width, height, right: left + width, bottom: top + height });
  const makeNode = (className = "", text = "", getShown = () => true) => ({
    className,
    innerText: text,
    textContent: text,
    getBoundingClientRect: () => getShown() ? rect(120, 24) : rect(0, 0),
    getAttribute: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
  });

  const link = makeNode("", productTitle);
  link.href = `https://www.aliexpress.com/item/${productId}.html`;
  link.getAttribute = (name) => name === "href" ? link.href : name === "title" ? productTitle : null;
  link.matches = () => false;
  const extraLinks = extraProductIds.map((id) => {
    const node = makeNode("", `IXSPY Store Product ${id}`);
    node.href = `https://www.aliexpress.com/item/${id}.html`;
    node.getAttribute = (name) => name === "href" ? node.href : name === "title" ? `IXSPY Store Product ${id}` : null;
    node.matches = () => false;
    return node;
  });
  const lazyExtraLinks = lazyExtraProductIds.map((id) => {
    const node = makeNode("", `IXSPY Store Product ${id}`);
    node.href = `https://www.aliexpress.com/item/${id}.html`;
    node.getAttribute = (name) => name === "href" ? node.href : name === "title" ? `IXSPY Store Product ${id}` : null;
    node.matches = () => false;
    return node;
  });

  const trendButton = makeNode("zhexiantu");
  trendButton.getAttribute = (name) => name === "onclick" ? `clickSmallChart(${productId},event)` : null;
  trendButton.scrollIntoView = () => {};
  trendButton.click = () => {
    miniVisible = !miniVisible;
    if (miniVisible) {
      moreActionVisibleAt = Date.now() + moreActionDelayMs;
      activePanelProductId = productId;
    }
  };
  const extraTrendButtons = multiProductCharts ? extraProductIds.map((id) => {
    const node = makeNode("zhexiantu");
    node.getAttribute = (name) => name === "onclick" ? `clickSmallChart(${id},event)` : null;
    node.scrollIntoView = () => {};
    node.matches = (selector) => selector === "img.zhexiantu";
    node.click = () => {
      const state = extraChartStates.get(id);
      state.miniVisible = !state.miniVisible;
      if (state.miniVisible) {
        state.moreActionVisibleAt = Date.now() + (moreActionDelayByProductId[id] ?? moreActionDelayMs);
        activePanelProductId = id;
      }
    };
    return node;
  }) : [];

  const panelVisible = () => Date.now() >= panelVisibleAt;
  const totalButton = makeNode("trade-total-button", "总量", panelVisible);
  totalButton.classList = { contains: (name) => name === "btn-active" && chartMode === "cumulative_total" };
  totalButton.click = () => { chartMode = "cumulative_total"; };
  const incrementButton = makeNode("trade-inc-button", "增量", panelVisible);
  incrementButton.classList = { contains: (name) => name === "btn-active" && chartMode === "daily_increment" };
  incrementButton.click = () => { chartMode = "daily_increment"; };
  const yearButton = makeNode("", "查看一整年数据", panelVisible);
  yearButton.getAttribute = (name) => name === "onclick"
    ? `getYearChart('trade_chart','product','',${multiProductCharts ? activePanelProductId : panelProductId})`
    : null;
  yearButton.click = () => { fullYearOpened = true; };

  const tooltip = makeNode("chart-tooltip", "", () => Boolean(tooltipText));
  Object.defineProperty(tooltip, "innerText", { get: () => tooltipText });
  Object.defineProperty(tooltip, "textContent", { get: () => tooltipText });
  tooltip.getAttribute = (name) => name === "role" ? "tooltip" : null;

  const canvas = makeNode("", "", panelVisible);
  canvas.getBoundingClientRect = () => rect(500, 180);
  canvas.dispatchEvent = (event) => {
    if (event.type !== "mousemove" || chartMode !== "daily_increment") return;
    dailyHoverSamples += 1;
    const visibleValues = staleDailyValues && dailyHoverSamples <= staleHoverSamples
      ? staleDailyValues : dailyValues;
    if (misalignedChart || narrowTooltipTargets) {
      const plotPosition = (event.clientX / 500 - 0.12) / 0.83;
      const pointIndex = Math.max(0, Math.min(29, Math.floor(plotPosition * 30)));
      if (pointIndex < 23) {
        tooltipText = "";
        return;
      }
      const pointCenter = 0.12 + ((pointIndex + 0.5) / 30) * 0.83;
      if (narrowTooltipTargets && Math.abs(event.clientX / 500 - pointCenter) * 500 > 3.2) {
        tooltipText = "";
        return;
      }
      const metricDate = new Date(Date.parse(`${targetDate}T00:00:00Z`) - (30 - pointIndex) * 86400000)
        .toISOString().slice(0, 10);
      if (shouldWithhold(metricDate)) {
        tooltipText = "";
        return;
      }
      tooltipText = `${metricDate} ${visibleValues[pointIndex - 23]}`;
      return;
    }
    const pointIndex = Math.min(27, Math.floor((event.clientX / 500) * 28));
    if (pointIndex < 21) return;
    const metricDate = new Date(Date.parse(`${targetDate}T00:00:00Z`) - (28 - pointIndex) * 86400000)
      .toISOString().slice(0, 10);
    if (shouldWithhold(metricDate)) {
      tooltipText = "";
      return;
    }
    tooltipText = `${metricDate} ${visibleValues[pointIndex - 21]}`;
  };
  const chart = {
    querySelector: (selector) => selector === "canvas" ? canvas : null,
    querySelectorAll: (selector) => selector === "div, span, [style]" ? [tooltip] : [],
  };

  const panel = makeNode("sales-180", "产品趋势概览 近一年销量趋势图", panelVisible);
  panel.querySelector = (selector) => ({
    ".trade-inc-button": incrementButton,
    ".trade-total-button": totalButton,
    "#trade_chart": chart,
    "#trade_chart canvas": canvas,
  }[selector] || null);
  panel.querySelectorAll = (selector) => selector === "button, [role='button'], a"
    ? [totalButton, incrementButton, yearButton]
    : selector === "button, [role='button'], a, [tabindex]"
      ? [totalButton, incrementButton, yearButton]
      : selector === "div, span, [style]" ? [tooltip] : [];

  const moreAction = makeNode(exactMoreMarkup ? `look-more small-chart-${productId}` : "", "更多...", () => miniVisible && !missingMoreAction && Date.now() >= moreActionVisibleAt);
  if (exactMoreMarkup) {
    moreAction.getAttribute = (name) => name === "value"
      ? `small-chart-${productId}`
      : name === "onclick" ? "lookMoreChart(this,event)" : null;
  }
  moreAction.click = () => {
    if (throwOnMoreClick) throw new Error("simulated IXSPY interaction failure");
    moreClicks += 1;
    activePanelProductId = productId;
    panelVisibleAt = Date.now() + chartInjectionDelayMs;
  };
  moreAction.matches = (selector) => selector === "a, button, [role='button'], [tabindex]";
  const miniChart = makeNode(`test-small-chart ${productId}_h3 store_product_${productId}`, "近一年销量趋势", () => miniVisible);
  miniChart.querySelectorAll = (selector) => selector === "*" && !moreOutsideMiniChart && !moreOutsideAncestors ? [moreAction] : [];
  const extraMiniCharts = multiProductCharts ? extraProductIds.map((id) => {
    const state = extraChartStates.get(id);
    const node = makeNode(`test-small-chart ${id}_h3 store_product_${id}`, "近一年销量趋势", () => state.miniVisible);
    return node;
  }) : [];
  const extraMoreActions = multiProductCharts ? extraProductIds.map((id) => {
    const state = extraChartStates.get(id);
    const node = makeNode(exactMoreMarkup ? `look-more small-chart-${id}` : "", "更多...", () =>
      state.miniVisible && !missingMoreAction && Date.now() >= state.moreActionVisibleAt);
    if (exactMoreMarkup) {
      node.getAttribute = (name) => name === "value"
        ? `small-chart-${id}`
        : name === "onclick" ? "lookMoreChart(this,event)" : null;
    }
    node.click = () => {
      if (throwOnMoreClick) throw new Error("simulated IXSPY interaction failure");
      moreClicks += 1;
      activePanelProductId = id;
      panelVisibleAt = Date.now() + chartInjectionDelayMs;
    };
    node.matches = (selector) => selector === "a, button, [role='button'], [tabindex]";
    return node;
  }) : [];
  extraMiniCharts.forEach((node, index) => {
    node.querySelectorAll = (selector) => selector === "*" ? [extraMoreActions[index]] : [];
  });
  if (moreOutsideAncestors) {
    miniChart.getBoundingClientRect = () => miniVisible ? rect(254, 80, 200, 100) : rect(0, 0);
    moreAction.getBoundingClientRect = () => miniVisible && !missingMoreAction && Date.now() >= moreActionVisibleAt ? rect(48, 18, 260, 185) : rect(0, 0);
    moreAction.parentElement = makeNode("ixspy-separate-overlay-root");
  }
  const storeOverviewMore = makeNode("", "更多店铺分析数据...", () => true);
  storeOverviewMore.getBoundingClientRect = () => rect(160, 24, 1200, 40);
  const ixspyOverviewHeading = makeNode("", "店铺概览", () => Boolean(ixspyStoreName));
  const ixspyOverviewName = makeNode("store-name", ixspyStoreName || "", () => Boolean(ixspyStoreName));
  const ixspyOverviewPanel = {
    querySelectorAll: (selector) => selector === "h1, h2, h3, [role='heading'], [class*='title' i], [class*='name' i]"
      ? [ixspyOverviewName] : [],
  };
  ixspyOverviewHeading.parentElement = ixspyOverviewPanel;
  ixspyOverviewName.parentElement = ixspyOverviewPanel;
  const storeIdentityHeader = makeNode("store-title", "AMZSTEM Pop Toys Store");
  storeIdentityHeader.dispatchEvent = (event) => {
    if (storeInfoOnHover && event.type === "mouseover") identityPanelVisibleAt = Date.now() + storeInfoDelayMs;
    if (storeInfoOnHover && event.type === "mouseout") identityPanelVisibleAt = Infinity;
    return true;
  };
  const neighborTrendButton = makeNode("zhexiantu");
  neighborTrendButton.getAttribute = (name) => name === "onclick" ? "clickSmallChart(1005009999999999,event)" : null;
  neighborTrendButton.matches = (selector) => selector === "img.zhexiantu";
  const storeCard = makeNode("product-card");
  if (moreInsideShadow) storeCard.shadowRoot = { querySelectorAll: (selector) => selector === "*" ? [moreAction] : [] };
  storeCard.contains = (node) => [trendButton, neighborTrendButton, miniChart, ...extraTrendButtons, ...extraMiniCharts,
    ...(moreOutsideAncestors ? [] : [moreAction, ...extraMoreActions])].includes(node);
  storeCard.querySelectorAll = (selector) => selector === "img.zhexiantu"
    ? [trendButton, ...extraTrendButtons, ...(extraTrendInCard ? [neighborTrendButton] : [])]
    : selector === "*" ? [miniChart, ...extraMiniCharts,
      ...(!moreOutsideAncestors && !moreInsideShadow ? [moreAction, ...extraMoreActions] : []),
      ...(extraTrendInCard ? [neighborTrendButton] : [])] : [];
  trendButton.matches = (selector) => selector === "img.zhexiantu";
  trendButton.parentElement = storeCard;
  miniChart.parentElement = storeCard;
  if (moreOutsideMiniChart) moreAction.parentElement = storeCard;

  const allNodes = [link, ...extraLinks, trendButton, ...extraTrendButtons, ...(extraTrendInCard ? [neighborTrendButton] : []),
    storeCard, miniChart, ...extraMiniCharts, moreAction, ...extraMoreActions, storeOverviewMore,
    ixspyOverviewHeading, ixspyOverviewName,
    panel, totalButton, incrementButton, yearButton, chart, canvas, tooltip];
  const nextPageLink = makeNode("pagination-next", "Next");
  nextPageLink.parentElement = makeNode("pagination");
  nextPageLink.href = nextPageUrl;
  nextPageLink.disabled = !hasNextPage && paginationKnown;
  nextPageLink.getAttribute = (name) => name === "aria-label" ? "Next"
    : name === "aria-disabled" ? (nextPageLink.disabled ? "true" : null)
      : name === "href" ? nextPageUrl : null;
  nextPageLink.matches = (selector) => selector === "a, button, [role='button']";
  const customPaginationControls = paginationEntries?.map((entry) => {
    const node = makeNode(entry.className || "pagination-link", entry.label || "");
    node.href = entry.href || null;
    node.disabled = Boolean(entry.disabled);
    node.parentElement = makeNode(entry.parentClass || "pagination");
    node.getAttribute = (name) => name === "aria-label" ? entry.ariaLabel || null
      : name === "aria-disabled" ? (node.disabled ? "true" : null)
        : name === "rel" ? entry.rel || null
          : name === "href" ? entry.href || null : null;
    node.matches = (selector) => selector === "a, button, [role='button']";
    return node;
  });
  const carouselNext = makeNode("carousel-next", "Next");
  carouselNext.parentElement = makeNode("product-carousel");
  carouselNext.getAttribute = (name) => name === "aria-label" ? "Next" : null;
  const document = {
    title: documentTitle,
    visibilityState: "visible",
    body: {
      get innerText() {
        const showStoreInfo = identityCardStoreId && !storeInfoHidden
          && (!storeInfoOnHover || Date.now() >= identityPanelVisibleAt);
        return challengePage ? "Security verify required"
          : `AliExpress public store listing\nShop${visibleStoreId} Store${showStoreInfo ? `\n${storeInfoLate ? "product card ".repeat(700) : ""}Store ID: ${identityCardStoreId}` : ""}`;
      },
      get textContent() {
        return `${this.innerText}${storeInfoHidden && identityCardStoreId ? `\nStore ID: ${identityCardStoreId}` : ""}`;
      },
    },
    documentElement: {
      scrollHeight: 1000,
      querySelectorAll: (selector) => selector === "*" ? allNodes : [],
    },
    querySelector: (selector) => ({
      "#trade_chart": panelVisible() ? chart : null,
      "#trade_chart canvas": panelVisible() ? canvas : null,
      'iframe[src*="captcha"], iframe[src*="punish"], [class*="captcha" i]': null,
    }[selector] || null),
    querySelectorAll(selector) {
      if (selector.startsWith("h1, h2, [role='heading'], [class*='store' i]")) return [storeIdentityHeader];
      if (selector === "[role='heading'], h1, h2, h3, [class*='title' i], [class*='name' i]"
        || selector === "[class*='title' i], [class*='name' i], [role='heading'], h1, h2, h3") {
        return ixspyStoreName ? [ixspyOverviewHeading, ixspyOverviewName] : [];
      }
      if (selector === "*") return allNodes;
      if (selector === 'a[href*="/item/"], [data-product-id], [data-item-id], [data-product-url], [data-item-url]') {
        productCardQueryCount += 1;
        return productCardQueryCount > productCardsReadyAfterScans
          ? [link, ...extraLinks, ...(lazyRevealed ? lazyExtraLinks : [])]
          : [];
      }
      if (selector.includes("pagination") || selector === 'a[rel="next"]' || selector === "a[href]") return customPaginationControls
        || (hasNextPage || paginationKnown ? [nextPageLink] : []);
      if (selector === "a, button, [role='button']") return [
        ...(customPaginationControls || (hasNextPage || paginationKnown ? [nextPageLink] : [])),
        ...(unrelatedNextControl ? [carouselNext] : []),
      ];
      if (selector === "*") return allNodes;
      if (selector === ".look-more") return exactMoreMarkup ? [moreAction, ...extraMoreActions] : [];
      if (selector === "img.zhexiantu") return Date.now() >= trendButtonVisibleAt
        ? [trendButton, ...extraTrendButtons, ...(extraTrendInCard ? [neighborTrendButton] : [])]
        : [];
      if (selector === ".test-small-chart") return [miniChart, ...extraMiniCharts];
      if (selector === ".sales-180") return panelVisible() ? [panel] : [];
      if (selector === '[role="tooltip"], [class*="tooltip" i]') return [tooltip];
      if (selector === 'button, [role="button"], a, [tabindex]') return panelVisible() ? [totalButton, incrementButton, yearButton] : [];
      return [];
    },
  };
  let currentOrdersSorted = ordersSorted;
  const location = {
    get href() {
      return `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?${currentOrdersSorted ? "shop_sortType=orders_desc" : "shop_sortType=bestmatch_sort"}${pageNumber > 1 ? `&page=${pageNumber}` : ""}`;
    },
    get pathname() { return `/store/${routeStoreId}/pages/all-items.html`; },
  };
  if (storeReadinessDelayMs > 0) {
    setTimeout(() => { currentOrdersSorted = true; }, storeReadinessDelayMs);
  }
  const context = {
    document,
    window: { scrollTo: () => { lazyRevealed = true; } },
    location,
    URL,
    Date: fastClockStepMs ? class extends Date {
      static now() {
        this.offset = (this.offset || 0) + fastClockStepMs;
        return Date.now() + this.offset;
      }
    } : Date,
    Map,
    Set,
    Math,
    Number,
    String,
    RegExp,
    Promise,
    MutationObserver: class { observe() {} disconnect() {} },
    MouseEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    getComputedStyle: (node) => ({
      display: node.getBoundingClientRect().height ? "block" : "none",
      visibility: "visible",
      opacity: "1",
    }),
    setTimeout: (callback) => setTimeout(callback, 1),
    clearTimeout,
    globalThis: null,
  };
  context.globalThis = context;
  const source = fs.readFileSync(path.join(__dirname, "..", "extension", "page_collector.js"), "utf8");
  vm.runInNewContext(source, context, { filename: "page_collector.js" });
  const collector = context.AliExpressMonitorPageCollector;
  Object.defineProperties(collector, {
    fullYearOpened: { get: () => fullYearOpened },
    moreClicks: { get: () => moreClicks },
    miniVisible: { get: () => miniVisible },
  });
  return collector;
}

test("scheduled store page reads only claimed IDs and returns a safe next Orders page", async () => {
  const nextPageUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?page=2&shop_sortType=orders_desc";
  const collector = loadStoreCollector({
    extraProductIds: ["1005009999999999"], hasNextPage: true, nextPageUrl,
  });
  const result = await collector.collect("STORE", targetDate, {
    monitored_product_ids: [productId, "1005008888888888"],
  });

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.products.map((product) => product.platform_product_id))), [productId]);
  assert.equal(result.page_result.next_page_url, nextPageUrl);
  assert.equal(collector.moreClicks, 1);
});

test("monitored store waits for product cards and reports page matching and chart outcomes", async () => {
  const collector = loadStoreCollector({ productCardsReadyAfterScans: 4, fastClockStepMs: 250 });
  const result = await collector.collect("STORE", targetDate, {
    monitored_product_ids: [productId, "1005008888888888"],
  });

  assert.equal(result.state, "ready");
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.page_result.products.map((product) => product.platform_product_id))),
    [productId],
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.page_result.diagnostics.page_scan.matched_product_ids)),
    [productId],
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.page_result.diagnostics.page_scan.unmatched_target_ids)),
    ["1005008888888888"],
  );
  assert.equal(result.page_result.diagnostics.page_scan.product_cards_ready, true);
  assert.equal(result.page_result.diagnostics.page_scan.candidate_node_count, 1);
  assert.equal(result.page_result.diagnostics.chart_reads[0].product_id, productId);
  assert.equal(result.page_result.diagnostics.chart_reads[0].outcome, "complete");
  assert.equal(result.page_result.diagnostics.chart_reads[0].point_count, 7);
});

test("maps a unique exact-title product ID change back to its monitored record", async () => {
  const monitoredId = "1005010587192858";
  const observedUrl = `https://www.aliexpress.com/item/${productId}.html`;
  const monitoredUrl = `https://www.aliexpress.com/item/${monitoredId}.html`;
  const collector = loadStoreCollector({ productTitle: "No-Drill Toothbrush Holder" });
  const result = await collector.collect("STORE", targetDate, {
    monitored_product_ids: [monitoredId],
    monitored_products: [{
      platform_product_id: monitoredId,
      title: "No-Drill Toothbrush Holder",
      url: monitoredUrl,
    }],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.products[0].platform_product_id, monitoredId);
  assert.equal(result.page_result.products[0].url, monitoredUrl);
  assert.equal(result.page_result.products[0].observed_product_id, productId);
  assert.equal(result.page_result.products[0].observed_url, observedUrl);
  assert.equal(result.page_result.products[0].identity_match_method, "exact_title");
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.page_result.diagnostics.page_scan.identity_matches)),
    [{
      monitored_product_id: monitoredId,
      observed_product_id: productId,
      match_method: "exact_title",
      page_url: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc",
    }],
  );
  assert.equal(result.page_result.diagnostics.chart_reads[0].product_id, monitoredId);
  assert.equal(result.page_result.diagnostics.chart_reads[0].observed_product_id, productId);
});

test("does not title-map when multiple monitored products share the same title", async () => {
  const monitoredIds = ["1005010587192858", "1005010588156092"];
  const result = await loadStoreCollector({ productTitle: "No-Drill Toothbrush Holder" }).collect("STORE", targetDate, {
    monitored_product_ids: monitoredIds,
    monitored_products: monitoredIds.map((id) => ({
      platform_product_id: id,
      title: "No-Drill Toothbrush Holder",
      url: `https://www.aliexpress.com/item/${id}.html`,
    })),
  });

  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.products)), []);
  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.diagnostics.page_scan.matched_product_ids)), []);
  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.diagnostics.chart_reads)), []);
});

test("scheduled store collection checkpoints each product immediately after its chart read", async () => {
  const checkpoints = [];
  const collector = loadStoreCollector();
  const result = await collector.collect("STORE", targetDate, {
    monitored_product_ids: [productId],
    on_product_collected: async (product) => checkpoints.push(product),
  });

  assert.equal(result.state, "ready");
  assert.equal(checkpoints.length, 1);
  assert.equal(checkpoints[0].platform_product_id, productId);
  assert.equal(checkpoints[0].history_daily_capture_complete, true);
});

test("scheduled store collection waits for the Orders page to finish client-side navigation", async () => {
  const collector = loadStoreCollector({ ordersSorted: false, storeReadinessDelayMs: 15 });
  const result = await collector.collect("STORE", targetDate, {
    monitored_product_ids: [productId],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.platform_store_id, "123456");
  assert.equal(result.page_result.products[0].platform_product_id, productId);
});

test("scheduled store page never follows a foreign or unsorted next link", async () => {
  const collector = loadStoreCollector({
    hasNextPage: true,
    nextPageUrl: "https://www.aliexpress.com/store/999999/pages/all-items.html?page=2&shop_sortType=orders_desc",
  });
  const result = await collector.collect("STORE", targetDate, {
    monitored_product_ids: [productId],
  });
  assert.equal(result.page_result.next_page_url, null);
  assert.equal(result.page_result.has_next_page, true);
});

test("reads seven signed IXSPY increments from the store card chart and never opens the year view", async () => {
  const collector = loadStoreCollector();
  const result = await collector.collect("STORE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.history_products_complete, 1);
  assert.equal(result.payload.products[0].history_daily_capture_complete, true);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.payload.products[0].historical_sales.map((point) => point.value))),
    [5, 0, -1, 2, 7, 1, 3],
  );
  assert.equal(collector.moreClicks, 1);
  assert.equal(collector.fullYearOpened, false);
  assert.equal(collector.miniVisible, false);
});

test("does not admit a high near-flat cumulative series with small corrections into store selection", async () => {
  const suspectSeries = [
    [26170, 26213, 26257, 26245, 26202, 26262, 26263],
    [13622, 13662, 13695, 13706, 13749, 13777, 13819],
    [10035, 10091, 10154, 10204, 10289, 10394, 10469],
    [5935, 6033, 6069, 6116, 6214, 6312, 6335],
  ];
  for (const dailyValues of suspectSeries) {
    const result = await loadStoreCollector({ dailyValues }).collect("STORE_SELECT_PAGE", targetDate);
    assert.equal(result.state, "ready");
    assert.equal(result.page_result.candidates[0].history_daily_capture_complete, false);
    assert.equal(result.page_result.candidates[0].historical_sales.length, 0);
    assert.equal(result.page_result.candidates[0].history_source, "ixspy_cumulative_like_series_rejected");
  }
});

test("keeps strongly varying high daily increments when the seven-day chart is readable", async () => {
  const result = await loadStoreCollector({
    dailyValues: [1200, 3400, 1700, 4100, 2400, 3200, 1800],
  }).collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.candidates[0].history_daily_capture_complete, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.candidates[0].historical_sales.map((point) => point.value))),
    [1200, 3400, 1700, 4100, 2400, 3200, 1800]);
});

test("waits through a stale cumulative chart before accepting the rendered increments", async () => {
  const dailyValues = [40, 43, 44, 0, 60, 1, 2];
  const result = await loadStoreCollector({
    dailyValues,
    staleDailyValues: [26170, 26213, 26257, 26245, 26202, 26262, 26263],
    staleHoverSamples: 35,
  }).collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.candidates[0].history_daily_capture_complete, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.candidates[0].historical_sales.map((point) => point.value))), dailyValues);
});

test("uses IXSPY's product-specific look-more element and onclick", async () => {
  const collector = loadStoreCollector({ exactMoreMarkup: true, moreOutsideAncestors: true });
  const result = await collector.collect("STORE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.products[0].history_daily_capture_complete, true);
  assert.equal(result.payload.products[0].historical_sales.length, 7);
  assert.equal(collector.moreClicks, 1);
});

test("store selection retry leaves previously complete cards unread", async () => {
  const collector = loadStoreCollector({ extraProductIds: ["1005001234567891"], fastClockStepMs: 1000 });
  const result = await collector.collect("STORE_SELECT_PAGE", targetDate, {
    skip_product_ids: [productId],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.candidates.length, 2);
  assert.equal(result.page_result.candidates[0].historical_sales, undefined);
  assert.equal(result.page_result.candidates[1].history_daily_capture_complete, false);
  assert.equal(collector.moreClicks, 0);
});

test("waits for IXSPY to inject the product-specific more action", async () => {
  const result = await loadStoreCollector({
    exactMoreMarkup: true,
    moreOutsideAncestors: true,
    moreActionDelayMs: 50,
  }).collect("STORE_TEST", targetDate);

  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.points.length, 7);
});

test("waits through a slow first-product IXSPY more-action injection", async () => {
  const collector = loadStoreCollector({
    exactMoreMarkup: true,
    moreOutsideAncestors: true,
    moreActionDelayMs: 1200,
  });
  const result = await collector.collect("STORE_TEST", targetDate);

  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.points.length, 7);
  assert.equal(result.test_result.diagnostics.retry_passes, 0);
  assert.equal(collector.moreClicks, 1);
});

test("waits for a delayed IXSPY chart panel instead of treating it as unavailable", async () => {
  const result = await loadStoreCollector({ chartInjectionDelayMs: 2300 })
    .collect("STORE_TEST", targetDate);

  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.points.length, 7);
  assert.equal(result.test_result.diagnostics.retry_passes, 0);
});

test("waits for IXSPY to inject the first product's trend button", async () => {
  const collector = loadStoreCollector({ trendButtonDelayMs: 100 });
  const result = await collector.collect("STORE_TEST", targetDate);

  assert.equal(result.test_result.platform_product_id, productId);
  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.diagnostics.retry_passes, 0);
  assert.equal(Object.hasOwn(result, "payload"), false);
});

test("retries a partial store-chart read with dense recent-range sampling", async () => {
  const result = await loadStoreCollector({ misalignedChart: true }).collect("STORE_TEST", targetDate);

  assert.equal(result.test_result.complete, true);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.test_result.points.map((point) => point.value))),
    [5, 0, -1, 2, 7, 1, 3],
  );
  assert.ok(result.test_result.diagnostics.sampled_positions > 14);
  assert.equal(result.test_result.diagnostics.attempts, 2);
});

test("uses dense recent-date sampling for narrow store-chart hover targets", async () => {
  const result = await loadStoreCollector({ misalignedChart: true, narrowTooltipTargets: true })
    .collect("STORE_TEST", targetDate);

  assert.equal(result.test_result.complete, true);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.test_result.points.map((point) => point.value))),
    [5, 0, -1, 2, 7, 1, 3],
  );
  assert.ok(result.test_result.diagnostics.sampled_positions >= 119);
  assert.equal(result.test_result.diagnostics.attempts, 2);
});

test("rejects a store chart whose visible IXSPY product ID differs from the card", async () => {
  const collector = loadStoreCollector({ panelProductId: "1005009999999999" });
  const result = await collector.collect("STORE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.products[0].history_daily_capture_complete, false);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.products[0].historical_sales)), []);
  assert.equal(result.payload.products[0].history_source, "ixspy_store_product_chart_unavailable");
  assert.equal(collector.fullYearOpened, false);
});

test("store-page diagnostic reads one card without creating a submission payload", async () => {
  const collector = loadStoreCollector();
  const result = await collector.collect("STORE_TEST", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.test_result.collector_version, "0.6.103");
  assert.equal(result.test_result.platform_product_id, productId);
  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.points.length, 7);
  assert.equal(result.test_result.diagnostics.chart_product_id, productId);
  assert.equal(result.test_result.diagnostics.daily_mode_selected, true);
  assert.equal(result.test_result.diagnostics.tooltip_hit_count, 7);
  assert.equal(result.test_result.diagnostics.observed_points.length, 7);
  assert.equal(result.test_result.diagnostics.tooltip_samples.length, 4);
  assert.equal(Object.hasOwn(result, "payload"), false);
});

test("store-page diagnostic advances through loaded products and wraps without submission", async () => {
  const extraProductIds = ["1005002222222222", "1005003333333333"];
  const collector = loadStoreCollector({ extraProductIds });
  const results = [];
  for (let attempt = 0; attempt < 4; attempt += 1) {
    results.push(await collector.collect("STORE_TEST", targetDate));
  }

  assert.deepEqual(results.map((item) => item.test_result.platform_product_id),
    [productId, ...extraProductIds, productId]);
  assert.deepEqual(results.map((item) => item.test_result.product_index), [1, 2, 3, 1]);
  assert.deepEqual(results.map((item) => item.test_result.product_count), [3, 3, 3, 3]);
  assert.equal(results[0].test_result.complete, true);
  assert.equal(results[1].test_result.source, "ixspy_store_card_trend_button_missing");
  assert.equal(results[2].test_result.source, "ixspy_store_card_trend_button_missing");
  assert.ok(results.every((item) => !Object.hasOwn(item, "payload")));
});

test("scheduled store collection continues waiting for a late IXSPY action on later products", async () => {
  const laterProductId = "1005001234567891";
  const collector = loadStoreCollector({
    extraProductIds: [laterProductId],
    multiProductCharts: true,
    moreActionDelayByProductId: { [laterProductId]: 5200 },
    exactMoreMarkup: true,
  });

  const result = await collector.collect("STORE", targetDate, {
    monitored_product_ids: [productId, laterProductId],
  });
  const reads = result.page_result.diagnostics.chart_reads;

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(reads.map((read) => read.product_id))), [productId, laterProductId]);
  assert.deepEqual(JSON.parse(JSON.stringify(reads.map((read) => read.outcome))), ["complete", "complete"]);
  assert.equal(reads[1].retry_passes, 1);
  assert.ok(reads[1].more_action_wait_ms >= 5000);
  assert.equal(collector.moreClicks, 2);
});

test("automatically reopens and rereads the chart once when the first test is partial", async () => {
  const collector = loadStoreCollector({ firstChartPartial: true });
  const result = await collector.collect("STORE_TEST", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.diagnostics.retry_passes, 1);
  assert.equal(result.test_result.points.length, 7);
  assert.equal(collector.moreClicks, 2);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.test_result.points.map((point) => point.value))),
    [5, 0, -1, 2, 7, 1, 3],
  );
});

test("scheduled store collection retries a partial chart before submitting product history", async () => {
  const collector = loadStoreCollector({ firstChartPartial: true });
  const result = await collector.collect("STORE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.history_products_complete, 1);
  assert.equal(result.payload.products[0].history_daily_capture_complete, true);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.payload.products[0].historical_sales.map((point) => point.value))),
    [5, 0, -1, 2, 7, 1, 3],
  );
  assert.equal(collector.moreClicks, 2);
});

test("store selection page reads candidate IXSPY charts without submitting an enrollment payload", async () => {
  const result = await loadStoreCollector({ hasNextPage: true }).collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.platform_store_id, "123456");
  assert.equal(result.page_result.candidates.length, 1);
  assert.equal(result.page_result.candidates[0].history_daily_capture_complete, true);
  assert.equal(result.page_result.candidates[0].historical_sales.length, 7);
  assert.equal(result.page_result.has_next_page, true);
  assert.equal(Object.hasOwn(result, "payload"), false);
});

test("store selection reads every already-loaded product instead of truncating at thirty", async () => {
  const extraProductIds = Array.from({ length: 30 }, (_, index) =>
    `100500${String(2000000000 + index)}`);
  const result = await loadStoreCollector({ extraProductIds, fastClockStepMs: 1000 })
    .collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.raw_data.visible_link_count, 31);
  assert.equal(result.page_result.candidates.length, 31);
  assert.equal(Object.hasOwn(result, "payload"), false);
});

test("store selection scrolls beyond thirty to include later lazy-loaded products", async () => {
  const extraProductIds = Array.from({ length: 29 }, (_, index) =>
    `100500${String(2100000000 + index)}`);
  const result = await loadStoreCollector({
    extraProductIds,
    lazyExtraProductIds: ["1005002200000000"],
    fastClockStepMs: 1000,
  }).collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.raw_data.visible_link_count, 31);
  assert.equal(result.page_result.candidates.length, 31);
  assert.equal(result.page_result.candidates.at(-1).platform_product_id, "1005002200000000");
});

test("store selection page refuses to rank candidates unless Orders sorting is active", async () => {
  const result = await loadStoreCollector({ ordersSorted: false }).collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "failed");
  assert.match(result.message, /Orders/i);
});

test("scheduled candidate scan reads the current Orders page and diagnoses missing charts without zeroes", async () => {
  const collector = loadStoreCollector({ extraProductIds: ["1005009999999999"] });
  assert.equal(typeof collector.collectStoreCandidatePage, "function");
  const result = await collector.collectStoreCandidatePage({
    targetDate,
    expectedStoreId: "123456",
    expectedPageUrl: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc",
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.scan_complete, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.candidates.map((candidate) => candidate.platform_product_id))),
    [productId, "1005009999999999"]);
  assert.equal(result.page_result.candidates[0].historical_sales.length, 7);
  assert.equal(result.page_result.candidates[1].history_daily_capture_complete, false);
  assert.equal(result.page_result.candidates[1].historical_sales.length, 0);
  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.diagnostics.chart_reads.map((read) => read.product_id))),
    [productId, "1005009999999999"]);
  assert.equal(result.page_result.diagnostics.chart_reads[1].outcome, "unavailable");
});

test("candidate scan keeps pagination unknown when the page exposes no pagination controls", async () => {
  const result = await loadStoreCollector({ paginationKnown: false }).collectStoreCandidatePage({
    targetDate,
    expectedStoreId: "123456",
    expectedPageUrl: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc",
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.pagination_known, false);
  assert.equal(result.page_result.has_next_page, false);
  assert.equal(result.page_result.scan_complete, true);
});

test("scheduled candidate scan sends a wrong store to review and rejects invalid sort or cursor URLs", async () => {
  const expectedPageUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const mismatch = await loadStoreCollector({ visibleStoreId: "1103845415" }).collectStoreCandidatePage({
    targetDate, expectedStoreId: "123456", expectedPageUrl,
  });
  assert.equal(mismatch.state, "identity_review");
  assert.equal(mismatch.error_type, "store_identity_mismatch");
  assert.match(mismatch.message, /店铺 ID/);

  const wrongSort = await loadStoreCollector({ ordersSorted: false }).collectStoreCandidatePage({
    targetDate, expectedStoreId: "123456", expectedPageUrl,
  });
  assert.equal(wrongSort.state, "failed");
  assert.match(wrongSort.message, /Orders/);

  const wrongUrl = await loadStoreCollector().collectStoreCandidatePage({
    targetDate,
    expectedStoreId: "123456",
    expectedPageUrl: `${expectedPageUrl}&page=2`,
  });
  assert.equal(wrongUrl.state, "failed");
  assert.match(wrongUrl.message, /候选页地址/);

});

test("candidate scan rejects an over-limit page without forwarding partial candidates", async () => {
  const collector = loadStoreCollector({
    extraProductIds: Array.from({ length: 50 }, (_, index) => `100500999999${String(index).padStart(4, "0")}`),
  });
  const result = await collector.collectStoreCandidatePage({
    targetDate,
    expectedStoreId: "123456",
    expectedPageUrl: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc",
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.scan_complete, false);
  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.candidates)), []);
  assert.equal(result.page_result.diagnostics.outcome, "candidate_limit_exceeded");
});

test("store collectors reject an alias URL when the visible storefront ID differs", async () => {
  for (const mode of ["STORE_SELECT_PAGE", "STORE", "STORE_TEST"]) {
    const result = await loadStoreCollector({ visibleStoreId: "1103845415" }).collect(mode, targetDate);

    assert.equal(result.state, "identity_review");
    assert.equal(result.error_type, "store_identity_mismatch");
    assert.match(result.message, /123456/);
    assert.match(result.message, /1103845415/);
    assert.equal(Object.hasOwn(result, "payload"), false);
    assert.equal(Object.hasOwn(result, "page_result"), false);
  }
});

test("store identity check is lightweight and catches a saved submission on an alias URL", async () => {
  const collector = loadStoreCollector({ visibleStoreId: "1103845415" });
  const result = await collector.collect("STORE_IDENTITY", targetDate);

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.match(result.message, /1103845415/);
  assert.equal(collector.moreClicks, 0);
});

test("identity review reports the actual URL and structured evidence including the IXSPY store name", async () => {
  const routeStoreId = "1105182474";
  const result = await loadStoreCollector({
    routeStoreId,
    visibleStoreId: "",
    storeInfoId: null,
    documentTitle: "AMZSTEM Pop Toys Store - AliExpress",
    ixspyStoreName: "AMZSTEM Pop Toys Store",
  }).collect("STORE_IDENTITY", targetDate, { expected_store_id: "1105179516" });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_unverified");
  assert.equal(result.page_url, `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`);
  assert.equal(result.evidence.route_store_id, routeStoreId);
  assert.equal(result.evidence.heading_store_id, null);
  assert.deepEqual(JSON.parse(JSON.stringify(result.evidence.labeled_store_ids)), []);
  assert.equal(result.evidence.page_title, "AMZSTEM Pop Toys Store - AliExpress");
  assert.equal(result.evidence.ixspy_store_name, "AMZSTEM Pop Toys Store");
  assert.equal(result.evidence.identity_method, "unverified");
});

test("identity review reports a null IXSPY store name when no overview name is visible", async () => {
  const result = await loadStoreCollector({
    routeStoreId: "1105182474",
    visibleStoreId: "",
    storeInfoId: null,
  }).collect("STORE_IDENTITY", targetDate, { expected_store_id: "1105179516" });

  assert.equal(result.state, "identity_review");
  assert.equal(result.evidence.ixspy_store_name, null);
});

test("backend-approved URL alias is accepted only when listed for the current store", async () => {
  const routeStoreId = "1105182474";
  const alias = {
    route_store_id: routeStoreId,
    normalized_page_identity: `www.aliexpress.com/store/${routeStoreId}/pages/all-items.html`,
    evidence: { ixspy_store_name: "AMZSTEM Pop Toys Store" },
  };
  const collector = loadStoreCollector({
    routeStoreId,
    visibleStoreId: "",
    storeInfoId: null,
    ixspyStoreName: " AMZSTEM   Pop Toys Store ",
  });
  const result = await collector.collect("STORE_IDENTITY", targetDate, {
    expected_store_id: "1105179516",
    approved_store_url_identities: [alias],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.platform_store_id, "1105179516");
  assert.equal(result.store_identity.identity_method, "backend_approved_url_alias");
});

test("approved URL alias remains under review when the IXSPY store name conflicts", async () => {
  const routeStoreId = "1105182474";
  const result = await loadStoreCollector({
    routeStoreId,
    visibleStoreId: "",
    storeInfoId: null,
    ixspyStoreName: "Different Store Name",
  }).collect("STORE_IDENTITY", targetDate, {
    expected_store_id: "1105179516",
    approved_store_url_identities: [{
      route_store_id: routeStoreId,
      normalized_page_identity: `www.aliexpress.com/store/${routeStoreId}/pages/all-items.html`,
      evidence: { ixspy_store_name: "AMZSTEM Pop Toys Store" },
    }],
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.equal(result.evidence.ixspy_store_name, "Different Store Name");
});

test("unapproved route alias returns review evidence before any product chart is read", async () => {
  const collector = loadStoreCollector({
    routeStoreId: "1105182474",
    visibleStoreId: "",
    storeInfoId: null,
    ixspyStoreName: "AMZSTEM Pop Toys Store",
  });
  const result = await collector.collect("STORE", targetDate, {
    expected_store_id: "1105179516",
    monitored_product_ids: [productId],
    approved_store_url_identities: [],
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.page_url, "https://www.aliexpress.com/store/1105182474/pages/all-items.html?shop_sortType=orders_desc");
  assert.equal(collector.moreClicks, 0);
  assert.equal(Object.hasOwn(result, "page_result"), false);
});

test("an unread Store ID on a redirected route is reported as unverified, not a proven mismatch", async () => {
  const result = await loadStoreCollector({
    routeStoreId: "1105182474",
    visibleStoreId: "",
    storeInfoId: null,
    documentTitle: "AMZSTEM Pop Toys Store - AliExpress",
  }).collect("STORE_IDENTITY", targetDate, { expected_store_id: "1105179516" });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_unverified");
  assert.match(result.message, /网址路由 ID 1105182474/);
  assert.match(result.message, /页面 Store ID 未识别，无法确认是否为同一店铺/);
  assert.doesNotMatch(result.message, /页面显示店铺 ID 未识别 不一致/);
});

test("store collectors reject a conflicting explicit Store ID before reading product charts", async () => {
  const collector = loadStoreCollector({ storeInfoId: "123457" });
  const result = await collector.collect("STORE", targetDate, {
    monitored_product_ids: [productId],
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.match(result.message, /123456/);
  assert.match(result.message, /123457/);
  assert.equal(collector.moreClicks, 0);
  assert.equal(Object.hasOwn(result, "page_result"), false);
});

test("store identity accepts an explicit Store ID that matches the URL ID", async () => {
  const result = await loadStoreCollector({ storeInfoId: "123456" }).collect("STORE_IDENTITY", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.platform_store_id, "123456");
});

test("store identity opens the info card and reads its ID when the route matches", async () => {
  const result = await loadStoreCollector({
    storeInfoId: "123456",
    storeInfoOnHover: true,
  }).collect("STORE_IDENTITY", targetDate, { expected_store_id: "123456" });

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.store_identity.labeled_store_ids)), ["123456"]);
});

test("store identity rejects a matching route when the info card shows another ID", async () => {
  const result = await loadStoreCollector({
    storeInfoId: "123457",
    storeInfoOnHover: true,
  }).collect("STORE_IDENTITY", targetDate, { expected_store_id: "123456" });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.match(result.message, /123457/);
});

test("store identity does not trust a matching route when the info card ID is unreadable", async () => {
  const result = await loadStoreCollector({ storeInfoId: null }).collect("STORE_IDENTITY", targetDate, {
    expected_store_id: "123456",
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_unverified");
});

test("store collection checks a hovered Store ID card before reading charts", async () => {
  const collector = loadStoreCollector({
    storeInfoId: "123457",
    storeInfoOnHover: true,
  });
  const result = await collector.collect("STORE", targetDate, {
    monitored_product_ids: [productId],
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.equal(collector.moreClicks, 0);
  assert.equal(Object.hasOwn(result, "page_result"), false);
});

test("scheduled collection accepts an AliExpress route alias when the info card Store ID matches the task", async () => {
  const expectedStoreId = "1105161198";
  const routeStoreId = "1105170187";
  const result = await loadStoreCollector({
    routeStoreId,
    visibleStoreId: expectedStoreId,
    storeInfoId: expectedStoreId,
  }).collect("STORE", targetDate, {
    expected_store_id: expectedStoreId,
    monitored_product_ids: [productId],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.platform_store_id, expectedStoreId);
  assert.equal(new URL(result.page_result.url).pathname, `/store/${routeStoreId}/pages/all-items.html`);
});

test("store identity probe reveals Store ID on hover and preserves proof for follow-up scans", async () => {
  const expectedStoreId = "1105179516";
  const routeStoreId = "1105182474";
  const collector = loadStoreCollector({
    routeStoreId,
    visibleStoreId: "",
    storeInfoId: expectedStoreId,
    storeInfoOnHover: true,
  });

  const identity = await collector.collect("STORE_IDENTITY", targetDate, {
    expected_store_id: expectedStoreId,
  });
  assert.equal(identity.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(identity.store_identity.labeled_store_ids)), [expectedStoreId]);

  const pageUrl = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const scan = await collector.collectStoreCandidatePage({
    targetDate,
    expectedStoreId,
    expectedPageUrl: pageUrl,
  });
  assert.equal(scan.state, "ready");
  assert.equal(scan.page_result.platform_store_id, expectedStoreId);
  assert.deepEqual(JSON.parse(JSON.stringify(scan.page_result.store_identity.labeled_store_ids)), [expectedStoreId]);
});

test("store identity probe waits for a delayed Store ID popover on an AliExpress route alias", async () => {
  const expectedStoreId = "1105179516";
  const collector = loadStoreCollector({
    routeStoreId: "1105182474",
    visibleStoreId: "",
    storeInfoId: expectedStoreId,
    storeInfoOnHover: true,
    storeInfoDelayMs: 450,
  });

  const result = await collector.collect("STORE_IDENTITY", targetDate, {
    expected_store_id: expectedStoreId,
  });

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.store_identity.labeled_store_ids)), [expectedStoreId]);
});

test("store identity can read an explicitly labeled ID from a hidden AliExpress info panel", async () => {
  const expectedStoreId = "1105179516";
  const result = await loadStoreCollector({
    routeStoreId: "1105182474",
    visibleStoreId: "",
    storeInfoId: expectedStoreId,
    storeInfoHidden: true,
    storeInfoOnHover: true,
  }).collect("STORE_IDENTITY", targetDate, { expected_store_id: expectedStoreId });

  assert.equal(result.state, "ready");
  assert.deepEqual(JSON.parse(JSON.stringify(result.store_identity.labeled_store_ids)), [expectedStoreId]);
});

test("store identity probe rejects a delayed Store ID popover that conflicts with the task", async () => {
  const expectedStoreId = "1105179516";
  const collector = loadStoreCollector({
    routeStoreId: "1105182474",
    visibleStoreId: "",
    storeInfoId: "1105999999",
    storeInfoOnHover: true,
    storeInfoDelayMs: 450,
  });

  const result = await collector.collect("STORE_IDENTITY", targetDate, {
    expected_store_id: expectedStoreId,
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.match(result.message, /1105999999/);
});

test("scheduled collection accepts a route alias when the explicit Store ID matches the task", async () => {
  const expectedStoreId = "1105179516";
  const routeStoreId = "1105182474";
  const result = await loadStoreCollector({
    routeStoreId,
    visibleStoreId: "",
    storeInfoId: expectedStoreId,
  }).collect("STORE", targetDate, {
    expected_store_id: expectedStoreId,
    monitored_product_ids: [productId],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.platform_store_id, expectedStoreId);
  assert.equal(result.page_result.store_identity.heading_store_id, null);
  assert.deepEqual(JSON.parse(JSON.stringify(result.page_result.store_identity.labeled_store_ids)), [expectedStoreId]);
});

test("scheduled collection accepts a hidden Store ID only with page-bound backend verification", async () => {
  const expectedStoreId = "1105179516";
  const routeStoreId = "1105182474";
  const pageUrl = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const result = await loadStoreCollector({
    routeStoreId,
    visibleStoreId: "",
    storeInfoId: expectedStoreId,
    storeInfoHidden: true,
  }).collect("STORE", targetDate, {
    expected_store_id: expectedStoreId,
    monitored_product_ids: [productId],
    verified_store_identity: {
      target_store_id: expectedStoreId,
      route_store_id: routeStoreId,
      page_url: pageUrl,
      heading_store_id: expectedStoreId,
      labeled_store_ids: [expectedStoreId],
    },
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.platform_store_id, expectedStoreId);
  assert.equal(result.page_result.store_identity.route_store_id, routeStoreId);
});

test("a matching explicit Store ID does not override a conflicting store heading", async () => {
  const expectedStoreId = "1105179516";
  const collector = loadStoreCollector({
    routeStoreId: "1105182474",
    visibleStoreId: "1105999999",
    storeInfoId: expectedStoreId,
  });

  const result = await collector.collect("STORE_IDENTITY", targetDate, {
    expected_store_id: expectedStoreId,
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.match(result.message, /1105999999/);
});

test("scheduled collection does not trust the browser title when the info card Store ID is unreadable", async () => {
  const expectedStoreId = "1105161198";
  const routeStoreId = "1105170187";
  const result = await loadStoreCollector({
    routeStoreId,
    visibleStoreId: "",
    storeInfoId: null,
    documentTitle: `Shop${expectedStoreId} Store - AliExpress`,
  }).collect("STORE", targetDate, {
    expected_store_id: expectedStoreId,
    monitored_product_ids: [productId],
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_unverified");
});

test("scheduled collection rejects a route alias when an explicit Store ID conflicts with the task", async () => {
  const expectedStoreId = "1105161198";
  const collector = loadStoreCollector({
    routeStoreId: "1105170187",
    visibleStoreId: expectedStoreId,
    storeInfoId: "1105999999",
  });

  const result = await collector.collect("STORE_IDENTITY", targetDate, {
    expected_store_id: expectedStoreId,
  });

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.match(result.message, /1105999999/);
});

test("store identity scans the full visible text for a late Store ID label", async () => {
  const result = await loadStoreCollector({ storeInfoId: "123457", storeInfoLate: true })
    .collect("STORE_IDENTITY", targetDate);

  assert.equal(result.state, "identity_review");
  assert.equal(result.error_type, "store_identity_mismatch");
  assert.match(result.message, /123457/);
});

test("candidate page scan resumes completed chart reads instead of reopening them", async () => {
  const resumedProductId = productId;
  const pendingProductId = "1005001234567891";
  const expectedDates = [
    "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
    "2026-09-26", "2026-09-27", "2026-09-28",
  ];
  const priorCandidate = {
    platform_product_id: resumedProductId,
    url: `https://www.aliexpress.com/item/${resumedProductId}.html`,
    title: "IXSPY Store Product",
    historical_sales: expectedDates.map((date, index) => ({ date, value: index + 1, value_type: "daily_increment" })),
    history_source: "ixspy_visible_daily_increment",
    history_daily_capture_complete: true,
    history_expected_dates: expectedDates,
    history_missing_dates: [],
    history_read_diagnostic: { product_id: resumedProductId, outcome: "complete" },
    store_identity: {
      target_store_id: "123456",
      route_store_id: "123456",
      heading_store_id: null,
      labeled_store_ids: [],
    },
  };
  const checkpoints = [];
  const collector = loadStoreCollector({
    extraProductIds: [pendingProductId],
    multiProductCharts: true,
  });

  const result = await collector.collectStoreCandidatePage({
    targetDate,
    expectedStoreId: "123456",
    expectedPageUrl: "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc",
    priorCandidates: [priorCandidate],
  }, {
    on_candidate_collected: async (candidate) => checkpoints.push(candidate.platform_product_id),
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.scan_complete, true);
  assert.deepEqual(checkpoints, [pendingProductId]);
  assert.equal(result.page_result.candidates[0].history_daily_capture_complete, true);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.page_result.candidates[0].historical_sales)),
    priorCandidate.historical_sales,
  );
  assert.equal(result.page_result.candidates[1].history_daily_capture_complete, true);
  assert.equal(collector.moreClicks, 1);
});

test("candidate history resumes on a backend-approved URL alias", async () => {
  const expectedStoreId = "123456";
  const routeStoreId = "654321";
  const pageUrl = `https://www.aliexpress.com/store/${routeStoreId}/pages/all-items.html?shop_sortType=orders_desc`;
  const expectedDates = [
    "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
    "2026-09-26", "2026-09-27", "2026-09-28",
  ];
  const priorCandidate = {
    platform_product_id: productId,
    url: `https://www.aliexpress.com/item/${productId}.html`,
    historical_sales: expectedDates.map((date, index) => ({ date, value: index + 1, value_type: "daily_increment" })),
    history_source: "ixspy_visible_daily_increment",
    history_daily_capture_complete: true,
    history_expected_dates: expectedDates,
    history_missing_dates: [],
    history_read_diagnostic: { product_id: productId, outcome: "complete" },
    store_identity: {
      target_store_id: expectedStoreId,
      route_store_id: routeStoreId,
      page_url: pageUrl,
      heading_store_id: null,
      labeled_store_ids: [],
      ixspy_store_name: "Reviewed Store",
      identity_method: "backend_approved_url_alias",
    },
  };
  const collector = loadStoreCollector({
    routeStoreId,
    visibleStoreId: "",
    storeInfoId: null,
    ixspyStoreName: "Reviewed Store",
  });

  const result = await collector.collectStoreCandidatePage({
    targetDate,
    expectedStoreId,
    expectedPageUrl: pageUrl,
    approvedStoreUrlIdentities: [{
      route_store_id: routeStoreId,
      normalized_page_identity: `www.aliexpress.com/store/${routeStoreId}/pages/all-items.html`,
      evidence: { ixspy_store_name: "Reviewed Store" },
    }],
    priorCandidates: [priorCandidate],
  });

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.candidates[0].history_daily_capture_complete, true);
  assert.equal(result.page_result.candidates[0].history_read_diagnostic.resumed, true);
  assert.equal(collector.moreClicks, 0);
});

test("does not mistake a product carousel Next control for store pagination", async () => {
  const result = await loadStoreCollector({ unrelatedNextControl: true }).collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.has_next_page, false);
});

test("uses the enabled Next link instead of Previous and numbered pagination links", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const pageTwo = `${firstUrl}&page=2`;
  const pageThree = `${firstUrl}&page=3`;
  const result = await loadStoreCollector({
    pageNumber: 2,
    hasNextPage: true,
    paginationEntries: [
      { label: "Previous", href: firstUrl },
      { label: "1", href: firstUrl },
      { label: "2", href: pageTwo, disabled: true, className: "pagination-current" },
      { label: "Next", href: pageThree, rel: "next" },
    ],
  }).collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.url, pageTwo);
  assert.equal(result.page_result.pagination_known, true);
  assert.equal(result.page_result.has_next_page, true);
  assert.equal(result.page_result.next_page_url, pageThree);
});

test("uses a numbered same-store Orders link when pagination has no semantic Next control", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc";
  const pageTwo = `${firstUrl}&page=2`;
  const result = await loadStoreCollector({
    paginationKnown: false,
    paginationEntries: [
      { label: "1", href: firstUrl, className: "page-current", parentClass: "pager" },
      { label: "2", href: pageTwo, className: "page-number", parentClass: "pager" },
      { label: "3", href: `${firstUrl}&page=3`, className: "page-number", parentClass: "pager" },
    ],
  }).collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.pagination_known, true);
  assert.equal(result.page_result.has_next_page, true);
  assert.equal(result.page_result.next_page_url, pageTwo);
});

test("continues from the current numbered Orders page without skipping ahead", async () => {
  const firstUrl = "https://www.aliexpress.com/store/123456/pages/all-items.html?shop_sortType=orders_desc&page=1";
  const pageTwo = firstUrl.replace("page=1", "page=2");
  const pageThree = firstUrl.replace("page=1", "page=3");
  const result = await loadStoreCollector({
    pageNumber: 2,
    paginationKnown: false,
    paginationEntries: [
      { label: "1", href: firstUrl, className: "page-number", parentClass: "pager" },
      { label: "2", href: pageTwo, className: "page-current", parentClass: "pager" },
      { label: "3", href: pageThree, className: "page-number", parentClass: "pager" },
    ],
  }).collect("STORE_SELECT_PAGE", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.page_result.next_page_url, pageThree);
});

test("finds the IXSPY more action at store-card level when it is outside the mini-chart node", async () => {
  const collector = loadStoreCollector({ moreOutsideMiniChart: true });
  const result = await collector.collect("STORE_TEST", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.points.length, 7);
  assert.equal(collector.moreClicks, 1);
  assert.equal(collector.fullYearOpened, false);
});

test("binds the more action to the unique visible product chart despite sibling trend controls", async () => {
  const collector = loadStoreCollector({ moreOutsideMiniChart: true, extraTrendInCard: true });
  const result = await collector.collect("STORE_TEST", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.points.length, 7);
  assert.equal(collector.moreClicks, 1);
});

test("locates the product more action by proximity without selecting the store overview link", async () => {
  const collector = loadStoreCollector({ moreOutsideAncestors: true });
  const result = await collector.collect("STORE_TEST", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.points.length, 7);
  assert.equal(collector.moreClicks, 1);
});

test("finds the product more action inside an open IXSPY shadow root", async () => {
  const collector = loadStoreCollector({ moreInsideShadow: true });
  const result = await collector.collect("STORE_TEST", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.test_result.complete, true);
  assert.equal(result.test_result.points.length, 7);
  assert.equal(collector.moreClicks, 1);
});

test("retries chart setup once when the product-card more action is missing", async () => {
  const collector = loadStoreCollector({ missingMoreAction: true });
  const result = await collector.collect("STORE_TEST", targetDate);

  assert.equal(result.state, "ready");
  assert.equal(result.test_result.complete, false);
  assert.equal(result.test_result.source, "ixspy_store_card_more_action_missing");
  assert.equal(result.test_result.diagnostics.retry_passes, 1);
  assert.ok(result.test_result.diagnostics.more_action_wait_ms >= 10000);
  assert.equal(result.test_result.diagnostics.sampled_positions, 0);
  assert.equal(collector.moreClicks, 0);
  assert.equal(collector.miniVisible, false);
});
