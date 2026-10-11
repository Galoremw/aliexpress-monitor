const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const TEST_TARGET_DATE = "2026-09-29";

function loadCollector({ expandYearView = false, sameChartInDailyMode = false, ixspyAvailable = true, ixspyMountAfterMs = 0, ixspyChartMountAfterMs = 0, ixspyLoginRequired = false, dailyChartReadyAfterSamples = 0, animationFramesPaused = false, tooltipDelayMs = 0, unlabeledTooltip = false, splitTooltipParts = false, tooltipFormat = "sales", missingDailyIndexes = [], negativeDailyIndex = null, throwOnChartQuery = false, collectionDeadlineCapMs = 3000 } = {}) {
  const collectorStartedAt = Date.now();
  const ixspyIsAvailable = () => ixspyAvailable && Date.now() - collectorStartedAt >= ixspyMountAfterMs;
  const ixspyChartIsAvailable = () => ixspyIsAvailable()
    && Date.now() - collectorStartedAt >= ixspyChartMountAfterMs;
  let chartMode = "cumulative_total";
  let fullYearOpened = false;
  let tooltipText = "";
  let chartSamples = 0;
  let dailyChartSamples = 0;
  let fullDomScans = 0;
  let shortTimerCalls = 0;
  const dailyValues = Array.from({ length: 28 }, (_, index) =>
    index >= 21 ? [7, 0, 2, 4, 1, 3, 5][index - 21] : index % 4,
  );
  if (negativeDailyIndex !== null) dailyValues[negativeDailyIndex] = -1;
  const cumulativeValues = dailyValues.reduce((values, value, index) => {
    values.push((values[index - 1] || 100) + value);
    return values;
  }, []);
  const chartRect = { left: 0, top: 0, width: 500, height: 180, right: 500, bottom: 180 };
  const rect = (width = 20, height = 20) => ({ left: 0, top: 0, width, height, right: width, bottom: height });

  const totalButton = {
    classList: {
      contains: (name) => name === "btn-active" && chartMode === "cumulative_total",
      add: () => { chartMode = "cumulative_total"; },
      remove: () => { if (chartMode === "cumulative_total") chartMode = null; },
    },
    click() { chartMode = "cumulative_total"; },
    getBoundingClientRect: () => rect(),
  };
  const incrementButton = {
    className: "mode-button",
    classList: {
      contains: (name) => name === "btn-active" && chartMode === "daily_increment",
      add: () => { chartMode = "daily_increment"; },
      remove: () => { if (chartMode === "daily_increment") chartMode = null; },
    },
    click() { chartMode = "daily_increment"; },
    getBoundingClientRect: () => rect(),
  };
  const fullYearButton = {
    innerText: expandYearView ? "查看一整年数据" : "查看趋势",
    className: "",
    click() {
      fullYearOpened = true;
      chartMode = "daily_increment";
    },
    getBoundingClientRect: () => rect(120, 24),
    getAttribute: () => null,
  };
  const loginButton = {
    innerText: "登录/注册可见",
    textContent: "登录/注册可见",
    getBoundingClientRect: () => rect(180, 28),
  };
  const historyRows = Array.from({ length: 7 }, (_, index) => ({
    getAttribute(name) {
      if (!fullYearOpened) return null;
      if (name === "data-history-date") return new Date(Date.UTC(2026, 8, 17 + index)).toISOString().slice(0, 10);
      if (name === "data-history-value") return String(dailyValues[index]);
      return null;
    },
    innerText: "",
    textContent: "",
    getBoundingClientRect: () => fullYearOpened ? rect(180, 24) : rect(0, 0),
    querySelectorAll: () => [],
  }));
  const tooltip = {
    className: unlabeledTooltip ? "" : "chart-tooltip",
    style: { position: unlabeledTooltip ? "absolute" : "static" },
    get innerText() {
      if (!splitTooltipParts) return tooltipText;
      const date = tooltipText.match(/\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b/)?.[0];
      const beforeDate = date ? tooltipText.slice(0, tooltipText.indexOf(date)) : "";
      const value = beforeDate.match(/(-?[\d,.]+)\s*$/)?.[1];
      return date && value ? `${date} ${value}` : tooltipText;
    },
    get textContent() { return this.innerText; },
    getBoundingClientRect: () => rect(100, 20),
    getAttribute: (name) => !unlabeledTooltip && name === "role" ? "tooltip" : null,
  };
  const tooltipDatePart = {
    parentElement: tooltip,
    get innerText() { return tooltipText.match(/\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b/)?.[0] || ""; },
    get textContent() { return this.innerText; },
    getBoundingClientRect: () => rect(60, 20),
  };
  const tooltipValuePart = {
    parentElement: tooltip,
    get innerText() {
      const date = tooltipText.match(/\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b/)?.[0];
      const beforeDate = date ? tooltipText.slice(0, tooltipText.indexOf(date)) : "";
      return beforeDate.match(/(-?[\d,.]+)\s*$/)?.[1] || "";
    },
    get textContent() { return this.innerText; },
    getBoundingClientRect: () => rect(20, 20),
  };
  const canvas = {
    getBoundingClientRect: () => chartRect,
    dispatchEvent(event) {
      if (event.type !== "mousemove") return;
      const pointIndex = Math.min(27, Math.floor((event.clientX / chartRect.width) * 28));
      const dailySeriesReady = chartMode === "daily_increment"
        && !sameChartInDailyMode
        && dailyChartSamples >= dailyChartReadyAfterSamples;
      const values = dailySeriesReady ? dailyValues : cumulativeValues;
      chartSamples += 1;
      if (chartMode === "daily_increment") dailyChartSamples += 1;
      const date = new Date(Date.parse(`${TEST_TARGET_DATE}T00:00:00Z`) - (28 - pointIndex) * 86400000).toISOString().slice(0, 10);
      const nextTooltip = dailySeriesReady && missingDailyIndexes.includes(pointIndex)
        ? ""
        : tooltipFormat === "cumulative_only"
          ? `近一年销量 ${cumulativeValues[pointIndex]} ${date}`
          : tooltipFormat === "mixed"
            ? `近一年销量 ${cumulativeValues[pointIndex]} 增量 ${dailyValues[pointIndex]} ${date}`
            : `Sales ${values[pointIndex]} ${date}`;
      if (tooltipDelayMs) setTimeout(() => { tooltipText = nextTooltip; }, tooltipDelayMs);
      else tooltipText = nextTooltip;
    },
  };
  const chart = {
    querySelector: (selector) => selector === "canvas" ? canvas : null,
    querySelectorAll: (selector) => selector === "*" ? [canvas]
      : selector === "div, span, [style]" && (unlabeledTooltip || splitTooltipParts)
        ? splitTooltipParts ? [tooltip, tooltipDatePart, tooltipValuePart] : [tooltip]
        : [],
    getBoundingClientRect: () => chartRect,
  };
  const panel = {
    innerText: "产品趋势概览 近一年销量趋势图",
    closest: (selector) => selector === ".sales-180" ? panel : null,
    querySelector: (selector) => ({
      ".trade-inc-button": incrementButton,
      ".trade-total-button": totalButton,
    }[selector] || null),
    querySelectorAll: (selector) => selector === "table tr, [data-history-date][data-history-value]"
      ? (fullYearOpened ? historyRows : [])
      : selector === "div, span, [style]" && (unlabeledTooltip || splitTooltipParts)
        ? splitTooltipParts ? [tooltip, tooltipDatePart, tooltipValuePart] : [tooltip]
        : [],
    getBoundingClientRect: () => rect(500, 220),
  };
  const summary = {
    innerText: "IXSPY Fixture Product 134 sold",
    parentElement: null,
    getBoundingClientRect: () => rect(400, 80),
  };
  const heading = {
    innerText: "IXSPY Fixture Product",
    parentElement: summary,
    getBoundingClientRect: () => rect(300, 40),
  };
  const allNodes = [heading, summary, panel, chart, canvas, totalButton, incrementButton, fullYearButton, tooltip, tooltipDatePart, tooltipValuePart, loginButton];
  const document = {
    visibilityState: "hidden",
    title: "IXSPY Fixture Product - AliExpress",
    images: [],
    body: { innerText: `${"IXSPY Fixture Product with public product details 134 sold ".repeat(12)}${ixspyLoginRequired ? " 登录/注册可见" : ""}` },
    documentElement: { querySelectorAll: () => allNodes },
    querySelector(selector) {
      if (throwOnChartQuery && selector === "#trade_chart") throw new Error("chart node unavailable");
      return {
        h1: heading,
        "#trade_chart": ixspyChartIsAvailable() ? chart : null,
        ".sales-180": ixspyIsAvailable() ? panel : null,
        ".trade-inc-button": ixspyIsAvailable() ? incrementButton : null,
        ".trade-total-button": ixspyIsAvailable() ? totalButton : null,
      }[selector] || null;
    },
    querySelectorAll(selector) {
      if (selector === "*") fullDomScans += 1;
      if (selector === 'script[type="application/ld+json"]') return [];
      if (selector === "*") return allNodes;
      if (selector === ".sales-180") return ixspyIsAvailable() ? [panel] : [];
      if (selector === ".trade-inc-button") return ixspyIsAvailable() ? [incrementButton] : [];
      if (selector === ".trade-total-button") return ixspyIsAvailable() ? [totalButton] : [];
      if (selector === "#trade_chart") return ixspyChartIsAvailable() ? [chart] : [];
      if (selector === 'button, [role="button"], a, [tabindex]') {
        return [...(ixspyLoginRequired ? [loginButton] : []), ...(ixspyIsAvailable() ? [fullYearButton, incrementButton, totalButton] : [])];
      }
      if (/tooltip/i.test(selector)) return unlabeledTooltip ? [] : [tooltip];
      if (selector === "table") return [];
      return [];
    },
  };
  const context = {
    document,
    window: {},
    location: { href: "https://www.aliexpress.com/item/1005001234567890.html", pathname: "/item/1005001234567890.html" },
    URL,
    Date,
    Map,
    Set,
    Math,
    Number,
    String,
    RegExp,
    Promise,
    MutationObserver: class { observe() {} disconnect() {} },
    MouseEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    PointerEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    getComputedStyle: (element) => ({
      display: "block",
      visibility: "visible",
      opacity: "1",
      position: element === tooltip ? tooltip.style.position : "static",
    }),
    requestAnimationFrame: (callback) => { if (!animationFramesPaused) queueMicrotask(callback); },
    setTimeout: (callback, delay, ...args) => {
      if (delay < 100) shortTimerCalls += 1;
      return setTimeout(callback, delay >= 45000 ? collectionDeadlineCapMs : Math.min(delay, 3000), ...args);
    },
    clearTimeout,
    globalThis: null,
  };
  context.globalThis = context;
  const source = fs.readFileSync(path.join(__dirname, "..", "extension", "page_collector.js"), "utf8");
  vm.runInNewContext(source, context, { filename: "page_collector.js" });
  const collector = context.AliExpressMonitorPageCollector;
  Object.defineProperty(collector, "fullYearOpened", { get: () => fullYearOpened });
  Object.defineProperty(collector, "fullDomScans", { get: () => fullDomScans });
  Object.defineProperty(collector, "shortTimerCalls", { get: () => shortTimerCalls });
  return collector;
}

