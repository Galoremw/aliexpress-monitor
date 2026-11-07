(function () {
  if (globalThis.__ALIEXPRESS_MONITOR_DIANXIAOMI_PRODUCT_BRIDGE__) return;
  globalThis.__ALIEXPRESS_MONITOR_DIANXIAOMI_PRODUCT_BRIDGE__ = true;

  const STORAGE_KEY = "dianxiaomiProductOperation";
  const HELPER_WAIT_MS = 90000;
  const RESULT_WAIT_MS = 300000;
  const POLL_INTERVAL_MS = 1000;
  let operation = null;
  let running = false;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function productIdFromUrl(url) {
    try {
      return new URL(url).pathname.match(/\/item\/(\d+)/i)?.[1] || null;
    } catch {
      return null;
    }
  }

  function visible(node) {
    if (!node?.isConnected) return false;
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    return style.display !== "none"
      && style.visibility !== "hidden"
      && Number(style.opacity || 1) > 0
      && rect.width > 0
      && rect.height > 0;
  }

  function confirmationReason() {
    const current = new URL(location.href);
    if (/login|passport|signin/i.test(current.pathname)) return "AliExpress 要求登录，请先在商品页完成登录";
    const text = (document.body?.innerText || "").slice(0, 12000);
    if (/安全验证|人机验证|滑动验证|验证码|验证中心/.test(text)) {
      return "AliExpress 商品页需要人工完成安全验证";
    }
    if (/请登录|立即登录|登录后查看/.test(text) && !/店小秘/.test(text)) {
      return "AliExpress 商品页要求登录，请先完成登录";
    }
    return null;
  }

  function helperButton() {
    const labelOf = (node) => (node?.innerText || node?.textContent || "").replace(/\s+/g, " ").trim();
    const expectedLabel = /本页采集|开始采集|采集成功|重复采集/;
    const visibleControls = [...document.querySelectorAll("button, [role='button']")].filter(visible);
    const startAction = visibleControls.find((node) => labelOf(node) === "开始采集");
    if (startAction) return startAction;

    const currentPageControls = [...document.querySelectorAll(
      ".dxmCurrentPag button, .dxmCurrentPag [role='button'], button.dxmCurrentPag, [role='button'].dxmCurrentPag",
    )].filter(visible);
    const directControl = currentPageControls.find((node) => expectedLabel.test(labelOf(node)));
    if (directControl) return directControl;

    const currentPageNodes = [...document.querySelectorAll(".dxmCurrentPag")].filter(visible);
    const currentPage = currentPageNodes.find((node) => expectedLabel.test(labelOf(node)));
    if (currentPage) {
      const nestedControl = [...currentPage.querySelectorAll("button, [role='button']")]
        .filter(visible)
        .find((node) => expectedLabel.test(labelOf(node)));
      return nestedControl || currentPage;
    }

    return visibleControls.find((node) => /^(本页采集|采集成功|重复采集)$/.test(labelOf(node))) || null;
  }

  function feedbackNodes() {
    const selectors = [
      "[role='alert']",
      "[role='status']",
      "[class*='message']",
      "[class*='Message']",
      "[class*='toast']",
      "[class*='Toast']",
      "[class*='notification']",
      "[class*='Notification']",
      "[class*='dxm']",
      "#msgModal",
      ".modal",
    ].join(",");
    return [...new Set(document.querySelectorAll(selectors))]
      .filter(visible)
      .map((node) => (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim())
      .filter(Boolean);
  }

  function resultFromText(text) {
    if (/采集完成，成功\s*\d+\s*个，失败\s*\d+\s*个/.test(text)) {
      const counts = text.match(/采集完成，成功\s*(\d+)\s*个，失败\s*(\d+)\s*个/);
      const succeeded = Number(counts?.[1] || 0);
      const failed = Number(counts?.[2] || 0);
      if (failed > 0) return { state: "failed", message: `店小秘助手报告成功 ${succeeded} 个、失败 ${failed} 个` };
      if (succeeded > 0) return { state: "succeeded", message: `店小秘助手报告采集成功 ${succeeded} 个` };
      return { state: "failed", message: "店小秘助手没有成功采集该商品" };
    }
    if (/采集失败|链接无效|商品不存在|无法采集|不支持该平台/.test(text)) {
      return { state: "failed", message: text.slice(0, 180) || "店小秘助手报告采集失败" };
    }
    if (/请先登录店小秘|登录店小秘后再采集|(?:是否|确认).{0,20}重复采集|重复采集.{0,20}(?:确认|是否)/.test(text)) {
      return { state: "needs_confirmation", message: text.slice(0, 180) };
    }
    if (/重复采集/.test(text)) {
      return { state: "succeeded", message: "店小秘助手标记该商品为重复采集，按已采集完成" };
    }
    if (/采集成功，共采集\s*\d+\s*个产品/.test(text)) {
      return { state: "succeeded", message: "店小秘助手已确认采集成功" };
    }
    if (/采集成功/.test(text) && !/失败/.test(text)) {
      return { state: "succeeded", message: "店小秘助手已确认采集成功" };
    }
    return null;
  }

  function setOperationState(state, message) {
    if (!operation) return Promise.resolve();
    operation = {
      ...operation,
      state,
      message: message || null,
      updated_at: Date.now(),
    };
    return chrome.storage.local.set({ [STORAGE_KEY]: operation });
  }

  async function finish(state, message) {
    if (!operation) return;
    await setOperationState(state, message);
    running = false;
  }

  function launchOperation(nextOperation) {
    void runOperation(nextOperation).catch((error) => {
      void finish("needs_confirmation", error.message || "店小秘助手运行状态异常，请检查后再重新排队");
    });
  }

  async function monitorFeedback(baseline) {
    const deadline = Date.now() + RESULT_WAIT_MS;
    while (operation && operation.state === "collecting" && Date.now() < deadline) {
      const blocked = confirmationReason();
      if (blocked) return finish("needs_confirmation", blocked);

      const nodes = feedbackNodes();
      const fresh = nodes.filter((text) => !baseline.includes(text));
      const result = resultFromText(fresh.join("\n"));
      if (result) return finish(result.state, result.message);

      if (fresh.some((text) => /重复采集/.test(text))) {
        return finish("succeeded", "店小秘助手标记该商品为重复采集，按已采集完成");
      }
      await sleep(POLL_INTERVAL_MS);
    }
    if (operation?.state === "collecting") {
      await finish("needs_confirmation", "等待店小秘助手结果超时，请先检查该商品的采集状态，再决定是否重新排队");
    }
  }

  async function runOperation(nextOperation) {
    if (running) return;
    running = true;
    operation = nextOperation;
    const expectedProductId = String(nextOperation.product_id || "");
    const currentProductId = productIdFromUrl(location.href);
    const blocked = confirmationReason();
    if (blocked) {
      await finish("needs_confirmation", blocked);
      return;
    }
    if (!currentProductId || currentProductId !== expectedProductId) {
      await finish("needs_confirmation", "打开的页面与队列商品不一致，请检查商品链接");
      return;
    }

    const helperDeadline = Date.now() + HELPER_WAIT_MS;
    let button = null;
    while (Date.now() < helperDeadline) {
      const blocked = confirmationReason();
      if (blocked) {
        await finish("needs_confirmation", blocked);
        return;
      }
      if (productIdFromUrl(location.href) !== expectedProductId) {
        await finish("needs_confirmation", "商品页跳转后与队列目标商品不一致，请检查商品链接");
        return;
      }
      button = helperButton();
      if (button) break;
      await sleep(POLL_INTERVAL_MS);
    }
    if (!button) {
      await finish("needs_confirmation", "商品页没有出现店小秘助手的“本页采集”控件，请确认隔离工作窗口已安装并启用助手");
      return;
    }
    if (productIdFromUrl(location.href) !== expectedProductId) {
      await finish("needs_confirmation", "商品页跳转后与队列目标商品不一致，请检查商品链接");
      return;
    }

    const buttonText = (button.innerText || button.textContent || "").replace(/\s+/g, " ").trim();
    if (/采集成功/.test(buttonText)) {
      await finish("succeeded", "店小秘助手显示该商品已采集");
      return;
    }
    if (/重复采集/.test(buttonText)) {
      await finish("succeeded", "店小秘助手标记该商品为重复采集，按已采集完成");
      return;
    }
    if (button.disabled || button.getAttribute("aria-disabled") === "true") {
      await finish("needs_confirmation", "店小秘助手的“本页采集”控件当前不可用，请检查商品页后重新排队");
      return;
    }

    const baseline = feedbackNodes();
    operation.baseline_feedback = baseline;
    try {
      await setOperationState("dispatching", "正在调用店小秘助手");
      button.click();
      await setOperationState("collecting", "店小秘助手已启动本页采集");
    } catch (error) {
      await finish("needs_confirmation", `助手调用结果无法确认，请检查店小秘采集箱后再重新排队。${error.message || ""}`);
      return;
    }
    await monitorFeedback(baseline);
  }

  async function loadOperation(handoffId) {
    const stored = await chrome.storage.local.get(STORAGE_KEY);
    const saved = stored[STORAGE_KEY];
    if (!saved || String(saved.handoff_id) !== String(handoffId)) return null;
    if (saved.state === "dispatching") {
      operation = saved;
      if (running) return operation;
      await finish("needs_confirmation", "助手调用状态中断，无法确认是否已提交；请检查店小秘后再重新排队");
      return operation;
    }
    operation = saved;
    if (["waiting_helper", "collecting"].includes(saved.state) && !running) {
      if (saved.state === "waiting_helper") launchOperation(saved);
      else {
        running = true;
        void monitorFeedback(saved.baseline_feedback || [])
          .catch((error) => finish("needs_confirmation", error.message || "店小秘助手结果读取异常，请检查后再重新排队"))
          .finally(() => { running = false; });
      }
    }
    return operation;
  }

  async function startOperation(message) {
    const handoffId = String(message.handoff_id || "");
    if (!handoffId) return { state: "failed", message: "店小秘队列任务编号缺失" };
    const saved = await loadOperation(handoffId);
    if (saved) return { state: saved.state, message: saved.message || null };

    const targetProductId = productIdFromUrl(message.target_url || "");
    if (!targetProductId) return { state: "failed", message: "队列商品链接不是有效的 AliExpress 商品页" };
    operation = {
      handoff_id: handoffId,
      product_id: targetProductId,
      state: "waiting_helper",
      message: "正在等待店小秘助手加载商品页控件",
      updated_at: Date.now(),
    };
    await chrome.storage.local.set({ [STORAGE_KEY]: operation });
    launchOperation(operation);
    return { state: operation.state, message: operation.message };
  }

  async function getOperation(handoffId) {
    const saved = await loadOperation(String(handoffId || ""));
    if (saved) return { state: saved.state, message: saved.message || null };
    const blocked = confirmationReason();
    if (blocked) return { state: "needs_confirmation", message: blocked };
    return { state: "not_started", message: null };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!["dianxiaomi-start-product", "dianxiaomi-product-status"].includes(message?.type)) return false;
    const operationPromise = message.type === "dianxiaomi-start-product"
      ? startOperation(message)
      : getOperation(message.handoff_id);
    operationPromise
      .then(sendResponse)
      .catch((error) => sendResponse({ state: "failed", message: error.message || "店小秘助手页面操作失败" }));
    return true;
  });

  void (async () => {
    const stored = await chrome.storage.local.get(STORAGE_KEY);
    const saved = stored[STORAGE_KEY];
    if (saved && String(saved.product_id) === String(productIdFromUrl(location.href))) {
      await loadOperation(saved.handoff_id);
    }
  })();
})();
