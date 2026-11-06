(function () {
  document.getElementById("aliexpress-monitor-collector")?.remove();
  const collector = globalThis.AliExpressMonitorPageCollector;
  if (!collector) return;

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "STORE_CANDIDATE_SCAN") {
      const options = {};
      if (Number.isSafeInteger(message.item_id)
        && typeof message.claim_token === "string" && message.claim_token) {
        options.on_candidate_collected = async (product) => {
          const response = await new Promise((resolve, reject) => {
            chrome.runtime.sendMessage({
              type: "store-candidate-progress",
              item_id: Number(message.item_id),
              claim_token: message.claim_token,
              expected_store_id: String(message.expectedStoreId || ""),
              approved_store_url_identities: message.approvedStoreUrlIdentities || [],
              expected_page_url: String(message.expectedPageUrl || ""),
              product,
            }, (value) => {
              if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
              else resolve(value);
            });
          });
          if (!response?.ok) {
            const error = new Error(response?.message || "后台未确认候选商品采集进度");
            error.code = response?.reason === "candidate_history_invalid"
              ? "CANDIDATE_HISTORY_INVALID" : "CANDIDATE_PROGRESS_REJECTED";
            error.failed_checks = Array.isArray(response?.failed_checks)
              ? response.failed_checks : [];
            throw error;
          }
        };
      }
      Promise.resolve()
        .then(() => collector.collectStoreCandidatePage(message, options))
        .then(sendResponse)
        .catch((error) => sendResponse({ state: "failed", message: error.message || "候选商品页读取失败" }));
      return true;
    }
    if (message?.type === "collect-visible-reviews") {
      collector.collectReviews(message.known_keys || [], message.max_new_reviews || 100)
        .then(sendResponse)
        .catch((error) => sendResponse({ state: "failed", message: error.message || "公开评论读取失败" }));
      return true;
    }
    if (message?.type !== "collect-visible-page") return false;
    const options = {
      skip_product_ids: message.skip_product_ids,
      monitored_product_ids: message.monitored_product_ids,
      monitored_products: message.monitored_products,
      expected_store_id: message.expected_store_id,
      expected_store_name: message.expected_store_name,
      identity_monitored_product_ids: message.identity_monitored_product_ids,
      verified_store_identity: message.verified_store_identity,
      approved_store_url_identities: message.approved_store_url_identities || [],
    };
    if (message.target_type === "STORE" && Number.isSafeInteger(message.item_id)
      && typeof message.claim_token === "string" && message.claim_token) {
      options.on_product_collected = async (product) => {
        const response = await new Promise((resolve, reject) => {
          chrome.runtime.sendMessage({
            type: "store-collection-product-progress",
            item_id: Number(message.item_id),
            claim_token: message.claim_token,
            product,
          }, (value) => {
            if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
            else resolve(value);
          });
        });
        if (!response?.ok) throw new Error("后台未确认店铺商品采集进度");
      };
    }
    Promise.resolve()
      .then(() => collector.collect(
        message.target_type,
        message.target_date || message.history_target_date,
        options,
      ))
      .then(sendResponse)
      .catch((error) => sendResponse({ state: "failed", message: error.message || "页面读取失败" }));
    return true;
  });

})();
