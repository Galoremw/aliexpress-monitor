const API_BASE = "http://127.0.0.1:8000";
const pageStatus = document.querySelector("#page-status");
const result = document.querySelector("#result");
const collectButton = document.querySelector("#collect");

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
  if (!tab?.url || !isAliExpressProductUrl(tab.url)) {
    pageStatus.textContent = "请先打开 AliExpress 商品页";
    return null;
  }
  pageStatus.textContent = "已定位商品页，可主动采集";
  collectButton.disabled = false;
  return tab;
}

collectButton.addEventListener("click", async () => {
  collectButton.disabled = true;
  showResult("读取公开页面信息…");
  try {
    const tab = await loadActiveTab();
    if (!tab) return;
    const [execution] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: collectVisibleProduct });
    const payload = execution.result;
    if (!payload?.platform_product_id) throw new Error("当前页面不是可识别的商品页");
    const response = await fetch(`${API_BASE}/api/collection/browser-extension`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `同步失败 (${response.status})`);
    showResult("采集成功，已同步至监控系统");
  } catch (error) {
    showResult(error.message || "采集失败", true);
  } finally {
    collectButton.disabled = false;
  }
});

loadActiveTab().catch((error) => showResult(error.message || "无法读取当前标签页", true));
