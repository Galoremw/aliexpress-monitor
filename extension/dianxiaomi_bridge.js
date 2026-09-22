(function () {
  if (globalThis.__ALIEXPRESS_MONITOR_DIANXIAOMI_BRIDGE__) return;
  globalThis.__ALIEXPRESS_MONITOR_DIANXIAOMI_BRIDGE__ = true;

  function textOf(node) {
    return (node?.innerText || node?.textContent || "").replace(/\s+/g, " ").trim();
  }

  function productIdFromUrl(url) {
    try {
      return new URL(url).pathname.match(/\/item\/(\d+)/i)?.[1] || null;
    } catch {
      return null;
    }
  }

  function isCollectionPage() {
    return /\/web\/productCrawl\/dataAcquisition/i.test(location.pathname);
  }

  function findUrlBox() {
    return [...document.querySelectorAll("textarea")].find((node) =>
      /产品详情页|多个网址|详情页的链接/i.test(node.placeholder || "")
    ) || document.querySelector("textarea");
  }

  function findStartButton() {
    return [...document.querySelectorAll("button")].find((node) => textOf(node) === "开始采集");
  }

  function agreementCheckbox() {
    const checkboxes = [...document.querySelectorAll("input[type='checkbox']")];
    return checkboxes.find((checkbox) => {
      let node = checkbox.parentElement;
      for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
        if (/采集请遵守平台相关规范/.test(textOf(node))) return true;
      }
      return false;
    }) || null;
  }

  function pageNeedsLogin() {
    const text = (document.body?.innerText || "").slice(0, 8000);
    return /登录|重新登录|账号登录|验证码/.test(text) && !/数据采集/.test(text);
  }

  function checkReady() {
    if (!isCollectionPage()) return { state: "failed", message: "当前不是店小秘数据采集页" };
    if (pageNeedsLogin()) {
      const text = document.body?.innerText || "";
      return {
        state: "needs_confirmation",
        message: /验证码|安全验证|人机验证/.test(text)
          ? "店小秘需要人工完成验证"
          : "请先在店小秘页面完成登录",
      };
    }
    const box = findUrlBox();
    const start = findStartButton();
    if (!box || !start) return { state: "failed", message: "没有找到店小秘链接采集控件" };
    const agreement = agreementCheckbox();
    if (agreement && !agreement.checked) {
      return { state: "needs_confirmation", message: "请先勾选店小秘采集协议" };
    }
    if (!agreement && /采集请遵守平台相关规范/.test(document.body?.innerText || "")) {
      return { state: "needs_confirmation", message: "请在店小秘页面确认采集协议" };
    }
    return { state: "ready" };
  }

  function resultLinkExists(url) {
    const productId = productIdFromUrl(url);
    if (!productId) return false;
    return [...document.links].some((link) => productIdFromUrl(link.href) === productId);
  }

  function hasExplicitFailure() {
    const nodes = [...document.querySelectorAll(
      '[role="alert"], [class*="message"], [class*="Message"], [class*="toast"], [class*="Toast"], [class*="alert"], [class*="Alert"], [class*="error"], [class*="Error"]'
    )];
    return nodes.some((node) => /采集失败|链接无效|商品不存在|无法采集|不支持该平台/.test(textOf(node)));
  }

  function visible(node) {
    const style = getComputedStyle(node);
    return style.display !== "none" && style.visibility !== "hidden";
  }

  function resultDialogs() {
    return [...document.querySelectorAll("[role='dialog'], .ant-modal, .modal")]
      .filter((node) => visible(node));
  }

  async function closeExistingResultModal() {
    const dialog = resultDialogs().find((node) => /自动采集/.test(textOf(node)) && /状态：/.test(textOf(node)));
    if (!dialog) return;
    const close = [...dialog.querySelectorAll("button")].find((button) => textOf(button) === "关闭");
    close?.click();
    for (let attempt = 0; attempt < 10; attempt += 1) {
      if (!resultDialogs().some((node) => /自动采集/.test(textOf(node)) && /状态：/.test(textOf(node)))) return;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }

  function collectionResult() {
    const dialog = resultDialogs().find((node) => /自动采集/.test(textOf(node)) && /状态：采集完成/.test(textOf(node)));
    if (!dialog) return null;
    const text = textOf(dialog);
    const counts = text.match(/已执行\s*(\d+)条，成功[:：]\s*(\d+)，跳过[:：]\s*(\d+)，失败[:：]\s*(\d+)/);
    if (!counts) return null;
    const [, executed, success, skipped, failed] = counts.map(Number);
    if (failed > 0) {
      return { state: "failed", message: `店小秘报告采集失败（${failed} 条）` };
    }
    if (success > 0 || skipped > 0) {
      return { state: "succeeded", message: `店小秘已处理 ${executed} 条链接` };
    }
    return null;
  }

  async function waitForCollection(url, timeoutMs = 60000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const ready = checkReady();
      if (ready.state === "needs_confirmation") return ready;
      if (ready.state === "failed") return ready;
      const modalResult = collectionResult();
      if (modalResult) return modalResult;
      if (resultLinkExists(url)) {
        return { state: "succeeded", message: "店小秘已出现对应商品链接" };
      }
      if (hasExplicitFailure()) {
        return { state: "failed", message: "店小秘页面报告采集失败或链接无效" };
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    return { state: "failed", message: "等待店小秘采集结果超时" };
  }

  async function submitLinks(urls) {
    const ready = checkReady();
    if (ready.state !== "ready") return ready;
    await closeExistingResultModal();
    const box = findUrlBox();
    const start = findStartButton();
    const cleanUrls = [...new Set(urls.filter((url) => /^https?:\/\//i.test(url)))];
    if (!cleanUrls.length) return { state: "failed", message: "没有有效商品链接" };
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    if (setter) setter.call(box, cleanUrls.join("\n"));
    else box.value = cleanUrls.join("\n");
    box.dispatchEvent(new Event("input", { bubbles: true }));
    box.dispatchEvent(new Event("change", { bubbles: true }));
    start.click();
    const result = await waitForCollection(cleanUrls[0]);
    return { ...result, count: cleanUrls.length };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!["dianxiaomi-submit-links", "dianxiaomi-check-ready"].includes(message?.type)) return false;
    const operation = message.type === "dianxiaomi-check-ready"
      ? Promise.resolve(checkReady())
      : submitLinks(Array.isArray(message.urls) ? message.urls : []);
    operation
      .then(sendResponse)
      .catch((error) => sendResponse({ state: "failed", message: error.message || "店小秘页面操作失败" }));
    return true;
  });
})();
