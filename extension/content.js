(function () {
  const collector = globalThis.AliExpressMonitorPageCollector;
  if (!collector) return;

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "collect-visible-page") return false;
    collector.collect(message.target_type)
      .then(sendResponse)
      .catch((error) => sendResponse({ state: "failed", message: error.message || "页面读取失败" }));
    return true;
  });

  if (window.top !== window || !location.pathname.match(/\/item\/\d+/i)) return;
  if (document.getElementById("aliexpress-monitor-collector")) return;

  const host = document.createElement("div");
  host.id = "aliexpress-monitor-collector";
  host.style.cssText = "position:fixed;right:20px;top:20px;bottom:auto;z-index:2147483647;";
  const shadow = host.attachShadow({ mode: "closed" });
  shadow.innerHTML = `
    <style>
      * { box-sizing: border-box; }
      .panel { width: 260px; padding: 14px; color: #17352d; background: #fff; border: 1px solid #cfe3da;
        border-radius: 10px; box-shadow: 0 8px 28px rgba(20, 60, 45, .18); font: 14px/1.45 Arial, sans-serif; }
      .title { margin: 0 0 5px; font-size: 15px; font-weight: 700; }
      .hint { margin: 0 0 10px; color: #587168; font-size: 12px; }
      button { width: 100%; padding: 9px 10px; color: #fff; background: #27835f; border: 0; border-radius: 6px;
        cursor: pointer; font-weight: 700; }
      button:disabled { cursor: wait; opacity: .65; }
      .result { min-height: 18px; margin: 9px 0 0; color: #27835f; font-size: 12px; }
      .error { color: #c03b35; }
    </style>
    <section class="panel" aria-label="AliExpress 监控采集">
      <p class="title">AliExpress 监控采集</p>
      <p class="hint">当前商品页已识别，可读取公开可见数据</p>
      <button type="button">采集当前商品</button>
      <p class="result" role="status" aria-live="polite"></p>
    </section>`;
  document.documentElement.appendChild(host);

  const button = shadow.querySelector("button");
  const result = shadow.querySelector(".result");
  button.addEventListener("click", async () => {
    button.disabled = true;
    result.className = "result";
    result.textContent = "正在读取公开数据…";
    try {
      const collected = await collector.collect("PRODUCT");
      if (collected.state === "challenge") throw new Error("请先正常完成 AliExpress 验证");
      if (!collected.payload) throw new Error(collected.message || "没有读取到商品公开数据");
      const response = await new Promise((resolve, reject) => {
        chrome.runtime.sendMessage(
          { type: "sync-product-snapshot", payload: collected.payload },
          (value) => {
            if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
            else resolve(value);
          },
        );
      });
      if (!response?.ok) throw new Error(response?.body?.detail || `同步失败 (${response?.status || 0})`);
      result.textContent = "采集成功，已同步至监控系统";
    } catch (error) {
      result.className = "result error";
      result.textContent = error.message || "采集失败";
    } finally {
      button.disabled = false;
    }
  });
})();