test("detects a visible IXSPY login gate before collecting a product", async () => {
  const collector = loadCollector({ ixspyLoginRequired: true });
  const result = await collector.collect("PRODUCT");

  assert.equal(result.state, "ixspy_login");
  assert.match(result.message, /IXSPY 登录已过期/);
});

test("IXSPY chart reads only the latest seven daily increments, not cumulative totals", async () => {
  const collector = loadCollector();
  const result = await collector.collect("PRODUCT", TEST_TARGET_DATE);
  const dates = Array.from({ length: 7 }, (_, index) =>
    new Date(Date.parse(`${TEST_TARGET_DATE}T00:00:00Z`) - (7 - index) * 86400000).toISOString().slice(0, 10),
  );

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.history_provider, "IXSPY_VISIBLE_UI");
  assert.equal(result.payload.raw_data.history_document_visibility, "hidden");
  assert.equal(result.payload.raw_data.history_canvas_present, true);
  assert.equal(result.payload.raw_data.history_capture_stage, "history_complete");
  assert.equal(result.payload.raw_data.history_sampling_strategy, "latest_7_daily_increments_only");
  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.history_expected_dates)), dates);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.history_missing_dates)), []);
  assert.equal(result.payload.sold_count, null);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.historical_sales)),
    [7, 0, 2, 4, 1, 3, 5].map((value, index) => ({
      date: dates[index], value, value_type: "daily_increment",
    })));
  assert.equal(collector.fullDomScans, 0, `expected direct selector discovery, got ${collector.fullDomScans} full scans`);
});

