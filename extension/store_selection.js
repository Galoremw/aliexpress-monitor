(function (root) {
  function completeHistory(candidate) {
    const expectedDates = candidate.history_expected_dates || [];
    if (!candidate.history_daily_capture_complete
      || candidate.history_source !== "ixspy_visible_daily_increment"
      || expectedDates.length !== 7
      || new Set(expectedDates.map(String)).size !== 7) return null;
    const expectedTimes = expectedDates.map((date) => Date.parse(`${date}T00:00:00Z`));
    if (expectedTimes.some((value) => !Number.isFinite(value))
      || expectedTimes.some((value, index) => index > 0 && value - expectedTimes[index - 1] !== 86400000)) return null;
    const dailyPoints = candidate.historical_sales || [];
    if (dailyPoints.length !== 7
      || dailyPoints.some((point) => point.value_type !== "daily_increment")) return null;
    const points = new Map(dailyPoints.map((point) => [String(point.date), point.value]));
    if (points.size !== 7 || expectedDates.some((date) => typeof points.get(String(date)) !== "number"
      || !Number.isFinite(points.get(String(date))))) return null;
    return expectedDates.reduce((sum, date) => sum + points.get(String(date)), 0);
  }

  function reusableHistory(candidate, targetDate) {
    const target = Date.parse(`${targetDate}T00:00:00Z`);
    if (!Number.isFinite(target) || completeHistory(candidate) === null
      || candidate.history_missing_dates?.length) return false;
    const latestExpected = new Date(target - 86400000).toISOString().slice(0, 10);
    return candidate.history_expected_dates[6] === latestExpected;
  }

  function getStoreSelectionRetrySkipIds(page, pageUrl, targetDate) {
    if (!page || page.url !== pageUrl
      || page.raw_data?.history_target_date !== targetDate) return [];
    return (page.candidates || [])
      .filter((candidate) => reusableHistory(candidate, targetDate))
      .map((candidate) => String(candidate.platform_product_id));
  }

  function mergeStoreSelectionRetryPage(previous, refreshed) {
    if (!previous || previous.url !== refreshed.url
      || previous.raw_data?.history_target_date !== refreshed.raw_data?.history_target_date) {
      return refreshed;
    }
    const targetDate = refreshed.raw_data?.history_target_date;
    const previousById = new Map((previous.candidates || [])
      .map((candidate) => [String(candidate.platform_product_id), candidate]));
    return {
      ...refreshed,
      candidates: (refreshed.candidates || []).map((candidate) => {
        if (candidate.history_expected_dates?.length) return candidate;
        const saved = previousById.get(String(candidate.platform_product_id));
        if (!saved || !reusableHistory(saved, targetDate)) return candidate;
        return {
          ...candidate,
          historical_sales: saved.historical_sales,
          history_source: saved.history_source,
          history_daily_capture_complete: true,
          history_expected_dates: saved.history_expected_dates,
          history_missing_dates: saved.history_missing_dates,
        };
      }),
    };
  }

  function rankStoreSelectionCandidates(candidates, limit = 20) {
    const unique = new Map();
    for (const [index, candidate] of (candidates || []).entries()) {
      const id = String(candidate.platform_product_id || "");
      if (!id) continue;
      const current = unique.get(id);
      const normalized = {
        ...candidate,
        orders_rank: Number(candidate.orders_rank) || index + 1,
        selection_score_7d: completeHistory(candidate),
      };
      if (!current) {
        unique.set(id, normalized);
        continue;
      }
      const currentPoints = current.historical_sales?.length || 0;
      const nextPoints = candidate.historical_sales?.length || 0;
      if ((normalized.selection_score_7d !== null && current.selection_score_7d === null)
        || (normalized.selection_score_7d === null && current.selection_score_7d === null
          && nextPoints > currentPoints)) {
        unique.set(id, { ...normalized, orders_rank: current.orders_rank });
      }
    }

    const values = [...unique.values()];
    const ranked = values.filter((item) => item.selection_score_7d !== null)
      .sort((left, right) => right.selection_score_7d - left.selection_score_7d
        || left.orders_rank - right.orders_rank);
    const selected = ranked.slice(0, Math.max(0, Number(limit) || 0));
    return selected.map((item, index) => ({
      ...item,
      selection_rank: index + 1,
      selection_reason: "latest_7d_increment",
    }));
  }

  function sameSevenDayWindow(pages) {
    const windows = new Set();
    const targetDates = new Set();
    for (const page of pages) {
      if (page.raw_data?.history_target_date) {
        targetDates.add(String(page.raw_data.history_target_date));
      }
      for (const candidate of page.candidates || []) {
        if (candidate.history_expected_dates?.length === 7) {
          windows.add(candidate.history_expected_dates.map(String).join("|"));
        }
      }
    }
    return windows.size <= 1 && targetDates.size <= 1;
  }

  function getStoreSelectionPageDecision(pages, limit = 20) {
    if (!Array.isArray(pages) || !pages.length) return "incomplete";
    const candidates = pages.flatMap((page) => page.candidates || []);
    const uniqueCount = new Set(candidates
      .map((candidate) => String(candidate.platform_product_id || ""))
      .filter(Boolean)).size;
    const completeCount = rankStoreSelectionCandidates(candidates, Number.MAX_SAFE_INTEGER).length;
    const targetCount = Math.max(0, Number(limit) || 0);
    const lastPage = pages[pages.length - 1];

    if (!targetCount || !uniqueCount) return "incomplete";
    if (!sameSevenDayWindow(pages)) return "window_mismatch";
    if (pages.length === 1 && lastPage.has_next_page) return "continue";
    if (completeCount >= targetCount) return "submit";
    if (lastPage.has_next_page) return "continue";
    if (uniqueCount < targetCount && completeCount === uniqueCount) return "submit";
    return "incomplete";
  }

  function pageIdentity(page) {
    const ids = [...new Set((page.candidates || [])
      .map((candidate) => String(candidate.platform_product_id || ""))
      .filter(Boolean))].sort();
    return ids.length ? ids.join("|") : null;
  }

  function mergeStoreSelectionPage(pages, page) {
    const currentPages = Array.isArray(pages) ? [...pages] : [];
    const identity = pageIdentity(page);
    if (!identity) throw new Error("当前页没有可识别的商品 ID");
    const matchingUrlIndex = page.url
      ? currentPages.findIndex((item) => item.url === page.url)
      : -1;
    const existingIndex = matchingUrlIndex >= 0
      ? matchingUrlIndex
      : currentPages.findIndex((item) => pageIdentity(item) === identity);
    if (existingIndex >= 0) {
      currentPages[existingIndex] = page;
      return currentPages;
    }
    const knownIds = new Set(currentPages.flatMap((item) => (item.candidates || [])
      .map((candidate) => String(candidate.platform_product_id || ""))));
    if (!(page.candidates || []).some((candidate) => !knownIds.has(String(candidate.platform_product_id || "")))) {
      throw new Error("当前页与已读页面商品重复；请切换到下一个 Orders 页面，或重读相同页面以刷新 IXSPY 数据");
    }
    currentPages.push(page);
    return currentPages;
  }

  function buildStoreSelectionPayload(pages, limit = 20) {
    if (!Array.isArray(pages) || !pages.length) {
      throw new Error("store selection requires at least one page");
    }
    const storeId = String(pages[0].platform_store_id || "");
    if (!storeId || pages.some((page) => String(page.platform_store_id || "") !== storeId)) {
      throw new Error("all candidate pages must belong to the same store");
    }
    if (!sameSevenDayWindow(pages)) {
      throw new Error("store selection requires the same seven-day window on every candidate page");
    }
    let precedingCount = 0;
    const candidates = pages.flatMap((page) => {
      const pageCandidates = page.candidates || [];
      const ranked = pageCandidates.map((candidate, index) => ({
        ...candidate,
        orders_rank: precedingCount + (Number(candidate.orders_rank) || index + 1),
      }));
      precedingCount += pageCandidates.length;
      return ranked;
    });
    const uniqueCandidateCount = new Set(candidates
      .map((item) => String(item.platform_product_id || ""))
      .filter(Boolean)).size;
    const readableCount = new Set(candidates
      .filter((candidate) => completeHistory(candidate) !== null)
      .map((candidate) => String(candidate.platform_product_id || ""))
      .filter(Boolean)).size;
    const selected = rankStoreSelectionCandidates(candidates, Number.MAX_SAFE_INTEGER);
    const lastPage = pages[pages.length - 1];
    const nextPageUrl = lastPage.next_page_url || lastPage.raw_data?.next_page_url || null;
    if (pages.length === 1 && lastPage.has_next_page && !nextPageUrl) {
      throw new Error("当前 Orders 页检测到下一页，但没有可保存的下一页链接；未提交");
    }
    const products = selected.map((candidate) => ({
      platform_product_id: String(candidate.platform_product_id),
      url: candidate.url || `https://www.aliexpress.com/item/${candidate.platform_product_id}.html`,
      title: candidate.title || null,
      image_url: candidate.image_url || null,
      historical_sales: candidate.historical_sales || [],
      history_source: candidate.history_source || null,
      history_daily_capture_complete: Boolean(candidate.history_daily_capture_complete),
      history_expected_dates: candidate.history_expected_dates || [],
      history_missing_dates: candidate.history_missing_dates || [],
    }));
    return {
      platform_store_id: storeId,
      url: pages[pages.length - 1].url || pages[0].url,
      products,
      raw_data: {
        extractor_version: "extension-store-selection-0.6.76",
        collection_mode: "top_7d_increment_selection",
        selection_method: "ixspy_visible_latest_7_day_increment_sum",
        order_sort_verified: true,
        scan_complete: true,
        pagination_known: lastPage.pagination_known === true,
        has_next_page: Boolean(lastPage.has_next_page),
        next_page_url: nextPageUrl,
        history_target_date: lastPage.raw_data?.history_target_date || null,
        candidate_count: uniqueCandidateCount,
        candidate_pages_scanned: pages.length,
        total_candidate_count: candidates.length,
        unique_candidate_count: uniqueCandidateCount,
        readable_history_candidates: readableCount,
        orders_fallback_count: 0,
        page_urls: pages.map((page) => page.url).filter(Boolean),
        selection: selected.map((item) => ({
          product_id: String(item.platform_product_id),
          selection_rank: item.selection_rank,
          orders_rank: item.orders_rank,
          selection_score_7d: item.selection_score_7d,
          selection_reason: item.selection_reason,
          history_daily_capture_complete: Boolean(item.history_daily_capture_complete),
        })),
      },
    };
  }

  const api = {
    rankStoreSelectionCandidates,
    getStoreSelectionPageDecision,
    getStoreSelectionRetrySkipIds,
    mergeStoreSelectionPage,
    mergeStoreSelectionRetryPage,
    buildStoreSelectionPayload,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.AliExpressStoreSelection = api;
})(globalThis);
