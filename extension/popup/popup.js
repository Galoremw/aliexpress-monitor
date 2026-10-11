const DEFAULT_API_BASE = globalThis.ALIEXPRESS_MONITOR_API_BASE || "http://127.0.0.1:8000";
const pageStatus = document.querySelector("#page-status");
const result = document.querySelector("#result");
const collectButton = document.querySelector("#collect");
const storeTestButton = document.querySelector("#test-store");
const storeTestResult = document.querySelector("#test-result");
const heading = document.querySelector("#heading");
const loginPanel = document.querySelector("#login-panel");
const collectorPanel = document.querySelector("#collector-panel");
const loginForm = document.querySelector("#login-form");
const loginResult = document.querySelector("#login-result");
const storeSelection = globalThis.AliExpressStoreSelection;

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

async function loadActiveTab({ keepCollectDisabled = false } = {}) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url || (!isAliExpressProductUrl(tab.url) && !isAliExpressStoreUrl(tab.url))) {
    pageStatus.textContent = "请先打开 AliExpress 商品页或店铺页";
    return null;
  }
  const storePage = isAliExpressStoreUrl(tab.url);
  storeTestButton.hidden = !storePage;
  storeTestResult.hidden = !storePage;
  heading.textContent = storePage ? "筛选店铺监控商品" : "采集当前商品";
  collectButton.textContent = storePage ? "按近 7 日增量筛选并入池" : "采集当前商品";
  pageStatus.textContent = storePage
    ? "店铺页已定位。请先按 Orders 排序，再逐页读取商品的 IXSPY 七日增量图表。"
    : "已定位商品页，可主动采集";
  if (!keepCollectDisabled) collectButton.disabled = false;
  return { tab, storePage };
}

async function monitorApi(path, options = {}) {
  const stored = await chrome.storage.local.get(["apiAccessToken", "apiAccessTokens", "activeApiBase"]);
  const apiBase = stored.activeApiBase || DEFAULT_API_BASE;
  const apiAccessToken = stored.apiAccessTokens?.[apiBase] || stored.apiAccessToken;
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (apiAccessToken) headers.Authorization = `Bearer ${apiAccessToken}`;
  const response = await fetch(`${apiBase}${path}`, { ...options, credentials: "include", headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `监控台请求失败 (${response.status})`);
  return body;
}

async function initializeAuth() {
  try {
    await monitorApi("/api/auth/me");
    loginPanel.hidden = true;
    collectorPanel.hidden = false;
    await loadActiveTab();
  } catch (error) {
    loginPanel.hidden = false;
    collectorPanel.hidden = true;
    loginResult.textContent = error.message.includes("401") ? "请登录监控台账号" : error.message;
    loginResult.className = error.message.includes("401") ? "" : "error";
    await loadActiveTab();
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginResult.textContent = "登录中…";
  loginResult.className = "";
  try {
    const body = await monitorApi("/api/auth/extension-login", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(new FormData(loginForm))),
    });
    const stored = await chrome.storage.local.get(["apiAccessTokens", "activeApiBase"]);
    const apiBase = stored.activeApiBase || DEFAULT_API_BASE;
    await chrome.storage.local.set({
      apiAccessToken: body.access_token,
      apiAccessTokens: { ...(stored.apiAccessTokens || {}), [apiBase]: body.access_token },
    });
    loginResult.textContent = "登录成功";
    loginPanel.hidden = true;
    collectorPanel.hidden = false;
    await loadActiveTab();
  } catch (error) {
    loginResult.textContent = error.message || "登录失败";
    loginResult.className = "error";
  }
});

async function collectVisiblePage(tabId, targetType, options = {}) {
  const message = { type: "collect-visible-page", target_type: targetType, ...options };
  try {
    return await chrome.tabs.sendMessage(tabId, message);
  } catch {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["page_collector.js", "content.js"],
    });
    return chrome.tabs.sendMessage(tabId, message);
  }
}

function localDateString(value = new Date()) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function selectionStorageKey(storeId) {
  return `storeSelectionProgress:v2:${storeId}`;
}

async function submitStoreSelection(storeId, storageKey, progress) {
  await chrome.storage.local.set({ [storageKey]: progress });
  const response = await monitorApi("/api/collection/browser-extension/store", {
    method: "POST",
    body: JSON.stringify(progress.pending_payload),
  });
  await chrome.storage.local.remove(storageKey);
  const selection = progress.pending_payload.raw_data.selection || [];
  const completeCount = selection.filter((item) => item.history_daily_capture_complete).length;
  showResult(`入池完成：${response.added_count} 新增，${response.existing_count} 更新；七日完整 ${completeCount}/${selection.length}。`);
}

