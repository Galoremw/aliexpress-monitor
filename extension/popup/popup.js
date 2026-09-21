const API_BASE = globalThis.ALIEXPRESS_MONITOR_API_BASE || "http://127.0.0.1:8000";
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
      && (/\/store\/\d+/i.test(parsed.pathname) || /(?:storeId|sellerId)=\d+/i.test(parsed.search));
  } catch {
    return false;
  }
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

async function collectVisiblePage(tabId, targetType) {
  try {
    return await chrome.tabs.sendMessage(tabId, {
      type: "collect-visible-page",
      target_type: targetType,
    });
  } catch {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["page_collector.js", "content.js"],
    });
    return chrome.tabs.sendMessage(tabId, {
      type: "collect-visible-page",
      target_type: targetType,
    });
  }
}

collectButton.addEventListener("click", async () => {
  collectButton.disabled = true;
  showResult("读取公开页面信息…");
  try {
    const active = await loadActiveTab();
    if (!active) return;
    const collected = await collectVisiblePage(
      active.tab.id,
      active.storePage ? "STORE" : "PRODUCT",
    );
    if (collected?.state === "challenge") throw new Error("请先正常完成 AliExpress 验证");
    if (!collected?.payload) throw new Error(collected?.message || "当前页面没有可同步的公开数据");
    const endpoint = active.storePage
      ? "/api/collection/browser-extension/store"
      : "/api/collection/browser-extension";
    const response = await fetch(`${API_BASE}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collected.payload),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `同步失败 (${response.status})`);
    showResult(active.storePage
      ? `店铺商品已同步，新增 ${body.added_count} 个监控商品`
      : "采集成功，已同步至监控系统");
  } catch (error) {
    showResult(error.message || "采集失败", true);
  } finally {
    collectButton.disabled = false;
  }
});

loadActiveTab().catch((error) => showResult(error.message || "无法读取当前标签页", true));
