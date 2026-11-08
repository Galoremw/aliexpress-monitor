const toast = document.querySelector("#toast");

function initializeDashboardSectionNav() {
  const nav = document.querySelector("#dashboard-section-nav");
  if (!nav) return;
  const links = [...nav.querySelectorAll('a[href^="#"]')];
  const sections = links
    .map((link) => document.querySelector(link.getAttribute("href")))
    .filter(Boolean);
  let scrollFrame = 0;

  const updateActiveLink = () => {
    scrollFrame = 0;
    let activeSection = sections[0];
    const activationLine = 110;
    for (const section of sections) {
      if (section.getBoundingClientRect().top <= activationLine) activeSection = section;
      else break;
    }
    if (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) {
      activeSection = sections.at(-1) || activeSection;
    }
    for (const link of links) {
      const active = link.hash === `#${activeSection?.id}`;
      if (active) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    }
  };

  nav.addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="#"]');
    if (!link) return;
    const target = document.querySelector(link.getAttribute("href"));
    target?.closest("details")?.setAttribute("open", "");
  });
  window.addEventListener("scroll", () => {
    if (scrollFrame) return;
    scrollFrame = window.requestAnimationFrame(updateActiveLink);
  }, { passive: true });
  window.addEventListener("resize", updateActiveLink, { passive: true });
  window.addEventListener("hashchange", updateActiveLink);
  updateActiveLink();
}

initializeDashboardSectionNav();

document.querySelectorAll(".history-periods").forEach((controls) => {
  const rows = [...(controls.closest(".trend-band")?.querySelectorAll(".trend-row[data-history-date]") || [])];
  controls.querySelectorAll("[data-history-days]").forEach((button) => button.addEventListener("click", () => {
    const days = Number(button.dataset.historyDays) || 30;
    const cutoff = new Date();
    cutoff.setHours(0, 0, 0, 0);
    cutoff.setDate(cutoff.getDate() - days + 1);
    const cutoffDate = `${cutoff.getFullYear()}-${String(cutoff.getMonth() + 1).padStart(2, "0")}-${String(cutoff.getDate()).padStart(2, "0")}`;
    rows.forEach((row) => { row.hidden = row.dataset.historyDate < cutoffDate; });
    controls.querySelectorAll("[data-history-days]").forEach((item) => {
      const active = item === button;
      item.classList.toggle("primary", active);
      item.setAttribute("aria-pressed", String(active));
    });
  }));
  controls.querySelector('[data-history-days="30"]')?.click();
});

function notify(message, error = false) {
  if (!toast) return;
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  window.setTimeout(() => { toast.className = "toast"; }, 3200);
}

document.querySelector("#launch-galore-collector")?.addEventListener("click", () => {
  notify("已请求启动 Galore 主机独立采集 Chrome；首次询问时请允许打开外部应用");
});

async function jsonRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

document.querySelector("#store-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  try {
    const store = await jsonRequest("/api/stores", { method: "POST", body: JSON.stringify({ name: data.get("name"), url: data.get("url") }) });
    const message = store.initial_collection_status === "deferred"
      ? store.initial_collection_message || "店铺已添加；首次采集任务等待队列恢复"
      : "店铺已添加；采集任务已进入待处理队列。选择在线采集电脑并点击“开始采集”后执行。";
    notify(message);
    window.setTimeout(() => window.location.reload(), 2200);
  } catch (error) { notify(error.message, true); }
});

document.querySelectorAll(".store-collect-products").forEach((button) => {
  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const oldText = button.textContent;
    button.disabled = true;
    button.textContent = "排队中…";
    try {
      const result = await jsonRequest(`/api/stores/${button.dataset.storeId}/browser-collection`, { method: "POST" });
      updateBrowserCollection(result.run);
      const queueMessage = result.queued_count
        ? "已排入该店铺的商品页采集任务"
        : "该店铺的商品页采集任务已在队列";
      await refreshBrowserCollectionControl();
      notify(`${queueMessage}；缺失日期标记待补，下次店铺页任务重试。队列保持待启动，请选择采集电脑并点击“开始采集”。`);
      button.textContent = "已排队";
      window.setTimeout(() => { button.textContent = oldText; }, 2400);
    } catch (error) {
      notify(error.message, true);
      button.textContent = oldText;
    } finally {
      button.disabled = false;
    }
  });
});

document.querySelector("#product-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  const target = String(data.get("target") || "").trim();
  const selectedStore = String(data.get("store_id") || "").trim();
  const payload = { store_id: selectedStore ? Number(selectedStore) : null };
  if (/^\d+$/.test(target)) payload.aliexpress_product_id = target;
  else payload.url = target;
  try {
    await jsonRequest("/api/products", { method: "POST", body: JSON.stringify(payload) });
    notify("商品已加入监控");
    window.location.reload();
  } catch (error) { notify(error.message, true); }
});

document.querySelectorAll(".edit-store-name").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const form = document.querySelector(`.store-rename-form[data-store-id="${button.dataset.storeId}"]`);
    if (!form) return;
    form.hidden = false;
    button.hidden = true;
    form.querySelector("input")?.focus();
    form.querySelector("input")?.select();
  });
});

document.querySelectorAll(".cancel-store-name").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const form = button.closest(".store-rename-form");
    if (!form) return;
    form.hidden = true;
    document.querySelector(`.edit-store-name[data-store-id="${form.dataset.storeId}"]`)?.removeAttribute("hidden");
  });
});