async function collectAndSelectStorePage(tab) {
  if (!storeSelection?.buildStoreSelectionPayload
    || !storeSelection?.getStoreSelectionPageDecision
    || !storeSelection?.mergeStoreSelectionPage
    || !storeSelection?.getStoreSelectionRetrySkipIds
    || !storeSelection?.mergeStoreSelectionRetryPage) {
    throw new Error("扩展缺少店铺商品排序模块，请重新加载扩展");
  }
  const storeId = tab.url.match(/\/store\/(\d+)/i)?.[1]
    || new URL(tab.url).search.match(/(?:storeId|sellerId)=(\d+)/i)?.[1];
  if (!storeId) throw new Error("尚未识别店铺 ID");
  const identity = await collectVisiblePage(tab.id, "STORE_IDENTITY");
  if (identity?.state === "challenge") throw new Error("AliExpress 要求验证，请人工完成后再试");
  if (identity?.state !== "ready") throw new Error(identity?.message || "无法核对当前店铺身份");
  if (identity.platform_store_id !== storeId) throw new Error("页面店铺 ID 与网址不一致");
  const storageKey = selectionStorageKey(storeId);
  const saved = await chrome.storage.local.get(storageKey);
  const progress = saved[storageKey];
  if (progress?.pending_payload) {
    await submitStoreSelection(storeId, storageKey, progress);
    return;
  }

  const targetDate = localDateString();
  const previousPage = progress?.pages?.find((item) => item.url === tab.url);
  const skipProductIds = storeSelection.getStoreSelectionRetrySkipIds(
    previousPage, tab.url, targetDate,
  );
  const collected = await collectVisiblePage(tab.id, "STORE_SELECT_PAGE", {
    target_date: targetDate,
    skip_product_ids: skipProductIds,
  });
  if (collected?.state === "challenge") throw new Error("AliExpress 要求验证，请人工完成后再试");
  if (collected?.state === "ixspy_login") throw new Error("IXSPY 登录已过期，请在当前页面正常登录后重试");
  if (!collected?.page_result) throw new Error(collected?.message || "没有读到当前页候选数据");
  const page = storeSelection.mergeStoreSelectionRetryPage(previousPage, collected.page_result);
  if (page.platform_store_id !== storeId) throw new Error("页面店铺 ID 与候选数据不一致");

  const pages = progress?.pages || [];
  const combinedPages = storeSelection.mergeStoreSelectionPage(pages, page);
  const decision = storeSelection.getStoreSelectionPageDecision(combinedPages, 20);
  const allCandidates = combinedPages.flatMap((item) => item.candidates || []);
  const candidateCount = new Set(allCandidates
    .map((candidate) => String(candidate.platform_product_id || ""))
    .filter(Boolean)).size;
  const completeCandidates = storeSelection.rankStoreSelectionCandidates(
    allCandidates,
    Number.MAX_SAFE_INTEGER,
  );
  const completeCount = completeCandidates.length;
  const savedProgress = { pages: combinedPages, saved_at: new Date().toISOString() };

  if (decision === "window_mismatch") {
    await chrome.storage.local.set({ [storageKey]: savedProgress });
    showResult("各页的 IXSPY 近 7 日日期范围不一致，未提交。请返回较早读取的 Orders 页面重读；同一页重读会更新暂存数据。", true);
    return;
  }
  if (decision === "continue") {
    await chrome.storage.local.set({ [storageKey]: savedProgress });
    showResult(`已读取 ${combinedPages.length} 页：${candidateCount} 件候选中有 ${completeCount} 件具备完整七日数据。未完整候选不会阻塞翻页，也不参与排名。请保持 Orders 排序，切换到下一页后再点一次；完整数据达到 20 件后才会入池。`);
    return;
  }
  if (decision === "incomplete") {
    await chrome.storage.local.set({ [storageKey]: savedProgress });
    const completeIds = new Set(completeCandidates.map((item) => String(item.platform_product_id)));
    const incomplete = [...new Map(allCandidates
      .filter((item) => !completeIds.has(String(item.platform_product_id)))
      .map((item) => [String(item.platform_product_id), item])).values()];
    const missingDetails = incomplete.slice(0, 8)
      .map((item) => `${item.platform_product_id} ${item.historical_sales?.length || 0}/7 (${item.history_source || "未识别图表"})`)
      .join("；");
    showResult(`已读完 ${combinedPages.length} 页：${candidateCount} 件候选中仅 ${completeCount} 件有完整七日 IXSPY 数据；未提交，原监控池未修改。缺失商品：${missingDetails}${incomplete.length > 8 ? `；另有 ${incomplete.length - 8} 件` : ""}。若还有下一页，请保持 Orders 排序并切换到下一页继续；若已到末页，可重读当前页补读未完整商品。`, true);
    return;
  }

  const payload = storeSelection.buildStoreSelectionPayload(combinedPages, 20);
  const pending = { pages: combinedPages, pending_payload: payload, saved_at: new Date().toISOString() };
  await submitStoreSelection(storeId, storageKey, pending);
}

