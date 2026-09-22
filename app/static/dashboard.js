const toast = document.querySelector("#toast");

function notify(message, error = false) {
  if (!toast) return;
  toast.textContent = message;
  toast.className = `toast show${error ? " error" : ""}`;
  window.setTimeout(() => { toast.className = "toast"; }, 3200);
}

async function jsonRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`);
  return body;
}

document.querySelector("#store-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  try {
    const store = await jsonRequest("/api/stores", { method: "POST", body: JSON.stringify({ name: data.get("name"), url: data.get("url") }) });
    notify("店铺已添加，正在发现公开商品…");
    const discovery = await jsonRequest(`/api/stores/${store.id}/discover?limit=20`, { method: "POST" });
    if (discovery.parse_status === "failed") {
      notify(`店铺已保存，但商品发现失败：${discovery.error_message || discovery.error_type}`, true);
    } else {
      notify(`发现 ${discovery.discovered_count} 个商品，新增监控 ${discovery.added_count} 个`);
    }
    window.location.reload();
  } catch (error) { notify(error.message, true); }
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

async function refreshDianxiaomiStatus() {
  const section = document.querySelector("#dianxiaomi-status");
  if (!section) return;
  try {
    const status = await jsonRequest(section.dataset.statusEndpoint);
    Object.keys(status).forEach((key) => {
      const target = section.querySelector(`[data-dianxiaomi-value="${key}"]`);
      if (target) target.textContent = status[key];
    });
    const latest = section.querySelector("#dianxiaomi-latest");
    const item = status.items?.[0] || status.latest?.[0];
    if (latest && item) latest.textContent = `商品 ID ${item.platform_product_id || item.product_id || "—"} · ${item.status}${item.error_message ? ` · ${item.error_message}` : ""}`;
    const list = section.querySelector("#dianxiaomi-items");
    if (list) list.innerHTML = (status.items || []).map(renderDianxiaomiItem).join("") || '<p class="muted-value">当前没有店小秘任务。</p>';
  } catch {
    // The integration is optional; a temporary API restart should not affect the dashboard.
  }
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

document.querySelector("#collect-all")?.addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  event.currentTarget.textContent = "采集中…";
  try {
    const result = await jsonRequest("/api/jobs/collect-now", { method: "POST" });
    notify(`采集完成：成功 ${result.succeeded}，失败 ${result.failed}`);
    window.location.reload();
  } catch (error) {
    notify(error.message, true);
    event.currentTarget.disabled = false;
    event.currentTarget.textContent = "立即采集全部";
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
  STORE_DISCOVERY: "刷新店铺前 20",
  PRODUCT_COLLECTION: "采集商品数据",
  COMPLETED: "今日任务完成",
};

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

function updateBrowserCollection(run) {
  const online = document.querySelector("#browser-online");
  const phase = document.querySelector("#browser-run-phase");
  const date = document.querySelector("#browser-run-date");
  const current = document.querySelector("#browser-current-item");
  const heartbeat = document.querySelector("#browser-last-heartbeat");
  const challenge = document.querySelector("#browser-challenge-message");
  const resume = document.querySelector("#browser-run-resume");

  if (!run) {
    if (online) {
      online.className = "status pending";
      online.textContent = "Chrome 离线";
    }
    if (phase) phase.textContent = "等待启动";
    if (date) date.textContent = "今日任务尚未创建";
    if (current) current.textContent = "队列空闲";
    if (heartbeat) heartbeat.textContent = "尚未收到扩展心跳";
    if (challenge) challenge.hidden = true;
    if (resume) resume.hidden = true;
    return;
  }

  if (online) {
    online.className = `status ${run.chrome_online ? "success" : "pending"}`;
    online.textContent = run.chrome_online ? "Chrome 在线" : "Chrome 离线";
  }
  if (phase) phase.textContent = browserPhaseLabels[run.phase] || run.phase;
  if (date) date.textContent = `${run.target_date} · 专用 Chrome 顺序采集`;
  if (current) {
    const item = run.current_item;
    current.textContent = item ? `当前：${item.title || item.target_url}` : (run.phase === "COMPLETED" ? "今日队列已完成" : "等待扩展领取任务");
  }
  if (heartbeat) heartbeat.textContent = formatBrowserTime(run.last_heartbeat_at);
  ["total_count", "succeeded_count", "partial_count", "failed_count", "pending_count"].forEach((key) => {
    const target = document.querySelector(`[data-browser-value="${key}"]`);
    if (target) target.textContent = run[key];
  });

  const needsVerification = run.status === "NEEDS_VERIFICATION";
  if (challenge) challenge.hidden = !needsVerification;
  if (resume) {
    resume.hidden = !needsVerification;
    resume.dataset.runId = run.id;
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

document.querySelector("#browser-run-now")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const run = await jsonRequest("/api/browser-collection/runs/ensure", {
      method: "POST",
      body: JSON.stringify({ trigger: "MANUAL" }),
    });
    updateBrowserCollection(run);
    notify(run.chrome_online ? "今日浏览器采集任务已启动" : "任务已创建，正在等待专用 Chrome 扩展上线");
  } catch (error) {
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#browser-run-resume")?.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!button.dataset.runId) return;
  button.disabled = true;
  try {
    const run = await jsonRequest(`/api/browser-collection/runs/${button.dataset.runId}/resume`, { method: "POST" });
    updateBrowserCollection(run);
    notify("任务已恢复，将从暂停位置继续");
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
  window.setInterval(refreshBrowserCollection, 3000);
}
