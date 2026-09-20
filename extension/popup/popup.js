const API_BASE = "http://127.0.0.1:8000";
const pageStatus = document.querySelector("#page-status");
const result = document.querySelector("#result");
const collectButton = document.querySelector("#collect");
const heading = document.querySelector("#heading");

function showResult(message, isError = false) {
  result.textContent = message;
  result.className = isError ? "error" : "";
}

function isAliExpressProductUrl(url) {
  try {
    const parsed = new URL(url);
    return /(?:^|\.)aliexpress\.[a-z.]+$/i.test(parsed.hostname)
      && /^\/item\/\d+(?:\.html)?/i.test(parsed.pathname);
  } catch {
    return false;
  }
}

function isAliExpressStoreUrl(url) {
  try {
    const parsed = new URL(url);
    return /(?:^|\.)aliexpress\.[a-z.]+$/i.test(parsed.hostname)
      && /\/store\/\d+/i.test(parsed.pathname);
  } catch {
    return false;
  }
}

function parseCount(value) {
  const match = String(value || "").replace(/,/g, "").match(/(\d+(?:\.\d+)?)\s*([KM])?/i);
  if (!match) return null;
  const multiplier = match[2]?.toUpperCase() === "M" ? 1000000 : match[2]?.toUpperCase() === "K" ? 1000 : 1;
  return Math.round(Number(match[1]) * multiplier);
}

async function collectVisibleStoreProducts() {
  window.scrollTo({ top: document.body.scrollHeight, behavior: "instant" });
  await new Promise((resolve) => setTimeout(resolve, 1200));
  const clean = (value) => value?.replace(/\s+/g, " ").trim() || null;
  const url = location.href;
  const idMatch = location.pathname.match(/\/store\/(\d+)/i);
  const byId = new Map();
  const save = (id, href, node) => {
    if (!id) return;
    const text = clean(node?.innerText || node?.textContent || "") || "";
    const soldMatch = text.match(/([\d,.]+\s*[KM]?)\s*(?:sold|orders?|已售|销量)/i);
    const existing = byId.get(id);
    const sold = soldMatch ? parseCount(soldMatch[1]) : null;
    byId.set(id, {
      platform_product_id: id,
      url: href || existing?.url || `https://www.aliexpress.com/item/${id}.html`,
      title: clean(node?.getAttribute?.("title") || node?.getAttribute?.("aria-label") || text) || existing?.title || null,
      public_cumulative_sold: sold ?? existing?.public_cumulative_sold ?? null,
    });
  };
  for (const node of document.querySelectorAll('a[href*="/item/"], [data-product-id], [data-item-id], [data-product-url], [data-item-url]')) {
    const href = node.href || node.getAttribute("href") || node.getAttribute("data-product-url") || node.getAttribute("data-item-url") || "";
    const match = `${href} ${node.getAttribute("data-product-id") || ""} ${node.getAttribute("data-item-id") || ""}`.match(/(?:\/item\/|^)(\d{8,})(?:\.html)?/i);
    if (match) save(match[1], href, node);
  }
  const htmlMatches = document.documentElement.outerHTML.matchAll(/(?:\/item\/|itemId["':= ]+)(\d{8,})(?:\.html)?/gi);
  for (const match of htmlMatches) save(match[1], `https://www.aliexpress.com/item/${match[1]}.html`, null);
  const products = [...byId.values()]
    .sort((a, b) => (b.public_cumulative_sold ?? -1) - (a.public_cumulative_sold ?? -1))
    .slice(0, 20);
  return {
    platform_store_id: idMatch?.[1] || null,
    url,
    products,
    raw_data: {
      extractor_version: "extension-store-0.1.0",
      page_title: clean(document.title),
      visible_link_count: byId.size,
    },
  };
}

async function collectVisibleProduct() {
  const text = (value) => value?.replace(/\s+/g, " ").trim() || null;
  const bodyText = text(document.body?.innerText) || "";
  const url = location.href;
  const idMatch = location.pathname.match(/\/item\/(\d+)/i);
  const jsonLd = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map((node) => {
      try { return JSON.parse(node.textContent); } catch { return null; }
    })
    .find((value) => value && typeof value === "object");
  const title = text(document.querySelector("h1")?.innerText)
    || text(jsonLd?.name)
    || text(document.title)?.replace(/\s*[-|].*$/, "");
  const findNumber = (patterns) => {
    for (const pattern of patterns) {
      const match = bodyText.match(pattern);
      if (match) return Number(match[1].replace(/,/g, ""));
    }
    return null;
  };
  const soldCount = findNumber([/(\d[\d,]*(?:\.\d+)?)\s*(?:sold|orders?)/i, /已售\s*(\d[\d,]*)/i]);
  const reviewCount = findNumber([/(\d[\d,]*)\s*(?:reviews?|ratings?)/i, /(\d[\d,]*)\s*条评价/i]);
  const ratingMatch = bodyText.match(/(?:rating|评分)\s*[:：]?\s*([0-5](?:\.\d+)?)/i);
  const priceMatch = bodyText.match(/(?:US\$|USD|\$)\s*([\d,.]+)/i);
  return {
    platform_product_id: idMatch?.[1] || null,
    url,
    title,
    sold_count: Number.isFinite(soldCount) ? soldCount : null,
    price: priceMatch ? Number(priceMatch[1].replace(/,/g, "")) : null,
    rating: ratingMatch ? Number(ratingMatch[1]) : null,
    review_count: Number.isFinite(reviewCount) ? reviewCount : null,
    raw_data: {
      extractor_version: "extension-0.1.0",
      page_title: text(document.title),
      visible_text_excerpt: bodyText.slice(0, 1200)
    }
  };
}

async function loadActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url || (!isAliExpressProductUrl(tab.url) && !isAliExpressStoreUrl(tab.url))) {
    pageStatus.textContent = "请先打开 AliExpress 商品页或店铺页";
    return null;
  }
  const storePage = isAliExpressStoreUrl(tab.url);
  heading.textContent = storePage ? "采集店铺前 20 个商品" : "采集当前商品";
  collectButton.textContent = storePage ? "采集店铺前 20 个商品" : "采集当前商品";
  pageStatus.textContent = storePage ? "已定位店铺页，可读取公开商品链接" : "已定位商品页，可主动采集";
  collectButton.disabled = false;
  return { tab, storePage };
}

collectButton.addEventListener("click", async () => {
  collectButton.disabled = true;
  showResult("读取公开页面信息…");
  try {
    const active = await loadActiveTab();
    if (!active) return;
    const [execution] = await chrome.scripting.executeScript({ target: { tabId: active.tab.id }, func: active.storePage ? collectVisibleStoreProducts : collectVisibleProduct });
    const payload = execution.result;
    if (active.storePage) {
      if (!payload?.platform_store_id || !payload.products?.length) throw new Error("当前店铺页未读取到公开商品链接");
    } else if (!payload?.platform_product_id) {
      throw new Error("当前页面不是可识别的商品页");
    }
    const endpoint = active.storePage ? "/api/collection/browser-extension/store" : "/api/collection/browser-extension";
    const response = await fetch(`${API_BASE}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `同步失败 (${response.status})`);
    showResult(active.storePage ? `店铺商品已同步，新增 ${body.added_count} 个监控商品` : "采集成功，已同步至监控系统");
  } catch (error) {
    showResult(error.message || "采集失败", true);
  } finally {
    collectButton.disabled = false;
  }
});

loadActiveTab().catch((error) => showResult(error.message || "无法读取当前标签页", true));
