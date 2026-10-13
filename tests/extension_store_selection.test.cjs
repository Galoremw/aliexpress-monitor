const assert = require("node:assert/strict");
const test = require("node:test");

const {
  buildStoreSelectionPayload,
  getStoreSelectionPageDecision,
  getStoreSelectionRetrySkipIds,
  mergeStoreSelectionPage,
  mergeStoreSelectionRetryPage,
  rankStoreSelectionCandidates,
} = require("../extension/store_selection.js");

function pageDecision(pages) {
  return typeof getStoreSelectionPageDecision === "function"
    ? getStoreSelectionPageDecision(pages)
    : null;
}

function mergePage(pages, page) {
  return typeof mergeStoreSelectionPage === "function"
    ? mergeStoreSelectionPage(pages, page)
    : [...pages, page];
}

const metricDates = [
  "2026-09-22",
  "2026-09-23",
  "2026-09-24",
  "2026-09-25",
  "2026-09-26",
  "2026-09-27",
  "2026-09-28",
];

function candidate(id, values, ordersRank, complete = true) {
  return {
    platform_product_id: String(id),
    url: `https://www.aliexpress.com/item/${id}.html`,
    title: `Product ${id}`,
    image_url: `https://ae01.alicdn.com/${id}.jpg`,
    orders_rank: ordersRank,
    history_source: "ixspy_visible_daily_increment",
    history_daily_capture_complete: complete,
    history_expected_dates: metricDates,
    historical_sales: values.map((value, index) => ({
      date: metricDates[index],
      value,
      value_type: "daily_increment",
    })),
  };
}

test("ranks complete candidates by signed latest-seven-day increment sum, preserving Orders order for ties", () => {
  const candidates = [
    candidate("1001", [1, 1, 1, 1, 1, 1, 1], 1),
    candidate("1002", [5, 0, -1, 2, 7, 1, 3], 2),
    candidate("1003", [3, 3, 3, 3, 3, 3, 3], 3),
    candidate("1004", [4, 4, 4, 4, 4, 4, 4], 4),
    candidate("1005", [3, 3, 3, 3, 3, 3, 3], 5),
  ];

  const selected = rankStoreSelectionCandidates(candidates, 5);

  assert.deepEqual(selected.map((item) => item.platform_product_id), ["1004", "1003", "1005", "1002", "1001"]);
  assert.deepEqual(selected.map((item) => item.selection_score_7d), [28, 21, 21, 17, 7]);
  assert.ok(selected.every((item) => item.selection_reason === "latest_7d_increment"));
});

test("keeps zero and negative increments but excludes incomplete or malformed seven-day histories", () => {
  const duplicateDate = candidate("2006", [1, 1, 1, 1, 1, 1, 1], 6);
  duplicateDate.historical_sales.push({ ...duplicateDate.historical_sales[0] });
  const gappedDates = [
    "2026-09-22", "2026-09-23", "2026-09-25", "2026-09-26",
    "2026-09-27", "2026-09-28", "2026-09-29",
  ];
  const gappedHistory = candidate("2007", Array(7).fill(1), 7);
  gappedHistory.history_expected_dates = gappedDates;
  gappedHistory.historical_sales = gappedDates.map((date) => ({
    date,
    value: 1,
    value_type: "daily_increment",
  }));
  const candidates = [
    candidate("2001", [0, 0, 0, 0, 0, 0, 0], 1),
    candidate("2002", [-1, -1, -1, -1, -1, -1, -1], 2),
    candidate("2003", [9], 3, false),
    candidate("2004", [8], 4, false),
    candidate("2005", [0, null, 0, 0, 0, 0, 0], 5),
    duplicateDate,
    gappedHistory,
  ];

  const selected = rankStoreSelectionCandidates(candidates, 5);

  assert.deepEqual(selected.map((item) => item.platform_product_id), ["2001", "2002"]);
  assert.deepEqual(selected.map((item) => item.selection_score_7d), [0, -7]);
  assert.ok(selected.every((item) => item.selection_reason === "latest_7d_increment"));
});