document.querySelectorAll(".store-rename-form").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const submit = form.querySelector("button[type='submit']");
    const name = String(new FormData(form).get("name") || "").trim();
    if (!name) {
      notify("店铺名称不能为空", true);
      return;
    }
    submit.disabled = true;
    try {
      await jsonRequest(`/api/stores/${form.dataset.storeId}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      notify("店铺名称已更新");
      window.location.reload();
    } catch (error) {
      notify(error.message, true);
      submit.disabled = false;
    }
  });
});

async function submitDianxiaomi(productIds, button) {
  const ids = [...new Set(productIds.map((value) => Number(value)).filter(Boolean))];
  if (!ids.length) {
    notify("没有可发送的有效监控商品", true);
    return;
  }
  const oldText = button.textContent;
  button.disabled = true;
  button.dataset.dianxiaomiSubmitting = "true";
  button.textContent = "加入队列…";
  try {
    const result = await jsonRequest("/api/integrations/dianxiaomi/handoffs", {
      method: "POST",
      body: JSON.stringify({ product_ids: ids }),
    });
    const reused = result.reused_count ? `，${result.reused_count} 个已在队列中` : "";
    notify(`已加入待启动队列：${result.queued_count} 个${reused}；选完后点击“开始店小秘采集”`);
    await refreshDianxiaomiStatus();
  } catch (error) {
    notify(error.message, true);
  } finally {
    delete button.dataset.dianxiaomiSubmitting;
    const succeeded = button.dataset.dianxiaomiStatus === "SUCCEEDED";
    button.disabled = succeeded;
    button.textContent = succeeded ? "已采集到店小秘" : oldText;
  }
}

document.querySelectorAll(".dianxiaomi-product").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    void submitDianxiaomi([button.dataset.productId], button);
  });
});

document.querySelectorAll(".dianxiaomi-store").forEach((button) => {
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    void submitDianxiaomi((button.dataset.productIds || "").split(","), button);
  });
});

let dianxiaomiItems = [];
let dianxiaomiActiveFilter = "queued";
let dianxiaomiCanDeleteFailed = false;
let dianxiaomiFailedCount = 0;
let dianxiaomiIdentitySelection = new Set();
let dianxiaomiIdentityBatchBusy = false;
let dianxiaomiIdentityBatchResult = "";

function updateDianxiaomiProductButtons(items) {
  const latestStatusByProduct = new Map();
  for (const item of items || []) {
    const productId = Number(item.product_id);
    if (productId && !latestStatusByProduct.has(productId)) {
      latestStatusByProduct.set(productId, item.status);
    }
  }
  document.querySelectorAll(".dianxiaomi-product").forEach((button) => {
    const latestStatus = latestStatusByProduct.get(Number(button.dataset.productId));
    if (!latestStatus) return;
    const succeeded = latestStatus === "SUCCEEDED";
    button.dataset.dianxiaomiStatus = latestStatus;
    if (button.dataset.dianxiaomiSubmitting === "true") return;
    button.textContent = succeeded ? "已采集到店小秘" : "采集到店小秘";
    button.classList.toggle("is-collected", succeeded);
    button.disabled = succeeded || button.dataset.dianxiaomiSubmitting === "true";
    if (succeeded) {
      button.title = "店小秘已成功采集";
      button.setAttribute("aria-label", "已采集到店小秘");
    } else {
      button.removeAttribute("title");
      button.removeAttribute("aria-label");
    }
  });
}

const dianxiaomiFilterStatuses = {
  queued: ["QUEUED"],
  processing: ["CLAIMED", "OPENED", "FILLED"],
  submitted: ["COLLECTING"],
  succeeded: ["SUCCEEDED"],
  failed: ["FAILED"],
  needs_confirmation: ["NEEDS_CONFIRMATION"],
};

function renderDianxiaomiFilter() {
  const section = document.querySelector("#dianxiaomi-status");
  const list = section?.querySelector("#dianxiaomi-items");
  if (!list) return;
  const statuses = dianxiaomiFilterStatuses[dianxiaomiActiveFilter] || [];
  const filteredItems = dianxiaomiItems.filter((item) => statuses.includes(item.status));
  const eligibleIds = new Set(dianxiaomiItems
    .filter(isDianxiaomiIdentityReviewItem)
    .map((item) => String(item.id)));
  for (const id of dianxiaomiIdentitySelection) {
    if (!eligibleIds.has(id)) dianxiaomiIdentitySelection.delete(id);
  }
  const label = section.querySelector(`[data-dianxiaomi-filter="${dianxiaomiActiveFilter}"] small`)?.textContent || "该状态";
  const title = section.querySelector("#dianxiaomi-list-title");
  const caption = section.querySelector("#dianxiaomi-list-caption");
  if (title) title.textContent = `${label}商品 · ${filteredItems.length}`;
  if (caption) caption.textContent = `最近 100 项中显示${label}状态；上方汇总为全队列总数`;
  list.innerHTML = filteredItems.map(renderDianxiaomiItem).join("") || `<p class="muted-value">当前没有${escapeDianxiaomiHtml(label)}商品。</p>`;
  updateDianxiaomiIdentityBulkControls(filteredItems);
  updateDianxiaomiDeleteControls();
}

function updateDianxiaomiIdentityBulkControls(visibleItems) {
  const toolbar = document.querySelector("#dianxiaomi-identity-bulk");
  const selectAll = document.querySelector("#dianxiaomi-identity-select-visible");
  const confirmSelected = document.querySelector("#dianxiaomi-identity-confirm-selected");
  const result = document.querySelector("#dianxiaomi-identity-bulk-result");
  if (!toolbar || !selectAll || !confirmSelected) return;

  const eligibleItems = visibleItems.filter(isDianxiaomiIdentityReviewItem);
  const selectedCount = eligibleItems
    .filter((item) => dianxiaomiIdentitySelection.has(String(item.id))).length;
  const selectedVisibleCount = eligibleItems.length - eligibleItems.filter((item) =>
    !dianxiaomiIdentitySelection.has(String(item.id))).length;

  toolbar.hidden = dianxiaomiActiveFilter !== "needs_confirmation" || eligibleItems.length === 0;
  selectAll.checked = eligibleItems.length > 0 && selectedVisibleCount === eligibleItems.length;
  selectAll.indeterminate = selectedVisibleCount > 0 && selectedVisibleCount < eligibleItems.length;
  selectAll.disabled = dianxiaomiIdentityBatchBusy || eligibleItems.length === 0;
  confirmSelected.disabled = dianxiaomiIdentityBatchBusy || selectedCount === 0;
  confirmSelected.textContent = dianxiaomiIdentityBatchBusy
    ? `正在确认所选商品…（${selectedCount}）`
    : `批量确认所选无误（${selectedCount}）`;
  if (result) result.textContent = dianxiaomiIdentityBatchResult;
}

function updateDianxiaomiDeleteControls() {
  const hasFailed = dianxiaomiFailedCount > 0;
  const deleteAll = document.querySelector("#dianxiaomi-delete-failed");
  if (deleteAll) {
    deleteAll.hidden = !hasFailed || !dianxiaomiCanDeleteFailed;
    deleteAll.textContent = `批量清空失败任务（${dianxiaomiFailedCount}）`;
    deleteAll.title = `永久删除全部 ${dianxiaomiFailedCount} 条失败记录`;
  }
}

async function refreshDianxiaomiStatus() {
  const section = document.querySelector("#dianxiaomi-status");
  const hasProductActions = document.querySelectorAll(".dianxiaomi-product").length > 0;
  if (!section && !hasProductActions) return;
  try {
    const status = await jsonRequest(section?.dataset.statusEndpoint || "/api/integrations/dianxiaomi/status");
    updateDianxiaomiProductButtons(status.items || []);
    if (!section) return status;
    Object.keys(status).forEach((key) => {
      const target = section.querySelector(`[data-dianxiaomi-value="${key}"]`);
      if (target) target.textContent = status[key];
    });
    dianxiaomiCanDeleteFailed = Boolean(status.can_delete_failed);
    dianxiaomiFailedCount = Number(status.failed_count) || 0;
    dianxiaomiItems = status.items || [];
    renderDianxiaomiFilter();
    updateDianxiaomiQueue(status);
    return status;
  } catch {
    // The integration is optional; a temporary API restart should not affect the dashboard.
    return null;
  }
}

function updateDianxiaomiQueue(status) {
  const online = document.querySelector("#dianxiaomi-worker-online");
  const state = document.querySelector("#dianxiaomi-queue-state");
  const current = document.querySelector("#dianxiaomi-current-item");
  const ownerWaiting = document.querySelector("#dianxiaomi-owner-waiting");
  const heartbeat = document.querySelector("#dianxiaomi-last-heartbeat");
  const resume = document.querySelector("#dianxiaomi-queue-resume");
  const pause = document.querySelector("#dianxiaomi-queue-pause");
  const workerSelect = document.querySelector("#dianxiaomi-worker-select");
  const workerSave = document.querySelector("#dianxiaomi-worker-select-save");
  if (workerSelect) {
    const currentValue = workerSelect.value;
    workerSelect.replaceChildren(new Option("请选择店小秘采集电脑", ""));
    for (const worker of status.workers || []) {
      workerSelect.add(new Option(
        `${worker.label} · ${worker.worker_id.slice(0, 8)} · ${worker.online ? "在线" : "离线"}`,
        worker.worker_id,
      ));
    }
    workerSelect.value = status.selected_worker?.worker_id || currentValue || "";
    workerSelect.disabled = !(status.workers || []).length;
  }
  if (workerSave) workerSave.disabled = !(status.workers || []).length;
  if (online) {
    const selected = status.selected_worker;
    online.className = `status ${selected?.online ? "success" : "pending"}`;
    online.textContent = selected
      ? `店小秘采集电脑：${selected.display_label || selected.label} · ${selected.online ? "在线" : "离线"}`
      : "默认第二台电脑尚未连接监控台";
  }
  if (state) {
    const waitingForStart = Number(status.waiting_for_start_count || 0);
    const activeRun = Boolean(status.active_run_started_at) && !status.paused;
    state.textContent = status.paused
      ? status.paused_reason === "awaiting_start"
        ? status.queued_count ? `已有 ${status.queued_count} 项待启动，等你选择开始`
          : status.needs_confirmation_count ? `本轮已结束，有 ${status.needs_confirmation_count} 项待人工确认`
          : "本轮店小秘采集已结束"
        : status.paused_reason === "needs_confirmation" ? "旧队列状态待恢复；请手动启动后续任务"
        : status.paused_reason === "selected_worker_offline" ? "指定电脑离线，队列已暂停；上线后可手动启动"
        : "店小秘队列已手动暂停"
      : status.waiting_for_owner ? "等待上一台采集电脑的在途任务结束"
      : status.waiting_reason ? status.waiting_reason
      : activeRun && waitingForStart ? `本轮正在采集；另有 ${waitingForStart} 项待下一次启动`
      : status.needs_confirmation_count ? "有商品待人工确认，队列继续处理后续商品"
      : status.stalled_count ? "任务响应超时，等待扩展恢复"
      : (status.processing_count || status.submitted_count) ? "店小秘队列正在处理"
      : activeRun ? "店小秘队列正在处理"
      : status.queued_count ? "队列待启动"
      : "当前没有待处理任务";
  }
  if (ownerWaiting) {
    const owner = status.active_owner;
    const ownerIsDifferent = owner && owner.worker_id !== status.selected_worker?.worker_id;
    ownerWaiting.hidden = !status.waiting_reason && !ownerIsDifferent;
    ownerWaiting.textContent = status.waiting_reason
      || (ownerIsDifferent ? `当前在途任务由 ${owner.label} 处理，任务结束后再交接。` : "");
  }
  if (current) {
    const item = status.current_item;
    current.textContent = item
      ? `当前：${item.product_title || `商品 #${item.platform_product_id || item.product_id}`} · ${item.store_name || `店铺 #${item.store_id}`}`
      : "当前没有正在处理的商品";
  }
  if (heartbeat) heartbeat.textContent = status.last_heartbeat_at
    ? `最近心跳：${formatDashboardDate(status.last_heartbeat_at)}`
    : "尚未收到扩展心跳";
  if (resume) {
    const activeRun = Boolean(status.active_run_started_at) && !status.paused;
    resume.hidden = !status.queued_count || activeRun;
    resume.textContent = status.paused_reason === "awaiting_start"
      ? "开始店小秘采集"
      : status.paused_reason === "needs_confirmation" ? "手动启动后续任务"
      : status.paused_reason === "selected_worker_offline" ? "等待采集电脑上线"
      : status.paused ? "继续店小秘队列" : "开始店小秘采集";
    resume.disabled = !status.selected_worker?.online;
  }
  if (pause) pause.hidden = Boolean(status.paused) || !status.active_run_started_at
    || !(status.queued_count || status.processing_count || status.submitted_count);
}

const dianxiaomiStatusLabels = {
  QUEUED: "待发送",
  CLAIMED: "扩展已领取",
  OPENED: "后台页面已打开",
  FILLED: "链接已填入",
  COLLECTING: "店小秘采集中",
  SUCCEEDED: "店小秘已完成",
  FAILED: "失败",
  NEEDS_CONFIRMATION: "需人工确认",
  CANCELED: "已取消",
};

function escapeDianxiaomiHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
}

function isDianxiaomiProductPageMismatch(item) {
  if (item.error_type === "product_page_mismatch") return true;
  const mismatch = item.detail?.product_page_mismatch;
  if (mismatch && /^\d+$/.test(String(mismatch.expected_product_id || ""))
    && /^\d+$/.test(String(mismatch.actual_product_id || ""))
    && String(mismatch.expected_product_id) !== String(mismatch.actual_product_id)) return true;
  const message = String(item.error_message || "");
  const ids = message.match(/目标商品 ID\s*(\d+)\D+当前商品 ID\s*(\d+)/);
  return Boolean(ids && ids[1] !== ids[2]);
}

function isDianxiaomiIdentityReviewItem(item) {
  return item.status === "NEEDS_CONFIRMATION" && isDianxiaomiProductPageMismatch(item);
}

function dianxiaomiNextAction(item) {
  if (item.status === "NEEDS_CONFIRMATION") {
    if (isDianxiaomiProductPageMismatch(item)) {
      return "请核对目标商品与实际打开的商品；确认后会记录此商品的 URL 并自动重排";
    }
    const message = String(item.error_message || "");
    if (message.includes("协议")) return "请在店小秘页面勾选采集协议后点击“重新排队”";
    if (message.includes("登录")) return "请在店小秘页面登录后点击“重新排队”";
    return "请处理店小秘页面提示后点击“重新排队”";
  }
  if (item.status === "FAILED") return "请检查店小秘页面后点击“重新排队”";
  return "";
}

function renderDianxiaomiItem(item) {
  const status = dianxiaomiStatusLabels[item.status] || item.status;
  const statusClass = item.status === "SUCCEEDED" ? "success" : ["FAILED", "NEEDS_CONFIRMATION"].includes(item.status) ? "error" : "";
  const productMismatchReview = item.status === "NEEDS_CONFIRMATION"
    && isDianxiaomiProductPageMismatch(item);
  const identityActionDisabled = dianxiaomiIdentityBatchBusy ? "disabled" : "";
  const identitySelection = productMismatchReview
    ? `<label class="dianxiaomi-identity-select"><input type="checkbox" data-dianxiaomi-identity-select data-handoff-id="${item.id}" aria-label="选择商品 ID ${escapeDianxiaomiHtml(item.platform_product_id || item.product_id || "")} 进行批量确认"${dianxiaomiIdentitySelection.has(String(item.id)) ? " checked" : ""}${dianxiaomiIdentityBatchBusy ? " disabled" : ""}><span>选择</span></label>`
    : "";
  const mismatchEvidence = item.detail?.product_page_mismatch || {};
  const actualId = mismatchEvidence.actual_product_id
    || String(item.error_message || "").match(/当前商品 ID\s*(\d+)/)?.[1]
    || "";
  const actualUrl = mismatchEvidence.actual_url
    || (actualId ? `https://www.aliexpress.com/item/${actualId}.html` : "");
  const actualPage = productMismatchReview && actualUrl
    ? `<span class="dianxiaomi-item-meta">实际打开页：ID ${escapeDianxiaomiHtml(actualId)} · <a href="${escapeDianxiaomiHtml(actualUrl)}" target="_blank" rel="noreferrer">查看实际商品</a></span>`
    : "";
  const action = productMismatchReview
    ? `<div class="dianxiaomi-identity-actions"><button class="link-button identity-confirm" type="button" data-dianxiaomi-action="identity-confirm" data-handoff-id="${item.id}" ${identityActionDisabled}>确认商品无误</button><button class="link-button identity-reject" type="button" data-dianxiaomi-action="identity-reject" data-handoff-id="${item.id}" ${identityActionDisabled}>错误</button></div>`
    : ["NEEDS_CONFIRMATION", "FAILED"].includes(item.status)
    ? `<button class="link-button" type="button" data-dianxiaomi-action="resume" data-handoff-id="${item.id}">重新排队</button>`
    : ["QUEUED", "CLAIMED", "OPENED", "FILLED", "COLLECTING"].includes(item.status)
      ? `<button class="link-button danger" type="button" data-dianxiaomi-action="cancel" data-handoff-id="${item.id}">取消</button>`
      : "";
  const deleteAction = item.status === "FAILED"
    && dianxiaomiCanDeleteFailed
    ? `<button class="link-button danger" type="button" data-dianxiaomi-action="delete" data-handoff-id="${item.id}">删除</button>`
    : "";
  const label = `商品 ID ${escapeDianxiaomiHtml(item.platform_product_id || item.product_id || "—")}`;
  const store = escapeDianxiaomiHtml(item.store_name || `店铺 #${item.store_id}`);
  const url = escapeDianxiaomiHtml(item.target_url);
  const error = item.error_message ? ` · ${escapeDianxiaomiHtml(item.error_message)}` : "";
  const nextAction = dianxiaomiNextAction(item);
  return `<article class="dianxiaomi-item"><div class="dianxiaomi-item-main"><strong class="dianxiaomi-item-title">${label}</strong><span class="dianxiaomi-item-meta">${store} · <a href="${url}" target="_blank" rel="noreferrer">${url}</a></span><span class="dianxiaomi-item-meta">${escapeDianxiaomiHtml(formatDashboardDate(item.requested_at))}${error}</span>${actualPage}${nextAction ? `<span class="dianxiaomi-item-hint">${escapeDianxiaomiHtml(nextAction)}</span>` : ""}</div><div class="dianxiaomi-item-side">${identitySelection}${deleteAction}<span class="dianxiaomi-status ${statusClass}">${status}</span><span class="dianxiaomi-item-actions">${action}</span></div></article>`;
}

function formatDashboardDate(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—"; }

void refreshDianxiaomiStatus();
window.setInterval(refreshDianxiaomiStatus, 5000);
document.querySelector("#dianxiaomi-status")?.addEventListener("click", (event) => {
  const filter = event.target.closest("[data-dianxiaomi-filter]");
  if (!filter) return;
  dianxiaomiActiveFilter = filter.dataset.dianxiaomiFilter;
  document.querySelectorAll("[data-dianxiaomi-filter]").forEach((button) => {
    const selected = button === filter;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
  });
  renderDianxiaomiFilter();
});
document.querySelector("#dianxiaomi-items")?.addEventListener("change", (event) => {
  const checkbox = event.target.closest("[data-dianxiaomi-identity-select]");
  if (!checkbox || dianxiaomiIdentityBatchBusy) return;
  const handoffId = String(checkbox.dataset.handoffId || "");
  const item = dianxiaomiItems.find((candidate) => String(candidate.id) === handoffId);
  if (!item || !isDianxiaomiIdentityReviewItem(item)) {
    checkbox.checked = false;
    dianxiaomiIdentitySelection.delete(handoffId);
    return;
  }
  if (checkbox.checked) dianxiaomiIdentitySelection.add(handoffId);
  else dianxiaomiIdentitySelection.delete(handoffId);
  const statuses = dianxiaomiFilterStatuses[dianxiaomiActiveFilter] || [];
  updateDianxiaomiIdentityBulkControls(
    dianxiaomiItems.filter((candidate) => statuses.includes(candidate.status)),
  );
});
document.querySelector("#dianxiaomi-identity-select-visible")?.addEventListener("change", (event) => {
  if (dianxiaomiIdentityBatchBusy) return;
  const statuses = dianxiaomiFilterStatuses[dianxiaomiActiveFilter] || [];
  const visibleItems = dianxiaomiItems.filter((item) => statuses.includes(item.status));
  const eligibleItems = visibleItems.filter(isDianxiaomiIdentityReviewItem);
  for (const item of eligibleItems) {
    const handoffId = String(item.id);
    if (event.currentTarget.checked) dianxiaomiIdentitySelection.add(handoffId);
    else dianxiaomiIdentitySelection.delete(handoffId);
  }
  document.querySelectorAll("[data-dianxiaomi-identity-select]").forEach((checkbox) => {
    checkbox.checked = dianxiaomiIdentitySelection.has(String(checkbox.dataset.handoffId));
  });
  updateDianxiaomiIdentityBulkControls(visibleItems);
});
document.querySelector("#dianxiaomi-identity-confirm-selected")?.addEventListener("click", async (event) => {
  if (dianxiaomiIdentityBatchBusy) return;
  const statuses = dianxiaomiFilterStatuses[dianxiaomiActiveFilter] || [];
  const selectedItems = dianxiaomiItems.filter((item) =>
    statuses.includes(item.status)
    && isDianxiaomiIdentityReviewItem(item)
    && dianxiaomiIdentitySelection.has(String(item.id)));
  if (!selectedItems.length) return;

  dianxiaomiIdentityBatchBusy = true;
  dianxiaomiIdentityBatchResult = "";
  updateDianxiaomiIdentityBulkControls(
    dianxiaomiItems.filter((item) => statuses.includes(item.status)),
  );
  renderDianxiaomiFilter();

  let confirmedCount = 0;
  const failures = [];
  for (const item of selectedItems) {
    const handoffId = String(item.id);
    try {
      await jsonRequest(
        `/api/integrations/dianxiaomi/handoffs/${handoffId}/identity-review/confirm`,
        { method: "POST" },
      );
      dianxiaomiIdentitySelection.delete(handoffId);
      confirmedCount += 1;
    } catch (error) {
      failures.push({ id: handoffId, message: String(error.message || "请求失败") });
    }
    updateDianxiaomiIdentityBulkControls(
      dianxiaomiItems.filter((candidate) => statuses.includes(candidate.status)),
    );
  }

  dianxiaomiIdentityBatchBusy = false;
  if (failures.length) {
    const details = failures.slice(0, 5)
      .map(({ id, message }) => `#${id}：${message.slice(0, 120)}`)
      .join("；");
    const more = failures.length > 5 ? `；另有 ${failures.length - 5} 项失败` : "";
    dianxiaomiIdentityBatchResult = `批量结果：成功 ${confirmedCount} 项，失败 ${failures.length} 项。${details}${more}`;
    notify(`批量确认结束：成功 ${confirmedCount} 项，失败 ${failures.length} 项；失败项仍待确认。`, true);
  } else {
    dianxiaomiIdentityBatchResult = `批量结果：${confirmedCount} 项已确认并重新排队。`;
    notify(`已批量确认 ${confirmedCount} 项，URL 身份已记录并重新排队。`);
  }
  renderDianxiaomiFilter();
  await refreshDianxiaomiStatus();
});
document.querySelector("#dianxiaomi-items")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-dianxiaomi-action]");
  if (!button) return;
  const action = button.dataset.dianxiaomiAction;
  if (dianxiaomiIdentityBatchBusy && ["identity-confirm", "identity-reject"].includes(action)) return;
  if (action === "delete" && !dianxiaomiCanDeleteFailed) return;
  if (action === "delete" && !window.confirm("确定永久删除这条采集失败的店小秘任务吗？此操作不可恢复。")) return;
  if (action === "identity-reject"
    && !window.confirm("确认实际打开的商品与监控商品不一致吗？本条任务将取消，不影响其他队列任务。")) return;
  button.disabled = true;
  try {
    if (action === "delete") {
      await jsonRequest(`/api/integrations/dianxiaomi/handoffs/${button.dataset.handoffId}`, { method: "DELETE" });
      notify("失败任务已删除");
    } else if (["identity-confirm", "identity-reject"].includes(action)) {
      const decision = action === "identity-confirm" ? "confirm" : "reject";
      await jsonRequest(`/api/integrations/dianxiaomi/handoffs/${button.dataset.handoffId}/identity-review/${decision}`, { method: "POST" });
      notify(action === "identity-confirm" ? "已记录商品 URL 身份并重新排队" : "已取消该商品任务，其他队列不受影响");
    } else {
      const endpoint = action === "resume" ? "resume" : "cancel";
      await jsonRequest(`/api/integrations/dianxiaomi/handoffs/${button.dataset.handoffId}/${endpoint}`, { method: "POST" });
      notify(action === "resume" ? "店小秘任务已重新排队" : "店小秘任务已取消");
    }
    await refreshDianxiaomiStatus();
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
});
document.querySelector("#dianxiaomi-delete-failed")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const count = dianxiaomiFailedCount;
  if (!count || !dianxiaomiCanDeleteFailed) return;
  if (!window.confirm(`确定永久删除全部 ${count} 条店小秘采集失败记录吗？其他状态的任务不会删除，此操作不可恢复。`)) return;
  button.disabled = true;
  try {
    const result = await jsonRequest("/api/integrations/dianxiaomi/handoffs/failed", { method: "DELETE" });
    notify(`已删除 ${result.deleted_count} 条失败记录`);
    await refreshDianxiaomiStatus();
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
});

