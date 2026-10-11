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
    await jsonRequest("/api/stores", { method: "POST", body: JSON.stringify({ name: data.get("name"), url: data.get("url") }) });
    notify("店铺已添加；请在 Orders 商品页用扩展按近 7 日增量筛选并入池");
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
  button.textContent = "加入队列…";
  try {
    const result = await jsonRequest("/api/integrations/dianxiaomi/handoffs", {
      method: "POST",
      body: JSON.stringify({ product_ids: ids }),
    });
    const reused = result.reused_count ? `，${result.reused_count} 个已在队列中` : "";
    notify(`已加入店小秘队列：${result.queued_count} 个${reused}`);
    window.dispatchEvent(new Event("aliexpress-monitor:dianxiaomi-queue-requested"));
    await refreshDianxiaomiStatus();
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = oldText;
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
  const label = section.querySelector(`[data-dianxiaomi-filter="${dianxiaomiActiveFilter}"] small`)?.textContent || "该状态";
  const title = section.querySelector("#dianxiaomi-list-title");
  const caption = section.querySelector("#dianxiaomi-list-caption");
  if (title) title.textContent = `${label}商品 · ${filteredItems.length}`;
  if (caption) caption.textContent = `最近 100 项中显示${label}状态；上方汇总为全队列总数`;
  list.innerHTML = filteredItems.map(renderDianxiaomiItem).join("") || `<p class="muted-value">当前没有${escapeDianxiaomiHtml(label)}商品。</p>`;
}

async function refreshDianxiaomiStatus() {
  const section = document.querySelector("#dianxiaomi-status");
  if (!section) return;
  try {
    const status = await jsonRequest(section.dataset.statusEndpoint);
    Object.keys(status).forEach((key) => {
      const target = section.querySelector(`[data-dianxiaomi-value="${key}"]`);
      if (target) target.textContent = status[key];
    });
    dianxiaomiItems = status.items || [];
    renderDianxiaomiFilter();
    updateDianxiaomiQueue(status);
  } catch {
    // The integration is optional; a temporary API restart should not affect the dashboard.
  }
}