test("deduplicates candidates across pages and selects at most twenty products", () => {
  const candidates = Array.from({ length: 24 }, (_, index) =>
    candidate(`300${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1));
  candidates.push(candidate("30023", Array(7).fill(999), 25));

  const selected = rankStoreSelectionCandidates(candidates, 20);

  assert.equal(selected.length, 20);
  assert.equal(new Set(selected.map((item) => item.platform_product_id)).size, 20);
  assert.equal(selected[0].platform_product_id, "30023");
});

test("returns every complete candidate when the store has fewer than twenty products", () => {
  const candidates = [
    candidate("4001", [1, 0, 0, 0, 0, 0, 0], 1),
    candidate("4002", [2, 0, 0, 0, 0, 0, 0], 2),
  ];

  const selected = rankStoreSelectionCandidates(candidates, 20);

  assert.equal(selected.length, 2);
  assert.deepEqual(selected.map((item) => item.platform_product_id), ["4002", "4001"]);
});

test("continues past unreadable candidates and submits after twenty complete histories", () => {
  const firstPage = {
    platform_store_id: "770700",
    has_next_page: true,
    candidates: Array.from({ length: 30 }, (_, index) =>
      candidate(`210${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1, index < 18)),
  };
  const secondPage = {
    platform_store_id: "770700",
    has_next_page: true,
    candidates: [candidate("22000", Array(7).fill(0), 31)],
  };
  const thirdPage = {
    platform_store_id: "770700",
    has_next_page: true,
    candidates: [candidate("23000", Array(7).fill(0), 32)],
  };

  assert.equal(pageDecision([firstPage]), "continue");
  assert.equal(pageDecision([firstPage, secondPage]), "continue");
  assert.equal(pageDecision([firstPage, secondPage, thirdPage]), "submit");
  const payload = buildStoreSelectionPayload([firstPage, secondPage, thirdPage]);
  assert.equal(payload.products.length, 20);
  assert.equal(payload.raw_data.unique_candidate_count, 32);
  assert.equal(payload.raw_data.readable_history_candidates, 20);
});