document.querySelector("#dianxiaomi-queue-resume")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await jsonRequest("/api/integrations/dianxiaomi/queue/resume", { method: "POST" });
    window.dispatchEvent(new Event("aliexpress-monitor:dianxiaomi-queue-requested"));
    notify("已启动本轮店小秘采集；本轮之后新增的商品会留待下次启动");
    await refreshDianxiaomiStatus();
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#dianxiaomi-worker-select-save")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const workerId = document.querySelector("#dianxiaomi-worker-select")?.value;
  if (!workerId) {
    notify("请先选择店小秘采集电脑", true);
    return;
  }
  button.disabled = true;
  try {
    const result = await jsonRequest("/api/integrations/dianxiaomi/queue/worker", {
      method: "POST",
      body: JSON.stringify({ worker_id: workerId }),
    });
    await refreshDianxiaomiStatus();
    notify(result.paused
      ? "采集电脑已保存；该电脑离线，队列已暂停并等待它上线"
      : "店小秘采集电脑已保存；后续任务只由这台电脑领取");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#dianxiaomi-queue-pause")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const status = await jsonRequest("/api/integrations/dianxiaomi/queue/pause", { method: "POST" });
    await refreshDianxiaomiStatus();
    notify(status.paused ? "店小秘队列已暂停；正在处理的单条任务会结束，之后不再领取新任务" : "店小秘队列已暂停");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#collect-all")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = "排队中…";
  try {
    const result = await jsonRequest("/api/browser-collection/runs/collect-all", { method: "POST" });
    updateBrowserCollection(result.run);
    await refreshBrowserCollectionControl();
    if (result.queued_count) {
      notify(`已排入 ${result.queued_count} 家店铺的商品页采集；队列保持待启动，请选择采集电脑并点击“开始采集”。`);
    } else if (result.active_store_count) {
      notify("今日店铺任务已在队列或已有采集结果，没有重复创建");
    } else {
      notify("当前没有已加入监控商品的活跃店铺", true);
    }
  } catch (error) {
    notify(error.status === 401 ? "监控台登录已失效，请重新登录后再采集" : error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
});

document.querySelectorAll(".collect-one").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    const oldText = button.textContent;
    button.textContent = "采集中…";
    try {
      const snapshot = await jsonRequest(`/api/products/${button.dataset.productId}/collect`, { method: "POST" });
      notify(`采集完成：${snapshot.parse_status}`);
      window.location.reload();
    } catch (error) {
      notify(error.message, true);
      button.disabled = false;
      button.textContent = oldText;
    }
  });
});