function updateDianxiaomiQueue(status) {
  const online = document.querySelector("#dianxiaomi-worker-online");
  const state = document.querySelector("#dianxiaomi-queue-state");
  const current = document.querySelector("#dianxiaomi-current-item");
  const heartbeat = document.querySelector("#dianxiaomi-last-heartbeat");
  const resume = document.querySelector("#dianxiaomi-queue-resume");
  const pause = document.querySelector("#dianxiaomi-queue-pause");
  if (online) {
    online.className = `status ${status.worker_online ? "success" : "pending"}`;
    online.textContent = status.worker_online ? "本机 Chrome worker 在线" : "本机 Chrome worker 离线";
  }
  if (state) {
    state.textContent = status.paused
      ? status.paused_reason === "needs_confirmation" ? "等待人工确认，队列已暂停" : "店小秘队列已手动暂停"
      : status.needs_confirmation_count ? "有任务等待人工确认"
      : status.stalled_count ? "任务响应超时，等待扩展恢复"
      : (status.processing_count || status.submitted_count) ? "店小秘队列正在处理"
      : status.queued_count ? (status.worker_online ? "队列运行中，等待下一项" : "队列待运行，等待本机 Chrome")
      : "当前没有待处理任务";
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
    resume.hidden = !status.paused && !status.queued_count;
    resume.textContent = status.paused_reason === "needs_confirmation"
      ? "验证完成，继续队列"
      : status.paused ? "继续店小秘队列" : "启动店小秘队列";
  }
  if (pause) pause.hidden = Boolean(status.paused) || !(status.queued_count || status.processing_count || status.submitted_count);
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

function dianxiaomiNextAction(item) {
  if (item.status === "NEEDS_CONFIRMATION") {
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
  const action = ["NEEDS_CONFIRMATION", "FAILED"].includes(item.status)
    ? `<button class="link-button" type="button" data-dianxiaomi-action="resume" data-handoff-id="${item.id}">重新排队</button>`
    : ["QUEUED", "CLAIMED", "OPENED", "FILLED", "COLLECTING"].includes(item.status)
      ? `<button class="link-button danger" type="button" data-dianxiaomi-action="cancel" data-handoff-id="${item.id}">取消</button>`
      : "";
  const label = `商品 ID ${escapeDianxiaomiHtml(item.platform_product_id || item.product_id || "—")}`;
  const store = escapeDianxiaomiHtml(item.store_name || `店铺 #${item.store_id}`);
  const url = escapeDianxiaomiHtml(item.target_url);
  const error = item.error_message ? ` · ${escapeDianxiaomiHtml(item.error_message)}` : "";
  const nextAction = dianxiaomiNextAction(item);
  return `<article class="dianxiaomi-item"><div class="dianxiaomi-item-main"><strong class="dianxiaomi-item-title">${label}</strong><span class="dianxiaomi-item-meta">${store} · <a href="${url}" target="_blank" rel="noreferrer">${url}</a></span><span class="dianxiaomi-item-meta">${escapeDianxiaomiHtml(formatDashboardDate(item.requested_at))}${error}</span>${nextAction ? `<span class="dianxiaomi-item-hint">${escapeDianxiaomiHtml(nextAction)}</span>` : ""}</div><div class="dianxiaomi-item-side"><span class="dianxiaomi-status ${statusClass}">${status}</span><span class="dianxiaomi-item-actions">${action}</span></div></article>`;
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
document.querySelector("#dianxiaomi-items")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-dianxiaomi-action]");
  if (!button) return;
  button.disabled = true;
  try {
    const action = button.dataset.dianxiaomiAction;
    const endpoint = action === "resume" ? "resume" : "cancel";
    await jsonRequest(`/api/integrations/dianxiaomi/handoffs/${button.dataset.handoffId}/${endpoint}`, { method: "POST" });
    notify(action === "resume" ? "店小秘任务已重新排队" : "店小秘任务已取消");
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
    notify("店小秘队列已恢复，将从待处理位置继续");
    await refreshDianxiaomiStatus();
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
  const filteredItems = browserCollectionItems.filter((item) => statuses.includes(item.status));
  const label = section.querySelector(`[data-browser-filter="${browserCollectionFilter}"] small`)?.textContent || "任务";
  const title = section.querySelector("#browser-list-title");
  const caption = section.querySelector("#browser-list-caption");
  if (title) title.textContent = `${label}任务 · ${filteredItems.length}`;
  if (caption) caption.textContent = browserCollectionFilter === "verification"
    ? "请在本机 Chrome 中完成正常登录或验证后继续"
    : "店铺商品展示页读取已监控商品的 IXSPY 最近 7 日增量；缺失日期标记待补";
  list.innerHTML = filteredItems.map(renderBrowserCollectionItem).join("")
    || `<p class="muted-value">当前没有${escapeDianxiaomiHtml(label)}任务。</p>`;
  const counts = {
    pending: browserCollectionItems.filter((item) => item.status === "PENDING").length,
    processing: browserCollectionItems.filter((item) => item.status === "RUNNING").length,
    succeeded: browserCollectionItems.filter((item) => item.status === "SUCCEEDED").length,
    partial: browserCollectionItems.filter((item) => item.status === "PARTIAL").length,
    failed: browserCollectionItems.filter((item) => ["FAILED", "SKIPPED"].includes(item.status)).length,
    verification: browserCollectionItems.filter((item) => item.status === "NEEDS_VERIFICATION").length,
  };
  Object.entries(counts).forEach(([key, count]) => {
    const target = section.querySelector(`[data-browser-filter-value="${key}"]`);
    if (target) target.textContent = count;
  });
}

function renderBrowserCollectionItem(item) {
  const status = item.status === "SKIPPED" && item.error_type === "queue_cancelled"
    ? "已取消" : browserCollectionStatusLabels[item.status] || item.status;
  const statusClass = ["SUCCEEDED"].includes(item.status) ? "success"
    : ["FAILED", "SKIPPED", "NEEDS_VERIFICATION"].includes(item.status) ? "error" : "";
  const action = ["FAILED", "PARTIAL"].includes(item.status)
    ? `<button class="link-button" type="button" data-browser-item-action="retry" data-browser-item-id="${item.id}">重新排队</button>` : "";
  const kind = item.target_type === "STORE" ? "店铺商品发现" : `商品 #${item.product_id ?? "—"}`;
  const title = escapeDianxiaomiHtml(item.title || kind);
  const url = escapeDianxiaomiHtml(item.target_url);
  const details = [kind, `第 ${item.position} 项`, item.attempt_count ? `尝试 ${item.attempt_count} 次` : ""]
    .filter(Boolean).map(escapeDianxiaomiHtml).join(" · ");
  const error = item.error_message ? `<span class="browser-item-error">${escapeDianxiaomiHtml(item.error_message)}</span>` : "";
  const time = item.completed_at || item.claimed_at;
  return `<article class="browser-queue-item"><div class="browser-queue-item-main"><strong class="browser-queue-item-title">${title}</strong><span class="browser-queue-item-meta">${details} · <a href="${url}" target="_blank" rel="noreferrer">打开目标页</a></span>${time ? `<span class="browser-queue-item-meta">${escapeDianxiaomiHtml(formatDashboardDate(time))}</span>` : ""}${error}</div><div class="browser-queue-item-side"><span class="dianxiaomi-status ${statusClass}">${status}</span>${action}</div></article>`;
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
  if (run.status === "NEEDS_VERIFICATION") {
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
  if (date) date.textContent = `${run.target_date} · 每日店铺页队列`;
  if (current) {
    const item = run.current_item;
    current.textContent = item ? `当前：${item.title || item.target_url}` : (run.phase === "COMPLETED" ? "今日队列已完成" : "等待扩展领取任务");
  }
  if (heartbeat) heartbeat.textContent = formatBrowserTime(run.last_heartbeat_at);
  browserCollectionItems = run.items || [];
  renderBrowserCollectionFilter();
  ["total_count", "succeeded_count", "partial_count", "failed_count", "pending_count"].forEach((key) => {
    const target = document.querySelector(`[data-browser-value="${key}"]`);
    if (target) target.textContent = run[key];
  });

  const needsVerification = run.status === "NEEDS_VERIFICATION";
  const paused = run.status === "PAUSED";
  const cancelled = paused && run.paused_reason === "manual_cancel";
  if (challenge) {
    challenge.hidden = !needsVerification;
    if (needsVerification) {
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
  const selectedState = document.querySelector("#browser-selected-worker-state");
  const start = document.querySelector("#browser-run-start");
  if (!selector || !start || !control) return;

  const selected = control.selected_worker;
  selector.replaceChildren(new Option("请选择在线采集电脑", ""));
  for (const worker of control.workers || []) {
    const option = new Option(`${worker.label} · ${worker.online ? "在线" : "离线"}`, worker.worker_id);
    selector.add(option);
  }
  selector.value = selected?.worker_id || "";
  selector.disabled = !(control.workers || []).length;

  if (selectedState) {
    if (control.handoff_pending) {
      selectedState.textContent = `等待旧采集任务结束（${control.draining_count}）`;
    } else if (!selected) {
      selectedState.textContent = "尚未选择采集电脑";
    } else {
      selectedState.textContent = `当前采集电脑：${selected.label} · ${selected.online ? "在线" : "离线"}`;
    }
  }

  const status = browserCollectionRun?.status;
  const terminal = ["COMPLETED", "PARTIAL", "FAILED"].includes(status)
    && Number(browserCollectionRun?.pending_count) === 0;
  start.textContent = control.dispatch_enabled
    ? "采集中"
    : ["PAUSED", "NEEDS_VERIFICATION"].includes(status) ? "继续采集"
      : terminal ? "今日已完成" : "开始采集";
  start.dataset.runId = browserCollectionRun?.id || "";
  start.disabled = !control.can_start || control.dispatch_enabled;
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
  const activeRun = ["PENDING", "RUNNING", "NEEDS_VERIFICATION", "PAUSED"]
    .includes(browserCollectionControl?.run_status);
  const switchingActiveOwner = Boolean(currentWorkerId && currentWorkerId !== workerId && activeRun);
  if (switchingActiveOwner && !window.confirm("切换采集电脑会暂停领取新任务，并等待旧电脑正在处理的任务结束。继续切换？")) {
    selector.value = currentWorkerId;
    return;
  }

  selector.disabled = true;
  try {
    const control = await jsonRequest("/api/browser-collection/control/worker", {
      method: "POST",
      body: JSON.stringify({ worker_id: workerId, confirm_handoff: switchingActiveOwner }),
    });
    updateBrowserCollectionControl(control);
    await refreshBrowserCollection();
    notify("采集电脑已选择；队列仍待启动，请单独点击“开始采集”。");
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
    const resuming = ["PAUSED", "NEEDS_VERIFICATION"].includes(browserCollectionRun?.status)
      && browserCollectionRun?.id;
    const run = resuming
      ? await jsonRequest(`/api/browser-collection/runs/${browserCollectionRun.id}/resume`, { method: "POST" })
      : await jsonRequest("/api/browser-collection/runs/start-today", { method: "POST" });
    updateBrowserCollection(run);
    await refreshBrowserCollectionControl();
    if (run.dispatch_enabled) {
      window.dispatchEvent(new Event("aliexpress-monitor:browser-run-requested"));
      notify(resuming ? "队列已恢复，将从暂停位置继续" : "采集已开始，由所选电脑处理队列");
    } else {
      notify("队列仍未启动，请确认所选电脑在线且旧任务已结束。", true);
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
    const run = await jsonRequest(`/api/browser-collection/items/${action.dataset.browserItemId}/retry`, { method: "POST" });
    updateBrowserCollection(run);
    await refreshBrowserCollectionControl();
    notify("任务已重新排队；请在队列区点击“开始采集”后运行。");
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