test("does not expand the full-year view and restores the original chart mode", async () => {
  const collector = loadCollector({ expandYearView: true });
  const result = await collector.collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.extractor_version, "extension-content-0.6.68");
  assert.equal(result.payload.raw_data.history_full_year_control_found, true);
  assert.equal(result.payload.raw_data.history_full_year_view_opened, false);
  assert.equal(result.payload.raw_data.history_daily_mode_selected, true);
  assert.equal(collector.fullYearOpened, false);
  assert.equal(result.payload.historical_sales.length, 7);
  assert.ok(result.payload.historical_sales.every((point) => point.value_type === "daily_increment"));
  assert.ok(result.payload.historical_sales.some((point) => point.value === 0));
});

test("rejects an unchanged cumulative chart after selecting daily mode", async () => {
  const result = await loadCollector({ sameChartInDailyMode: true }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.history_daily_mode_control_found, true);
  assert.equal(result.payload.raw_data.history_daily_mode_selected, true);
  assert.equal(result.payload.raw_data.history_daily_series_implausible, true);
  assert.equal(result.payload.raw_data.history_daily_capture_complete, false);
  assert.equal(result.payload.historical_sales.length, 0);
});

test("retries chart sampling when IXSPY mode changes before its daily series rerenders", async () => {
  const result = await loadCollector({ dailyChartReadyAfterSamples: 7 }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.history_capture_attempts, 2);
  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
  assert.equal(result.payload.historical_sales.length, 7);
  assert.ok(result.payload.historical_sales.every((point) => point.value_type === "daily_increment"));
});

