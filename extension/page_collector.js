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
    const payload = {
      platform_product_id: productId,
      url: location.href,
      title,
      sold_count: Number.isFinite(soldCount) ? soldCount : null,
      price: priceMatch ? Number(priceMatch[1].replace(/,/g, "")) : null,
      rating: ratingMatch ? Number(ratingMatch[1]) : null,
      review_count: Number.isFinite(reviewCount) ? reviewCount : null,
      raw_data: {
        extractor_version: "extension-content-0.2.0",
        page_title: clean(document.title),
        visible_text_excerpt: bodyText.slice(0, 1200),
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
      const finish = (result) => {
        if (settled) return;
        settled = true;
        observer.disconnect();
        clearTimeout(deadline);
        resolve(result);
      };
      const check = () => {
        const result = readProduct();
        if (result.state === "ready" || result.state === "challenge") finish(result);
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
      check();
    });
  }

  globalThis.AliExpressMonitorPageCollector = {
    collect: (targetType) => targetType === "STORE" ? collectStore() : collectProduct(),
    readProduct,
    isChallengePage,
  };
})();
