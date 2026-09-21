(function () {
  function textOf(node) {
    return (node?.innerText || node?.textContent || "").replace(/\s+/g, " ").trim();
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
    const marker = [...document.querySelectorAll("label, section, div")].find((node) =>
      /采集请遵守平台相关规范/.test(textOf(node)) && node.querySelector("input[type='checkbox']")
    );
    if (marker) return marker.querySelector("input[type='checkbox']");
    return null;
  }

  function pageNeedsLogin() {
    const text = (document.body?.innerText || "").slice(0, 8000);
    return /登录|重新登录|账号登录|验证码/.test(text) && !/数据采集/.test(text);
  }

  async function submitLinks(urls) {
    if (!isCollectionPage()) return { state: "failed", message: "当前不是店小秘数据采集页" };
    if (pageNeedsLogin()) return { state: "needs_confirmation", message: "请先在店小秘页面完成登录" };
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
    const cleanUrls = [...new Set(urls.filter((url) => /^https?:\/\//i.test(url)))];
    if (!cleanUrls.length) return { state: "failed", message: "没有有效商品链接" };
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    if (setter) setter.call(box, cleanUrls.join("\n"));
    else box.value = cleanUrls.join("\n");
    box.dispatchEvent(new Event("input", { bubbles: true }));
    box.dispatchEvent(new Event("change", { bubbles: true }));
    start.click();
    await new Promise((resolve) => setTimeout(resolve, 500));
    return { state: "submitted", count: cleanUrls.length, message: `已提交 ${cleanUrls.length} 条商品链接` };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "dianxiaomi-submit-links") return false;
    submitLinks(Array.isArray(message.urls) ? message.urls : [])
      .then(sendResponse)
      .catch((error) => sendResponse({ state: "failed", message: error.message || "店小秘页面操作失败" }));
    return true;
  });
})();