test("waits for the visible tooltip to update and records explicit zero values", async () => {
  const result = await loadCollector({ tooltipDelayMs: 30 }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
  assert.equal(result.payload.historical_sales.length, 7);
  assert.ok(result.payload.historical_sales.some((point) => point.value === 0));
});

test("preserves a signed negative daily value shown by the IXSPY tooltip", async () => {
  const result = await loadCollector({ negativeDailyIndex: 23 }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
  assert.equal(result.payload.historical_sales[2].date, "2026-09-24");
  assert.equal(result.payload.historical_sales[2].value, -1);
});

test("reads date and value from IXSPY's visible classless chart overlay", async () => {
  const result = await loadCollector({ unlabeledTooltip: true }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
  assert.equal(result.payload.historical_sales.length, 7);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.payload.historical_sales.map((point) => point.date))),
    ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28"],
  );
});

test("combines date and value when IXSPY exposes them in separate tooltip children", async () => {
  const result = await loadCollector({ unlabeledTooltip: true, splitTooltipParts: true }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
  assert.equal(result.payload.historical_sales.length, 7);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.payload.historical_sales.map((point) => point.value))),
    [7, 0, 2, 4, 1, 3, 5],
  );
});

test("prefers the daily increment when IXSPY tooltip shows both cumulative and daily values", async () => {
  const result = await loadCollector({ tooltipFormat: "mixed" }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.payload.historical_sales.map((point) => point.value))),
    [7, 0, 2, 4, 1, 3, 5],
  );
});