document.querySelector("#product-business-role")?.addEventListener("change", async (event) => {
  const select = event.currentTarget;
  select.disabled = true;
  try {
    await jsonRequest(`/api/products/${select.dataset.productId}`, {
      method: "PATCH",
      body: JSON.stringify({ business_role: select.value }),
    });
    notify("商品归属已更新");
  } catch (error) {
    notify(error.message, true);
  } finally {
    select.disabled = false;
  }
});

document.querySelectorAll(".collect-reviews").forEach((button) => {
  button.addEventListener("click", async () => {
    const state = reviewStateForButton(button);
    button.disabled = true;
    if (state) state.textContent = "正在创建评论采集任务…";
    try {
      const task = await jsonRequest(`/api/products/${button.dataset.productId}/reviews/collection-tasks`, {
        method: "POST",
        body: JSON.stringify({ max_new_reviews: 100 }),
      });
      if (state) {
        state.dataset.taskId = String(task.id);
        state.dataset.taskStatus = task.status;
        state.textContent = `评论任务：${reviewTaskLabel(task.status)}。销量任务优先，Chrome 空闲后执行。`;
      }
      notify("评论采集已加入队列");
      void refreshProductReviewTask();
    } catch (error) {
      if (state) state.textContent = error.message;
      notify(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
});

function reviewTaskLabel(status) {
  return ({
    PENDING: "待采集",
    RUNNING: "采集中",
    SUCCEEDED: "采集完成",
    PARTIAL: "部分完成，可检查采集页面结构",
    FAILED: "采集失败，可重试",
    NEEDS_VERIFICATION: "等待你在本机 Chrome 中完成验证",
  })[status] || status;
}

function reviewStateForButton(button) {
  return button.closest(".row-actions")?.querySelector(".review-task-state") ||
    document.querySelector("#review-collection-state");
}

async function refreshReviewTaskStates() {
  const buttons = [...document.querySelectorAll(".collect-reviews")];
  if (!buttons.length) return;
  try {
    const tasks = await jsonRequest("/api/reviews/collection-tasks?limit=200");
    for (const button of buttons) {
      const state = reviewStateForButton(button);
      const task = tasks.find((item) => item.product_id === Number(button.dataset.productId));
      if (!state || !task) continue;
      state.dataset.taskId = String(task.id);
      state.dataset.taskStatus = task.status;
      state.textContent = `评论：${reviewTaskLabel(task.status)} · 新增 ${task.saved_count} 条${task.error_message ? ` · ${task.error_message}` : ""}`;
      button.disabled = ["PENDING", "RUNNING", "NEEDS_VERIFICATION"].includes(task.status);
    }
  } catch {
    // Keep the last rendered task state if a transient request fails.
  }
}

if (document.querySelector(".collect-reviews")) {
  void refreshReviewTaskStates();
  window.setInterval(refreshReviewTaskStates, 5000);
}

document.querySelectorAll(".deactivate-store").forEach((button) => {
  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const storeName = button.dataset.storeName || "该店铺";
    if (!window.confirm(`确定删除“${storeName}”的监控吗？店铺将从活跃监控中移除，商品及历史数据会保留。`)) return;
    button.disabled = true;
    try {
      await jsonRequest(`/api/stores/${button.dataset.storeId}/deactivate`, { method: "POST" });
      notify(`店铺“${storeName}”已移出监控，历史数据已保留`);
      window.location.reload();
    } catch (error) {
      notify(error.message, true);
      button.disabled = false;
    }
  });
});

document.querySelectorAll(".deactivate-product").forEach((button) => {
  button.addEventListener("click", async () => {
    if (!window.confirm("确定删除这个商品的监控吗？历史快照会保留，但它将不再参与自动采集和店铺汇总。")) return;
    button.disabled = true;
    try {
      await jsonRequest(`/api/products/${button.dataset.productId}/deactivate`, { method: "POST" });
      notify("商品已移出监控，历史数据已保留");
      window.location.reload();
    } catch (error) {
      notify(error.message, true);
      button.disabled = false;
    }
  });
});

document.querySelectorAll(".discover-store").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    const oldText = button.textContent;
    button.textContent = "发现中…";
    try {
      const result = await jsonRequest(`/api/stores/${button.dataset.storeId}/discover?limit=20`, { method: "POST" });
      if (result.parse_status === "failed") throw new Error(result.error_message || result.error_type);
      notify(`发现 ${result.discovered_count} 个商品，新增 ${result.added_count} 个`);
      window.location.reload();
    } catch (error) {
      notify(error.message, true);
      button.disabled = false;
      button.textContent = oldText;
    }
  });
});

