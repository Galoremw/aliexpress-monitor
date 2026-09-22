(function () {
  if (globalThis.AliExpressMonitorPageCollector) return;

  const clean = (value) => value?.replace(/\s+/g, " ").trim() || null;

  function parseCount(value) {
    const match = String(value || "").replace(/,/g, "").match(/(\d+(?:\.\d+)?)\s*([KM])?/i);
    if (!match) return null;
    const suffix = match[2]?.toUpperCase();
    const multiplier = suffix === "M" ? 1000000 : suffix === "K" ? 1000 : 1;
    return Math.round(Number(match[1]) * multiplier);
  }

  function numberFrom(patterns, text) {
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match) return parseCount(match[1]);
    }
    return null;
  }

  function isChallengePage() {
    const url = location.href.toLowerCase();
    const text = clean(document.body?.innerText)?.toLowerCase() || "";
    return url.includes("/punish")
      || url.includes("captcha")
      || Boolean(document.querySelector('iframe[src*="captcha"], iframe[src*="punish"], [class*="captcha" i]'))
      || ((text.includes("verify") || text.includes("验证"))
        && (text.includes("security") || text.includes("captcha") || text.includes("滑块")));
  }

  function productIdFromPage() {
    return location.pathname.match(/\/item\/(\d+)/i)?.[1]
      || location.href.match(/(?:productId|itemId)=(\d+)/i)?.[1]
      || null;
  }

  function monitoredProductIdFromPage() {
    const url = new URL(location.href);
    return url.searchParams.get("monitor_product_id")
      || url.hash.match(/monitor_product_id=(\d+)/i)?.[1]
      || null;
  }

  function visible(element) {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none"
      && style.visibility !== "hidden"
      && Number(style.opacity || 1) > 0
      && rect.width > 0
      && rect.height > 0;
  }

  function fullDateFromText(value) {
    const match = String(value || "").match(/\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b/);
    if (!match) return null;
    const month = String(Number(match[2])).padStart(2, "0");
    const day = String(Number(match[3])).padStart(2, "0");
    return `${match[1]}-${month}-${day}`;
  }

  function readVisibleHistory() {
    const salesPanel = [...document.querySelectorAll(".sales-180")]
      .find((panel) => visible(panel) && /近一年销量趋势图|sales|orders/i.test(panel.innerText || ""))
      || [...document.querySelectorAll("table")]
        .find((table) => visible(table) && /销量|订单|sales|orders/i.test(table.innerText || ""));
    if (!salesPanel) return { points: [], observed_rows: 0, value_type: null };

    const modeRoot = salesPanel.closest(".sales-180") || document;
    const totalButton = modeRoot.querySelector(".trade-total-button")
      || document.querySelector(".trade-total-button");
    const incrementButton = modeRoot.querySelector(".trade-inc-button")
      || document.querySelector(".trade-inc-button");
    const valueType = incrementButton?.classList.contains("btn-active")
      ? "daily_increment"
      : totalButton?.classList.contains("btn-active") ? "cumulative_total" : null;
    if (!valueType) return { points: [], observed_rows: 0, value_type: null };

    const rows = [...salesPanel.querySelectorAll("table tr, [data-history-date][data-history-value]")]
      .filter(visible);
    const points = [];
    for (const row of rows) {
      const dateText = row.getAttribute("data-history-date") || row.innerText || "";
      const metricDate = fullDateFromText(dateText);
      if (!metricDate) continue;
      const rawValue = row.getAttribute("data-history-value")
        || [...row.querySelectorAll("th,td")].slice(1).map((cell) => cell.innerText).join(" ");
      const valueMatch = String(rawValue).replace(/,/g, "").match(/\b\d+(?:\.\d+)?\b/);
      if (!valueMatch) continue;
      points.push({
        date: metricDate,
        value: Math.max(0, Math.round(Number(valueMatch[0]))),
        value_type: valueType,
      });
    }
    const unique = [...new Map(points.map((point) => [point.date, point])).values()];
    return { points: unique, observed_rows: rows.length, value_type: valueType };
  }

  function nextPaint() {
    return new Promise((resolve) => requestAnimationFrame(() => resolve()));
  }

  function readVisibleChartTooltip(valueType) {
    const candidates = [...document.querySelectorAll("body *")]
      .filter((element) => visible(element))
      .map((element) => clean(element.innerText || element.textContent) || "")
      .filter((text) => text.length > 0 && text.length <= 400)
      .filter((text) => fullDateFromText(text) && /近一年销量|sales|orders/i.test(text))
      .sort((left, right) => left.length - right.length);
    for (const text of candidates) {
      const metricDate = fullDateFromText(text);
      const valueMatch = text.match(/(?:近一年销量|sales|orders?)\s*[:：]?\s*([\d,.]+\s*[KM]?)/i);
      if (!metricDate || !valueMatch) continue;
      const value = parseCount(valueMatch[1]);
      if (value === null) continue;
      return { date: metricDate, value, value_type: valueType };
    }
    return null;
  }

  async function readVisibleChartHistory() {
    const chart = document.querySelector("#trade_chart");
    const canvas = chart?.querySelector("canvas");
    if (!canvas || !visible(canvas)) {
      return { points: [], source: "visible_dom_only", sampled_positions: 0 };
    }
    const totalButton = document.querySelector(".trade-total-button");
    const incrementButton = document.querySelector(".trade-inc-button");
    const valueType = incrementButton?.classList.contains("btn-active")
      ? "daily_increment"
      : totalButton?.classList.contains("btn-active") ? "cumulative_total" : null;
    if (!valueType) return { points: [], source: "visible_chart_tooltip", sampled_positions: 0 };

    const rect = canvas.getBoundingClientRect();
    const sampleCount = Math.min(120, Math.max(24, Math.floor(rect.width / 4)));
    const points = new Map();
    for (let index = 0; index <= sampleCount; index += 1) {
      const x = rect.left + (rect.width * index) / sampleCount;
      const y = rect.top + rect.height * 0.45;
      canvas.dispatchEvent(new MouseEvent("mousemove", {
        bubbles: true,
        clientX: x,
        clientY: y,
        view: window,
      }));
      await nextPaint();
      const point = readVisibleChartTooltip(valueType);
      if (point) points.set(point.date, point);
    }
    canvas.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, view: window }));
    return {
      points: [...points.values()],
      source: "visible_chart_tooltip",
      sampled_positions: sampleCount + 1,
    };
  }

  async function collectHistoricalHistory(timeoutMs = 10000) {
    const fromTable = readVisibleHistory();
    if (fromTable.points.length) {
      return {
        points: fromTable.points,
        source: "visible_dom_table",
        sampled_positions: 0,
      };
    }

    const chart = document.querySelector("#trade_chart canvas");
    if (chart && visible(chart)) return readVisibleChartHistory();

    return new Promise((resolve) => {
      let settled = false;
      let checking = false;
      let retryTimer;
      const finish = async (result) => {
        if (settled) return;
        settled = true;
        observer.disconnect();
        clearTimeout(deadline);
        clearTimeout(retryTimer);
        resolve(result || await readVisibleChartHistory());
      };
      const check = async () => {
        if (settled || checking) return;
        checking = true;
        const table = readVisibleHistory();
        if (table.points.length) {
          await finish({
            points: table.points,
            source: "visible_dom_table",
            sampled_positions: 0,
          });
          checking = false;
          return;
        }
        const visibleCanvas = document.querySelector("#trade_chart canvas");
        if (visibleCanvas && visible(visibleCanvas)) {
          await finish(await readVisibleChartHistory());
          checking = false;
          return;
        }
        checking = false;
        retryTimer = setTimeout(() => void check(), 250);
      };
      const observer = new MutationObserver(() => {
        void check();
      });
      observer.observe(document.documentElement, { childList: true, subtree: true });
      const deadline = setTimeout(() => void finish(readVisibleChartHistory()), timeoutMs);
      void check();
    });
  }

  function readProduct() {
    if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    const productId = productIdFromPage();
    if (!productId) return { state: "not_ready", message: "尚未识别商品 ID" };
    const bodyText = clean(document.body?.innerText) || "";
    const jsonLd = [...document.querySelectorAll('script[type="application/ld+json"]')]
      .map((node) => { try { return JSON.parse(node.textContent); } catch { return null; } })
      .find((value) => value && typeof value === "object");
    const title = clean(document.querySelector("h1")?.innerText)
      || clean(jsonLd?.name)
      || clean(document.title)?.replace(/\s*[-|].*$/, "");
    const soldCount = numberFrom(
      [/(\d[\d,.]*\s*[KM]?)\s*(?:sold|orders?)/i, /已售\s*(\d[\d,.]*\s*[KM]?)/i],
      bodyText,
    );
    const reviewCount = numberFrom(
      [/(\d[\d,.]*\s*[KM]?)\s*(?:reviews?|ratings?)/i, /(\d[\d,.]*\s*[KM]?)\s*条评价/i],
      bodyText,
    );
    const ratingMatch = bodyText.match(/(?:rating|评分)\s*[:：]?\s*([0-5](?:\.\d+)?)/i);
    const priceMatch = bodyText.match(/(?:US\$|USD|\$)\s*([\d,.]+)/i);
    const monitoredProductId = monitoredProductIdFromPage();
    const historical = readVisibleHistory();
    const payload = {
      platform_product_id: productId,
      url: location.href,
      title,
      sold_count: Number.isFinite(soldCount) ? soldCount : null,
      price: priceMatch ? Number(priceMatch[1].replace(/,/g, "")) : null,
      rating: ratingMatch ? Number(ratingMatch[1]) : null,
      review_count: Number.isFinite(reviewCount) ? reviewCount : null,
      historical_sales: historical.points,
      raw_data: {
        extractor_version: "extension-content-0.4.0",
        page_title: clean(document.title),
        visible_text_excerpt: bodyText.slice(0, 1200),
        history_source: historical.points.length ? "visible_dom_table" : "visible_chart_tooltip_pending",
        history_observed_rows: historical.observed_rows,
        history_value_type: historical.value_type,
        ...(monitoredProductId ? { monitored_product_id: monitoredProductId } : {}),
      },
    };
    const hasObservableData = title && (
      payload.sold_count !== null
      || payload.price !== null
      || payload.review_count !== null
      || bodyText.length >= 500
    );
    return {
      state: hasObservableData ? "ready" : "not_ready",
      payload,
      message: hasObservableData ? null : "商品公开数据仍在加载",
    };
  }

  function storeIdFromPage() {
    return `${location.pathname}${location.search}`
      .match(/(?:\/store\/|storeId=|sellerId=)(\d+)/i)?.[1] || null;
  }

  function scanStoreProducts() {
    const byId = new Map();
    const nodes = [];
    const collectNodes = (root) => {
      const selector = 'a[href*="/item/"], [data-product-id], [data-item-id], [data-product-url], [data-item-url]';
      for (const node of root.querySelectorAll(selector)) nodes.push(node);
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) collectNodes(element.shadowRoot);
      }
    };
    collectNodes(document);
    for (const node of nodes) {
      const href = node.href
        || node.getAttribute("href")
        || node.getAttribute("data-product-url")
        || node.getAttribute("data-item-url")
        || "";
      const match = `${href} ${node.getAttribute("data-product-id") || ""} ${node.getAttribute("data-item-id") || ""}`
        .match(/(?:\/item\/|^)(\d{8,})(?:\.html)?/i);
      if (!match) continue;
      const id = match[1];
      const text = clean(node.innerText || node.textContent) || "";
      const soldMatch = text.match(/([\d,.]+\s*[KM]?)\s*(?:sold|orders?|已售|销量)/i);
      const existing = byId.get(id);
      byId.set(id, {
        platform_product_id: id,
        url: href || existing?.url || `https://www.aliexpress.com/item/${id}.html`,
        title: clean(node.getAttribute("title") || node.getAttribute("aria-label") || text) || existing?.title || null,
        public_cumulative_sold: soldMatch ? parseCount(soldMatch[1]) : existing?.public_cumulative_sold ?? null,
      });
    }
    return { products: [...byId.values()], candidateNodeCount: nodes.length };
  }

  function waitForStoreGrowth(previousCount, timeoutMs) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (grew) => {
        if (settled) return;
        settled = true;
        observer.disconnect();
        clearTimeout(deadline);
        resolve(grew);
      };
      const observer = new MutationObserver(() => {
        if (scanStoreProducts().products.length > previousCount) finish(true);
      });
      observer.observe(document.documentElement, { childList: true, subtree: true });
      const deadline = setTimeout(() => finish(false), timeoutMs);
    });
  }

  async function collectStore() {
    if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    const storeId = storeIdFromPage();
    if (!storeId) return { state: "failed", message: "尚未识别店铺 ID" };
    let stableRounds = 0;
    let latest = scanStoreProducts();
    for (let round = 0; round < 12 && latest.products.length < 20 && stableRounds < 2; round += 1) {
      const previousCount = latest.products.length;
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" });
      const grew = await waitForStoreGrowth(previousCount, 2500);
      latest = scanStoreProducts();
      stableRounds = grew || latest.products.length > previousCount ? 0 : stableRounds + 1;
      if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    }
    const products = latest.products
      .sort((left, right) => (right.public_cumulative_sold ?? -1) - (left.public_cumulative_sold ?? -1))
      .slice(0, 20);
    if (!products.length) return { state: "failed", message: "当前店铺页未读取到公开商品链接" };
    return {
      state: "ready",
      payload: {
        platform_store_id: storeId,
        url: location.href,
        products,
        raw_data: {
          extractor_version: "extension-store-0.3.0",
          page_title: clean(document.title),
          visible_link_count: latest.products.length,
          candidate_node_count: latest.candidateNodeCount,
          collection_mode: "scheduled",
        },
      },
    };
  }

  function collectProduct(timeoutMs = 45000) {
    return new Promise((resolve) => {
      let settled = false;
      let collecting = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        observer.disconnect();
        clearTimeout(deadline);
        resolve(result);
      };
      const check = async () => {
        if (settled || collecting) return;
        const result = readProduct();
        if (result.state === "challenge") {
          finish(result);
          return;
        }
        if (result.state !== "ready") return;
        collecting = true;
        const history = result.payload.historical_sales?.length
          ? { points: result.payload.historical_sales, source: "visible_dom_table", sampled_positions: 0 }
          : await collectHistoricalHistory();
        finish({
          ...result,
          payload: {
            ...result.payload,
            historical_sales: history.points,
            raw_data: {
              ...result.payload.raw_data,
              history_source: history.source,
              history_sampled_positions: history.sampled_positions,
            },
          },
        });
      };
      const observer = new MutationObserver(check);
      observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        characterData: true,
      });
      const deadline = setTimeout(() => {
        const result = readProduct();
        finish(result.payload ? { ...result, state: "partial" } : { state: "failed", message: result.message });
      }, timeoutMs);
      void check();
    });
  }

  globalThis.AliExpressMonitorPageCollector = {
    collect: (targetType) => targetType === "STORE" ? collectStore() : collectProduct(),
    readProduct,
    isChallengePage,
  };
})();