test("rejects cumulative-only tooltip values while daily increment mode is selected", async () => {
  const result = await loadCollector({ tooltipFormat: "cumulative_only" }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.payload.raw_data.history_daily_capture_complete, false);
  assert.equal(result.payload.raw_data.history_daily_series_implausible, false);
  assert.equal(result.payload.historical_sales.length, 0);
  assert.equal(result.payload.raw_data.history_missing_dates.length, 7);
});

test("does not mark an unread day as zero or claim complete seven-day coverage", async () => {
  const result = await loadCollector({ missingDailyIndexes: [24] }).collect("PRODUCT", TEST_TARGET_DATE);
  const missingDate = new Date(Date.parse(`${TEST_TARGET_DATE}T00:00:00Z`) - 4 * 86400000)
    .toISOString().slice(0, 10);

  assert.equal(result.payload.raw_data.history_daily_capture_complete, false);
  assert.equal(result.payload.historical_sales.length, 6);
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.raw_data.history_missing_dates)), [missingDate]);
  assert.ok(!result.payload.historical_sales.some((point) => point.date === missingDate));
});

test("retries when the IXSPY chart canvas appears after its controls", async () => {
  const result = await loadCollector({ ixspyChartMountAfterMs: 200 }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.history_capture_attempts, 2);
  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
  assert.equal(result.payload.historical_sales.length, 7);
});

test("reads the visible IXSPY chart without relying on throttled background animation frames", async () => {
  const startedAt = Date.now();
  const result = await loadCollector({ animationFramesPaused: true }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
  assert.equal(result.payload.historical_sales.length, 7);
  assert.ok(Date.now() - startedAt < 1000);
});

test("bounds hidden-tab retries when IXSPY chart tooltips do not appear", async () => {
  const collector = loadCollector({ missingDailyIndexes: [21, 22, 23, 24, 25, 26, 27] });
  const result = await collector.collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.history_daily_capture_complete, false);
  assert.equal(result.payload.raw_data.history_missing_dates.length, 7);
  assert.ok(collector.shortTimerCalls <= 36, `expected bounded waits, got ${collector.shortTimerCalls}`);
});

test("bounds the wait when no visible IXSPY history is available", async () => {
  const startedAt = Date.now();
  const result = await loadCollector({ ixspyAvailable: false }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.history_provider, "PUBLIC_PAGE_VISIBLE");
  assert.equal(result.payload.raw_data.history_daily_capture_complete, false);
  assert.equal(result.payload.historical_sales.length, 0);
  assert.ok(Date.now() - startedAt < 900);
});

test("waits briefly for the IXSPY panel to appear before declaring history unavailable", async () => {
  const result = await loadCollector({ ixspyMountAfterMs: 150 }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.raw_data.history_provider, "IXSPY_VISIBLE_UI");
  assert.ok(result.payload.raw_data.history_provider_wait_attempts >= 1);
  assert.equal(result.payload.raw_data.history_daily_capture_complete, true);
});

test("records a clear partial result when chart extraction throws", async () => {
  const result = await loadCollector({ throwOnChartQuery: true }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.state, "ready");
  assert.equal(result.payload.sold_count, null);
  assert.equal(result.payload.raw_data.history_capture_state, "error");
  assert.equal(result.payload.raw_data.history_source, "visible_ixspy_capture_error");
  assert.match(result.payload.raw_data.history_capture_error, /chart node unavailable/);
  assert.equal(result.payload.raw_data.history_capture_stage, "daily_mode_selection");
  assert.deepEqual(JSON.parse(JSON.stringify(result.payload.historical_sales)), []);
});

test("marks an overdue history capture instead of saving an unannotated product payload", async () => {
  const result = await loadCollector({ collectionDeadlineCapMs: 25 }).collect("PRODUCT", TEST_TARGET_DATE);

  assert.equal(result.state, "partial");
  assert.equal(result.payload.sold_count, null);
  assert.equal(result.payload.raw_data.history_capture_state, "timeout");
  assert.equal(result.payload.raw_data.history_source, "visible_ixspy_capture_timeout");
  assert.equal(result.payload.raw_data.history_daily_capture_complete, false);
  assert.equal(result.payload.raw_data.history_missing_dates.length, 7);
});