function trackedProgressUrl(item) {
  return `${item.product_url}#monitor_product_id=${encodeURIComponent(item.platform_product_id)}`;
}

function updateCollectionProgress(progress) {
  ["completed", "pending", "failed", "total"].forEach((key) => {
    const target = document.querySelector(`[data-progress-value="${key}"]`);
    if (target) target.textContent = progress[key];
  });
  const date = document.querySelector("#collection-progress-date");
  if (date) date.textContent = `${progress.date} · 手动采集同步`;

  const nextLink = document.querySelector("#next-product-link");
  const nextLabel = document.querySelector("#next-product-label");
  if (nextLink && nextLabel) {
    if (progress.next_product) {
      nextLink.href = trackedProgressUrl(progress.next_product);
      nextLink.target = "_blank";
      nextLink.rel = "noreferrer";
      nextLink.classList.remove("disabled-link");
      nextLabel.textContent = "打开下一条商品";
    } else {
      nextLink.removeAttribute("href");
      nextLink.classList.add("disabled-link");
      nextLabel.textContent = "本轮已完成";
    }
  }

  const itemsById = new Map(progress.items.map((item) => [String(item.product_id), item]));
  document.querySelectorAll("tr[data-product-id]").forEach((row) => {
    const item = itemsById.get(row.dataset.productId);
    if (!item) return;
    const status = row.querySelector("[data-collection-status]");
    if (status) {
      status.className = `status ${item.status_class}`;
      status.textContent = item.status_label;
    }
    const cell = status?.parentElement;
    if (!cell) return;
    let time = cell.querySelector("[data-collection-time]");
    if (item.captured_at) {
      if (!time) {
        time = document.createElement("small");
        time.dataset.collectionTime = "";
        cell.appendChild(time);
      }
      time.textContent = new Date(item.captured_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
    } else if (time) {
      time.remove();
    }
  });
}

async function refreshCollectionProgress() {
  const section = document.querySelector("#collection-progress");
  if (!section) return;
  try {
    updateCollectionProgress(await jsonRequest(section.dataset.progressEndpoint));
  } catch {
    // A short backend restart should not interrupt the current collection flow.
  }
}

const browserPhaseLabels = {
  STORE_DISCOVERY: "采集店铺商品页",
  PRODUCT_COLLECTION: "单商品诊断",
  COMPLETED: "今日任务完成",
};

let browserCollectionItems = [];
let browserCollectionFilter = "pending";
let browserCollectionRun = null;
let browserCollectionControl = null;
const browserCollectionFilterStatuses = {
  pending: ["PENDING"],
  processing: ["RUNNING"],
  succeeded: ["SUCCEEDED"],
  partial: ["PARTIAL"],
  failed: ["FAILED", "SKIPPED"],
  verification: ["NEEDS_VERIFICATION"],
  identity: [],
};
const browserCollectionStatusLabels = {
  PENDING: "待处理",
  RUNNING: "采集中",
  SUCCEEDED: "采集成功",
  PARTIAL: "部分数据",
  FAILED: "失败",
  NEEDS_VERIFICATION: "待人工登录或验证",
  SKIPPED: "已跳过",
};

function renderBrowserCollectionFilter() {
  const section = document.querySelector("#browser-collection-status");
  const list = section?.querySelector("#browser-collection-items");
  if (!list) return;
  const statuses = browserCollectionFilterStatuses[browserCollectionFilter] || [];
  const filteredItems = browserCollectionItems.filter((item) => {
    if (browserCollectionFilter === "identity") return isIdentityDeferred(item);
    if (browserCollectionFilter === "failed") {
      return statuses.includes(item.status) && !isIdentityDeferred(item);
    }
    return statuses.includes(item.status);
  });
  const label = section.querySelector(`[data-browser-filter="${browserCollectionFilter}"] small`)?.textContent || "任务";
  const title = section.querySelector("#browser-list-title");
  const caption = section.querySelector("#browser-list-caption");
  if (title) title.textContent = `${label}任务 · ${filteredItems.length}`;
  if (caption) caption.textContent = browserCollectionFilter === "identity"
    ? "待审核项目不会写入商品、销量或续页游标；确认只记录 URL，仍需手动重新排队。"
    : browserCollectionFilter === "verification"
    ? browserCollectionRun?.paused_reason === "store_identity_review"
      ? "采集队列已暂停，等待人工确认当前店铺身份"
      : "请在本机 Chrome 中完成正常登录或验证后继续"
    : "店铺商品展示页读取已监控商品的 IXSPY 最近 7 日增量；缺失日期标记待补";
  list.innerHTML = filteredItems.map(renderBrowserCollectionItem).join("")
    || `<p class="muted-value">当前没有${escapeDianxiaomiHtml(label)}任务。</p>`;
  const counts = {
    pending: browserCollectionItems.filter((item) => item.status === "PENDING").length,
    processing: browserCollectionItems.filter((item) => item.status === "RUNNING").length,
    succeeded: browserCollectionItems.filter((item) => item.status === "SUCCEEDED").length,
    partial: browserCollectionItems.filter((item) => item.status === "PARTIAL").length,
    failed: browserCollectionItems.filter((item) => ["FAILED", "SKIPPED"].includes(item.status)
      && !isIdentityDeferred(item)).length,
    verification: browserCollectionItems.filter((item) => item.status === "NEEDS_VERIFICATION").length,
    identity: browserCollectionItems.filter((item) => isIdentityDeferred(item)).length,
  };
  Object.entries(counts).forEach(([key, count]) => {
    const target = section.querySelector(`[data-browser-filter-value="${key}"]`);
    if (target) target.textContent = count;
  });
}

function isIdentityDeferred(item) {
  return ["store_identity_deferred", "store_identity_review"].includes(item.error_type);
}

function isBrowserCollectionBulkRetryable(item) {
  if (isIdentityDeferred(item)) return false;
  return item.status === "FAILED"
    || (item.status === "SKIPPED" && item.error_type === "queue_cancelled");
}

function updateBrowserCollectionRetryButton(run) {
  const button = document.querySelector("#browser-run-retry-failed");
  if (!button) return;
  const items = run?.items || [];
  const count = items.filter(isBrowserCollectionBulkRetryable).length;
  const hasActiveItems = items.some((item) => ["RUNNING", "NEEDS_VERIFICATION"].includes(item.status));
  button.hidden = !run || count === 0;
  button.disabled = Boolean(run?.dispatch_enabled || hasActiveItems);
  button.dataset.runId = run?.id || "";
  button.dataset.retryableCount = String(count);
  button.textContent = `失败/已取消项重排（${count}）`;
  button.title = button.disabled
    ? "请先暂停队列并处理活动任务或平台验证；重排后仍需手动继续"
    : "将失败和因取消队列跳过的任务放回待处理；不会自动开始采集";
}

function renderBrowserCollectionItem(item) {
  const identityDeferred = isIdentityDeferred(item);
  const identityEvidence = item.identity_review_evidence || {};
  const identityDecision = item.identity_review_decision
    || identityEvidence.identity_review_decision
    || (item.identity_review_pending === true ? "pending" : null);
  const identityReview = identityDeferred && identityDecision === "pending";
  const identityConfirmed = identityDeferred && identityDecision === "confirmed";
  const identityRejected = identityDeferred && identityDecision === "rejected";
  const status = item.status === "SKIPPED" && item.error_type === "queue_cancelled"
    ? "已取消" : identityReview ? "待身份复核"
      : identityConfirmed ? "已确认 URL 身份" : identityRejected ? "已拒绝"
      : browserCollectionStatusLabels[item.status] || item.status;
  const statusClass = ["SUCCEEDED"].includes(item.status) ? "success"
    : identityDeferred ? "partial"
      : ["FAILED", "SKIPPED", "NEEDS_VERIFICATION"].includes(item.status) ? "error" : "";
  const reviewedUrl = escapeDianxiaomiHtml(item.identity_review_url || "");
  const visibleStoreIds = [
    identityEvidence.heading_store_id,
    ...(Array.isArray(identityEvidence.labeled_store_ids) ? identityEvidence.labeled_store_ids : []),
  ].filter((id) => id != null && String(id)).map(String);
  const matchedProductIds = Array.isArray(identityEvidence.matched_product_ids)
    ? identityEvidence.matched_product_ids.map(String)
    : [];
  const productMatchCount = Number.isInteger(identityEvidence.catalog_match_count)
    ? identityEvidence.catalog_match_count
    : matchedProductIds.length;
  const observedStoreName = identityEvidence.ixspy_store_name
    || identityEvidence.matched_store_name
    || (Array.isArray(identityEvidence.observed_store_names)
      ? identityEvidence.observed_store_names[0]
      : null);
  const expectedStoreName = identityEvidence.expected_store_name || item.title || "未识别";
  const reviewDetails = identityDeferred
    ? `<div class="browser-identity-evidence"><strong>${identityReview ? "人工核验店铺身份" : "店铺身份审核记录"}</strong><span>采集页面：${reviewedUrl
      ? `<a class="browser-identity-url" href="${reviewedUrl}" target="_blank" rel="noreferrer">${reviewedUrl}</a>`
      : "未记录"}</span><span>页面标题：${escapeDianxiaomiHtml(identityEvidence.page_title || "未识别")}</span><span>任务店铺名称：${escapeDianxiaomiHtml(expectedStoreName)}</span><span>页面店铺名称：${escapeDianxiaomiHtml(observedStoreName || "未识别")}</span><span>页面显示 ID：${escapeDianxiaomiHtml(visibleStoreIds.join("、") || "未识别")}</span><span>命中监控商品：${escapeDianxiaomiHtml(`${productMatchCount} 个${matchedProductIds.length ? `（${matchedProductIds.join("、")}）` : ""}`)}</span><span>店铺名称匹配：${identityEvidence.store_name_match === true ? "是" : "否"}</span></div>`
    : "";
  const action = identityReview
    ? `<div class="browser-identity-actions"><button class="link-button identity-confirm" type="button" data-browser-item-action="identity-confirm" data-browser-item-id="${item.id}">确定无误</button><button class="link-button identity-reject" type="button" data-browser-item-action="identity-reject" data-browser-item-id="${item.id}">错误</button></div>`
    : identityConfirmed
      ? `<button class="link-button" type="button" data-browser-item-action="retry" data-browser-item-id="${item.id}">重新排队</button>`
    : ["FAILED", "PARTIAL"].includes(item.status)
      ? `<button class="link-button" type="button" data-browser-item-action="retry" data-browser-item-id="${item.id}">重新排队</button>` : "";
  const kind = item.target_type === "STORE" ? "店铺商品发现" : `商品 #${item.product_id ?? "—"}`;
  const title = escapeDianxiaomiHtml(item.title || kind);
  const url = escapeDianxiaomiHtml(item.target_url);
  const details = [kind, `第 ${item.position} 项`, item.attempt_count ? `尝试 ${item.attempt_count} 次` : ""]
    .filter(Boolean).map(escapeDianxiaomiHtml).join(" · ");
  const error = item.error_message ? `<span class="browser-item-error">${escapeDianxiaomiHtml(item.error_message)}</span>` : "";
  const time = item.completed_at || item.claimed_at;
  return `<article class="browser-queue-item"><div class="browser-queue-item-main"><strong class="browser-queue-item-title">${title}</strong><span class="browser-queue-item-meta">${details} · <a href="${url}" target="_blank" rel="noreferrer">打开目标页</a></span>${time ? `<span class="browser-queue-item-meta">${escapeDianxiaomiHtml(formatDashboardDate(time))}</span>` : ""}${error}${reviewDetails}</div><div class="browser-queue-item-side"><span class="dianxiaomi-status ${statusClass}">${status}</span>${action}</div></article>`;
}

function formatBrowserTime(value) {
  if (!value) return "尚未收到扩展心跳";
  return `最近心跳：${new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  })}`;
}

function browserWorkerState(run) {
  if (!run) return { label: "任务尚未启动", className: "pending" };
  if (run.status === "PAUSED" && run.paused_reason === "manual_cancel") {
    return {
      label: run.current_item ? "剩余队列已取消；当前采集项结束后停止" : "剩余采集队列已取消",
      className: "pending",
    };
  }
  if (run.status === "PAUSED" && run.paused_reason === "manual_pause") {
    return { label: "采集队列已手动暂停", className: "pending" };
  }
  if (run.status === "PAUSED" && run.paused_reason === "collection_partial") {
    return { label: "店铺采集存在部分数据，队列已暂停，等待检查", className: "partial" };
  }
  if (run.status === "NEEDS_VERIFICATION") {
    if (run.paused_reason === "store_identity_review") {
      return { label: "等待人工确认店铺身份；队列已暂停", className: "partial" };
    }
    const item = (run.items || []).find((candidate) => candidate.status === "NEEDS_VERIFICATION");
    const message = item?.error_message || "";
    return {
      label: "等待 AliExpress 登录或验证；队列已暂停",
      className: "partial",
    };
  }
  if (run.status === "COMPLETED") return { label: "今日任务已完成", className: "success" };
  if (run.status === "PARTIAL") return { label: "任务结束，存在未完成项", className: "partial" };
  if (run.status === "FAILED") return { label: "任务失败", className: "failed" };
  if (!run.dispatch_enabled && Number(run.pending_count) > 0 && !run.current_item) {
    return { label: "任务已准备，等待选择电脑并手动开始", className: "pending" };
  }
  if (!run.last_heartbeat_at) return { label: "等待采集扩展心跳；尚未开始处理队列", className: "pending" };
  if (!run.chrome_online) return { label: "扩展心跳已超时；队列未在推进", className: "failed" };

  const currentItem = run.current_item;
  const progressTimes = [run.started_at, ...(run.items || []).flatMap((item) => [item.claimed_at, item.completed_at])]
    .map((value) => Date.parse(value || ""))
    .filter(Number.isFinite);
  const lastProgress = progressTimes.length ? Math.max(...progressTimes) : 0;
  const idleFor = lastProgress ? Date.now() - lastProgress : 0;

  if (currentItem?.status === "RUNNING") {
    const claimedAt = Date.parse(currentItem.claimed_at || "");
    if (Number.isFinite(claimedAt) && Date.now() - claimedAt > 5 * 60 * 1000) {
      return { label: "当前页面超过 5 分钟未回报；扩展仍在线", className: "partial" };
    }
    return { label: `正在处理${currentItem.target_type === "STORE" ? "店铺商品发现" : "商品数据"}`, className: "success" };
  }
  if (run.status === "PENDING") return { label: "扩展已连接，等待任务启动", className: "pending" };
  if (Number(run.pending_count) > 0 && idleFor > 2 * 60 * 1000) {
    return { label: "扩展在线但队列超过 2 分钟未推进；可能停滞", className: "partial" };
  }
  if (Number(run.pending_count) > 0) return { label: "扩展在线，等待领取下一项", className: "success" };
  return { label: "正在整理采集结果", className: "pending" };
}

function updateBrowserCollection(run) {
  browserCollectionRun = run;
  if (browserCollectionControl) updateBrowserCollectionControl(browserCollectionControl);
  else renderBrowserRunLockedRoles();
  const online = document.querySelector("#browser-online");
  const phase = document.querySelector("#browser-run-phase");
  const date = document.querySelector("#browser-run-date");
  const current = document.querySelector("#browser-current-item");
  const heartbeat = document.querySelector("#browser-last-heartbeat");
  const workerState = document.querySelector("#browser-worker-state");
  const challenge = document.querySelector("#browser-challenge-message");
  const recheck = document.querySelector("#browser-run-now");
  const pause = document.querySelector("#browser-run-pause");
  const cancel = document.querySelector("#browser-run-cancel");

  if (!run) {
    browserCollectionItems = [];
    renderBrowserCollectionFilter();
    if (online) {
      online.className = "status pending";
      online.textContent = "采集扩展离线";
    }
    if (workerState) {
      workerState.textContent = "任务尚未启动";
      workerState.className = "browser-worker-state status pending";
    }
    if (phase) phase.textContent = "等待启动";
    if (date) date.textContent = "今日任务尚未创建";
    if (current) current.textContent = "队列空闲";
    if (heartbeat) heartbeat.textContent = "尚未收到扩展心跳";
    if (challenge) challenge.hidden = true;
    if (recheck) recheck.hidden = false;
    if (pause) pause.hidden = true;
    if (cancel) cancel.hidden = true;
    updateBrowserCollectionRetryButton(null);
    return;
  }

  if (online) {
    online.className = `status ${run.chrome_online ? "success" : "pending"}`;
    online.textContent = run.chrome_online ? "采集扩展在线" : "采集扩展离线";
  }
  const taskState = browserWorkerState(run);
  if (workerState) {
    workerState.textContent = taskState.label;
    workerState.className = `browser-worker-state status ${taskState.className}`;
  }
  if (phase) phase.textContent = browserPhaseLabels[run.phase] || run.phase;
  if (date) date.textContent = `目标 IXSPY 数据日 ${run.latest_data_date || run.target_date} · 每日店铺页队列`;
  if (current) {
    const item = run.current_item;
    current.textContent = item ? `当前：${item.title || item.target_url}` : (run.phase === "COMPLETED" ? "今日队列已完成" : "等待扩展领取任务");
  }
  if (heartbeat) heartbeat.textContent = formatBrowserTime(run.last_heartbeat_at);
  browserCollectionItems = run.items || [];
  updateBrowserCollectionRetryButton(run);
  renderBrowserCollectionFilter();
  ["total_count", "succeeded_count", "partial_count", "failed_count", "pending_count"].forEach((key) => {
    const target = document.querySelector(`[data-browser-value="${key}"]`);
    if (target) target.textContent = run[key];
  });

  const needsVerification = run.status === "NEEDS_VERIFICATION";
  const identityReview = needsVerification && run.paused_reason === "store_identity_review";
  const platformVerification = needsVerification && !identityReview;
  const paused = run.status === "PAUSED";
  const cancelled = paused && run.paused_reason === "manual_cancel";
  if (challenge) {
    challenge.hidden = !platformVerification;
    if (platformVerification) {
      const item = (run.items || []).find((candidate) => candidate.status === "NEEDS_VERIFICATION");
      const message = item?.error_message || "平台需要人工登录或验证。请在本机 Chrome 中正常完成后，扩展会继续当前任务。";
      challenge.textContent = `${message} 请正常完成后等待扩展继续当前任务。`;
    }
  }
  if (recheck) recheck.hidden = cancelled;
  if (pause) {
    pause.hidden = !["PENDING", "RUNNING"].includes(run.status);
    pause.dataset.runId = run.id;
  }
  if (cancel) {
    cancel.hidden = cancelled || !["PENDING", "RUNNING", "NEEDS_VERIFICATION", "PAUSED"].includes(run.status);
    cancel.dataset.runId = run.id;
  }
}

function updateBrowserCollectionControl(control) {
  browserCollectionControl = control;
  const selector = document.querySelector("#browser-collector-select");
  const helperSelector = document.querySelector("#browser-helper-collectors-select");
  const selectedState = document.querySelector("#browser-selected-worker-state");
  const workerLoads = document.querySelector("#browser-worker-loads");
  const start = document.querySelector("#browser-run-start");
  if (!selector || !start || !control) return;

  const selected = control.selected_worker;
  const pendingHelperIds = new Set(
    Array.from(helperSelector?.selectedOptions || []).map((option) => option.value),
  );
  const rolesLocked = Boolean(browserCollectionRun?.primary_worker_id);
  selector.replaceChildren(new Option("请选择在线采集电脑", ""));
  for (const worker of control.workers || []) {
    const option = new Option(`${worker.display_label || worker.label} · ${worker.online ? "在线" : "离线"}`, worker.worker_id);
    selector.add(option);
  }
  selector.value = selected?.worker_id || "";
  selector.disabled = !(control.workers || []).length;

  if (helperSelector) {
    helperSelector.replaceChildren();
    for (const worker of control.workers || []) {
      if (worker.worker_id === selected?.worker_id) continue;
      const option = new Option(
        `${worker.display_label || worker.label} · ${worker.online ? "在线" : "离线"}`,
        worker.worker_id,
      );
      option.selected = !rolesLocked && pendingHelperIds.has(worker.worker_id);
      helperSelector.add(option);
    }
    helperSelector.disabled = rolesLocked || !(control.workers || []).length;
  }

  if (selectedState) {
    selectedState.textContent = selected
      ? `${rolesLocked ? "下轮主采集电脑" : "手动启动主采集电脑"}：${selected.display_label || selected.label} · ${selected.online ? "在线" : "离线"}`
      : "尚未选择主采集电脑";
  }
  renderBrowserRunLockedRoles();
  if (workerLoads) {
    workerLoads.replaceChildren();
    const workers = control.workers || [];
    if (!workers.length) {
      const empty = document.createElement("p");
      empty.className = "muted-value";
      empty.textContent = "尚无采集电脑心跳";
      workerLoads.append(empty);
    }
    for (const worker of workers) {
      const row = document.createElement("div");
      row.className = "browser-worker-load";
      row.dataset.workerId = worker.worker_id;
      row.dataset.workerClaimCount = String(Number(worker.active_claim_count) || 0);
      row.setAttribute("aria-label", `${worker.display_label || worker.label}，${worker.online ? "在线" : "离线"}，领取 ${Number(worker.active_claim_count) || 0} 项`);
      const name = document.createElement("span");
      name.className = "browser-worker-load-name";
      name.textContent = worker.display_label || worker.label;
      const state = document.createElement("span");
      state.className = "browser-worker-load-state";
      state.textContent = `${worker.online ? "在线" : "离线"} · 领取 ${Number(worker.active_claim_count) || 0} 项`;
      row.append(name, state);
      workerLoads.append(row);
    }
  }

  const status = browserCollectionRun?.status;
  const terminal = ["COMPLETED", "PARTIAL", "FAILED"].includes(status)
    && Number(browserCollectionRun?.pending_count) === 0;
  start.textContent = control.dispatch_enabled
    ? rolesLocked ? "采集中" : "锁定本轮主辅电脑"
    : ["PAUSED", "NEEDS_VERIFICATION"].includes(status) ? "继续采集"
      : terminal ? "今日已完成" : "开始采集";
  start.dataset.runId = browserCollectionRun?.id || "";
  start.disabled = !control.can_start || (control.dispatch_enabled && rolesLocked);
}

function renderBrowserRunLockedRoles() {
  const roles = document.querySelector("#browser-run-locked-roles");
  if (!roles) return;
  const primaryWorkerId = browserCollectionRun?.primary_worker_id || "";
  const helperWorkerIds = browserCollectionRun?.helper_worker_ids || [];
  roles.hidden = !primaryWorkerId;
  roles.dataset.primaryWorkerId = primaryWorkerId;
  const workers = browserCollectionControl?.workers || [];
  const workerById = new Map(workers.map((worker) => [worker.worker_id, worker]));
  const describe = (workerId) => {
    const worker = workerById.get(workerId);
    return worker ? `${worker.display_label || worker.label}（${workerId}）` : workerId;
  };
  const primaryLabel = document.querySelector("#browser-run-primary-label");
  const helperLabels = document.querySelector("#browser-run-helper-labels");
  if (primaryLabel) primaryLabel.textContent = primaryWorkerId ? describe(primaryWorkerId) : "";
  if (helperLabels) helperLabels.textContent = primaryWorkerId
    ? helperWorkerIds.map(describe).join("、") || "无"
    : "尚未锁定";
}

async function refreshBrowserCollectionControl() {
  try {
    updateBrowserCollectionControl(await jsonRequest("/api/browser-collection/control"));
  } catch {
    // Keep the last known ownership state visible during a short backend interruption.
  }
}

async function refreshBrowserCollection() {
  const section = document.querySelector("#browser-collection-status");
  if (!section) return;
  try {
    updateBrowserCollection(await jsonRequest(section.dataset.statusEndpoint));
  } catch (error) {
    if (!String(error.message).includes("404")) return;
    updateBrowserCollection(null);
  }
}

document.querySelector("#browser-collector-select")?.addEventListener("change", async (event) => {
  const selector = event.currentTarget;
  const workerId = selector.value;
  const currentWorkerId = browserCollectionControl?.selected_worker?.worker_id || "";
  if (!workerId || workerId === currentWorkerId) {
    selector.value = currentWorkerId;
    return;
  }
  selector.disabled = true;
  try {
    const control = await jsonRequest("/api/browser-collection/control/worker", {
      method: "POST",
      body: JSON.stringify({ worker_id: workerId }),
    });
    updateBrowserCollectionControl(control);
    await refreshBrowserCollection();
    notify("已设置下轮主采集电脑；本轮已锁定的主辅角色不会改变。");
  } catch (error) {
    selector.value = currentWorkerId;
    notify(error.message, true);
    await refreshBrowserCollectionControl();
  } finally {
    selector.disabled = !(browserCollectionControl?.workers || []).length;
  }
});

document.querySelector("#browser-run-start")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const rolesLocked = Boolean(browserCollectionRun?.primary_worker_id);
    const helperSelector = document.querySelector("#browser-helper-collectors-select");
    const requestOptions = { method: "POST" };
    if (!rolesLocked) {
      requestOptions.body = JSON.stringify({
        helper_worker_ids: Array.from(helperSelector?.selectedOptions || []).map((option) => option.value),
      });
    }
    const resuming = ["PAUSED", "NEEDS_VERIFICATION"].includes(browserCollectionRun?.status)
      && browserCollectionRun?.id;
    const run = resuming
      ? await jsonRequest(`/api/browser-collection/runs/${browserCollectionRun.id}/resume`, requestOptions)
      : await jsonRequest("/api/browser-collection/runs/start-today", requestOptions);
    updateBrowserCollection(run);
    await refreshBrowserCollectionControl();
    if (run.dispatch_enabled) {
      window.dispatchEvent(new Event("aliexpress-monitor:browser-run-requested"));
      notify(resuming ? "队列已恢复，本轮主采集电脑和辅助电脑可继续领取任务" : "采集已开始，本轮主采集电脑和已启用辅助电脑可领取任务");
    } else {
      notify("队列仍未启动，请确认所选电脑在线并重试。", true);
    }
  } catch (error) {
    notify(error.message, true);
    await refreshBrowserCollectionControl();
  } finally {
    button.disabled = false;
    await refreshBrowserCollectionControl();
  }
});