test("selects twenty complete histories from twenty-one candidates without ranking the incomplete one", () => {
  const page = {
    platform_store_id: "770714",
    url: "https://www.aliexpress.com/store/770714?shop_sortType=orders_desc",
    has_next_page: false,
    candidates: Array.from({ length: 21 }, (_, index) =>
      candidate(`760${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1, index < 20)),
  };

  assert.equal(pageDecision([page]), "submit");
  const payload = buildStoreSelectionPayload([page]);
  assert.equal(payload.products.length, 20);
  assert.equal(payload.raw_data.readable_history_candidates, 20);
  assert.equal(payload.products.some((item) => item.platform_product_id === "76020"), false);
});

test("does not submit an incomplete pool after the final Orders page", () => {
  const page = {
    platform_store_id: "770700",
    has_next_page: false,
    candidates: Array.from({ length: 25 }, (_, index) =>
      candidate(`230${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1, index < 19)),
  };

  assert.equal(pageDecision([page]), "incomplete");
});

test("allows a small store when all products have complete seven-day histories", () => {
  const page = {
    platform_store_id: "770700",
    has_next_page: false,
    candidates: [
      candidate("2401", [3, 0, 0, 0, 0, 0, 0], 1),
      candidate("2402", [2, 0, 0, 0, 0, 0, 0], 2),
    ],
  };

  assert.equal(pageDecision([page]), "submit");
});

test("submits eligible products from the current page while saving its continuation", () => {
  const page = {
    platform_store_id: "770700",
    url: "https://www.aliexpress.com/store/770700/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc",
    has_next_page: true,
    next_page_url: "https://www.aliexpress.com/store/770700/pages/all-items.html?page=2&sortType=bestmatch_sort&shop_sortType=orders_desc",
    raw_data: { history_target_date: "2026-09-29" },
    candidates: Array.from({ length: 12 }, (_, index) =>
      candidate(`245${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1)),
  };

  assert.equal(pageDecision([page]), "continue");
  const payload = buildStoreSelectionPayload([page]);
  assert.equal(payload.products.length, 12);
  assert.equal(payload.raw_data.candidate_count, 12);
  assert.equal(payload.raw_data.has_next_page, true);
  assert.equal(payload.raw_data.next_page_url, page.next_page_url);
});

test("submits a valid page snapshot when no candidate has a complete history", () => {
  const page = {
    platform_store_id: "770719",
    url: "https://www.aliexpress.com/store/770719/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc",
    has_next_page: true,
    next_page_url: "https://www.aliexpress.com/store/770719/pages/all-items.html?page=2&sortType=bestmatch_sort&shop_sortType=orders_desc",
    raw_data: { history_target_date: "2026-09-29" },
    candidates: [candidate("85001", [4], 1, false), candidate("85002", [0], 2, false)],
  };

  const payload = buildStoreSelectionPayload([page]);

  assert.equal(payload.products.length, 0);
  assert.equal(payload.raw_data.candidate_count, 2);
  assert.equal(payload.raw_data.readable_history_candidates, 0);
  assert.equal(payload.raw_data.has_next_page, true);
  assert.equal(payload.raw_data.next_page_url, page.next_page_url);
  assert.equal(payload.raw_data.history_target_date, "2026-09-29");
  assert.ok(page.candidates.every((item) => item.historical_sales.length === 1));
});

test("does not submit a current page when its detected continuation URL is missing", () => {
  const page = {
    platform_store_id: "770720",
    url: "https://www.aliexpress.com/store/770720/pages/all-items.html?sortType=bestmatch_sort&shop_sortType=orders_desc",
    has_next_page: true,
    raw_data: { history_target_date: "2026-09-29" },
    candidates: [candidate("86001", Array(7).fill(1), 1)],
  };

  assert.throws(() => buildStoreSelectionPayload([page]), /没有可保存的下一页链接/);
});

test("continues to a third Orders page when fewer than twenty candidates have been seen", () => {
  const pages = [
    {
      platform_store_id: "770715",
      has_next_page: true,
      candidates: Array.from({ length: 18 }, (_, index) =>
        candidate(`770${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1)),
    },
    {
      platform_store_id: "770715",
      has_next_page: true,
      candidates: [candidate("78000", Array(7).fill(18), 1)],
    },
  ];

  assert.equal(pageDecision(pages), "continue");
});

test("re-reading the same Orders page replaces its incomplete histories", () => {
  const original = {
    platform_store_id: "770702",
    url: "https://www.aliexpress.com/store/770702?shop_sortType=orders_desc&page=1",
    candidates: [candidate("2501", [1], 1, false), candidate("2502", [2], 2, false)],
  };
  const refreshed = {
    ...original,
    candidates: [candidate("2501", [1, 1, 1, 1, 1, 1, 1], 1), candidate("2502", [2, 2, 2, 2, 2, 2, 2], 2)],
  };

  const pages = mergePage([original], refreshed);

  assert.equal(pages.length, 1);
  assert.equal(pages[0].candidates[0].history_daily_capture_complete, true);
});

test("same-day retry skips only verified complete histories", () => {
  const complete = candidate("2501", Array(7).fill(1), 1);
  const partial = candidate("2502", [2], 2, false);
  const falselyComplete = candidate("2503", [3], 3, true);
  const page = {
    url: "https://www.aliexpress.com/store/770702?shop_sortType=orders_desc",
    raw_data: { history_target_date: "2026-09-29" },
    candidates: [complete, partial, falselyComplete],
  };

  assert.deepEqual(getStoreSelectionRetrySkipIds(page, page.url, "2026-09-29"), ["2501"]);
  assert.deepEqual(getStoreSelectionRetrySkipIds(page, page.url, "2026-09-30"), []);
  assert.deepEqual(getStoreSelectionRetrySkipIds(page, `${page.url}&page=2`, "2026-09-29"), []);
});

test("retry keeps complete same-day histories and uses fresh Orders ranks", () => {
  const url = "https://www.aliexpress.com/store/770702?shop_sortType=orders_desc";
  const original = {
    url,
    raw_data: { history_target_date: "2026-09-29" },
    candidates: [candidate("2501", Array(7).fill(1), 1), candidate("2502", [2], 2, false)],
  };
  const refreshed = {
    url,
    raw_data: { history_target_date: "2026-09-29" },
    candidates: [
      { platform_product_id: "2501", orders_rank: 2 },
      candidate("2502", Array(7).fill(2), 1),
    ],
  };

  const merged = mergeStoreSelectionRetryPage(original, refreshed);

  assert.equal(merged.candidates[0].orders_rank, 2);
  assert.equal(merged.candidates[0].history_daily_capture_complete, true);
  assert.equal(merged.candidates[0].historical_sales.length, 7);
  assert.equal(merged.candidates[1].history_daily_capture_complete, true);
  assert.equal(merged.candidates[1].historical_sales[0].value, 2);
  assert.equal(pageDecision([{ ...merged, has_next_page: false }]), "submit");
});

test("retry never reuses an older seven-day window", () => {
  const url = "https://www.aliexpress.com/store/770702?shop_sortType=orders_desc";
  const original = {
    url,
    raw_data: { history_target_date: "2026-09-29" },
    candidates: [candidate("2501", Array(7).fill(1), 1)],
  };
  const refreshed = {
    url,
    raw_data: { history_target_date: "2026-09-30" },
    candidates: [{ platform_product_id: "2501", orders_rank: 1 }],
  };

  const merged = mergeStoreSelectionRetryPage(original, refreshed);
  assert.equal(merged.candidates[0].historical_sales, undefined);
  assert.equal(pageDecision([{ ...merged, has_next_page: false }]), "incomplete");
});

test("re-reading the same Orders URL replaces a stale page when its products changed", () => {
  const original = {
    platform_store_id: "770717",
    url: "https://www.aliexpress.com/store/770717?shop_sortType=orders_desc&page=1",
    candidates: [candidate("81001", Array(7).fill(1), 1)],
  };
  const secondPage = {
    platform_store_id: "770717",
    url: "https://www.aliexpress.com/store/770717?shop_sortType=orders_desc&page=2",
    candidates: [candidate("82001", Array(7).fill(2), 1)],
  };
  const refreshed = {
    ...original,
    candidates: [candidate("81002", Array(7).fill(3), 1)],
  };

  const pages = mergePage([original, secondPage], refreshed);

  assert.equal(pages.length, 2);
  assert.equal(pages[0].candidates[0].platform_product_id, "81002");
  assert.equal(pages[1].candidates[0].platform_product_id, "82001");
});

test("does not combine complete seven-day histories from different collection days", () => {
  const firstPage = {
    platform_store_id: "770716",
    url: "https://www.aliexpress.com/store/770716?shop_sortType=orders_desc&page=1",
    has_next_page: true,
    raw_data: { history_target_date: "2026-09-29" },
    candidates: Array.from({ length: 10 }, (_, index) =>
      candidate(`790${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1)),
  };
  const freshDates = metricDates.map((date) => new Date(Date.parse(`${date}T00:00:00Z`) + 86400000)
    .toISOString().slice(0, 10));
  const secondPage = {
    platform_store_id: "770716",
    url: "https://www.aliexpress.com/store/770716?shop_sortType=orders_desc&page=2",
    has_next_page: false,
    raw_data: { history_target_date: "2026-09-30" },
    candidates: Array.from({ length: 10 }, (_, index) => {
      const item = candidate(`800${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1);
      return {
        ...item,
        history_expected_dates: freshDates,
        historical_sales: item.historical_sales.map((point, day) => ({ ...point, date: freshDates[day] })),
      };
    }),
  };

  assert.equal(pageDecision([firstPage, secondPage]), "window_mismatch");
  assert.throws(() => buildStoreSelectionPayload([firstPage, secondPage]), /same seven-day window/i);

  const refreshedFirstPage = {
    ...firstPage,
    raw_data: { history_target_date: "2026-09-30" },
    candidates: firstPage.candidates.map((item) => ({
      ...item,
      history_expected_dates: freshDates,
      historical_sales: item.historical_sales.map((point, day) => ({ ...point, date: freshDates[day] })),
    })),
  };
  const pages = mergePage([firstPage, secondPage], refreshedFirstPage);
  assert.equal(pageDecision(pages), "submit");
});

test("sends all ranked candidates from two Orders pages for backend vacancy filtering", () => {
  const firstPage = {
    platform_store_id: "770701",
    url: "https://www.aliexpress.com/store/770701?shop_sortType=orders_desc&page=1",
    candidates: Array.from({ length: 12 }, (_, index) =>
      candidate(`500${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1)),
  };
  const secondPage = {
    platform_store_id: "770701",
    url: "https://www.aliexpress.com/store/770701?shop_sortType=orders_desc&page=2",
    candidates: Array.from({ length: 12 }, (_, index) =>
      candidate(`600${String(index).padStart(2, "0")}`, Array(7).fill(index + 12), index + 1)),
  };

  const payload = buildStoreSelectionPayload([firstPage, secondPage]);

  assert.equal(payload.platform_store_id, "770701");
  assert.equal(payload.raw_data.extractor_version, "extension-store-selection-0.6.76");
  assert.equal(payload.products.length, 24);
  assert.equal(payload.raw_data.candidate_pages_scanned, 2);
  assert.equal(payload.raw_data.readable_history_candidates, 24);
  assert.equal(payload.raw_data.selection[0].selection_rank, 1);
  assert.equal(payload.raw_data.selection[0].product_id, "60011");
  assert.equal(payload.raw_data.selection[19].product_id, "50004");
  assert.equal(payload.raw_data.selection[23].product_id, "50000");
});

test("preserves Orders tie order when the first page has more than thirty products", () => {
  const pages = [
    {
      platform_store_id: "770718",
      url: "https://www.aliexpress.com/store/770718?shop_sortType=orders_desc&page=1",
      candidates: Array.from({ length: 39 }, (_, index) =>
        candidate(`830${String(index).padStart(2, "0")}`, Array(7).fill(1), index + 1)),
    },
    {
      platform_store_id: "770718",
      url: "https://www.aliexpress.com/store/770718?shop_sortType=orders_desc&page=2",
      candidates: [candidate("84000", Array(7).fill(1), 1)],
    },
  ];

  const payload = buildStoreSelectionPayload(pages, 40);
  const last = payload.raw_data.selection.find((item) => item.product_id === "84000");

  assert.equal(last.orders_rank, 40);
  assert.equal(last.selection_rank, 40);
});

test("builds the pool after continuing through a third Orders page", () => {
  const pages = [
    {
      platform_store_id: "770713",
      url: "https://www.aliexpress.com/store/770713?shop_sortType=orders_desc&page=1",
      has_next_page: true,
      candidates: Array.from({ length: 18 }, (_, index) =>
        candidate(`730${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1)),
    },
    {
      platform_store_id: "770713",
      url: "https://www.aliexpress.com/store/770713?shop_sortType=orders_desc&page=2",
      has_next_page: true,
      candidates: [candidate("74001", Array(7).fill(18), 1)],
    },
    {
      platform_store_id: "770713",
      url: "https://www.aliexpress.com/store/770713?shop_sortType=orders_desc&page=3",
      has_next_page: true,
      candidates: [candidate("75001", Array(7).fill(19), 1)],
    },
  ];

  const payload = buildStoreSelectionPayload(pages);

  assert.equal(payload.products.length, 20);
  assert.equal(payload.raw_data.candidate_pages_scanned, 3);
  assert.equal(payload.raw_data.readable_history_candidates, 20);
});

test("does not combine pages from different stores", () => {
  assert.throws(() => buildStoreSelectionPayload([
    { platform_store_id: "1", candidates: [candidate("7001", Array(7).fill(1), 1)] },
    { platform_store_id: "2", candidates: [candidate("7002", Array(7).fill(1), 1)] },
  ]), /same store/i);
});

test("submits the readable subset when unreadable charts leave fewer than twenty candidates", () => {
  const firstPage = {
    platform_store_id: "770711",
    url: "https://www.aliexpress.com/store/770711?shop_sortType=orders_desc&page=1",
    candidates: Array.from({ length: 10 }, (_, index) =>
      candidate(`710${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 1)),
  };
  const secondPage = {
    platform_store_id: "770711",
    url: "https://www.aliexpress.com/store/770711?shop_sortType=orders_desc&page=2",
    candidates: Array.from({ length: 10 }, (_, index) =>
      candidate(`720${String(index).padStart(2, "0")}`, Array(7).fill(index), index + 11, index < 9)),
  };

  const payload = buildStoreSelectionPayload([firstPage, secondPage]);
  assert.equal(payload.products.length, 19);
  assert.equal(payload.raw_data.readable_history_candidates, 19);
});

test("keeps all readable page candidates so backend exclusions can still fill pool vacancies", () => {
  const page = {
    platform_store_id: "770722",
    url: "https://www.aliexpress.com/store/770722/pages/all-items.html?shop_sortType=orders_desc",
    has_next_page: true,
    next_page_url: "https://www.aliexpress.com/store/770722/pages/all-items.html?page=2&shop_sortType=orders_desc",
    raw_data: { history_target_date: "2026-09-29" },
    candidates: Array.from({ length: 21 }, (_, index) =>
      candidate(`860${String(index).padStart(2, "0")}`, Array(7).fill(21 - index), index + 1)),
  };

  const payload = buildStoreSelectionPayload([page], 20);

  assert.equal(payload.products.length, 21);
  assert.equal(payload.raw_data.selection.length, 21);
  assert.equal(payload.products.at(-1).platform_product_id, "86020");
});
