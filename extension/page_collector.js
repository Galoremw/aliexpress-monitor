(function () {
  if (globalThis.AliExpressMonitorPageCollector) return;

  const PAGE_COLLECTOR_VERSION = "0.6.68";
  const clean = (value) => value?.replace(/\s+/g, " ").trim() || null;
  const cleanProductTitle = (value) => {
    const title = clean(value);
    if (!title) return null;
    return title.replace(/\s+(?:\d[\d,.]*(?:\s*[KM])?\+?\s*(?:sold|orders?)\b|(?:HK\$|US\$|CA\$|AU\$|SG\$|NZ\$|USD\s*|HKD\s*|CNY\s*|RMB\s*|EUR\s*|GBP\s*|JPY\s*|KRW\s*|\$|€|£|¥|￥|₩)\s*\d[\d,.]*|new shoppers save\b|free shipping\b|extra\s+\d+(?:\.\d+)?%\s+off\b|(?:已售|销量)\s*[:：]?\s*\d[\d,.]*).*$/i, "").trim() || title;
  };
  const absoluteImageUrl = (value) => {
    if (!value) return null;
    try {
      const url = new URL(value, location.href);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch {
      return null;
    }
  };
  const imageSource = (node) => {
    const srcset = node?.getAttribute?.("srcset") || node?.getAttribute?.("data-srcset");
    const srcsetUrl = srcset?.split(",")[0]?.trim()?.split(/\s+/)[0];
    return [
      node?.currentSrc,
      node?.getAttribute?.("data-src"),
      node?.getAttribute?.("data-lazy-src"),
      node?.getAttribute?.("data-original"),
      srcsetUrl,
      node?.src,
    ].map(absoluteImageUrl).find(Boolean) || null;
  };

  function productImageUrl(jsonLd) {
    const ogImage = document.querySelector('meta[property="og:image"]')?.content;
    if (ogImage) return absoluteImageUrl(ogImage);
    const jsonImage = Array.isArray(jsonLd?.image) ? jsonLd.image[0] : jsonLd?.image;
    const structuredImage = typeof jsonImage === "string" ? jsonImage : jsonImage?.url;
    if (structuredImage) return absoluteImageUrl(structuredImage);
    const galleryImages = [...document.querySelectorAll(
      '[class*="gallery" i] img, [class*="slider" i] img, [class*="carousel" i] img',
    )];
    const candidates = galleryImages.length ? galleryImages : [...document.images];
    const image = candidates.find((node) => {
      const rect = node.getBoundingClientRect();
      const source = absoluteImageUrl(imageSource(node));
      return rect.width >= 80 && rect.height >= 80
        && source && !/(avatar|logo|icon)/i.test(source);
    });
    return absoluteImageUrl(imageSource(image));
  }

  function parseCount(value) {
    const match = String(value || "").replace(/,/g, "").match(/(\d+(?:\.\d+)?)\s*(万|千|[KMB])?/i);
    if (!match) return null;
    const suffix = match[2]?.toUpperCase();
    const multiplier = { B: 1000000000, M: 1000000, K: 1000, "万": 10000, "千": 1000 }[suffix] || 1;
    return Math.round(Number(match[1]) * multiplier);
  }

  function numberFrom(patterns, text) {
    return countMatchFrom(patterns, text)?.value ?? null;
  }

  function countMatchFrom(patterns, text) {
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match) {
        const value = parseCount(match[1]);
        if (value !== null) return { value, text: match[1].trim() };
      }
    }
    return null;
  }

  function productSummaryText(title, fallback) {
    const heading = document.querySelector("h1");
    if (!heading || !visible(heading)) return fallback;
    let node = heading;
    let best = null;
    for (let depth = 0; node && depth < 10; depth += 1, node = node.parentElement) {
      const text = clean(node.innerText || node.textContent) || "";
      if (text.length < 80 || text.length > 12000 || !text.includes(title)) continue;
      if (!/(?:sold|orders?|purchased|buyers?|已售|销量|购买|交易成功)/i.test(text)) continue;
      if (!best || text.length < best.length) best = text;
    }
    return best || fallback;
  }

  function isChallengePage() {
    const url = location.href.toLowerCase();
    const text = clean(document.body?.innerText)?.toLowerCase() || "";
    return url.includes("/punish")
      || url.includes("captcha")
      || Boolean(document.querySelector('iframe[src*="captcha"], iframe[src*="punish"], [class*="captcha" i]'))
      || ((text.includes("verify") || text.includes("验证"))
        && (text.includes("security") || text.includes("captcha") || text.includes("滑块")));
  }

  function productIdFromPage() {
    return location.pathname.match(/\/item\/(\d+)/i)?.[1]
      || location.href.match(/(?:productId|itemId)=(\d+)/i)?.[1]
      || null;
  }

  function monitoredProductIdFromPage() {
    const url = new URL(location.href);
    return url.searchParams.get("monitor_product_id")
      || url.hash.match(/monitor_product_id=(\d+)/i)?.[1]
      || null;
  }

  function visible(element) {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none"
      && style.visibility !== "hidden"
      && Number(style.opacity || 1) > 0
      && rect.width > 0
      && rect.height > 0;
  }

  function fullDateFromText(value) {
    const match = String(value || "").match(/\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b/);
    if (!match) return null;
    const month = String(Number(match[2])).padStart(2, "0");
    const day = String(Number(match[3])).padStart(2, "0");
    return `${match[1]}-${month}-${day}`;
  }

  function localDateString(value = new Date()) {
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, "0");
    const day = String(value.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function expectedDailyDates(targetDate) {
    const target = new Date(`${targetDate}T00:00:00Z`);
    return Array.from({ length: 7 }, (_, index) => {
      const pointDate = new Date(target.getTime() - (7 - index) * 86400000);
      return pointDate.toISOString().slice(0, 10);
    });
  }

  function ixspyPanelProductId(panel) {
    const controls = [...(panel?.querySelectorAll?.("button, [role='button'], a") || [])];
    for (const control of controls) {
      const handler = control.getAttribute?.("onclick") || "";
      const args = handler.match(/getYearChart\s*\((.*)\)/i)?.[1];
      if (!args) continue;
      const productId = args.split(",").map((value) => value.trim().replace(/^['\"]|['\"]$/g, ""))
        .find((value) => /^\d{8,}$/.test(value));
      if (productId) return productId;
    }
    return null;
  }

  function ixspyPanel(expectedProductId = null) {
    return [...document.querySelectorAll(".sales-180")]
      .find((panel) => visible(panel)
        && /趋势|sales|orders/i.test(panel.innerText || panel.textContent || "")
        && (!expectedProductId || ixspyPanelProductId(panel) === expectedProductId)) || null;
  }

  function hasIxspyLoginGate(panel) {
    const candidates = [
      ...(panel?.querySelectorAll?.('button, [role="button"], a, [tabindex]') || []),
      ...document.querySelectorAll('button, [role="button"], a, [tabindex]'),
    ];
    return candidates.some((node) => visible(node)
      && /登录\s*\/\s*注册可见|login\s*\/\s*register\s*to\s*view/i.test(clean(node.innerText || node.textContent) || ""));
  }

  function ixspyChartTooltip(panel, chart) {
    const seeds = new Set([
      ...document.querySelectorAll('[role="tooltip"], [class*="tooltip" i]'),
      ...(panel?.querySelectorAll?.("div, span, [style]") || []),
      ...(chart?.querySelectorAll?.("div, span, [style]") || []),
    ]);
    const candidates = new Map();
    for (const seed of seeds) {
      let node = seed;
      for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
        if (!visible(node)) continue;
        const text = clean(node.innerText || node.textContent) || "";
        if (!text || text.length > 400 || !fullDateFromText(text)) continue;
        candidates.set(node, { node, text, point: dailyPointFromTooltip(text) });
      }
    }
    return [...candidates.values()].sort((left, right) => Number(Boolean(right.point)) - Number(Boolean(left.point))
      || left.text.length - right.text.length)[0] || null;
  }

  function dailyPointFromTooltip(text) {
    const metricDate = fullDateFromText(text);
    if (!metricDate) return null;
    const prefix = text.slice(0, text.indexOf(metricDate));
    const incrementLabel = prefix.match(/(?:增量|daily\s*(?:sales|increment)|increment)\s*[:：]?\s*(-?[\d,.]+\s*[KMB万千]?)/i);
    if (/(?:近一年销量|累计销量|累计售出|total\s+sales)/i.test(prefix) && !incrementLabel) return null;
    const valueText = incrementLabel?.[1]
      || prefix.match(/(?:sales|orders?)\s*[:：]?\s*(-?[\d,.]+\s*[KMB万千]?)/i)?.[1]
      || prefix.match(/(-?[\d,.]+\s*[KMB万千]?)\s*$/)?.[1]
      || text.slice(text.indexOf(metricDate) + metricDate.length).match(/(?:^|\s)[^\d-]*(-[\d,.]+\s*[KMB万千]?|[\d,.]+\s*[KMB万千]?)/i)?.[1];
    const normalizedValue = valueText?.replace(/,/g, "").match(/^(-?\d+(?:\.\d+)?)\s*(万|千|[KMB])?$/i);
    if (!normalizedValue) return null;
    const multiplier = { B: 1000000000, M: 1000000, K: 1000, "万": 10000, "千": 1000 }[normalizedValue[2]?.toUpperCase()] || 1;
    const value = Math.round(Number(normalizedValue[1]) * multiplier);
    if (!Number.isInteger(value)) return null;
    return { date: metricDate, value, value_type: "daily_increment" };
  }

  function chartModeControl(panel, selector) {
    return panel?.querySelector?.(selector) || document.querySelector(selector);
  }

  async function waitForChartMode(button, selector) {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      if (button?.classList?.contains("btn-active")
        || document.querySelector(`${selector}.btn-active`)) return true;
      await new Promise((resolve) => setTimeout(resolve, 40));
    }
    return Boolean(button && document.querySelector(`${selector}.btn-active`));
  }

  async function sampleDailyChart(canvas, expectedDates, {
    panel = null,
    scanVisibleRange = false,
    scanRecentRange = false,
  } = {}) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return { points: [], sampled_positions: 0 };
    const chartNode = panel?.querySelector?.("#trade_chart") || document.querySelector("#trade_chart");
    const sampled = new Map();
    const observedPoints = new Map();
    const tooltipSamples = [];
    const allValues = [];
    let sampledPositions = 0;
    let tooltipHitCount = 0;
    const sampleCount = scanRecentRange ? 112 : scanVisibleRange ? 56 : expectedDates.length;
    let previousTooltip = "";
    for (let index = 0; index < sampleCount; index += 1) {
      const fraction = scanRecentRange
        ? 0.66 + (0.32 * (index + 0.5)) / sampleCount
        : scanVisibleRange
          ? 0.11 + (0.87 * (index + 0.5)) / sampleCount
          : (21.5 + index) / 28;
      const x = rect.left + rect.width * fraction;
      canvas.dispatchEvent(new MouseEvent("mousemove", {
        bubbles: true,
        clientX: x,
        clientY: rect.top + rect.height * 0.45,
        view: window,
      }));
      sampledPositions += 1;
      await new Promise((resolve) => setTimeout(resolve, 1));
      let tooltip = ixspyChartTooltip(panel, chartNode)?.text || "";
      if (tooltip === previousTooltip) {
        await new Promise((resolve) => setTimeout(resolve, 35));
        tooltip = ixspyChartTooltip(panel, chartNode)?.text || "";
      }
      previousTooltip = tooltip;
      if (tooltip) {
        tooltipHitCount += 1;
        if (tooltipSamples.length < 4 && !tooltipSamples.includes(tooltip)) {
          tooltipSamples.push(tooltip.slice(0, 160));
        }
      }
      const point = dailyPointFromTooltip(tooltip);
      if (point) {
        allValues.push(point.value);
        observedPoints.set(point.date, point);
        if (expectedDates.includes(point.date)) sampled.set(point.date, point);
      }
    }
    canvas.dispatchEvent(new MouseEvent("mouseout", { bubbles: true, view: window }));
    const latestValues = expectedDates.map((date) => sampled.get(date)?.value);
    const nearFlatLargeSeries = latestValues.every(Number.isFinite)
      && Math.min(...latestValues) >= 1000
      && Math.max(...latestValues) - Math.min(...latestValues) <= Math.max(...latestValues) * 0.1;
    const cumulativeLike = nearFlatLargeSeries || allValues.length >= 7
      && allValues.every((value, index) => index === 0 || value >= allValues[index - 1])
      && allValues[0] >= 20;
    const result = [...sampled.values()].sort((left, right) => left.date.localeCompare(right.date));
    return {
      points: result,
      observed_points: [...observedPoints.values()].sort((left, right) => left.date.localeCompare(right.date)),
      tooltip_hit_count: tooltipHitCount,
      tooltip_samples: tooltipSamples,
      sampled_positions: sampledPositions,
      cumulative_like_series: cumulativeLike,
      render_changed_mid_sample: allValues.some((value, index) => index > 0 && allValues[index - 1] > 30 && value < allValues[index - 1] / 2),
    };
  }

  async function collectIxspyDailyHistory(targetDate, {
    expectedProductId = null,
    scanVisibleRange = false,
    retryVisibleRange = false,
  } = {}) {
    const expectedDates = expectedDailyDates(targetDate);
    const unavailable = {
      points: [],
      provider: "PUBLIC_PAGE_VISIBLE",
      source: "visible_page_only",
      expected_dates: expectedDates,
      missing_dates: expectedDates,
      daily_capture_complete: false,
      sampling_strategy: "latest_7_daily_increments_only",
    };
    const deadline = Date.now() + 550;
    let panel = null;
    let providerWaitAttempts = 0;
    while (Date.now() < deadline) {
      panel = ixspyPanel(expectedProductId);
      if (panel) break;
      providerWaitAttempts += 1;
      await new Promise((resolve) => setTimeout(resolve, 80));
    }
    if (!panel) return { ...unavailable, provider_wait_attempts: providerWaitAttempts };
    if (hasIxspyLoginGate(panel)) {
      return { ...unavailable, provider: "IXSPY_VISIBLE_UI", source: "ixspy_login_required", login_required: true };
    }
    const incrementButton = chartModeControl(panel, ".trade-inc-button");
    const totalButton = chartModeControl(panel, ".trade-total-button");
    let chart;
    let canvas;
    try {
      chart = panel?.querySelector?.("#trade_chart") || document.querySelector("#trade_chart");
      canvas = chart?.querySelector("canvas");
    } catch (error) {
      return {
        ...unavailable,
        provider: "IXSPY_VISIBLE_UI",
        source: "visible_ixspy_capture_error",
        error: String(error?.message || error).slice(0, 300),
        stage: "daily_mode_selection",
      };
    }
    const chartDeadline = Date.now() + 550;
    let chartWaitAttempts = 0;
    while ((!canvas || !visible(canvas)) && Date.now() < chartDeadline) {
      chartWaitAttempts += 1;
      await new Promise((resolve) => setTimeout(resolve, 80));
      try {
        chart = panel?.querySelector?.("#trade_chart") || document.querySelector("#trade_chart");
        canvas = chart?.querySelector("canvas");
      } catch (error) {
        return {
          ...unavailable,
          provider: "IXSPY_VISIBLE_UI",
          source: "visible_ixspy_capture_error",
          error: String(error?.message || error).slice(0, 300),
          stage: "daily_mode_selection",
        };
      }
    }
    if (!incrementButton || !canvas || !visible(canvas)) {
      return {
        ...unavailable,
        provider: "IXSPY_VISIBLE_UI",
        source: "visible_ixspy_chart_unavailable",
        provider_wait_attempts: providerWaitAttempts,
      };
    }
    const originalMode = incrementButton.classList.contains("btn-active")
      ? "daily_increment"
      : totalButton?.classList?.contains("btn-active") ? "cumulative_total" : null;
    const fullYearButton = [...document.querySelectorAll('button, [role="button"], a, [tabindex]')]
      .find((node) => visible(node) && /查看一整年数据|view\s+(?:the\s+)?whole\s+year/i.test(node.innerText || node.textContent || ""));
    let attempts = 0;
    let points = [];
    let sampledPositions = 0;
    let sampleDiagnostics = null;
    let renderChanged = false;
    let implausible = false;
    const startedAt = Date.now();
    try {
      if (!incrementButton.classList.contains("btn-active")) incrementButton.click();
      if (!(await waitForChartMode(incrementButton, ".trade-inc-button"))) {
        return {
          ...unavailable,
          provider: "IXSPY_VISIBLE_UI",
          source: "visible_ixspy_capture_error",
          error: "IXSPY 日增量模式未能切换",
          stage: "daily_mode_selection",
          daily_mode_control_found: true,
          daily_mode_selected: false,
        };
      }
      for (attempts = 1; attempts <= 4; attempts += 1) {
        const result = await sampleDailyChart(canvas, expectedDates, {
          panel,
          scanVisibleRange,
          scanRecentRange: retryVisibleRange && attempts > 1 && points.length < expectedDates.length,
        });
        sampleDiagnostics = result;
        sampledPositions += result.sampled_positions;
        points = result.points;
        renderChanged = result.render_changed_mid_sample;
        implausible = result.cumulative_like_series;
        const coverageIncomplete = points.length < expectedDates.length;
        if (!renderChanged && !coverageIncomplete && !implausible) break;
        if (attempts >= (implausible ? 4 : 2)) break;
        await new Promise((resolve) => setTimeout(resolve, implausible ? 250 : 80));
      }
    } catch (error) {
      return {
        ...unavailable,
        provider: "IXSPY_VISIBLE_UI",
        source: "visible_ixspy_capture_error",
        error: String(error?.message || error).slice(0, 300),
        stage: "chart_sampling",
        attempts,
      };
    } finally {
      if (originalMode === "cumulative_total" && totalButton && !totalButton.classList.contains("btn-active")) {
        totalButton.click();
        await waitForChartMode(totalButton, ".trade-total-button");
      }
    }
    const acceptedPoints = implausible ? [] : points;
    const found = new Set(acceptedPoints.map((point) => point.date));
    const missingDates = expectedDates.filter((value) => !found.has(value));
    const complete = missingDates.length === 0;
    return {
      points: acceptedPoints,
      provider: "IXSPY_VISIBLE_UI",
      source: implausible ? "ixspy_cumulative_like_series_rejected"
        : acceptedPoints.length ? "ixspy_visible_daily_increment" : "visible_ixspy_daily_increment_unavailable",
      expected_dates: expectedDates,
      missing_dates: missingDates,
      daily_capture_complete: complete,
      sampling_strategy: "latest_7_daily_increments_only",
      sampled_positions: sampledPositions,
      attempts,
      provider_wait_attempts: providerWaitAttempts,
      chart_wait_attempts: chartWaitAttempts,
      chart_visible: true,
      chart_product_id: ixspyPanelProductId(panel),
      full_year_control_found: Boolean(fullYearButton),
      full_year_view_opened: false,
      daily_mode_control_found: true,
      daily_mode_selected: true,
      observed_points: sampleDiagnostics?.observed_points || [],
      tooltip_hit_count: sampleDiagnostics?.tooltip_hit_count || 0,
      tooltip_samples: sampleDiagnostics?.tooltip_samples || [],
      series_unchanged_after_mode_switch: implausible,
      elapsed_ms: Date.now() - startedAt,
    };
  }

  function readProduct() {
    if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    const bodyText = clean(document.body?.innerText) || "";
    const productId = productIdFromPage();
    if (!productId) return { state: "not_ready", message: "尚未识别商品 ID" };
    const jsonLd = [...document.querySelectorAll('script[type="application/ld+json"]')]
      .map((node) => { try { return JSON.parse(node.textContent); } catch { return null; } })
      .find((value) => value && typeof value === "object");
    const title = cleanProductTitle(document.querySelector("h1")?.innerText)
      || cleanProductTitle(jsonLd?.name)
      || cleanProductTitle(clean(document.title)?.replace(/\s*[-|].*$/, ""));
    const productText = productSummaryText(title, bodyText);
    const reviewCount = numberFrom(
      [/(\d[\d,.]*\s*[KM]?)\s*(?:reviews?|ratings?)/i, /(\d[\d,.]*\s*[KM]?)\s*条评价/i],
      productText,
    );
    const ratingMatch = productText.match(/(?:rating|评分)\s*[:：]?\s*([0-5](?:\.\d+)?)/i);
    const priceMatch = productText.match(/(?:US\$|USD|\$)\s*([\d,.]+)/i);
    const monitoredProductId = monitoredProductIdFromPage();
    const payload = {
      platform_product_id: productId,
      url: location.href,
      title,
      sold_count: null,
      price: priceMatch ? Number(priceMatch[1].replace(/,/g, "")) : null,
      rating: ratingMatch ? Number(ratingMatch[1]) : null,
      review_count: Number.isFinite(reviewCount) ? reviewCount : null,
      raw_data: {
        extractor_version: `extension-content-${PAGE_COLLECTOR_VERSION}`,
        main_image_url: productImageUrl(jsonLd),
        page_title: clean(document.title),
        product_text_scope: productText === bodyText ? "page_fallback" : "h1_ancestor",
        history_provider: "IXSPY_VISIBLE_UI",
        history_sampling_strategy: "latest_7_daily_increments_only",
        ...(monitoredProductId ? { monitored_product_id: monitoredProductId } : {}),
      },
    };
    const hasObservableData = title && (
      payload.price !== null
      || payload.review_count !== null
      || bodyText.length >= 500
    );
    return {
      state: hasObservableData ? "ready" : "not_ready",
      payload,
      message: hasObservableData ? null : "商品公开数据仍在加载",
    };
  }

  function visibleReviewCards() {
    const selectors = [
      "[data-review-id]",
      "[data-review-content]",
      "[class*='review-item' i]",
      "[class*='feedback-item' i]",
      "[class*='comment-item' i]",
      "article",
    ];
    const candidates = [...new Set(selectors.flatMap((selector) => [...document.querySelectorAll(selector)]))]
      .filter(visible)
      .filter((node) => {
        const text = clean(node.innerText || node.textContent) || "";
        return text.length >= 20 && text.length <= 5000;
      });
    return candidates.filter((node) => !candidates.some((other) => other !== node && other.contains(node)));
  }

  function reviewDateFromText(value) {
    const full = fullDateFromText(value);
    if (full) return full;
    const match = String(value || "").match(/\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(20\d{2})\b/i);
    if (!match) return null;
    const month = new Date(`${match[1]} 1, 2000`).getMonth() + 1;
    return `${match[3]}-${String(month).padStart(2, "0")}-${String(Number(match[2])).padStart(2, "0")}`;
  }

  async function reviewFingerprint(review) {
    const normalized = (value) => clean(value)?.toLocaleLowerCase() || "";
    const signature = [
      "fp",
      normalized(review.review_text),
      review.rating === null ? "" : Number(review.rating).toFixed(2),
      review.review_date || "",
      normalized(review.country_region),
      normalized(review.variant),
    ].join("\u001f");
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(signature));
    return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function visibleReviewText(card) {
    const contentSelectors = [
      "[data-review-content]",
      "[class*='review-content' i]",
      "[class*='feedback-content' i]",
      "[class*='comment-content' i]",
      "[class*='review-text' i]",
      "[class*='feedback-text' i]",
    ];
    const values = [
      ...(contentSelectors.some((selector) => card.matches(selector)) ? [card] : []),
      ...contentSelectors.flatMap((selector) => [...card.querySelectorAll(selector)]),
    ]
      .filter(visible)
      .map((node) => {
        const safeNode = node.cloneNode(true);
        safeNode.querySelectorAll(
          "[class*='author' i], [class*='user-name' i], [class*='nickname' i], [class*='avatar' i], [data-reviewer]",
        ).forEach((privateNode) => privateNode.remove());
        return clean(safeNode.innerText || safeNode.textContent);
      })
      .filter((value) => value && value.length >= 8 && value.length <= 12000);
    return values.sort((left, right) => left.length - right.length)[0] || null;
  }

  function readReviewCard(card) {
    const reviewText = visibleReviewText(card);
    if (!reviewText) return null;
    const metadata = clean(card.innerText || card.textContent) || "";
    const idNode = card.matches("[data-review-id], [data-id]") ? card : card.querySelector("[data-review-id], [data-id]");
    const rawId = idNode?.getAttribute("data-review-id") || idNode?.getAttribute("data-id") || "";
    const platformReviewId = /^[\w-]{1,160}$/.test(rawId) ? rawId : null;
    const ratingNode = [...card.querySelectorAll("[aria-label], [title], [data-rating]")]
      .filter(visible)
      .map((node) => node.getAttribute("data-rating") || node.getAttribute("aria-label") || node.getAttribute("title") || "")
      .find((text) => /(?:[1-5](?:\.\d)?)\s*(?:out of 5|\/\s*5|stars?|星)|评分\s*[:：]?\s*[1-5]/i.test(text));
    const ratingMatch = ratingNode?.match(/([1-5](?:\.\d)?)/)
      || metadata.match(/(?:rating|评分)\s*[:：]?\s*([1-5](?:\.\d)?)/i);
    const dateText = [...card.querySelectorAll("time, [class*='date' i], [data-date]")]
      .filter(visible)
      .map((node) => node.getAttribute("datetime") || node.getAttribute("data-date") || node.innerText || node.textContent)
      .find((value) => reviewDateFromText(value));
    const reviewDate = reviewDateFromText(dateText || metadata);
    const countryNode = [...card.querySelectorAll("[class*='country' i], [class*='region' i], [data-country]")]
      .filter(visible)
      .map((node) => clean(node.getAttribute("data-country") || node.innerText || node.textContent))
      .find(Boolean);
    const variantNode = [...card.querySelectorAll("[class*='sku' i], [class*='variant' i], [class*='specification' i]")]
      .filter(visible)
      .map((node) => clean(node.innerText || node.textContent))
      .find(Boolean);
    const verificationNode = [...card.querySelectorAll("[class*='verified' i], [class*='purchase' i]")]
      .filter(visible)
      .map((node) => clean(node.innerText || node.textContent) || "")
      .find((value) => /verified purchase|已验证购买|已验证买家/i.test(value));
    const hasMedia = [...card.querySelectorAll(
      "video, [data-review-media], [class*='review-image' i], [class*='feedback-image' i]",
    )].some(visible) ? true : null;
    return {
      platform_review_id: platformReviewId,
      review_text: reviewText,
      rating: ratingMatch ? Number(ratingMatch[1]) : null,
      review_date: reviewDate,
      country_region: countryNode || null,
      variant: variantNode || null,
      has_media: hasMedia,
      verified_purchase: verificationNode ? true : null,
      raw_data: { extractor_version: "extension-reviews-0.5.0" },
    };
  }

  async function collectReviews(knownKeys = [], maxReviews = 100, timeoutMs = 45000) {
    const deadline = Date.now() + timeoutMs;
    if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    if (!productIdFromPage()) return { state: "failed", message: "当前页面不是商品详情页" };

    const known = new Set(knownKeys);
    const byIdentity = new Map();
    let scannedCount = 0;
    let stoppedOnKnown = false;
    let hadReviewSection = false;
    const reviewHeading = [...document.querySelectorAll("h1,h2,h3,[role='heading']")]
      .filter(visible)
      .find((node) => /reviews?|feedback|评价|评论/i.test(node.innerText || node.textContent || ""));
    if (reviewHeading) {
      hadReviewSection = true;
      reviewHeading.scrollIntoView({ block: "center", behavior: "instant" });
    }

    for (let page = 0; page < 18 && Date.now() < deadline && byIdentity.size < maxReviews; page += 1) {
      if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
      const cards = visibleReviewCards();
      for (const card of cards) {
        const review = readReviewCard(card);
        if (!review) continue;
        scannedCount += 1;
        const key = review.platform_review_id
          ? `id:${review.platform_review_id}`
          : `fp:${await reviewFingerprint(review)}`;
        if (known.has(key)) {
          stoppedOnKnown = true;
          break;
        }
        if (!byIdentity.has(key)) byIdentity.set(key, review);
        if (byIdentity.size >= maxReviews) break;
      }
      if (stoppedOnKnown || byIdentity.size >= maxReviews) break;
      if (/no reviews|no feedback|暂无评价|暂无评论/i.test(document.body?.innerText || "")) {
        hadReviewSection = true;
        break;
      }
      const before = byIdentity.size;
      window.scrollBy({ top: Math.max(600, window.innerHeight * 0.75), behavior: "instant" });
      await new Promise((resolve) => setTimeout(resolve, 900));
      if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
      if (byIdentity.size === before && page >= 3 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 8) break;
    }

    const reviews = [...byIdentity.values()];
    if (!reviews.length && !stoppedOnKnown && !hadReviewSection) {
      return {
        state: "partial",
        reviews,
        scanned_count: scannedCount,
        message: "没有找到可可靠识别的公开评论文本区域",
      };
    }
    return {
      state: "ready",
      reviews,
      scanned_count: scannedCount,
      stopped_on_known: stoppedOnKnown,
      raw_data: { extractor_version: "extension-reviews-0.5.0", visible_review_mode: true },
    };
  }

  function storeIdFromPage() {
    return `${location.pathname}${location.search}`
      .match(/(?:\/store\/|storeId=|sellerId=)(\d+)/i)?.[1] || null;
  }

  function storeIdentityError(urlStoreId) {
    const headingStoreId = (document.body?.innerText || "")
      .slice(0, 5000)
      .match(/(?:^|\n)\s*Shop\s*(\d{6,})\s+Store\s*(?=\n|$)/im)?.[1];
    return headingStoreId && headingStoreId !== urlStoreId
      ? `网址店铺 ID ${urlStoreId} 与页面显示的店铺 ID ${headingStoreId} 不一致，请打开 /store/${headingStoreId} 的商品展示页后重试`
      : null;
  }

  function checkStoreIdentity() {
    if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    const storeId = storeIdFromPage();
    if (!storeId) return { state: "failed", message: "尚未识别店铺 ID" };
    const identityError = storeIdentityError(storeId);
    return identityError
      ? { state: "failed", message: identityError }
      : { state: "ready", platform_store_id: storeId };
  }

  function storeOrdersSortSelected() {
    try {
      const url = new URL(location.href);
      if ([...url.searchParams.values()].some((value) => /orders_desc/i.test(value))) return true;
    } catch {}
    return [...document.querySelectorAll("span, a, button")].some((node) => {
      if (!/^orders$/i.test(clean(node.innerText || node.textContent) || "")) return false;
      const color = getComputedStyle(node).color || "";
      return /253\s*,\s*56\s*,\s*79|255\s*,\s*0\s*,\s*0/.test(color)
        || /active|selected/i.test(String(node.className || ""));
    });
  }

  function storeHasNextPage() {
    const controls = [
      ...document.querySelectorAll('a[rel="next"], a[aria-label*="next" i], button[aria-label*="next" i], [class*="pagination" i] a, [class*="pagination" i] button, [class*="pagination" i] [role="button"]'),
      ...document.querySelectorAll("a, button, [role='button']"),
    ];
    return controls.some((node) => {
      if (!visible(node) || node.disabled || node.getAttribute?.("aria-disabled") === "true") return false;
      const rel = clean(node.getAttribute?.("rel") || "") || "";
      const label = [node.innerText, node.textContent, node.getAttribute?.("aria-label"), node.getAttribute?.("title"), node.getAttribute?.("rel")]
        .map((value) => clean(value) || "")
        .join(" ");
      const nodeClass = String(node.className || "");
      const ancestorClasses = [node.parentElement?.className, node.parentElement?.parentElement?.className]
        .map((value) => String(value || "")).join(" ");
      const inPagination = /pag(?:e|ination|ing)/i.test(`${nodeClass} ${ancestorClasses}`);
      return /^next$/i.test(rel) || (inPagination && /(?:\bnext\b|下一页|下页|›|»|❯|→)/i.test(label));
    });
  }

  function storeNextPageUrl(storeId) {
    const current = new URL(location.href);
    const controls = [...document.querySelectorAll(
      'a[rel="next"], a[aria-label*="next" i], [class*="pagination" i] a, [class*="pagination" i] button'
    )];
    for (const node of controls) {
      if (!visible(node) || node.disabled || node.getAttribute?.("aria-disabled") === "true") continue;
      const parentClass = `${node.className || ""} ${node.parentElement?.className || ""}`;
      const label = `${node.innerText || ""} ${node.getAttribute?.("aria-label") || ""} ${node.getAttribute?.("rel") || ""}`;
      if (!/pagination|rel/i.test(parentClass + (node.getAttribute?.("rel") || ""))
          && !/next|下一页|下页|›|»|❯|→/i.test(label)) continue;
      const href = node.href || node.getAttribute?.("href");
      if (!href) continue;
      try {
        const next = new URL(href, current);
        if (next.origin !== current.origin || next.href === current.href
            || !next.pathname.match(new RegExp(`^/store/${storeId}/pages/all-items\\.html$`, "i"))
            || next.searchParams.get("shop_sortType") !== "orders_desc") continue;
        return next.href;
      } catch {}
    }
    return null;
  }

  function scanStoreProducts() {
    const byId = new Map();
    const nodes = [];
    const collectNodes = (root) => {
      const selector = 'a[href*="/item/"], [data-product-id], [data-item-id], [data-product-url], [data-item-url]';
      for (const node of root.querySelectorAll(selector)) nodes.push(node);
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) collectNodes(element.shadowRoot);
      }
    };
    collectNodes(document);
    for (const node of nodes) {
      const href = node.href
        || node.getAttribute("href")
        || node.getAttribute("data-product-url")
        || node.getAttribute("data-item-url")
        || "";
      const match = `${href} ${node.getAttribute("data-product-id") || ""} ${node.getAttribute("data-item-id") || ""}`
        .match(/(?:\/item\/|^)(\d{8,})(?:\.html)?/i);
      if (!match) continue;
      const id = match[1];
      const text = clean(node.innerText || node.textContent) || "";
      const imageNode = node.matches?.("img") ? node : node.querySelector?.("img");
      const imageUrl = absoluteImageUrl(imageSource(imageNode));
      const existing = byId.get(id);
      byId.set(id, {
        platform_product_id: id,
        url: href || existing?.url || `https://www.aliexpress.com/item/${id}.html`,
        title: cleanProductTitle(node.getAttribute("title") || node.getAttribute("aria-label") || text) || existing?.title || null,
        image_url: imageUrl || existing?.image_url || null,
      });
    }
    return { products: [...byId.values()], candidateNodeCount: nodes.length };
  }

  function waitForStoreGrowth(previousCount, timeoutMs) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (grew) => {
        if (settled) return;
        settled = true;
        observer.disconnect();
        clearTimeout(deadline);
        resolve(grew);
      };
      const observer = new MutationObserver(() => {
        if (scanStoreProducts().products.length > previousCount) finish(true);
      });
      observer.observe(document.documentElement, { childList: true, subtree: true });
      const deadline = setTimeout(() => finish(false), timeoutMs);
    });
  }

  function storeTrendButton(productId) {
    return [...document.querySelectorAll("img.zhexiantu")].find((node) => {
      const handler = node.getAttribute("onclick") || "";
      const match = handler.match(/clickSmallChart\s*\(\s*['\"]?(\d+)/i);
      return match?.[1] === productId;
    }) || null;
  }

  function storeMiniChart(productId) {
    const classToken = `store_product_${productId}`;
    return [...document.querySelectorAll(".test-small-chart")]
      .find((node) => String(node.className || "").split(/\s+/).includes(classToken)) || null;
  }

  function storeTrendProductId(node) {
    const handler = node?.getAttribute?.("onclick") || "";
    return handler.match(/clickSmallChart\s*\(\s*['\"]?(\d+)/i)?.[1] || null;
  }

  function storePageElements(root) {
    const elements = [];
    const seenElements = new Set();
    const seenRoots = new Set();
    const visit = (tree) => {
      if (!tree?.querySelectorAll || seenRoots.has(tree)) return;
      seenRoots.add(tree);
      if (tree.shadowRoot) visit(tree.shadowRoot);
      for (const element of tree.querySelectorAll("*")) {
        if (!seenElements.has(element)) {
          seenElements.add(element);
          elements.push(element);
        }
        if (element.shadowRoot) visit(element.shadowRoot);
      }
    };
    visit(root);
    return elements;
  }

  function findProductSpecificMoreAction(productId, includeOpenRoots = false) {
    const productChartToken = `small-chart-${productId}`;
    const matchesProduct = (node) => {
      const classes = String(node.className || "").split(/\s+/);
      const value = node.getAttribute?.("value");
      const handler = node.getAttribute?.("onclick") || "";
      return classes.includes("look-more")
        && classes.includes(productChartToken)
        && (!value || value === productChartToken)
        && /lookMoreChart\s*\(/i.test(handler);
    };
    const directMatch = [...document.querySelectorAll(".look-more")].find(matchesProduct);
    if (directMatch || !includeOpenRoots) return directMatch || null;
    return storePageElements(document).find(matchesProduct) || null;
  }

  function storeMoreAction(miniChart, trigger, productId) {
    const exactProductMore = findProductSpecificMoreAction(productId, true);
    if (exactProductMore && visible(exactProductMore)) return exactProductMore;

    const isMoreLabel = (node) => /^更多(?:\.\.\.|…)?$/.test(clean(node.innerText || node.textContent) || "");
    const findMore = (root) => storePageElements(root)
      .filter((node) => visible(node) && isMoreLabel(node))
      .sort((left, right) => (clean(left.innerText || left.textContent) || "").length
        - (clean(right.innerText || right.textContent) || "").length)[0] || null;
    const insideMiniChart = findMore(miniChart);
    if (insideMiniChart) return insideMiniChart;

    let scope = miniChart?.parentElement || null;
    for (let depth = 0; scope && depth < 8; depth += 1, scope = scope.parentElement || scope.getRootNode?.().host || null) {
      const scopedNodes = storePageElements(scope);
      const visibleMiniCharts = scopedNodes.filter((node) =>
        String(node.className || "").split(/\s+/).includes("test-small-chart") && visible(node));
      const isolatedTargetChart = scopedNodes.includes(miniChart)
        && visibleMiniCharts.length === 1 && visibleMiniCharts[0] === miniChart;
      const containsTrigger = typeof scope.contains === "function" && scope.contains(trigger);
      const trendIds = new Set(
        scopedNodes.filter((node) => node.matches?.("img.zhexiantu"))
          .map(storeTrendProductId)
          .filter(Boolean),
      );
      const uniquelyMatchesTrigger = containsTrigger && trendIds.size === 1 && trendIds.has(productId);
      if (!uniquelyMatchesTrigger && !isolatedTargetChart) continue;
      const action = findMore(scope);
      if (action) return action;
    }

    const chartRect = miniChart?.getBoundingClientRect?.();
    if (!chartRect?.width || !chartRect?.height) return null;
    const candidates = storePageElements(document)
      .filter((node) => typeof node.getBoundingClientRect === "function" && visible(node) && isMoreLabel(node))
      .map((node) => {
        const rect = node.getBoundingClientRect();
        const horizontalGap = Math.max(0, chartRect.left - rect.right, rect.left - chartRect.right);
        const verticalGap = Math.max(0, chartRect.top - rect.bottom, rect.top - chartRect.bottom);
        return {
          node,
          distance: Math.hypot(horizontalGap, verticalGap),
          interactive: Boolean(node.matches?.("a, button, [role='button'], [tabindex]")),
          area: rect.width * rect.height,
        };
      })
      .sort((left, right) => left.distance - right.distance
        || Number(right.interactive) - Number(left.interactive)
        || left.area - right.area);
    const maxDistance = Math.max(100, Math.min(220, chartRect.height * 1.75));
    return candidates.find((candidate) => candidate.distance <= maxDistance)?.node || null;
  }

  async function waitForStoreMoreAction(miniChart, trigger, productId, timeoutMs = 700) {
    const existing = storeMoreAction(miniChart, trigger, productId);
    if (existing) return existing;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline && !isChallengePage()) {
      const exactProductMore = findProductSpecificMoreAction(productId);
      if (exactProductMore && visible(exactProductMore)) return exactProductMore;
      await new Promise((resolve) => setTimeout(resolve, 80));
    }
    return storeMoreAction(miniChart, trigger, productId);
  }

  function unavailableStoreHistory(targetDate, source) {
    const expectedDates = expectedDailyDates(targetDate);
    return {
      points: [],
      provider: "IXSPY_VISIBLE_UI",
      source,
      expected_dates: expectedDates,
      missing_dates: expectedDates,
      daily_capture_complete: false,
      sampling_strategy: "latest_7_daily_increments_only",
    };
  }

  async function waitForStoreChart(productId, timeoutMs = 2200) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (isChallengePage()) return { challenge: true };
      const panel = ixspyPanel(productId);
      if (hasIxspyLoginGate(panel)) return { loginRequired: true };
      const canvas = panel?.querySelector?.("#trade_chart canvas")
        || document.querySelector("#trade_chart canvas");
      const incrementButton = panel?.querySelector?.(".trade-inc-button");
      if (panel && incrementButton && canvas && visible(canvas)) return { panel, canvas };
      await new Promise((resolve) => setTimeout(resolve, 90));
    }
    return null;
  }

  async function collectStoreProductHistory(product, targetDate, setupWaitMs = 0) {
    const deadline = Date.now() + setupWaitMs;
    let trigger = storeTrendButton(product.platform_product_id);
    while (!trigger && Date.now() < deadline && !isChallengePage()) {
      await new Promise((resolve) => setTimeout(resolve, 80));
      trigger = storeTrendButton(product.platform_product_id);
    }
    if (isChallengePage()) return { challenge: true };
    if (!trigger) return unavailableStoreHistory(targetDate, "ixspy_store_card_trend_button_missing");
    trigger.scrollIntoView?.({ block: "center", behavior: "instant" });
    let miniChart = storeMiniChart(product.platform_product_id);
    try {
      if (!miniChart || !visible(miniChart)) {
        trigger.click();
        const miniDeadline = Date.now() + 1800;
        while (Date.now() < miniDeadline) {
          if (isChallengePage()) return { challenge: true };
          miniChart = storeMiniChart(product.platform_product_id);
          if (miniChart && visible(miniChart)) break;
          await new Promise((resolve) => setTimeout(resolve, 80));
        }
      }
      if (!miniChart || !visible(miniChart)) {
        return unavailableStoreHistory(targetDate, "ixspy_store_card_mini_chart_unavailable");
      }
      const more = await waitForStoreMoreAction(
        miniChart, trigger, product.platform_product_id,
        Math.min(5000, Math.max(700, setupWaitMs)),
      );
      if (!more) return unavailableStoreHistory(targetDate, "ixspy_store_card_more_action_missing");
      more.click();
      await new Promise((resolve) => setTimeout(resolve, 140));
      const chart = await waitForStoreChart(product.platform_product_id);
      if (chart?.challenge) return { challenge: true };
      if (chart?.loginRequired) return { login_required: true };
      if (!chart) return unavailableStoreHistory(targetDate, "ixspy_store_product_chart_unavailable");
      return await collectIxspyDailyHistory(targetDate, {
        expectedProductId: product.platform_product_id,
        retryVisibleRange: true,
      });
    } finally {
      if (miniChart && visible(miniChart) && !isChallengePage()) trigger.click();
    }
  }

  async function collectStoreProductHistoryWithRetry(product, targetDate, setupWaitMs = 0) {
    let history = await collectStoreProductHistory(product, targetDate, setupWaitMs);
    if (history.challenge || history.login_required || history.daily_capture_complete) {
      return { history, retryPasses: 0 };
    }
    await new Promise((resolve) => setTimeout(resolve, 180));
    const retry = await collectStoreProductHistory(product, targetDate, 700);
    if (retry.challenge || retry.login_required) return { history: retry, retryPasses: 1 };

    const expectedDates = retry.expected_dates || history.expected_dates || expectedDailyDates(targetDate);
    const pointsByDate = new Map();
    for (const point of [...(history.points || []), ...(retry.points || [])]) {
      if (expectedDates.includes(point.date)) pointsByDate.set(point.date, point);
    }
    const points = expectedDates.map((metricDate) => pointsByDate.get(metricDate)).filter(Boolean);
    const missingDates = expectedDates.filter((metricDate) => !pointsByDate.has(metricDate));
    const observedByDate = new Map();
    for (const point of [...(history.observed_points || []), ...(retry.observed_points || [])]) {
      observedByDate.set(point.date, point);
    }
    history = {
      ...history,
      ...retry,
      points,
      expected_dates: expectedDates,
      missing_dates: missingDates,
      daily_capture_complete: missingDates.length === 0,
      source: points.length ? "ixspy_visible_daily_increment" : retry.source || history.source,
      sampled_positions: (history.sampled_positions || 0) + (retry.sampled_positions || 0),
      tooltip_hit_count: (history.tooltip_hit_count || 0) + (retry.tooltip_hit_count || 0),
      attempts: (history.attempts || 0) + (retry.attempts || 0),
      observed_points: [...observedByDate.values()].sort((left, right) => left.date.localeCompare(right.date)),
      tooltip_samples: [...new Set([...(history.tooltip_samples || []), ...(retry.tooltip_samples || [])])].slice(0, 4),
    };
    return { history, retryPasses: 1 };
  }

  async function collectStore(targetDate = localDateString()) {
    if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    const storeId = storeIdFromPage();
    if (!storeId) return { state: "failed", message: "尚未识别店铺 ID" };
    const identityError = storeIdentityError(storeId);
    if (identityError) return { state: "failed", message: identityError };
    let stableRounds = 0;
    let latest = scanStoreProducts();
    for (let round = 0; round < 12 && latest.products.length < 20 && stableRounds < 2; round += 1) {
      const previousCount = latest.products.length;
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" });
      const grew = await waitForStoreGrowth(previousCount, 1800);
      latest = scanStoreProducts();
      stableRounds = grew || latest.products.length > previousCount ? 0 : stableRounds + 1;
      if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    }
    const products = latest.products.slice(0, 20);
    if (!products.length) return { state: "failed", message: "当前店铺页未读取到公开商品链接" };
    let historyCompleteCount = 0;
    for (const [index, product] of products.entries()) {
      if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
      const { history } = await collectStoreProductHistoryWithRetry(product, targetDate, index === 0 ? 6000 : 800);
      if (history.challenge) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
      if (history.login_required) {
        return { state: "ixspy_login", message: "IXSPY 登录已过期，请先正常登录后重试" };
      }
      product.historical_sales = history.points || [];
      product.history_source = history.points?.length ? "ixspy_visible_daily_increment" : history.source;
      product.history_daily_capture_complete = Boolean(history.daily_capture_complete);
      product.history_expected_dates = history.expected_dates || expectedDailyDates(targetDate);
      product.history_missing_dates = history.missing_dates || product.history_expected_dates;
      if (product.history_daily_capture_complete) historyCompleteCount += 1;
    }
    return {
      state: "ready",
      payload: {
        platform_store_id: storeId,
        url: location.href,
        products,
        raw_data: {
          extractor_version: "extension-store-0.4.0",
          history_collector_version: `extension-content-${PAGE_COLLECTOR_VERSION}`,
          history_provider: "IXSPY_VISIBLE_UI",
          history_sampling_strategy: "store_card_increment_chart_latest_7_dates",
          history_target_date: targetDate,
          history_products_attempted: products.length,
          history_products_complete: historyCompleteCount,
          page_title: clean(document.title),
          visible_link_count: latest.products.length,
          candidate_node_count: latest.candidateNodeCount,
          collection_mode: "scheduled",
        },
      },
    };
  }

  async function waitForMonitoredStorePageReady(timeoutMs = 20000) {
    const deadline = Date.now() + timeoutMs;
    let lastMessage = "店铺 ID 或 Orders 排序未就绪";
    while (Date.now() < deadline) {
      if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
      const storeId = storeIdFromPage();
      const identityError = storeId ? storeIdentityError(storeId) : null;
      if (identityError) return { state: "failed", message: identityError };
      if (storeId && !identityError && storeOrdersSortSelected()) {
        return { state: "ready", storeId };
      }
      lastMessage = !storeId ? "尚未识别店铺 ID" : "店铺 Orders 排序尚未就绪";
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return { state: "failed", message: `店铺页面在 20 秒内未就绪：${lastMessage}` };
  }

  async function collectMonitoredStorePage(targetDate, options) {
    const readiness = await waitForMonitoredStorePageReady();
    if (readiness.state !== "ready") return readiness;
    const storeId = readiness.storeId;
    const targetIds = new Set((options.monitored_product_ids || []).map(String));
    if (!targetIds.size) return { state: "failed", message: "任务缺少监控商品 ID" };
    let latest = scanStoreProducts();
    let stableRounds = 0;
    for (let round = 0; round < 12 && stableRounds < 2; round += 1) {
      const previousCount = latest.products.length;
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" });
      const grew = await waitForStoreGrowth(previousCount, 900);
      latest = scanStoreProducts();
      stableRounds = grew || latest.products.length > previousCount ? 0 : stableRounds + 1;
      if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    }
    const products = latest.products.filter((product) => targetIds.has(product.platform_product_id));
    for (const [index, product] of products.entries()) {
      const { history } = await collectStoreProductHistoryWithRetry(product, targetDate, index === 0 ? 6000 : 800);
      if (history.challenge) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
      if (history.login_required) return { state: "ixspy_login", message: "IXSPY 登录需要人工完成" };
      product.historical_sales = history.points || [];
      product.history_source = product.historical_sales.length ? "ixspy_visible_daily_increment" : history.source;
      product.history_daily_capture_complete = Boolean(history.daily_capture_complete);
      product.history_expected_dates = history.expected_dates || expectedDailyDates(targetDate);
      product.history_missing_dates = history.missing_dates || product.history_expected_dates;
      await options.on_product_collected?.(product);
    }
    return {
      state: "ready",
      page_result: {
        platform_store_id: storeId,
        url: location.href,
        products,
        has_next_page: storeHasNextPage(),
        next_page_url: storeNextPageUrl(storeId),
      },
    };
  }

  async function collectStoreSelectionPage(targetDate = localDateString(), options = {}) {
    if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    const storeId = storeIdFromPage();
    if (!storeId) return { state: "failed", message: "尚未识别店铺 ID" };
    const identityError = storeIdentityError(storeId);
    if (identityError) return { state: "failed", message: identityError };
    if (!storeOrdersSortSelected()) {
      return { state: "failed", message: "请先在店铺商品页点击 Orders 按销量排序，再读取候选" };
    }

    let stableRounds = 0;
    let latest = scanStoreProducts();
    for (let round = 0; round < 12 && stableRounds < 2; round += 1) {
      const previousCount = latest.products.length;
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" });
      const grew = await waitForStoreGrowth(previousCount, 900);
      latest = scanStoreProducts();
      stableRounds = grew || latest.products.length > previousCount ? 0 : stableRounds + 1;
      if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    }
    const candidates = latest.products;
    if (!candidates.length) return { state: "failed", message: "当前店铺页未读取到公开商品链接" };
    const skipProductIds = new Set((options.skip_product_ids || []).map(String));

    for (const [index, product] of candidates.entries()) {
      if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
      product.orders_rank = index + 1;
      if (skipProductIds.has(product.platform_product_id)) continue;
      const { history } = await collectStoreProductHistoryWithRetry(
        product, targetDate, index === 0 ? 6000 : 800,
      );
      if (history.challenge) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
      if (history.login_required) {
        return { state: "ixspy_login", message: "IXSPY 登录已过期，请先正常登录后重试" };
      }
      product.historical_sales = history.points || [];
      product.history_source = history.points?.length ? "ixspy_visible_daily_increment" : history.source;
      product.history_daily_capture_complete = Boolean(history.daily_capture_complete);
      product.history_expected_dates = history.expected_dates || expectedDailyDates(targetDate);
      product.history_missing_dates = history.missing_dates || product.history_expected_dates;
    }
    return {
      state: "ready",
      page_result: {
        platform_store_id: storeId,
        url: location.href,
        candidates,
        has_next_page: storeHasNextPage(),
        next_page_url: storeNextPageUrl(storeId),
        raw_data: {
          extractor_version: `extension-store-selection-${PAGE_COLLECTOR_VERSION}`,
          history_provider: "IXSPY_VISIBLE_UI",
          history_sampling_strategy: "orders_sorted_page_latest_7_daily_increments",
          history_target_date: targetDate,
          page_candidate_count: candidates.length,
          visible_link_count: latest.products.length,
          next_page_detected: storeHasNextPage(),
        },
      },
    };
  }

  let lastTestedStoreId = null;
  let lastTestedProductId = null;

  async function testStorePage(targetDate = localDateString()) {
    if (isChallengePage()) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    const storeId = storeIdFromPage();
    if (!storeId) return { state: "failed", message: "尚未识别店铺 ID" };
    const identityError = storeIdentityError(storeId);
    if (identityError) return { state: "failed", message: identityError };
    const products = scanStoreProducts().products;
    if (!products.length) return { state: "failed", message: "当前店铺页没有已加载的商品卡片" };
    if (lastTestedStoreId !== storeId) {
      lastTestedStoreId = storeId;
      lastTestedProductId = null;
    }
    const previousIndex = products.findIndex((item) => item.platform_product_id === lastTestedProductId);
    const productIndex = previousIndex < 0 ? 0 : (previousIndex + 1) % products.length;
    const product = products[productIndex];
    const { history, retryPasses } = await collectStoreProductHistoryWithRetry(
      product, targetDate, productIndex === 0 ? 6000 : 800,
    );
    if (history.challenge) return { state: "challenge", message: "AliExpress 要求人工完成验证" };
    if (history.login_required) return { state: "ixspy_login", message: "IXSPY 登录已过期，请先正常登录后重试" };
    lastTestedProductId = product.platform_product_id;
    return {
      state: "ready",
      test_result: {
        collector_version: PAGE_COLLECTOR_VERSION,
        platform_store_id: storeId,
        platform_product_id: product.platform_product_id,
        product_index: productIndex + 1,
        product_count: products.length,
        title: product.title,
        points: history.points || [],
        expected_dates: history.expected_dates || expectedDailyDates(targetDate),
        missing_dates: history.missing_dates || expectedDailyDates(targetDate),
        complete: Boolean(history.daily_capture_complete),
        source: history.source,
        diagnostics: {
          chart_product_id: history.chart_product_id || null,
          chart_visible: Boolean(history.chart_visible),
          daily_mode_selected: Boolean(history.daily_mode_selected),
          sampled_positions: history.sampled_positions || 0,
          tooltip_hit_count: history.tooltip_hit_count || 0,
          attempts: history.attempts || 0,
          retry_passes: retryPasses,
          observed_points: history.observed_points || [],
          tooltip_samples: history.tooltip_samples || [],
        },
      },
    };
  }

  function collectProduct(timeoutMs = 45000, targetDate = localDateString()) {
    return new Promise((resolve) => {
      let settled = false;
      let collecting = false;
      let readyResult = null;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        observer.disconnect();
        clearTimeout(deadline);
        resolve(result);
      };
      const check = async () => {
        if (settled || collecting) return;
        const result = readProduct();
        if (result.state === "challenge") {
          finish(result);
          return;
        }
        if (result.state !== "ready") return;
        collecting = true;
        readyResult = result;
        try {
          const history = await collectIxspyDailyHistory(targetDate);
          if (history.login_required) {
            finish({ state: "ixspy_login", message: "IXSPY 登录已过期，请先正常登录后重试" });
            return;
          }
          const expectedDates = history.expected_dates || expectedDailyDates(targetDate);
          const observedDates = new Set(history.points.map((point) => point.date));
          const missingDates = history.missing_dates || expectedDates.filter((value) => !observedDates.has(value));
          finish({
            ...result,
            state: "ready",
            payload: {
              ...result.payload,
              historical_sales: history.points,
              raw_data: {
                ...result.payload.raw_data,
                history_provider: history.provider || "PUBLIC_PAGE_VISIBLE",
                history_target_date: targetDate,
                history_document_visibility: document.visibilityState || "unknown",
                history_source: history.source,
                history_capture_stage: history.stage || "history_complete",
                history_capture_state: history.daily_capture_complete
                  ? "complete"
                  : history.source === "visible_ixspy_capture_error"
                    ? "error"
                    : history.source === "visible_ixspy_capture_timeout" ? "timeout" : "partial",
                history_capture_error: history.error || null,
                history_expected_dates: expectedDates,
                history_missing_dates: missingDates,
                history_points_count: history.points.length,
                history_daily_capture_complete: Boolean(history.daily_capture_complete),
                history_daily_mode_control_found: Boolean(history.daily_mode_control_found),
                history_daily_mode_selected: Boolean(history.daily_mode_selected),
                history_full_year_control_found: Boolean(history.full_year_control_found),
                history_full_year_view_opened: false,
                history_canvas_present: Boolean(history.chart_visible),
                history_daily_series_implausible: Boolean(history.series_unchanged_after_mode_switch),
                history_capture_attempts: (history.attempts || 0) + (history.chart_wait_attempts ? 1 : 0),
                history_provider_wait_attempts: history.provider_wait_attempts || 0,
                history_sampling_strategy: "latest_7_daily_increments_only",
              },
            },
          });
        } catch (error) {
          finish({
            ...result,
            state: "partial",
            payload: {
              ...result.payload,
              historical_sales: [],
              raw_data: {
                ...result.payload.raw_data,
                history_provider: "IXSPY_VISIBLE_UI",
                history_source: "visible_ixspy_capture_error",
                history_capture_stage: "chart_sampling",
                history_capture_state: "error",
                history_capture_error: String(error?.message || error).slice(0, 300),
                history_daily_capture_complete: false,
                history_expected_dates: expectedDailyDates(targetDate),
                history_missing_dates: expectedDailyDates(targetDate),
              },
            },
          });
        }
      };
      const observer = new MutationObserver(check);
      observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        characterData: true,
      });
      const deadline = setTimeout(() => {
        if (readyResult?.payload) {
          const expectedDates = expectedDailyDates(targetDate);
          finish({
            ...readyResult,
            state: "partial",
            payload: {
              ...readyResult.payload,
              historical_sales: [],
              raw_data: {
                ...readyResult.payload.raw_data,
                history_provider: "IXSPY_VISIBLE_UI",
                history_source: "visible_ixspy_capture_timeout",
                history_capture_state: "timeout",
                history_capture_error: "IXSPY visible chart read exceeded its deadline",
                history_expected_dates: expectedDates,
                history_missing_dates: expectedDates,
                history_daily_capture_complete: false,
              },
            },
          });
          return;
        }
        const result = readProduct();
        finish(result.state === "challenge" ? result : { state: "failed", message: result.message });
      }, timeoutMs);
      void check();
    });
  }

  globalThis.AliExpressMonitorPageCollector = {
    collect: (targetType, targetDate, options) => targetType === "STORE_IDENTITY"
      ? checkStoreIdentity()
      : targetType === "STORE_TEST"
      ? testStorePage(targetDate || localDateString())
      : targetType === "STORE_SELECT_PAGE" ? collectStoreSelectionPage(targetDate || localDateString(), options)
      : targetType === "STORE" ? (options?.monitored_product_ids
        ? collectMonitoredStorePage(targetDate || localDateString(), options)
        : collectStore(targetDate || localDateString()))
      : collectProduct(45000, targetDate || localDateString()),
    collectReviews,
    readProduct,
    isChallengePage,
  };
})();