document.querySelector("#browser-run-now")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const run = await jsonRequest("/api/browser-collection/runs/ensure", {
      method: "POST",
      body: JSON.stringify({ trigger: "MANUAL" }),
    });
    updateBrowserCollection(run);
    await refreshBrowserCollectionControl();
    notify("今日任务已重新检查并保持待启动；不会自动开始采集。");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#browser-run-retry-failed")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const runId = button.dataset.runId;
  const retryableCount = Number(button.dataset.retryableCount || 0);
  if (!runId || retryableCount <= 0) return;
  const confirmed = window.confirm(
    `将 ${retryableCount} 个失败或已取消任务重新放入待处理？部分数据与身份审核任务不受影响；队列会保持暂停，需你手动继续。`
  );
  if (!confirmed) return;

  button.disabled = true;
  try {
    const result = await jsonRequest(`/api/browser-collection/runs/${runId}/retry-failed`, {
      method: "POST",
    });
    updateBrowserCollection(result.run);
    await refreshBrowserCollectionControl();
    notify(`已将 ${result.requeued_count} 个失败/已取消任务放回待处理；队列仍暂停。`);
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#browser-collection-status")?.addEventListener("click", async (event) => {
  const filter = event.target.closest("[data-browser-filter]");
  if (filter) {
    browserCollectionFilter = filter.dataset.browserFilter;
    document.querySelectorAll("[data-browser-filter]").forEach((button) => {
      const selected = button === filter;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    renderBrowserCollectionFilter();
    return;
  }
  const action = event.target.closest("[data-browser-item-action]");
  if (!action) return;
  action.disabled = true;
  try {
    const actionType = action.dataset.browserItemAction;
    const isIdentityDecision = ["identity-confirm", "identity-reject"].includes(actionType);
    const endpoint = actionType === "identity-confirm" ? "confirm" : "reject";
    const run = await jsonRequest(
      isIdentityDecision
        ? `/api/browser-collection/items/${action.dataset.browserItemId}/identity-review/${endpoint}`
        : `/api/browser-collection/items/${action.dataset.browserItemId}/retry`,
      { method: "POST" },
    );
    updateBrowserCollection(run);
    await refreshBrowserCollectionControl();
    notify(actionType === "identity-confirm"
      ? "已确认该店铺的 URL 身份；本轮仍保持跳过，需手动重新排队。"
      : actionType === "identity-reject"
        ? "已记录人工拒绝；本轮队列不受影响。"
        : "该店铺已重新排队；任务将由在线采集电脑领取。");
  } catch (error) {
    notify(error.message, true);
  } finally {
    action.disabled = false;
  }
});

document.querySelector("#browser-run-pause")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!button.dataset.runId) return;
  button.disabled = true;
  try {
    const run = await jsonRequest(`/api/browser-collection/runs/${button.dataset.runId}/pause`, { method: "POST" });
    updateBrowserCollection(run);
    notify(run.current_item ? "队列已暂停；当前店铺任务完成后不会继续下一项" : "采集队列已暂停");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#browser-run-cancel")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!button.dataset.runId) return;
  const confirmed = window.confirm(
    "取消剩余采集队列？待处理任务将标记为已取消并保留记录；正在采集的店铺任务可能完成，但之后不会继续下一项。"
  );
  if (!confirmed) return;

  button.disabled = true;
  try {
    const run = await jsonRequest(`/api/browser-collection/runs/${button.dataset.runId}/cancel`, { method: "POST" });
    updateBrowserCollection(run);
    notify("剩余队列已取消；已完成数据保留。可从监控店铺列表单独启动店铺页采集。");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

if (document.querySelector("#collection-progress")) {
  void refreshCollectionProgress();
  window.setInterval(refreshCollectionProgress, 3000);
}

if (document.querySelector("#browser-collection-status")) {
  void refreshBrowserCollection();
  void refreshBrowserCollectionControl();
  window.setInterval(refreshBrowserCollection, 3000);
  window.setInterval(refreshBrowserCollectionControl, 3000);
}