collectButton.addEventListener("click", async () => {
  collectButton.disabled = true;
  showResult("读取公开页面信息…");
  try {
    const active = await loadActiveTab({ keepCollectDisabled: true });
    if (!active) return;
    if (active.storePage) {
      await collectAndSelectStorePage(active.tab);
      return;
    }
    const collected = await collectVisiblePage(
      active.tab.id,
      "PRODUCT",
    );
    if (collected?.state === "challenge") throw new Error("请先正常完成 AliExpress 验证");
    if (!collected?.payload) throw new Error(collected?.message || "当前页面没有可同步的公开数据");
    await monitorApi("/api/collection/browser-extension", {
      method: "POST",
      body: JSON.stringify(collected.payload),
    });
    showResult("采集成功，已同步至监控系统");
  } catch (error) {
    showResult(error.message || "采集失败", true);
  } finally {
    collectButton.disabled = false;
  }
});

storeTestButton.addEventListener("click", async () => {
  const buttonLabel = storeTestButton.textContent;
  storeTestButton.disabled = true;
  storeTestButton.textContent = "读取中…";
  storeTestResult.hidden = false;
  storeTestResult.className = "";
  storeTestResult.textContent = "正在读取下一件店铺商品…";
  try {
    const active = await loadActiveTab();
    if (!active?.storePage) throw new Error("请先打开 AliExpress 店铺商品展示页");
    const result = await collectVisiblePage(active.tab.id, "STORE_TEST");
    if (result?.state === "challenge") throw new Error("AliExpress 要求验证，请人工完成后再试");
    if (result?.state === "ixspy_login") throw new Error("IXSPY 登录已过期，请在当前页面正常登录后重试");
    if (!result?.test_result) throw new Error(result?.message || "没有读到店铺页 IXSPY 数据");
    const sample = result.test_result;
    const values = sample.points.map((point) => `${point.date.slice(5)}:${point.value}`).join("  ");
    const missing = sample.missing_dates?.map((date) => date.slice(5)).join(", ");
    const diagnostics = sample.diagnostics || {};
    const observed = (diagnostics.observed_points || [])
      .map((point) => `${point.date.slice(5)}:${point.value}`)
      .join(", ");
    const tooltipSamples = (diagnostics.tooltip_samples || [])
      .map((text) => text.replace(/\s+/g, " ").slice(0, 90))
      .join(" | ");
    const setupNote = sample.source === "ixspy_store_card_more_action_missing"
      ? "；未找到商品卡片“更多...”入口，尚未开始图表采样"
      : sample.source === "ixspy_store_card_mini_chart_unavailable"
        ? "；未找到目标商品的小图表，尚未开始图表采样"
        : sample.source === "ixspy_store_card_trend_button_missing"
          ? "；未找到目标商品的趋势入口，尚未开始图表采样"
          : "";
    const diagnosticText = `诊断：采集器 ${sample.collector_version || "版本未识别"}；面板ID ${diagnostics.chart_product_id || "未识别"}；增量模式 ${diagnostics.daily_mode_selected ? "已选" : "未选"}；悬停 ${diagnostics.tooltip_hit_count || 0}/${diagnostics.sampled_positions || 0}；解析值 ${observed || "无"}${setupNote}${tooltipSamples ? `；样本 ${tooltipSamples}` : ""}`;
    const retryNote = diagnostics.retry_passes ? "首次结果不完整，已自动重读一次。" : "";
    storeTestResult.textContent = `${retryNote}第 ${sample.product_index}/${sample.product_count} 件，商品 ${sample.platform_product_id}：${sample.points.length}/7${sample.complete ? " 完整" : " 未完整"}${values ? `；${values}` : `；${sample.source || "没有可见增量"}`}${missing ? `；缺失 ${missing}` : ""}。${diagnosticText}。未提交。`;
  } catch (error) {
    storeTestResult.className = "error";
    storeTestResult.textContent = error.message || "店铺页试读失败";
  } finally {
    storeTestButton.disabled = false;
    storeTestButton.textContent = buttonLabel;
  }
});

initializeAuth();
