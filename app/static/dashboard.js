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
    const discovery = await jsonRequest(`/api/stores/${store.id}/discover?limit=100`, { method: "POST" });
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
  const payload = { store_id: Number(data.get("store_id")) };
  if (/^\d+$/.test(target)) payload.aliexpress_product_id = target;
  else payload.url = target;
  try {
    await jsonRequest("/api/products", { method: "POST", body: JSON.stringify(payload) });
    notify("商品已加入监控");
    window.location.reload();
  } catch (error) { notify(error.message, true); }
});

document.querySelectorAll(".store-rename-form").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type='submit']");
    const name = String(new FormData(form).get("name") || "").trim();
    if (!name) {
      notify("店铺名称不能为空", true);
      return;
    }
    button.disabled = true;
    const oldText = button.textContent;
    button.textContent = "保存中…";
    try {
      await jsonRequest(`/api/stores/${form.dataset.storeId}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      notify("店铺名称已更新");
      window.location.reload();
    } catch (error) {
      notify(error.message, true);
      button.disabled = false;
      button.textContent = oldText;
    }
  });
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

document.querySelectorAll(".discover-store").forEach((button) => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    const oldText = button.textContent;
    button.textContent = "发现中…";
    try {
      const result = await jsonRequest(`/api/stores/${button.dataset.storeId}/discover?limit=100`, { method: "POST" });
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
